#!/usr/bin/env python3
"""Freeze Task 1 results under a system-failures-only retry policy."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_unified_mllm_smoke import provider_output_observed  # noqa: E402


DEFAULT_MODELS = (
    "gpt-5.4",
    "claude-opus-4-8-kiro",
    "kimi-k2.6",
    "gemini-3.6-flash",
)
SAMPLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def embedded_hash(value: dict[str, Any], field: str) -> str:
    unhashed = copy.deepcopy(value)
    unhashed.pop(field, None)
    return sha256_text(canonical_json(unhashed))


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def atomic_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        "".join(canonical_json(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "model"


def parse_models(value: str) -> list[str]:
    models = [row.strip() for row in value.split(",") if row.strip()]
    if not models or len(models) != len(set(models)):
        raise ValueError("models must be a unique non-empty list")
    return models


def attempt_directories(task_root: Path) -> list[Path]:
    attempts = task_root / "attempts"
    if not attempts.is_dir():
        return []
    return sorted(
        (
            path
            for path in attempts.iterdir()
            if path.is_dir() and path.name.isdigit()
        ),
        key=lambda path: int(path.name),
    )


def source_reference(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
    }


def inspect_task_attempts(
    task_root: Path,
    *,
    sample_id: str,
    model: str,
) -> dict[str, Any]:
    """Select the first model-observed outcome after any system-only failures."""

    system_failures: list[dict[str, Any]] = []
    directories = attempt_directories(task_root)
    for directory_index, attempt_dir in enumerate(directories):
        call_path = attempt_dir / "model_call.json"
        if not call_path.is_file():
            system_failures.append(
                {
                    "attempt_path": str(attempt_dir.resolve()),
                    "reason": "MODEL_CALL_AUDIT_MISSING",
                }
            )
            continue
        call = read_json(call_path)
        provider_attempts = [
            row for row in call.get("attempts") or [] if isinstance(row, dict)
        ]
        observed = [
            (index, row)
            for index, row in enumerate(provider_attempts, 1)
            if provider_output_observed(row)
        ]
        if not observed:
            system_failures.append(
                {
                    "attempt_path": str(attempt_dir.resolve()),
                    "model_call_sha256": sha256_file(call_path),
                    "reason": "NO_PROVIDER_OUTPUT_OBSERVED",
                    "provider_attempt_count": len(provider_attempts),
                    "statuses": [
                        str(row.get("status") or "UNKNOWN")
                        for row in provider_attempts
                    ],
                }
            )
            continue

        provider_index, provider_row = observed[0]
        for internal_index, system_row in enumerate(
            provider_attempts[: provider_index - 1],
            1,
        ):
            system_failures.append(
                {
                    "attempt_path": str(attempt_dir.resolve()),
                    "model_call_sha256": sha256_file(call_path),
                    "provider_attempt_index": internal_index,
                    "reason": "PRE_PROVIDER_SYSTEM_FAILURE",
                    "status": str(system_row.get("status") or "UNKNOWN"),
                    "http_status": system_row.get("http_status"),
                    "error_type": system_row.get("error_type"),
                    "error": str(system_row.get("error") or "")[:500],
                }
            )
        terminal = {
            "sample_id": sample_id,
            "model": model,
            "source_attempt_path": str(attempt_dir.resolve()),
            "source_model_call": source_reference(call_path),
            "provider_attempt_index": provider_index,
            "provider_attempt_status": str(provider_row.get("status") or "UNKNOWN"),
            "provider_response_model": provider_row.get("response_model"),
            "provider_finish_reason": provider_row.get("finish_reason"),
            "provider_response_sha256": provider_row.get("response_sha256"),
            "system_failure_count_before_terminal": len(system_failures),
            "system_failures_before_terminal": system_failures,
            "ignored_later_task_attempt_count": len(directories) - directory_index - 1,
            "retry_policy": "SYSTEM_FAILURES_ONLY",
        }
        if provider_row.get("status") == "PASS":
            prediction_path = attempt_dir / "prediction.json"
            if not prediction_path.is_file():
                system_failures.append(
                    {
                        "attempt_path": str(attempt_dir.resolve()),
                        "model_call_sha256": sha256_file(call_path),
                        "reason": "POST_MODEL_PREDICTION_MISSING",
                    }
                )
                continue
            prediction = read_json(prediction_path)
            if (
                prediction.get("sample_id") != sample_id
                or prediction.get("model_id") != model
            ):
                raise ValueError(f"prediction identity mismatch: {prediction_path}")
            terminal.update(
                {
                    "status": "PASS",
                    "prediction": prediction,
                    "source_prediction": source_reference(prediction_path),
                }
            )
            return terminal

        terminal.update(
            {
                "status": "TASK1_MODEL_FAILURE",
                "error_type": str(
                    provider_row.get("error_type")
                    or provider_row.get("status")
                    or "MODEL_OUTPUT_FAILURE"
                ),
                "error": str(
                    provider_row.get("error")
                    or "first provider-observed output did not satisfy the protocol"
                )[:1000],
            }
        )
        return terminal

    terminal_attempt = directories[-1] if directories else task_root
    return {
        "sample_id": sample_id,
        "model": model,
        "status": "TASK1_SYSTEM_FAILURE_EXHAUSTED",
        "error_type": "SYSTEM_FAILURE_EXHAUSTED",
        "error": "no provider-observed output was obtained",
        "source_attempt_path": str(terminal_attempt.resolve()),
        "system_failure_count_before_terminal": len(system_failures),
        "system_failures_before_terminal": system_failures,
        "ignored_later_task_attempt_count": 0,
        "retry_policy": "SYSTEM_FAILURES_ONLY",
    }


def selected_primary_tasks(task1_release: Path) -> list[dict[str, Any]]:
    tasks = read_jsonl(task1_release / "model_visible/task1_inputs.jsonl")
    selected = [
        row for row in tasks if str(row.get("sample_id") or "").endswith("--v000")
    ]
    if len(selected) != 600:
        raise ValueError(f"Task 1 primary selection must contain 600 rows: {len(selected)}")
    if len({str(row.get("sample_id") or "") for row in selected}) != len(selected):
        raise ValueError("Task 1 primary selection contains duplicate sample_id values")
    if any(not SAMPLE_ID_RE.fullmatch(str(row.get("sample_id") or "")) for row in selected):
        raise ValueError("Task 1 primary selection contains an unsafe sample_id")
    return selected


def freeze_model(
    *,
    model: str,
    raw_results: Path,
    output: Path,
    selected: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_run = raw_results / "models" / safe_slug(model) / "primary/run"
    raw_config_path = raw_run / "config.json"
    raw_summary_path = raw_run / "summary.json"
    if not raw_config_path.is_file() or not raw_summary_path.is_file():
        raise ValueError(f"raw Task 1 run is not terminal: {model}")
    raw_config = read_json(raw_config_path)
    raw_summary = read_json(raw_summary_path)
    if (
        raw_config.get("requested_model") != model
        or raw_summary.get("requested_model") != model
        or raw_summary.get("selected_task_count") != 600
        or int(raw_summary.get("valid_prediction_count") or 0)
        + int(raw_summary.get("failed_prediction_count") or 0)
        != 600
    ):
        raise ValueError(f"raw Task 1 accounting mismatch: {model}")

    run_root = output / "models" / safe_slug(model) / "primary/run"
    outcomes: list[dict[str, Any]] = []
    predictions: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for task in selected:
        sample_id = str(task["sample_id"])
        task_root = raw_run / "tasks" / sample_id
        outcome = inspect_task_attempts(
            task_root,
            sample_id=sample_id,
            model=model,
        )
        raw_prediction_exists = (task_root / "prediction.json").is_file()
        outcome["raw_canonical_prediction_existed"] = raw_prediction_exists
        outcomes.append(outcome)
        if outcome["status"] == "PASS":
            predictions.append(outcome["prediction"])
        else:
            failure = {
                "sample_id": sample_id,
                "error_type": outcome["status"],
                "error": outcome["error"],
                "source_attempt_path": outcome["source_attempt_path"],
                "ignored_later_task_attempt_count": outcome[
                    "ignored_later_task_attempt_count"
                ],
            }
            failures.append(failure)
            state = {
                "schema_version": "task1-protocol-frozen-task-state/v0.1",
                "sample_id": sample_id,
                "status": "FAIL",
                "error_type": outcome["status"],
                "error": outcome["error"],
                "attempt_path": outcome["source_attempt_path"],
                "retry_policy": "SYSTEM_FAILURES_ONLY",
                "source_raw_run": str(raw_run.resolve()),
                "finished_at": utc_now(),
            }
            atomic_json(run_root / "tasks" / sample_id / "state.json", state)

    atomic_jsonl(run_root / "predictions.jsonl", predictions)
    atomic_jsonl(run_root / "failures.jsonl", failures)
    audit_rows = []
    for outcome in outcomes:
        audit = dict(outcome)
        audit.pop("prediction", None)
        audit_rows.append(audit)
    audit_path = run_root / "protocol_accounting.jsonl"
    atomic_jsonl(audit_path, audit_rows)
    model_failure_count = sum(
        row["status"] == "TASK1_MODEL_FAILURE" for row in outcomes
    )
    system_failure_count = sum(
        row["status"] == "TASK1_SYSTEM_FAILURE_EXHAUSTED" for row in outcomes
    )
    masked_success_count = sum(
        row["status"] != "PASS" and row["raw_canonical_prediction_existed"]
        for row in outcomes
    )
    config: dict[str, Any] = {
        "schema_version": "task1-protocol-frozen-config/v0.1",
        "requested_model": model,
        "selected_task_count": 600,
        "variant_indices": [0],
        "transport": "libinfer-neo",
        "oneapi_used": False,
        "retry_policy": "SYSTEM_FAILURES_ONLY",
        "terminal_policy": "FIRST_PROVIDER_OBSERVED_OUTCOME",
        "source_raw_config": source_reference(raw_config_path),
        "source_raw_summary": source_reference(raw_summary_path),
        "source_raw_run": str(raw_run.resolve()),
        "created_at": utc_now(),
    }
    config["config_fingerprint"] = embedded_hash(config, "config_fingerprint")
    atomic_json(run_root / "config.json", config)
    summary: dict[str, Any] = {
        "schema_version": "task1-protocol-frozen-summary/v0.1",
        "status": "PASS" if not failures else "PARTIAL",
        "requested_model": model,
        "selected_task_count": 600,
        "valid_prediction_count": len(predictions),
        "failed_prediction_count": len(failures),
        "model_failure_count": model_failure_count,
        "system_failure_exhausted_count": system_failure_count,
        "raw_success_masked_by_prior_model_failure_count": masked_success_count,
        "system_failure_attempt_count": sum(
            int(row["system_failure_count_before_terminal"]) for row in outcomes
        ),
        "retry_policy": "SYSTEM_FAILURES_ONLY",
        "terminal_policy": "FIRST_PROVIDER_OBSERVED_OUTCOME",
        "predictions_path": str(run_root / "predictions.jsonl"),
        "predictions_sha256": sha256_file(run_root / "predictions.jsonl"),
        "failures_path": str(run_root / "failures.jsonl"),
        "failures_sha256": sha256_file(run_root / "failures.jsonl"),
        "protocol_accounting": {
            "path": str(audit_path),
            "sha256": sha256_file(audit_path),
            "row_count": len(audit_rows),
        },
        "source_raw_summary": source_reference(raw_summary_path),
        "generated_at": utc_now(),
    }
    summary["summary_sha256"] = embedded_hash(summary, "summary_sha256")
    atomic_json(run_root / "summary.json", summary)
    status = {
        "schema_version": "riskchainbench-task1-track-status/v0.1",
        "model": model,
        "track": "primary",
        "state": "COMPLETE" if not failures else "PARTIAL",
        "selected_task_count": 600,
        "valid_prediction_count": len(predictions),
        "failed_prediction_count": len(failures),
        "protocol_freeze_status": "PASS",
        "retry_policy": "SYSTEM_FAILURES_ONLY",
        "updated_at": utc_now(),
    }
    atomic_json(output / "models" / safe_slug(model) / "primary/matrix_status.json", status)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task1-release", type=Path, required=True)
    parser.add_argument("--raw-results", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    try:
        models = parse_models(args.models)
        if args.out.exists():
            if not args.replace:
                raise ValueError("output already exists; pass --replace to rebuild")
            shutil.rmtree(args.out)
        selected = selected_primary_tasks(args.task1_release)
        statuses = [
            freeze_model(
                model=model,
                raw_results=args.raw_results,
                output=args.out,
                selected=selected,
            )
            for model in models
        ]
        terminal = all(row["state"] in {"COMPLETE", "PARTIAL"} for row in statuses)
        matrix: dict[str, Any] = {
            "schema_version": "task1-protocol-frozen-matrix/v0.1",
            "status": (
                "COMPLETE"
                if terminal and all(row["state"] == "COMPLETE" for row in statuses)
                else "COMPLETE_WITH_PARTIAL"
                if terminal
                else "FAIL"
            ),
            "models": models,
            "case_count_per_model": 600,
            "total_case_count": 600 * len(models),
            "retry_policy": "SYSTEM_FAILURES_ONLY",
            "terminal_policy": "FIRST_PROVIDER_OBSERVED_OUTCOME",
            "source_raw_results": str(args.raw_results.resolve()),
            "rows": statuses,
            "generated_at": utc_now(),
        }
        matrix["matrix_sha256"] = embedded_hash(matrix, "matrix_sha256")
        atomic_json(args.out / "matrix_summary.json", matrix)
        print(json.dumps(matrix, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if terminal else 2
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
