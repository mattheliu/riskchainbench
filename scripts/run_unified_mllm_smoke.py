#!/usr/bin/env python3
"""Run a few guided, image-grounded end-to-end cases with one frozen MLLM.

This is an engineering smoke track, not a full-corpus accuracy experiment.  The
same libinfer model reconstructs a reserved entry from a rendered message,
selects each state-changing local browser action from screenshot pixels, and
produces a citation-bound risk judgment.  Deterministic code executes actions
and verifies the local scenario; it never substitutes for the model decision.
"""

from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.error
import urllib.request
from urllib.parse import urlparse
import uuid

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import Page, sync_playwright


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from verify_stateful_profile import (  # noqa: E402
    execute_step,
    free_port,
    start_local_runtime_process,
    stop_process,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = PROJECT_ROOT.parent.parent / ".secrets/libinfer.env"
DEFAULT_CASES = PROJECT_ROOT / "configs/unified_mllm_smoke_cases_v0.1.json"
DEFAULT_PROMPTS = PROJECT_ROOT / "configs/unified_mllm_smoke_prompts_v0.1.json"
DEFAULT_RESOLVER = (
    PROJECT_ROOT
    / "outputs/riskchainbench_30_100_600_v0.1/scale_600/private/resolver.json"
)
DEFAULT_TASKS = (
    PROJECT_ROOT
    / "outputs/riskchainbench_30_100_600_v0.1/scale_600/public/task1_inputs.jsonl"
)
DEFAULT_ROUTE_PROBE = (
    PROJECT_ROOT / "outputs/riskchainbench_30_100_600_v0.1/pilot_30/audits/"
    "libinfer_multimodal_route_probe_domestic_v0.3.json"
)
DEFAULT_CONFUSABLES = (
    PROJECT_ROOT / "data/derived/unicode_confusables_ascii_v17.0.0.json"
)
_FONT_CANDIDATES = (
    Path(os.environ["RISKCHAINBENCH_CJK_FONT"])
    if os.environ.get("RISKCHAINBENCH_CJK_FONT")
    else None,
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path.home() / ".local/share/fonts/NotoSansCJK-Regular.ttc",
    Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
)
DEFAULT_FONT = next(
    (candidate for candidate in _FONT_CANDIDATES if candidate and candidate.is_file()),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
)
DEFAULT_TESSERACT_ROOT = (
    PROJECT_ROOT.parent / "galgameworld/local-private/tools/tesseract/root"
)
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RESERVED_ENTRY_RE = re.compile(
    r"^https://case-[0-9a-f]{20}\.test/start$", re.IGNORECASE
)
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
DOMAIN_RE = re.compile(
    r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|net|org|cn|io|app|top|xyz|info|me|co|in|cc|test|blog|online)\b"
)
IPV4_RE = re.compile(
    r"(?<![0-9])(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})"
    r"(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}(?![0-9])"
)
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
LONG_NUMBER_RE = re.compile(r"(?<![0-9])[0-9][0-9 ._-]{5,}[0-9](?![0-9])")
MUTATING_OPS = {"fill", "check", "click", "direct_submit", "reload"}
ACTION_VERIFICATION_PURPOSES = {
    "fill": "Populate this visible local synthetic field so the controlled state transition can be exercised.",
    "check": "Set this visible local control so the controlled state transition can be exercised.",
    "click": "Activate this visible local control to exercise the next controlled state transition.",
    "direct_submit": "Submit this visible local form through the isolated controller to exercise its state transition.",
    "reload": "Reload the current local page to test whether the just-created browser state resumes.",
}


class ModelCallError(RuntimeError):
    """A failed model call whose complete retry audit is still available."""

    def __init__(self, phase: str, audit: dict[str, Any]):
        super().__init__(f"model call failed in phase {phase}")
        self.phase = phase
        self.audit = audit


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"expected JSON objects: {path}")
            rows.append(row)
    return rows


def pinned_response_models(
    route_probe: dict[str, Any], requested_model: str
) -> set[str]:
    """Return the single response-model identity attested by a passing probe."""

    row = next(
        (
            candidate
            for candidate in route_probe.get("results") or []
            if isinstance(candidate, dict)
            and candidate.get("model") == requested_model
            and candidate.get("status") == "PASS_MULTIMODAL_ROUTE"
        ),
        None,
    )
    if row is None:
        raise ValueError("requested model lacks a passing multimodal probe")
    resolved = {
        str(probe["response_model"])
        for probe in row.get("probes") or []
        if isinstance(probe, dict)
        and probe.get("status") == "PASS"
        and isinstance(probe.get("response_model"), str)
        and probe["response_model"]
    }
    if isinstance(row.get("response_model"), str) and row["response_model"]:
        resolved.add(str(row["response_model"]))
    if not resolved:
        resolved.add(requested_model)
    if len(resolved) != 1:
        raise ValueError("route probe resolved to multiple response-model identities")
    return resolved


def thinking_policy_for_model(model: str) -> str:
    if model.startswith("qwen"):
        return "enable_thinking_false"
    if model.startswith("kimi-"):
        return "thinking_type_disabled"
    return "provider_default"


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_export_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError("invalid libinfer env line")
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip("'\"")
    return result


def libinfer_chat_url(value: str) -> str:
    base = value.rstrip("/")
    lowered = base.lower()
    if "oneapi" in lowered or "not-for-automation" in lowered:
        raise ValueError("refusing non-libinfer endpoint")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def response_content(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("libinfer response lacks message content") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    raise ValueError("unsupported libinfer content")


def extract_json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    start = text.find("{")
    if start < 0:
        raise ValueError("model response contains no JSON object")
    result, end = decoder.raw_decode(text[start:])
    if not isinstance(result, dict):
        raise ValueError("model response JSON is not an object")
    if text[start + end :].strip():
        raise ValueError("model response has trailing content")
    return result


def image_data_url(path: Path) -> str:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("model image is not PNG")
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def task_message_text(task: dict[str, Any]) -> str:
    text = "\n\n".join(
        str(row.get("content") or "") for row in task.get("messages") or []
    )
    if not text:
        raise ValueError("Task 1 input has no message text")
    return text


def build_confusable_legend(text: str, mapping_path: Path) -> dict[str, Any]:
    """Return only pinned UTS #39 inverse relations observed in this input."""

    payload = read_json(mapping_path)
    mappings = payload.get("mappings")
    if not isinstance(mappings, dict):
        raise ValueError("confusable mapping lacks mappings object")
    reverse: dict[str, set[str]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for target, candidates in mappings.items():
        if (
            not isinstance(target, str)
            or len(target) != 1
            or not isinstance(candidates, list)
        ):
            raise ValueError("invalid confusable mapping row")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError("invalid confusable candidate")
            character = candidate.get("character")
            if not isinstance(character, str) or len(character) != 1:
                continue
            reverse.setdefault(character, set()).add(target.lower())
            metadata.setdefault(character, candidate)
    rows = []
    for character in dict.fromkeys(text):
        targets = sorted(reverse.get(character) or [])
        if not targets:
            continue
        candidate = metadata[character]
        rows.append(
            {
                "observed_character": character,
                "codepoint": f"U+{ord(character):04X}",
                "ascii_skeleton_candidates": targets,
                "script_hint": candidate.get("script_hint"),
            }
        )
    return {
        "setting": "TAXONOMY_AWARE",
        "mapping_id": payload.get("mapping_id"),
        "unicode_version": payload.get("unicode_version"),
        "mapping_file_sha256": sha256_file(mapping_path),
        "semantics": (
            "Observed glyph to ASCII skeleton candidates from the pinned UTS #39 "
            "inverse mapping; this is syntax evidence, not URL reputation or Gold."
        ),
        "entries": rows,
    }


def call_model_json(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_payload: dict[str, Any],
    images: list[tuple[str, Path]],
    phase: str,
    case_ref: str,
    max_tokens: int,
    call_index: int,
    validator: Callable[[dict[str, Any]], list[str]] | None = None,
    audit_path: Path | None = None,
    request_protocol: str = "unified_mllm_smoke_v0.1",
    notes_task_prefix: str = "unified-mllm-smoke",
    notes_extra: str = "single-frozen-mllm-guided-smoke-v0.1",
    max_attempts: int = 3,
    timeout_seconds: float = 240,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not 1 <= max_attempts <= 5:
        raise ValueError("max_attempts must be between 1 and 5")
    user_text = canonical_json(user_payload)
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    image_records = []
    for evidence_id, path in images:
        digest = sha256_file(path)
        content.extend(
            [
                {"type": "text", "text": f"IMAGE_EVIDENCE_ID={evidence_id}"},
                {
                    "type": "image_url",
                    "image_url": {"url": image_data_url(path), "detail": "high"},
                },
            ]
        )
        image_records.append(
            {
                "evidence_id": evidence_id,
                "path": str(path),
                "sha256": digest,
                "size_bytes": path.stat().st_size,
            }
        )
    attempts = []
    validation_feedback: list[str] = []
    last_response_model: Any = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    model_request_options: dict[str, Any] = {"response_format": {"type": "json_object"}}
    temperature_policy = "explicit_zero"
    reasoning_effort_policy = "explicit_minimal"
    thinking_policy = thinking_policy_for_model(model)
    if model.startswith("gpt-5."):
        # GPT-5 libinfer routes reject explicit temperature and reasoning
        # effort values. Keep both omissions visible in the audit instead of
        # treating them as transport or multimodal capability failures.
        temperature_policy = "provider_default_omitted"
        reasoning_effort_policy = "provider_default_omitted"
    else:
        model_request_options["temperature"] = 0
        model_request_options["reasoning_effort"] = "minimal"
    if thinking_policy == "enable_thinking_false":
        # Qwen reasoning routes may exhaust the completion budget before
        # emitting message.content. This provider-compatible switch keeps the
        # benchmark response observable and schema-auditable.
        model_request_options["enable_thinking"] = False
    elif thinking_policy == "thinking_type_disabled":
        # Kimi thinking routes can consume the entire completion budget before
        # emitting message.content. Moonshot's OpenAI-compatible instant-mode
        # contract uses this explicit switch.
        model_request_options["thinking"] = {"type": "disabled"}

    def make_audit(status: str, response_model: Any = None) -> dict[str, Any]:
        return {
            "schema_version": "unified-mllm-call-audit/v0.2",
            "phase": phase,
            "call_index": call_index,
            "requested_model": model,
            "reasoning_effort_policy": reasoning_effort_policy,
            "temperature_policy": temperature_policy,
            "thinking_policy": thinking_policy,
            "model_request_options": model_request_options,
            "system_prompt_sha256": sha256_text(system_prompt),
            "user_payload_sha256": sha256_text(canonical_json(user_payload)),
            "images": image_records,
            "image_count": len(image_records),
            "multimodal_input": bool(image_records),
            "request_protocol": request_protocol,
            "max_attempts": max_attempts,
            "timeout_seconds": timeout_seconds,
            "attempts": attempts,
            "status": status,
            "response_model": response_model,
        }

    def persist(audit: dict[str, Any]) -> None:
        if audit_path is not None:
            atomic_json(audit_path, audit)

    for attempt in range(1, max_attempts + 1):
        run_id = str(uuid.uuid4())
        budget = min(max_tokens * (2 ** (attempt - 1)), 4096)
        feedback_text = None
        if validation_feedback:
            feedback_text = canonical_json(
                {
                    "protocol_validation_feedback": validation_feedback,
                    "instruction": (
                        "Correct only the response protocol. This feedback does not "
                        "contain the expected answer or hidden Gold."
                    ),
                }
            )
        if image_records:
            attempt_content: str | list[dict[str, Any]] = list(content)
            if feedback_text is not None:
                attempt_content.append({"type": "text", "text": feedback_text})
        else:
            attempt_content = user_text
            if feedback_text is not None:
                attempt_content += "\n\n" + feedback_text
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": attempt_content},
            ],
            "max_tokens": budget,
            "libinfer-notes": {
                "project": "riskchainbench",
                "task": f"{notes_task_prefix}-{phase}-{call_index}"[:160],
                "runId": run_id,
                "extra": notes_extra,
            },
            "libinfer-metadata": {
                "protocol": request_protocol,
                "phase": phase,
                "case_ref": case_ref,
                "image_sha256s": [row["sha256"] for row in image_records],
            },
            "libinfer-retries": 1,
            **model_request_options,
        }
        request = urllib.request.Request(
            libinfer_chat_url(base_url),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.monotonic()
        row: dict[str, Any] = {
            "attempt": attempt,
            "run_id": run_id,
            "max_tokens": budget,
            "request_sha256": sha256_text(canonical_json(body)),
            "request_size_bytes": len(
                json.dumps(body, ensure_ascii=False).encode("utf-8")
            ),
            "started_at": utc_now(),
        }
        payload: dict[str, Any] | None = None
        text: str | None = None
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            last_response_model = payload.get("model")
            text = response_content(payload)
            parsed = extract_json_object(text)
            validation_errors = validator(parsed) if validator is not None else []
            if validation_errors:
                row.update(
                    {
                        "status": "FAIL_PROTOCOL_VALIDATION",
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "response_sha256": sha256_text(text),
                        "response_content": text,
                        "response_id": payload.get("id"),
                        "response_model": payload.get("model"),
                        "finish_reason": (payload.get("choices") or [{}])[0].get(
                            "finish_reason"
                        ),
                        "usage": payload.get("usage"),
                        "validation_errors": validation_errors,
                        "finished_at": utc_now(),
                    }
                )
                attempts.append(row)
                validation_feedback = validation_errors
                audit = make_audit(
                    "RETRYING" if attempt < max_attempts else "FAIL",
                    payload.get("model"),
                )
                persist(audit)
                if attempt < max_attempts:
                    time.sleep(attempt)
                    continue
                break
            row.update(
                {
                    "status": "PASS",
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "response_sha256": sha256_text(text),
                    "response_content": text,
                    "response_id": payload.get("id"),
                    "response_model": payload.get("model"),
                    "finish_reason": (payload.get("choices") or [{}])[0].get(
                        "finish_reason"
                    ),
                    "usage": payload.get("usage"),
                    "finished_at": utc_now(),
                }
            )
            attempts.append(row)
            audit = make_audit("PASS", payload.get("model"))
            audit.update(
                {
                    "response_sha256": sha256_text(text),
                    "parsed_response": parsed,
                }
            )
            persist(audit)
            return parsed, audit
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            urllib.error.HTTPError,
        ) as exc:
            http_status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
            retryable = http_status is None or http_status in {
                408,
                409,
                425,
                429,
                500,
                502,
                503,
                504,
            }
            row.update(
                {
                    "status": "FAIL_RETRYABLE" if retryable else "FAIL_TERMINAL",
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:800],
                    "http_status": http_status,
                    "retryable": retryable,
                    "finished_at": utc_now(),
                }
            )
            if payload is not None:
                row.update(
                    {
                        "response_id": payload.get("id"),
                        "response_model": payload.get("model"),
                        "finish_reason": (payload.get("choices") or [{}])[0].get(
                            "finish_reason"
                        ),
                        "usage": payload.get("usage"),
                    }
                )
            if text is not None:
                row.update(
                    {
                        "response_sha256": sha256_text(text),
                        "response_content": text,
                    }
                )
            attempts.append(row)
            validation_feedback = [
                f"{type(exc).__name__}:INVALID_OR_MISSING_JSON_OBJECT"
            ]
            should_retry = retryable and attempt < max_attempts
            persist(make_audit("RETRYING" if should_retry else "FAIL"))
            if should_retry:
                time.sleep(attempt)
            else:
                break
    audit = make_audit("FAIL", last_response_model)
    persist(audit)
    raise ModelCallError(phase, audit)


def wrap_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int
) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in text:
        if character == "\n":
            lines.append(current)
            current = ""
            continue
        candidate = current + character
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > width:
            lines.append(current)
            current = character
        else:
            current = candidate
    lines.append(current)
    return lines


def render_task_image(
    task: dict[str, Any], path: Path, font_path: Path
) -> dict[str, Any]:
    text = task_message_text(task)
    font = ImageFont.truetype(str(font_path), 34)
    label_font = ImageFont.truetype(str(font_path), 22)
    scratch = Image.new("RGB", (1400, 400), "white")
    draw = ImageDraw.Draw(scratch)
    lines = wrap_text(draw, text, font, 1280)
    line_height = 52
    height = max(420, 120 + line_height * len(lines))
    image = Image.new("RGB", (1400, height), (247, 248, 250))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (42, 42, 1358, height - 42),
        radius=18,
        fill="white",
        outline=(210, 214, 220),
        width=2,
    )
    draw.text(
        (78, 67), "Controlled social-message input", fill=(82, 88, 98), font=label_font
    )
    y = 112
    for line in lines:
        draw.text((78, y), line, fill=(23, 27, 33), font=font)
        y += line_height
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=False)
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "width": image.width,
        "height": image.height,
        "purpose": "TASK1_RESERVED_ENTRY_INPUT",
        "source_task_sha256": task.get("task_sha256"),
    }


def tesseract_environment(root: Path) -> tuple[Path, dict[str, str]]:
    binary = root / "usr/bin/tesseract"
    library_root = root / "usr/lib/x86_64-linux-gnu"
    tessdata = root / "usr/share/tesseract-ocr/5/tessdata"
    if not binary.is_file() or not library_root.is_dir() or not tessdata.is_dir():
        raise FileNotFoundError("private tesseract tool root is incomplete")
    environment = os.environ.copy()
    existing = environment.get("LD_LIBRARY_PATH", "")
    environment["LD_LIBRARY_PATH"] = str(library_root) + (
        ":" + existing if existing else ""
    )
    environment["TESSDATA_PREFIX"] = str(tessdata)
    return binary, environment


def tesseract_rows(path: Path, root: Path) -> list[dict[str, Any]]:
    binary, environment = tesseract_environment(root)
    completed = subprocess.run(
        [str(binary), str(path), "stdout", "-l", "eng", "--psm", "6", "tsv"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("tesseract OCR failed")
    text = completed.stdout.decode("utf-8", errors="strict")
    rows = []
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        word = str(row.get("text") or "").strip()
        if not word:
            continue
        try:
            rows.append(
                {
                    "text": word,
                    "left": int(row["left"]),
                    "top": int(row["top"]),
                    "width": int(row["width"]),
                    "height": int(row["height"]),
                    "line_key": (
                        int(row["block_num"]),
                        int(row["par_num"]),
                        int(row["line_num"]),
                    ),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def sensitive_text(value: str) -> bool:
    return any(
        pattern.search(value)
        for pattern in (URL_RE, DOMAIN_RE, IPV4_RE, EMAIL_RE, LONG_NUMBER_RE)
    )


def sensitive_ocr_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["line_key"], []).append(row)
    selected: list[dict[str, Any]] = []
    for line_rows in groups.values():
        spaced = " ".join(row["text"] for row in line_rows)
        compact = "".join(row["text"] for row in line_rows)
        if sensitive_text(spaced) or sensitive_text(compact):
            selected.extend(line_rows)
        else:
            selected.extend(row for row in line_rows if sensitive_text(row["text"]))
    unique = {}
    for row in selected:
        key = (row["left"], row["top"], row["width"], row["height"])
        unique[key] = row
    return list(unique.values())


def merged_redaction_regions(
    rows: list[dict[str, Any]],
    *,
    image_width: int,
    image_height: int,
    pass_index: int,
) -> list[dict[str, Any]]:
    """Merge sensitive OCR words by line to avoid creating text-like box runs."""

    groups: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["line_key"], []).append(row)
    regions = []
    for line_key, line_rows in groups.items():
        padding = 7
        left = max(0, min(row["left"] for row in line_rows) - padding)
        top = max(0, min(row["top"] for row in line_rows) - padding)
        right = min(
            image_width,
            max(row["left"] + row["width"] for row in line_rows) + padding,
        )
        bottom = min(
            image_height,
            max(row["top"] + row["height"] for row in line_rows) + padding,
        )
        regions.append(
            {
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "source_text_sha256": sha256_text(
                    canonical_json([row["text"] for row in line_rows])
                ),
                "source_line_key": list(line_key),
                "pass_index": pass_index,
            }
        )
    return regions


def redact_screenshot(
    raw_path: Path,
    derived_path: Path,
    audit_path: Path,
    *,
    tesseract_root: Path,
) -> dict[str, Any]:
    rows = tesseract_rows(raw_path, tesseract_root)
    findings = sensitive_ocr_rows(rows)
    image = Image.open(raw_path).convert("RGB")
    boxes: list[dict[str, Any]] = []
    derived_path.parent.mkdir(parents=True, exist_ok=True)
    current_findings = findings
    post_rows: list[dict[str, Any]] = []
    post_findings: list[dict[str, Any]] = []
    redaction_pass_count = 0
    for pass_index in range(1, 4):
        if current_findings:
            redaction_pass_count = pass_index
            regions = merged_redaction_regions(
                current_findings,
                image_width=image.width,
                image_height=image.height,
                pass_index=pass_index,
            )
            draw = ImageDraw.Draw(image)
            for region in regions:
                draw.rectangle(
                    (
                        region["left"],
                        region["top"],
                        region["right"],
                        region["bottom"],
                    ),
                    fill=(96, 96, 96),
                )
            boxes.extend(regions)
        image.save(derived_path, format="PNG", optimize=False)
        post_rows = tesseract_rows(derived_path, tesseract_root)
        post_findings = sensitive_ocr_rows(post_rows)
        if not post_findings:
            break
        current_findings = post_findings
    audit = {
        "schema_version": "pixel-redaction-audit/v0.2",
        "generated_at": utc_now(),
        "parent": {
            "path": str(raw_path),
            "sha256": sha256_file(raw_path),
            "size_bytes": raw_path.stat().st_size,
        },
        "derived": {
            "path": str(derived_path),
            "sha256": sha256_file(derived_path),
            "size_bytes": derived_path.stat().st_size,
        },
        "ocr_engine": "private-tesseract-5-eng",
        "pre_redaction_ocr_word_count": len(rows),
        "pre_redaction_sensitive_count": len(findings),
        "redaction_pass_count": redaction_pass_count,
        "redaction_box_count": len(boxes),
        "redaction_boxes": boxes,
        "post_redaction_ocr_word_count": len(post_rows),
        "post_redaction_sensitive_count": len(post_findings),
        "status": "PASS" if not post_findings else "FAIL_POST_REDACTION_LEAK",
    }
    atomic_json(audit_path, audit)
    if audit["status"] != "PASS":
        raise ValueError("post-redaction OCR still detects sensitive text")
    return audit


def normalize_reserved_entry(value: Any) -> str:
    text = str(value or "").strip().strip("`\"'")
    parsed = urlparse(text)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return text
    normalized = f"https://{parsed.hostname.lower()}{parsed.path or '/'}"
    return normalized.rstrip("/") if normalized.endswith("/start/") else normalized


def validate_task1_response(value: dict[str, Any]) -> list[str]:
    errors = []
    allowed_keys = {
        "reconstructed_text",
        "reconstructed_entry",
        "intent",
        "confidence",
        "uncertainties",
    }
    if set(value) not in {
        frozenset(allowed_keys),
        frozenset(allowed_keys - {"reconstructed_text"}),
    }:
        errors.append("TASK1_KEYS_INVALID")
    if "reconstructed_text" in value and not isinstance(value["reconstructed_text"], str):
        errors.append("TASK1_RECONSTRUCTED_TEXT_INVALID")
    if value.get("intent") not in {
        "OPEN_SITE",
        "ADD_ACCOUNT",
        "SEARCH_QUERY",
        "ENTER_CODE",
        "CONTACT_SERVICE",
        "UNKNOWN",
        # Retained only so historical v0.1 smoke artifacts remain replayable.
        "OPEN_CONTROLLED_SITE_AND_INSPECT",
    }:
        errors.append("TASK1_INTENT_INVALID")
    if (
        not isinstance(value.get("confidence"), (int, float))
        or not 0 <= value["confidence"] <= 1
    ):
        errors.append("TASK1_CONFIDENCE_INVALID")
    if not isinstance(value.get("uncertainties"), list):
        errors.append("TASK1_UNCERTAINTIES_INVALID")
    if not RESERVED_ENTRY_RE.fullmatch(str(value.get("reconstructed_entry") or "")):
        errors.append("TASK1_RESERVED_ENTRY_GRAMMAR_INVALID")
    return errors


def safe_text(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = URL_RE.sub("[MASKED_URL]", text)
    text = DOMAIN_RE.sub("[MASKED_DOMAIN]", text)
    text = IPV4_RE.sub("[MASKED_IP]", text)
    text = EMAIL_RE.sub("[MASKED_EMAIL]", text)
    return text[:limit]


def selector_descriptor(page: Page, step: dict[str, Any]) -> dict[str, Any]:
    if step["op"] == "reload":
        return {"kind": "browser", "label": "Reload the current local page"}
    selector = step.get("selector")
    if not selector:
        return {"kind": "controller", "label": step["op"]}
    locator = page.locator(selector).first
    if locator.count() < 1:
        raise AssertionError("scenario action target is absent")
    value = locator.evaluate(
        r"""element => ({
          tag: element.tagName.toLowerCase(),
          type: element.getAttribute('type') || '',
          role: element.getAttribute('role') || '',
          name: element.getAttribute('name') || '',
          aria_label: element.getAttribute('aria-label') || '',
          placeholder: element.getAttribute('placeholder') || '',
          title: element.getAttribute('title') || '',
          labels: Array.from(element.labels || []).map(label =>
            (label.innerText || label.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120)
          ).filter(Boolean),
          text: (element.innerText || element.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 180),
          visible: !!(element.offsetWidth || element.offsetHeight || element.getClientRects().length)
        })"""
    )
    return {
        "kind": "page_control",
        "tag": safe_text(value.get("tag"), 32),
        "type": safe_text(value.get("type"), 32),
        "role": safe_text(value.get("role"), 32),
        "name": safe_text(value.get("name"), 80),
        "aria_label": safe_text(value.get("aria_label"), 120),
        "placeholder": safe_text(value.get("placeholder"), 120),
        "title": safe_text(value.get("title"), 120),
        "associated_labels": [safe_text(row, 120) for row in value.get("labels") or []],
        "visible_text": safe_text(value.get("text"), 180),
        "visible": bool(value.get("visible")),
        "visual_target_marker": "The proposed target is enclosed by a 4 px red outline in the screenshot.",
    }


def prepare_action_view(page: Page, step: dict[str, Any]) -> dict[str, Any]:
    selector = step.get("selector")
    if selector:
        locator = page.locator(selector).first
        if locator.count() < 1:
            raise AssertionError("scenario action target is absent")
        locator.scroll_into_view_if_needed(timeout=10_000)
        page.wait_for_timeout(100)
        locator.evaluate(
            r"""element => {
              const old = document.getElementById('__sitemirror_smoke_target_marker');
              if (old) old.remove();
              const rect = element.getBoundingClientRect();
              const marker = document.createElement('div');
              marker.id = '__sitemirror_smoke_target_marker';
              Object.assign(marker.style, {
                position: 'fixed',
                left: `${Math.max(0, rect.left - 4)}px`,
                top: `${Math.max(0, rect.top - 4)}px`,
                width: `${rect.width + 8}px`,
                height: `${rect.height + 8}px`,
                border: '4px solid rgb(220, 38, 38)',
                boxSizing: 'border-box',
                pointerEvents: 'none',
                zIndex: '2147483647'
              });
              document.documentElement.appendChild(marker);
            }"""
        )
    descriptor = selector_descriptor(page, step)
    if descriptor.get("kind") == "page_control" and not descriptor.get("visible"):
        raise AssertionError("scenario action target is not visible after scrolling")
    return descriptor


def remove_action_marker(page: Page) -> None:
    page.evaluate(
        "document.getElementById('__sitemirror_smoke_target_marker')?.remove()"
    )


def validate_action_response(value: dict[str, Any], expected: str) -> list[str]:
    errors = validate_action_response_shape(value)
    if value.get("action_id") != expected:
        errors.append("ACTION_ID_INVALID")
    return errors


def validate_action_response_shape(value: dict[str, Any]) -> list[str]:
    errors = []
    if set(value) != {"action_id", "reason", "visual_observations"}:
        errors.append("ACTION_KEYS_INVALID")
    if not isinstance(value.get("action_id"), str) or not value["action_id"].strip():
        errors.append("ACTION_ID_TYPE_INVALID")
    if not isinstance(value.get("reason"), str) or not value["reason"].strip():
        errors.append("ACTION_REASON_INVALID")
    observations = value.get("visual_observations")
    if not isinstance(observations, list) or not all(
        isinstance(row, str) for row in observations
    ):
        errors.append("ACTION_VISUAL_OBSERVATIONS_INVALID")
    return errors


def validate_judgment(value: dict[str, Any], evidence_ids: set[str]) -> list[str]:
    errors = []
    required = {
        "intent",
        "evidence",
        "risk_reason",
        "answer",
        "evidence_sufficiency",
        "counterevidence",
    }
    if set(value) != required:
        errors.append("JUDGMENT_KEYS_INVALID")
    if value.get("answer") not in {"YES", "NO", "INSUFFICIENT"}:
        errors.append("JUDGMENT_ANSWER_INVALID")
    if value.get("evidence_sufficiency") not in {"SUFFICIENT", "INSUFFICIENT"}:
        errors.append("JUDGMENT_SUFFICIENCY_INVALID")
    if (
        not isinstance(value.get("risk_reason"), str)
        or not value["risk_reason"].strip()
    ):
        errors.append("JUDGMENT_REASON_INVALID")
    evidence = value.get("evidence")
    if not isinstance(evidence, list):
        errors.append("JUDGMENT_EVIDENCE_INVALID")
    elif not evidence:
        errors.append("JUDGMENT_EVIDENCE_EMPTY")
    else:
        for row in evidence:
            if not isinstance(row, dict) or set(row) != {"evidence_id", "observation"}:
                errors.append("JUDGMENT_EVIDENCE_ITEM_INVALID")
                continue
            if row.get("evidence_id") not in evidence_ids:
                errors.append("JUDGMENT_UNKNOWN_EVIDENCE_ID")
            if (
                not isinstance(row.get("observation"), str)
                or not row["observation"].strip()
            ):
                errors.append("JUDGMENT_EMPTY_OBSERVATION")
    if not isinstance(value.get("counterevidence"), list):
        errors.append("JUDGMENT_COUNTEREVIDENCE_INVALID")
    return sorted(set(errors))


def response_event(response: Any, base_url: str) -> dict[str, Any]:
    parsed = urlparse(response.url)
    base = urlparse(base_url)
    local = parsed.hostname == base.hostname and parsed.port == base.port
    return {
        "scope": "LOCAL_REPLAY" if local else "EXTERNAL",
        "path": parsed.path if local else "[BLOCKED_EXTERNAL]",
        "method": response.request.method,
        "status": response.status,
    }


def run_case(
    *,
    binding: dict[str, Any],
    task: dict[str, Any],
    scenario_id: str,
    model: str,
    prompts: dict[str, str],
    prompt_bundle_sha256: str,
    base_url_model: str,
    api_key: str,
    output_dir: Path,
    font_path: Path,
    tesseract_root: Path,
    confusables_path: Path,
    max_tokens: int,
) -> dict[str, Any]:
    case_ref = binding["case_ref"]
    case_root = output_dir / "cases" / case_ref
    raw_root = case_root / "internal_raw"
    visible_root = case_root / "model_visible"
    audit_root = case_root / "redaction_audits"
    runtime_root = case_root / "runtime_state"
    for path in (raw_root, visible_root, audit_root, runtime_root):
        path.mkdir(parents=True, exist_ok=True)
    call_root = case_root / "model_calls"
    call_root.mkdir(parents=True, exist_ok=True)
    calls: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    started_at = utc_now()

    def invoke_model(
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
        images: list[tuple[str, Path]],
        phase: str,
        validator: Callable[[dict[str, Any]], list[str]] | None,
    ) -> dict[str, Any]:
        call_index = len(calls) + 1
        audit_path = call_root / f"{call_index:03d}_{phase}.json"
        try:
            parsed, audit = call_model_json(
                base_url=base_url_model,
                api_key=api_key,
                model=model,
                system_prompt=system_prompt,
                user_payload=user_payload,
                images=images,
                phase=phase,
                case_ref=case_ref,
                max_tokens=max_tokens,
                call_index=call_index,
                validator=validator,
                audit_path=audit_path,
            )
        except ModelCallError as exc:
            calls.append(exc.audit)
            atomic_json(case_root / "model_calls.json", calls)
            raise
        calls.append(audit)
        atomic_json(case_root / "model_calls.json", calls)
        return parsed

    task_image = visible_root / "task1_message.png"
    task_image_record = render_task_image(task, task_image, font_path)
    message_text = task_message_text(task)
    confusable_legend = build_confusable_legend(message_text, confusables_path)
    task1 = invoke_model(
        system_prompt=prompts["task1_system"],
        user_payload={
            "phase": "ENTRY_RECONSTRUCTION",
            "sample_id": task["sample_id"],
            "setting": "TAXONOMY_AWARE",
            "input_view": "MULTIMODAL",
            "message_token_text": message_text,
            "confusable_legend": confusable_legend,
            "required_entry_grammar": "https://case-<20 lowercase hex>.test/start",
        },
        images=[("TASK1_MESSAGE", task_image)],
        phase="task1",
        validator=validate_task1_response,
    )
    task1_errors = validate_task1_response(task1)
    predicted_entry = normalize_reserved_entry(task1.get("reconstructed_entry"))
    expected_entry = normalize_reserved_entry(binding["entry_value"])
    entry_match = predicted_entry == expected_entry
    if not entry_match:
        task1_errors.append("TASK1_TOP1_ENTRY_MISMATCH")
    if task1_errors:
        raise ValueError(";".join(task1_errors))

    stage1_result = {
        "schema_version": "unified-mllm-stage1-result/v0.2",
        "case_ref": case_ref,
        "sample_id": task["sample_id"],
        "status": "PASS",
        "finished_at": utc_now(),
        "prediction": task1,
        "message_token_text_sha256": sha256_text(message_text),
        "predicted_entry_sha256": sha256_text(predicted_entry),
        "expected_entry_sha256": binding["entry_value_sha256"],
        "top1_exact_match": entry_match,
    }
    atomic_json(case_root / "stage1_result.json", stage1_result)
    atomic_json(
        case_root / "task1_to_task2_handoff.json",
        {
            "schema_version": "task1-to-task2-handoff/v0.1",
            "case_ref": case_ref,
            "sample_id": task["sample_id"],
            "task1_status": "PASS",
            "task1_finished_at": stage1_result["finished_at"],
            "predicted_entry_sha256": stage1_result["predicted_entry_sha256"],
            "entry_type": "RESERVED_SITE",
            "local_mirror_ref": binding["local_mirror_ref"],
            "network_policy": "LOCAL_REPLAY_ONLY",
            "task2_authorized": True,
        },
    )

    profile_path = PROJECT_ROOT / binding["profile"]["path"]
    if sha256_file(profile_path) != binding["profile"]["sha256"]:
        raise ValueError("profile hash mismatch")
    profile = read_json(profile_path)
    scenarios = list((profile.get("verification") or {}).get("scenarios") or [])
    scenario = next((row for row in scenarios if row.get("id") == scenario_id), None)
    if scenario is None:
        raise ValueError(f"profile lacks scenario {scenario_id}")
    site_out = PROJECT_ROOT / binding["site_output"]
    binary = site_out / "mirrorserve"
    site_dir = site_out / "site"
    if sha256_file(binary) != binding["runtime_binary_sha256"]:
        raise ValueError("runtime binary hash mismatch")
    log_handle = (case_root / "mirrorserve.log").open("w", encoding="utf-8")
    process = None
    external_attempts: list[str] = []
    verification_responses: list[dict[str, Any]] = []
    response_events: list[dict[str, Any]] = []
    assertion_count = 0
    try:
        process, port = start_local_runtime_process(
            binary,
            site_dir,
            runtime_root,
            port=free_port(),
            dynamic_port=True,
            log=log_handle,
        )
        local_base = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                device_scale_factor=1,
                service_workers="block",
            )

            def route_handler(route: Any) -> None:
                parsed = urlparse(route.request.url)
                if (
                    parsed.hostname in {"127.0.0.1", "localhost"}
                    and parsed.port == port
                ):
                    route.continue_()
                elif parsed.scheme in {"data", "blob", "about"}:
                    route.continue_()
                else:
                    external_attempts.append(sha256_text(route.request.url))
                    route.abort()

            context.route("**/*", route_handler)
            page = context.new_page()

            def record_response(response: Any) -> None:
                verification_responses.append(
                    {
                        "url": response.url,
                        "status": response.status,
                        "method": response.request.method,
                        "resource_type": response.request.resource_type,
                    }
                )
                response_events.append(response_event(response, local_base))

            page.on("response", record_response)
            try:
                for step_index, step in enumerate(scenario["steps"], 1):
                    op = step["op"]
                    if op not in MUTATING_OPS:
                        execute_step(
                            page,
                            context,
                            step,
                            base_url=local_base,
                            responses=verification_responses,
                            screenshots_dir=raw_root,
                            scenario_id=scenario_id,
                        )
                        if op.startswith("expect_"):
                            assertion_count += 1
                        continue
                    evidence_id = f"EVID{len(evidence) + 1:04d}"
                    descriptor = prepare_action_view(page, step)
                    raw_path = raw_root / f"{evidence_id}_before_{op}.png"
                    visible_path = visible_root / f"{evidence_id}_before_{op}.png"
                    try:
                        page.screenshot(
                            path=str(raw_path), full_page=False, animations="disabled"
                        )
                    finally:
                        remove_action_marker(page)
                    audit = redact_screenshot(
                        raw_path,
                        visible_path,
                        audit_root / f"{evidence_id}.json",
                        tesseract_root=tesseract_root,
                    )
                    evidence.append(
                        {
                            "evidence_id": evidence_id,
                            "phase": "BEFORE_ACTION",
                            "operation": op,
                            "model_visible_path": str(visible_path),
                            "model_visible_sha256": audit["derived"]["sha256"],
                            "parent_sha256": audit["parent"]["sha256"],
                            "redaction_audit_path": str(
                                audit_root / f"{evidence_id}.json"
                            ),
                            "redaction_status": audit["status"],
                        }
                    )
                    action_id = f"ACT{len(actions) + 1:04d}"
                    candidate = {
                        "action_id": action_id,
                        "operation": op,
                        "target": descriptor,
                        "verification_purpose": ACTION_VERIFICATION_PURPOSES[op],
                        "synthetic_fixture_injected_by_controller": op
                        in {"fill", "check"},
                    }
                    decision = invoke_model(
                        system_prompt=prompts["action_system"],
                        user_payload={
                            "phase": "LOCAL_BROWSER_ACTION",
                            "objective": safe_text(scenario.get("description"), 360),
                            "completed_action_ids": [
                                row["action_id"] for row in actions
                            ],
                            "candidate_actions": [
                                candidate,
                                {"action_id": "ABSTAIN", "operation": "stop"},
                            ],
                            "network_policy": "LOCAL_REPLAY_ONLY",
                        },
                        images=[(evidence_id, visible_path)],
                        phase="web_action",
                        validator=validate_action_response_shape,
                    )
                    action_errors = validate_action_response(decision, action_id)
                    if action_errors:
                        raise ValueError(";".join(action_errors))
                    execute_step(
                        page,
                        context,
                        step,
                        base_url=local_base,
                        responses=verification_responses,
                        screenshots_dir=raw_root,
                        scenario_id=scenario_id,
                    )
                    actions.append(
                        {
                            "action_id": action_id,
                            "scenario_step_index": step_index,
                            "operation": op,
                            "target": descriptor,
                            "evidence_id": evidence_id,
                            "model_reason": safe_text(decision["reason"], 500),
                            "visual_observations": [
                                safe_text(row, 300)
                                for row in decision["visual_observations"]
                            ],
                            "synthetic_value_sha256": (
                                sha256_text(str(step.get("value") or ""))
                                if "value" in step
                                else None
                            ),
                            "status": "EXECUTED",
                        }
                    )
                final_evidence_id = f"EVID{len(evidence) + 1:04d}"
                final_raw = raw_root / f"{final_evidence_id}_final.png"
                final_visible = visible_root / f"{final_evidence_id}_final.png"
                page.screenshot(
                    path=str(final_raw), full_page=False, animations="disabled"
                )
                final_audit = redact_screenshot(
                    final_raw,
                    final_visible,
                    audit_root / f"{final_evidence_id}.json",
                    tesseract_root=tesseract_root,
                )
                evidence.append(
                    {
                        "evidence_id": final_evidence_id,
                        "phase": "FINAL_STATE",
                        "operation": "observe",
                        "model_visible_path": str(final_visible),
                        "model_visible_sha256": final_audit["derived"]["sha256"],
                        "parent_sha256": final_audit["parent"]["sha256"],
                        "redaction_audit_path": str(
                            audit_root / f"{final_evidence_id}.json"
                        ),
                        "redaction_status": final_audit["status"],
                    }
                )
            finally:
                context.close()
                browser.close()
    finally:
        stop_process(process)
        log_handle.close()

    judge_images = [
        (row["evidence_id"], Path(row["model_visible_path"])) for row in evidence
    ]
    judgment = invoke_model(
        system_prompt=prompts["judge_system"],
        user_payload={
            "phase": "EVIDENCE_GROUNDED_RISK_JUDGMENT",
            "reconstructed_intent": task1["intent"],
            "entry_type": "RESERVED_SITE",
            "action_ledger": [
                {
                    "action_id": row["action_id"],
                    "operation": row["operation"],
                    "evidence_id": row["evidence_id"],
                    "status": row["status"],
                }
                for row in actions
            ],
            "available_evidence_ids": [row[0] for row in judge_images],
            "scenario_assertions_passed": assertion_count,
            "forbidden_shortcuts": [
                "URL",
                "DOMAIN",
                "IP",
                "REPUTATION",
                "SAMPLING_STRATUM",
            ],
        },
        images=judge_images,
        phase="final_judge",
        validator=lambda value: validate_judgment(
            value, {row[0] for row in judge_images}
        ),
    )
    judgment_errors = validate_judgment(
        judgment, {row["evidence_id"] for row in evidence}
    )
    if judgment_errors:
        raise ValueError(";".join(judgment_errors))
    resolved_models = sorted(
        {
            str(call.get("response_model") or "")
            for call in calls
            if call.get("response_model")
        }
    )
    result = {
        "schema_version": "unified-mllm-smoke-case/v0.1",
        "case_ref": case_ref,
        "sample_id": task["sample_id"],
        "requested_model": model,
        "resolved_models": resolved_models,
        "prompt_bundle_sha256": prompt_bundle_sha256,
        "started_at": started_at,
        "finished_at": utc_now(),
        "status": "PASS",
        "track": "GUIDED_SCENARIO_SMOKE",
        "entry_reconstruction": {
            "status": "PASS",
            "setting": "TAXONOMY_AWARE",
            "input_view": "MULTIMODAL",
            "task_image": task_image_record,
            "message_token_text_sha256": sha256_text(message_text),
            "confusable_legend": confusable_legend,
            "prediction": task1,
            "predicted_entry_sha256": sha256_text(predicted_entry),
            "expected_entry_sha256": binding["entry_value_sha256"],
            "top1_exact_match": entry_match,
        },
        "browser_execution": {
            "status": "PASS",
            "scenario_id": scenario_id,
            "scenario_assertions_passed": assertion_count,
            "action_count": len(actions),
            "actions": actions,
            "external_request_attempt_count": len(external_attempts),
            "external_request_attempt_sha256s": external_attempts,
            "local_response_count": sum(
                row["scope"] == "LOCAL_REPLAY" for row in response_events
            ),
            "local_responses": [
                row for row in response_events if row["scope"] == "LOCAL_REPLAY"
            ],
        },
        "evidence": evidence,
        "judgment": judgment,
        "judgment_status": "PROTOCOL_VALID_UNSCORED_NO_HUMAN_GOLD",
        "model_calls": calls,
        "claim_boundary": {
            "full_corpus_model_evaluation": False,
            "accuracy_or_f1_allowed": False,
            "human_gold_present": False,
            "sampling_stratum_used_as_gold": False,
        },
    }
    atomic_json(case_root / "case_result.json", result)
    return result


def select_inputs(
    resolver: dict[str, Any],
    tasks: list[dict[str, Any]],
    case_config: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    if resolver.get("schema_version") != "benchmark-private-resolver/v0.2":
        raise ValueError("smoke runner requires resolver v0.2")
    if case_config.get("schema_version") != "unified-mllm-smoke-cases/v0.1":
        raise ValueError("unsupported smoke case config")
    bindings = {row["site_id"]: row for row in resolver.get("bindings") or []}
    task_by_id = {row["sample_id"]: row for row in tasks}
    result = []
    seen_strata = set()
    for spec in case_config.get("cases") or []:
        binding = bindings.get(spec.get("site_id"))
        if binding is None:
            raise ValueError(f"smoke site missing from resolver: {spec.get('site_id')}")
        if binding.get("execution_status") != "WEBWORK_READY":
            raise ValueError("smoke case is not WebWork-ready")
        variant = int(spec.get("variant_index", 0))
        sample_ids = binding.get("sample_ids") or []
        if variant < 0 or variant >= len(sample_ids):
            raise ValueError("smoke variant index is invalid")
        task = task_by_id.get(sample_ids[variant])
        if task is None:
            raise ValueError("smoke Task 1 input is missing")
        stratum = binding.get("sampling_stratum")
        if stratum in seen_strata:
            raise ValueError("smoke case config repeats a sampling stratum")
        seen_strata.add(stratum)
        result.append((binding, task, str(spec["scenario_id"])))
    if not result:
        raise ValueError("smoke case config is empty")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolver", type=Path, default=DEFAULT_RESOLVER)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--prompts", type=Path, default=DEFAULT_PROMPTS)
    parser.add_argument("--route-probe", type=Path, default=DEFAULT_ROUTE_PROBE)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT)
    parser.add_argument("--tesseract-root", type=Path, default=DEFAULT_TESSERACT_ROOT)
    parser.add_argument("--confusables", type=Path, default=DEFAULT_CONFUSABLES)
    parser.add_argument("--model")
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--replace", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.out.exists():
        if not args.replace:
            print(json.dumps({"status": "FAIL", "error": "output exists"}))
            return 1
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)
    started_at = utc_now()
    try:
        if not 256 <= args.max_tokens <= 4096:
            raise ValueError("max-tokens must be between 256 and 4096")
        route_probe = read_json(args.route_probe)
        if route_probe.get("status") != "PASS_FIXED_MLLM_SELECTED":
            raise ValueError("route probe has not selected a fixed MLLM")
        model = args.model or route_probe.get("selected_fixed_mllm")
        if not isinstance(model, str) or not MODEL_ID_RE.fullmatch(model):
            raise ValueError("invalid fixed model id")
        passing = {
            row.get("model")
            for row in route_probe.get("results") or []
            if row.get("status") == "PASS_MULTIMODAL_ROUTE"
        }
        if model not in passing:
            raise ValueError("requested model did not pass the frozen route probe")
        environment = load_export_env(args.env_file)
        base_url = environment.get("LIBINFER_NEO_URL") or os.environ.get(
            "LIBINFER_NEO_URL"
        )
        api_key = environment.get("LIBINFER_SK") or os.environ.get("LIBINFER_SK")
        if not base_url or not api_key:
            raise ValueError("missing libinfer credentials")
        prompts_payload = read_json(args.prompts)
        if prompts_payload.get("schema_version") not in {
            "unified-mllm-smoke-prompts/v0.1",
            "unified-mllm-smoke-prompts/v0.2",
        }:
            raise ValueError("unsupported smoke prompt bundle")
        prompts = {
            key: str(prompts_payload[key])
            for key in ("task1_system", "action_system", "judge_system")
        }
        selected = select_inputs(
            read_json(args.resolver), read_jsonl(args.tasks), read_json(args.cases)
        )
        results = []
        failures = []
        for binding, task, scenario_id in selected:
            try:
                result = run_case(
                    binding=binding,
                    task=task,
                    scenario_id=scenario_id,
                    model=model,
                    prompts=prompts,
                    prompt_bundle_sha256=sha256_file(args.prompts),
                    base_url_model=base_url,
                    api_key=api_key,
                    output_dir=args.out,
                    font_path=args.font,
                    tesseract_root=args.tesseract_root,
                    confusables_path=args.confusables,
                    max_tokens=args.max_tokens,
                )
                results.append(result)
            except Exception as exc:  # preserve partial cases and continue smoke matrix
                failure = {
                    "case_ref": binding["case_ref"],
                    "site_ref": binding["site_ref"],
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                }
                failures.append(failure)
                atomic_json(
                    args.out / "cases" / binding["case_ref"] / "failure.json", failure
                )
        resolved_models = sorted(
            {
                resolved
                for result in results
                for resolved in result.get("resolved_models") or []
            }
        )
        summary = {
            "schema_version": "unified-mllm-smoke-summary/v0.1",
            "started_at": started_at,
            "finished_at": utc_now(),
            "status": "PASS"
            if len(results) == len(selected) and not failures
            else "FAIL",
            "track": "GUIDED_SCENARIO_SMOKE",
            "transport": "libinfer-neo",
            "oneapi_used": False,
            "requested_model": model,
            "resolved_models": resolved_models,
            "route_probe": {
                "path": str(args.route_probe),
                "sha256": sha256_file(args.route_probe),
                "status": route_probe["status"],
            },
            "prompt_bundle": {
                "path": str(args.prompts),
                "sha256": sha256_file(args.prompts),
            },
            "target_case_count": len(selected),
            "pass_case_count": len(results),
            "fail_case_count": len(failures),
            "case_results": [
                {
                    "case_ref": row["case_ref"],
                    "status": row["status"],
                    "result_path": str(
                        args.out / "cases" / row["case_ref"] / "case_result.json"
                    ),
                }
                for row in results
            ],
            "failures": failures,
            "claim_boundary": {
                "scope": "THREE_CASE_ENGINEERING_SMOKE_ONLY",
                "scale_600_model_evaluation_status": "NOT_RUN",
                "fraud_accuracy_f1_allowed": False,
                "human_gold_present": False,
                "judgments_are_protocol_valid_but_unscored": True,
            },
        }
        atomic_json(args.out / "summary.json", summary)
    except Exception as exc:
        summary = {
            "schema_version": "unified-mllm-smoke-summary/v0.1",
            "started_at": started_at,
            "finished_at": utc_now(),
            "status": "FAIL_SETUP",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
        }
        atomic_json(args.out / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
