#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import html
import json
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

try:
    from stateful_scope import require_profile_site_output
except ModuleNotFoundError:
    from scripts.stateful_scope import require_profile_site_output


HTTP_CONSOLE_STATUS_RE = re.compile(r"status of ([1-5][0-9]{2})\b")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_health(port: int, process: subprocess.Popen | None = None, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"mirrorserve exited with {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25) as sock:
                sock.sendall(b"GET /__health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                if b"200 OK" in sock.recv(512):
                    return
        except OSError:
            pass
        time.sleep(0.1)
    raise TimeoutError("mirrorserve health check timed out")


def stop_process(process: subprocess.Popen | None, timeout: float = 5.0) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def start_local_runtime_process(
    binary: Path,
    site_dir: Path,
    state_dir: Path,
    *,
    port: int,
    dynamic_port: bool,
    log,
) -> tuple[subprocess.Popen, int]:
    attempts = 5 if dynamic_port else 1
    last_error: Exception | None = None
    candidate_port = port
    for attempt in range(attempts):
        if attempt:
            candidate_port = free_port()
        process = subprocess.Popen(
            [
                str(binary),
                "-root",
                str(site_dir),
                "-addr",
                f"127.0.0.1:{candidate_port}",
                "-state-dir",
                str(state_dir),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_for_health(candidate_port, process)
            return process, candidate_port
        except (RuntimeError, TimeoutError) as exc:
            last_error = exc
            stop_process(process, timeout=2)
    raise RuntimeError(f"mirrorserve failed to start after {attempts} attempt(s): {last_error}")


def local_request(url: str, port: int) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == port


def response_matches(response: dict[str, Any], expected: dict[str, Any]) -> bool:
    parsed = urlparse(response["url"])
    if expected.get("path") and parsed.path != expected["path"]:
        return False
    if expected.get("status") is not None and response["status"] != expected["status"]:
        return False
    if expected.get("method") and response["method"] != expected["method"]:
        return False
    return True


def expected_error_statuses(scenario: dict[str, Any]) -> set[int]:
    statuses: set[int] = set()
    for step in scenario.get("steps") or []:
        if step.get("op") not in {"goto", "expect_response", "fetch"}:
            continue
        status = step.get("status")
        if isinstance(status, int) and status >= 400:
            statuses.add(status)
    return statuses


def load_source_degradations(site_dir: Path) -> dict[str, dict[str, Any]]:
    report_path = site_dir / "__mirror" / "report.json"
    try:
        report = read_json(report_path)
    except (OSError, json.JSONDecodeError):
        return {}
    degradations = {}
    for asset in report.get("assets") or []:
        status = int(asset.get("status") or 0)
        out_rel = str(asset.get("out_rel") or "").lstrip("/")
        if status < 400 or not out_rel:
            continue
        degradations[out_rel] = {
            "out_rel": out_rel,
            "source_status": status,
            "source_url": str(asset.get("fetch_url") or asset.get("url") or ""),
            "source_error": str(asset.get("error") or ""),
            "source_fallback": str(asset.get("fallback") or ""),
        }
    return degradations


def response_source_degradation(
    response: dict[str, Any], source_degradations: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    if response.get("resource_type") not in {"image", "font", "media"}:
        return None
    out_rel = unquote(urlparse(str(response.get("url") or "")).path).lstrip("/")
    source = source_degradations.get(out_rel)
    if not source or int(source.get("source_status") or 0) != int(response.get("status") or 0):
        return None
    return {
        **source,
        "mirror_status": int(response.get("status") or 0),
        "resource_type": response.get("resource_type"),
        "mirror_url": response.get("url"),
    }


def unexpected_console_errors(
    messages: list[str],
    scenario: dict[str, Any],
    responses: list[dict[str, Any]] | None = None,
    source_degradations: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    expected_statuses = expected_error_statuses(scenario)
    responses = responses or []
    source_degradations = source_degradations or {}
    unexpected = []
    for message in messages:
        lowered = message.lower()
        if "fonts.googleapis.com" in lowered or "fonts.gstatic.com" in lowered:
            continue
        if "content security policy" in lowered and any(
            marker in lowered
            for marker in ("refused to", "has been blocked", "violates the following")
        ):
            continue
        match = HTTP_CONSOLE_STATUS_RE.search(message)
        if match:
            status = int(match.group(1))
            if status in expected_statuses:
                continue
            status_responses = [response for response in responses if response.get("status") == status]
            if status_responses and all(response.get("resource_type") == "font" for response in status_responses):
                continue
            if status_responses and all(
                response_source_degradation(response, source_degradations) is not None
                for response in status_responses
            ):
                continue
        unexpected.append(message)
    return unexpected


def unexpected_page_errors(messages: list[str], scenario: dict[str, Any]) -> list[str]:
    allowed = set(str(message) for message in scenario.get("allowed_source_page_errors") or [])
    return [message for message in messages if message not in allowed]


def nested_json_value(payload: Any, dotted_path: str) -> Any:
    value = payload
    for part in dotted_path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise AssertionError(f"JSON path {dotted_path!r} is missing")
    return value


def execute_step(
    page: Page,
    context: BrowserContext,
    step: dict[str, Any],
    *,
    base_url: str,
    responses: list[dict[str, Any]],
    screenshots_dir: Path,
    scenario_id: str,
) -> dict[str, Any] | None:
    op = step["op"]
    if op == "goto":
        response = page.goto(
            base_url + step["path"],
            wait_until=step.get("wait_until", "domcontentloaded"),
            timeout=int(step.get("timeout_ms", 30_000)),
        )
        page.wait_for_timeout(int(step.get("settle_ms", 350)))
        if response is None:
            raise AssertionError(f"goto {step['path']} returned no response")
        if step.get("status") is not None and response.status != step["status"]:
            raise AssertionError(f"goto {step['path']} returned {response.status}, expected {step['status']}")
    elif op == "fill":
        page.locator(step["selector"]).fill(step["value"])
    elif op == "check":
        page.locator(step["selector"]).check()
    elif op == "click":
        page.locator(step["selector"]).click(timeout=30_000)
        page.wait_for_timeout(int(step.get("settle_ms", 350)))
    elif op == "direct_submit":
        with page.expect_navigation(
            wait_until=step.get("wait_until", "commit"),
            timeout=int(step.get("timeout_ms", 30_000)),
        ):
            page.locator(step["selector"]).evaluate("form => HTMLFormElement.prototype.submit.call(form)")
        page.wait_for_timeout(int(step.get("settle_ms", 250)))
    elif op == "reload":
        page.reload(wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(int(step.get("settle_ms", 250)))
    elif op == "wait":
        page.wait_for_timeout(int(step["ms"]))
    elif op == "expect_text":
        locator = page.get_by_text(step["text"], exact=bool(step.get("exact", False)))
        locator.first.wait_for(state="visible", timeout=int(step.get("timeout_ms", 10_000)))
    elif op == "expect_selector":
        page.locator(step["selector"]).wait_for(state=step.get("state", "visible"), timeout=int(step.get("timeout_ms", 10_000)))
    elif op == "expect_url":
        parsed = urlparse(page.url)
        if parsed.path != step["path"]:
            raise AssertionError(f"current path {parsed.path!r}, expected {step['path']!r}")
    elif op == "expect_response":
        if not any(response_matches(response, step) for response in responses):
            raise AssertionError(f"missing response {step}; observed={responses[-12:]}")
    elif op == "fetch":
        path = step["path"]
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError(f"fetch path must be local: {path!r}")
        method = step.get("method", "GET").upper()
        options: dict[str, Any] = {"method": method, "headers": step.get("headers", {})}
        if "form" in step:
            options["form"] = step["form"]
        if "json" in step:
            options["data"] = step["json"]
        response = context.request.fetch(base_url + path, **options)
        responses.append(
            {
                "url": response.url,
                "status": response.status,
                "method": method,
                "resource_type": "api_request",
            }
        )
        if step.get("status") is not None and response.status != step["status"]:
            raise AssertionError(f"fetch {path} returned {response.status}, expected {step['status']}")
        if "expect_json" in step:
            payload = response.json()
            for dotted_path, expected in step["expect_json"].items():
                actual = nested_json_value(payload, dotted_path)
                if actual != expected:
                    raise AssertionError(f"fetch {path} JSON {dotted_path!r}={actual!r}, expected {expected!r}")
    elif op == "expect_cookie":
        cookies = {item["name"]: item for item in context.cookies()}
        present = step.get("present", True)
        if present and step["name"] not in cookies:
            raise AssertionError(f"cookie {step['name']!r} is missing")
        if not present and step["name"] in cookies:
            raise AssertionError(f"cookie {step['name']!r} should be absent")
    elif op == "screenshot":
        path = screenshots_dir / f"{scenario_id}__{step['name']}.png"
        if step.get("selector"):
            page.locator(step["selector"]).first.screenshot(path=str(path), animations="disabled")
        else:
            page.screenshot(path=str(path), full_page=bool(step.get("full_page", False)), animations="disabled")
        return {"name": step["name"], "path": str(path)}
    else:
        raise ValueError(f"unsupported verification operation {op!r}")
    return None


def run_scenario(
    browser: Browser,
    scenario: dict[str, Any],
    *,
    base_url: str,
    port: int,
    screenshots_dir: Path,
    source_degradations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    context = browser.new_context(
        viewport={"width": int(scenario.get("viewport_width", 1280)), "height": int(scenario.get("viewport_height", 800))},
        service_workers="block",
    )
    page = context.new_page()
    responses: list[dict[str, Any]] = []
    external_requests: list[str] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    screenshots = []

    def route_request(route) -> None:
        if local_request(route.request.url, port):
            route.continue_()
        else:
            external_requests.append(route.request.url)
            route.abort("blockedbyclient")

    def record_response(response) -> None:
        responses.append(
            {
                "url": response.url,
                "status": response.status,
                "method": response.request.method,
                "resource_type": response.request.resource_type,
            }
        )

    context.route("**/*", route_request)
    page.on("response", record_response)
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    started = time.monotonic()
    error = ""
    unexpected_console: list[str] = []
    unexpected_page: list[str] = []
    try:
        for step in scenario["steps"]:
            screenshot = execute_step(
                page,
                context,
                step,
                base_url=base_url,
                responses=responses,
                screenshots_dir=screenshots_dir,
                scenario_id=scenario["id"],
            )
            if screenshot:
                screenshots.append(screenshot)
        if external_requests:
            raise AssertionError(f"external requests attempted: {external_requests[:5]}")
        unexpected_console = unexpected_console_errors(
            console_errors,
            scenario,
            responses,
            source_degradations,
        )
        if unexpected_console:
            raise AssertionError(f"unexpected console errors: {unexpected_console[:5]}")
        unexpected_page = unexpected_page_errors(page_errors, scenario)
        if unexpected_page:
            raise AssertionError(f"page errors: {unexpected_page[:5]}")
        status = "PASS"
    except Exception as exc:
        status = "FAIL"
        error = f"{type(exc).__name__}: {exc}"
        failure_path = screenshots_dir / f"{scenario['id']}__failure.png"
        try:
            page.screenshot(path=str(failure_path), full_page=True, animations="disabled")
            screenshots.append({"name": "failure", "path": str(failure_path)})
        except Exception:
            pass
    finally:
        final_url = page.url
        context.close()
    observed_source_degradations = []
    seen_degradations = set()
    for response in responses:
        degradation = response_source_degradation(response, source_degradations)
        if not degradation:
            continue
        key = (degradation["out_rel"], degradation["mirror_status"])
        if key in seen_degradations:
            continue
        seen_degradations.add(key)
        observed_source_degradations.append(degradation)
    return {
        "id": scenario["id"],
        "description": scenario.get("description", ""),
        "status": status,
        "error": error,
        "duration_seconds": round(time.monotonic() - started, 3),
        "covers_states": scenario.get("covers_states", []),
        "responses": responses,
        "external_requests": sorted(set(external_requests)),
        "console_errors": console_errors,
        "unexpected_console_errors": unexpected_console,
        "non_blocking_console_warnings": [message for message in console_errors if message not in unexpected_console],
        "source_degradations": observed_source_degradations,
        "page_errors": page_errors,
        "unexpected_page_errors": unexpected_page,
        "non_blocking_source_page_errors": [message for message in page_errors if message not in unexpected_page],
        "screenshots": screenshots,
        "final_url": final_url,
    }


def report_html(payload: dict[str, Any], report_dir: Path) -> str:
    cards = []
    for scenario in payload["scenarios"]:
        shots = []
        for screenshot in scenario["screenshots"]:
            path = Path(screenshot["path"])
            if not path.is_absolute():
                path = Path.cwd() / path
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            shots.append(
                f'<figure><img src="data:image/png;base64,{encoded}" alt="">'
                f'<figcaption>{html.escape(screenshot["name"])}</figcaption></figure>'
            )
        cards.append(
            f'<section class="scenario {scenario["status"].lower()}"><header><h2>{html.escape(scenario["id"])}</h2><strong>{scenario["status"]}</strong></header>'
            f'<p>{html.escape(scenario.get("description", ""))}</p><p class="error">{html.escape(scenario.get("error", ""))}</p>'
            f'<div class="shots">{"".join(shots)}</div></section>'
        )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(payload['site_id'])} 状态验收</title><style>
body{{margin:0;background:#eef1f4;color:#17202a;font:14px/1.5 system-ui,sans-serif}}main{{max-width:1180px;margin:auto;padding:28px 18px}}h1{{font-size:25px}}.summary{{background:#fff;border-left:4px solid #3478f6;padding:14px 16px;margin-bottom:18px}}.scenario{{background:#fff;border:1px solid #d8dee6;margin:14px 0;padding:16px}}.scenario.fail{{border-left:4px solid #b42318}}.scenario.pass{{border-left:4px solid #18794e}}header{{display:flex;justify-content:space-between;gap:20px;align-items:center}}h2{{font-size:17px;margin:0}}.error{{color:#b42318;white-space:pre-wrap}}.shots{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}}figure{{margin:0}}img{{display:block;max-width:100%;max-height:520px;border:1px solid #ccd3dc;object-fit:contain;background:#fff}}figcaption{{padding:5px 0;color:#52606d}}</style></head>
<body><main><h1>{html.escape(payload['site_id'])}</h1><div class="summary"><strong>{payload['fidelity_status']}</strong> · {payload['passed_scenarios']}/{payload['total_scenarios']} scenarios · state coverage {payload['covered_state_count']}/{payload['required_state_count']} · external requests {payload['external_request_count']} · source degradations {payload['source_degradation_count']}</div>{''.join(cards)}</main></body></html>"""


def verify(
    site_out: Path,
    profile_path: Path,
    *,
    port: int = 0,
    headed: bool = False,
    base_url: str = "",
    report_dir: Path | None = None,
) -> dict[str, Any]:
    profile = read_json(profile_path)
    verification = profile.get("verification") or {}
    scenarios = verification.get("scenarios") or []
    if not scenarios:
        raise ValueError(f"{profile_path}: no verification scenarios")
    require_profile_site_output(profile, site_out)
    site_dir = site_out / "site"
    if not site_dir.is_dir():
        raise FileNotFoundError("site directory is missing")
    source_degradations = load_source_degradations(site_dir)

    report_dir = report_dir or site_out / "stateful_verify"
    if report_dir.exists():
        shutil.rmtree(report_dir)
    screenshots_dir = report_dir / "screenshots"
    screenshots_dir.mkdir(parents=True)
    runtime_mode = "external_url" if base_url else "host_binary"
    dynamic_port = not base_url and port == 0
    if base_url:
        base_url = base_url.rstrip("/")
        parsed_base = urlparse(base_url)
        if parsed_base.scheme != "http" or parsed_base.hostname not in {"127.0.0.1", "localhost"} or not parsed_base.port:
            raise ValueError("--base-url must be an explicit local HTTP origin, for example http://127.0.0.1:18080")
        port = parsed_base.port
    else:
        port = port or free_port()
        base_url = f"http://127.0.0.1:{port}"
    log_path = report_dir / "mirrorserve.log"
    started_at = datetime.now(timezone.utc).isoformat()

    restart_count = 0

    def run_all_scenarios(restart_server=None) -> list[dict[str, Any]]:
        nonlocal restart_count
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not headed)
            try:
                results = []
                for scenario in scenarios:
                    if scenario.get("restart_server_before"):
                        if restart_server is None:
                            raise ValueError(f"scenario {scenario['id']} requires a server restart unavailable for external URL mode")
                        restart_server()
                        restart_count += 1
                    results.append(
                        run_scenario(
                            browser,
                            scenario,
                            base_url=base_url,
                            port=port,
                            screenshots_dir=screenshots_dir,
                            source_degradations=source_degradations,
                        )
                    )
                return results
            finally:
                browser.close()

    if runtime_mode == "external_url":
        wait_for_health(port)
        results = run_all_scenarios()
    else:
        binary = site_out / "mirrorserve"
        if not binary.is_file():
            raise FileNotFoundError("mirrorserve binary is missing")
        state_dir = report_dir / "runtime_state"
        state_dir.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log:
            process: subprocess.Popen | None = None

            def start_server() -> subprocess.Popen:
                nonlocal port, base_url
                next_process, port = start_local_runtime_process(
                    binary,
                    site_dir,
                    state_dir,
                    port=port,
                    dynamic_port=dynamic_port,
                    log=log,
                )
                base_url = f"http://127.0.0.1:{port}"
                return next_process

            def stop_server() -> None:
                nonlocal process
                if process is None:
                    return
                stop_process(process)
                process = None

            def restart_server() -> None:
                nonlocal process
                stop_server()
                process = start_server()

            try:
                process = start_server()
                results = run_all_scenarios(restart_server)
            finally:
                stop_server()

    required_states = set(verification.get("required_states") or [])
    covered_states = {state for result in results if result["status"] == "PASS" for state in result["covers_states"]}
    missing_states = sorted(required_states - covered_states)
    external_requests = sorted({url for result in results for url in result["external_requests"]})
    passed = sum(result["status"] == "PASS" for result in results)
    overall = "PASS" if passed == len(results) and not missing_states and not external_requests else "FAIL"
    observed_source_degradations = {
        (item["out_rel"], item["mirror_status"]): item
        for result in results
        for item in result.get("source_degradations") or []
    }
    fidelity_status = (
        "FAIL"
        if overall != "PASS"
        else "PASS_WITH_SOURCE_DEGRADATION"
        if observed_source_degradations
        else "PASS"
    )
    payload = {
        "schema_version": "1.0",
        "site_id": profile["site_id"],
        "fidelity_scope": profile.get("fidelity_scope", ""),
        "runtime_mode": runtime_mode,
        "runtime_origin": base_url,
        "runtime_restart_count": restart_count,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "fidelity_status": fidelity_status,
        "total_scenarios": len(results),
        "passed_scenarios": passed,
        "required_state_count": len(required_states),
        "covered_state_count": len(required_states & covered_states),
        "missing_states": missing_states,
        "external_request_count": len(external_requests),
        "external_requests": external_requests,
        "source_degradation_count": len(observed_source_degradations),
        "source_degradations": [observed_source_degradations[key] for key in sorted(observed_source_degradations)],
        "scenarios": results,
    }
    (report_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (report_dir / "index.html").write_text(report_html(payload, report_dir), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Run declarative Playwright verification for a stateful mirror profile.")
    parser.add_argument("--site-out", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    profile = args.profile or Path("configs/stateful_profiles") / f"{args.site_out.name}.json"
    if args.render_only:
        report_dir = args.report_dir or args.site_out / "stateful_verify"
        payload = read_json(report_dir / "summary.json")
        (report_dir / "index.html").write_text(report_html(payload, report_dir), encoding="utf-8")
        print(json.dumps({"site_id": payload["site_id"], "overall": payload["overall"], "rendered": str(report_dir / "index.html")}, ensure_ascii=False, indent=2))
        return
    payload = verify(
        args.site_out,
        profile,
        port=args.port,
        headed=args.headed,
        base_url=args.base_url,
        report_dir=args.report_dir,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload["overall"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
