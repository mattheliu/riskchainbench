#!/usr/bin/env python3
"""Independently validate a resumable Task 1 multimodal batch run."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_task1_multimodal_batch as runner  # noqa: E402
from validate_unified_mllm_smoke import validate_generation_policy  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class Checks:
    def __init__(self) -> None:
        self.pass_count = 0
        self.errors: list[dict[str, str]] = []

    def require(
        self, condition: bool, code: str, *, sample_id: str = "RUN", detail: str = ""
    ) -> None:
        if condition:
            self.pass_count += 1
        else:
            self.errors.append(
                {"sample_id": sample_id, "code": code, "detail": detail[:500]}
            )

    def file_hash(
        self, path: Path, expected: Any, code: str, *, sample_id: str = "RUN"
    ) -> None:
        self.require(path.is_file(), f"{code}_MISSING", sample_id=sample_id, detail=str(path))
        if path.is_file():
            self.require(
                runner.sha256_file(path) == expected,
                f"{code}_HASH_MISMATCH",
                sample_id=sample_id,
                detail=str(path),
            )


def embedded_hash(value: dict[str, Any], field: str) -> str:
    payload = copy.deepcopy(value)
    payload.pop(field, None)
    return runner.sha256_text(runner.canonical_json(payload))


def validate_run(run_dir: Path) -> dict[str, Any]:
    checks = Checks()
    config_path = run_dir / "config.json"
    summary_path = run_dir / "summary.json"
    config = read_json(config_path)
    summary = read_json(summary_path)
    fingerprint_payload = copy.deepcopy(config)
    fingerprint_payload.pop("created_at", None)
    fingerprint_payload.pop("config_fingerprint", None)
    expected_fingerprint = runner.sha256_text(
        runner.canonical_json(fingerprint_payload)
    )
    checks.require(
        config.get("config_fingerprint") == expected_fingerprint,
        "CONFIG_FINGERPRINT_MISMATCH",
    )
    checks.require(summary.get("status") == "PASS", "SUMMARY_NOT_PASS")
    checks.require(summary.get("oneapi_used") is False, "SUMMARY_ONEAPI_USED")
    checks.require(config.get("oneapi_used") is False, "CONFIG_ONEAPI_USED")
    checks.require(
        summary.get("config_fingerprint") == config.get("config_fingerprint"),
        "SUMMARY_CONFIG_MISMATCH",
    )
    checks.require(
        summary.get("summary_sha256") == embedded_hash(summary, "summary_sha256"),
        "SUMMARY_HASH_MISMATCH",
    )
    for name, row in (config.get("files") or {}).items():
        if not isinstance(row, dict):
            checks.require(False, "CONFIG_FILE_RECORD_INVALID", detail=str(name))
            continue
        checks.file_hash(Path(str(row.get("path") or "")), row.get("sha256"), f"CONFIG_{name.upper()}")

    tasks_path = Path(config["files"]["tasks"]["path"])
    prediction_schema = read_json(Path(config["files"]["prediction_schema"]["path"]))
    all_tasks = read_jsonl(tasks_path)
    variant_config = config.get("variant_indices", "ALL")
    variant_indices = (
        {int(value) for value in variant_config}
        if isinstance(variant_config, list)
        else None
    )
    selected = runner.select_tasks(
        all_tasks,
        int(config["source_offset"]),
        int(config["source_limit"]),
        variant_indices,
    )
    checks.require(
        len(selected) == config.get("selected_task_count"),
        "SELECTED_TASK_COUNT_MISMATCH",
    )
    checks.require(
        len({runner.source_id(row["sample_id"]) for row in selected})
        == config.get("selected_source_count"),
        "SELECTED_SOURCE_COUNT_MISMATCH",
    )
    checks.require(
        config.get("selected_task_order_sha256")
        == runner.sha256_text(
            runner.canonical_json([row["task_sha256"] for row in selected])
        ),
        "SELECTED_TASK_ORDER_HASH_MISMATCH",
    )

    aggregate_predictions = read_jsonl(run_dir / "predictions.jsonl")
    aggregate_by_id = {row.get("sample_id"): row for row in aggregate_predictions}
    checks.require(
        len(aggregate_predictions) == len(selected),
        "AGGREGATE_PREDICTION_COUNT_MISMATCH",
    )
    checks.require(
        len(aggregate_by_id) == len(aggregate_predictions),
        "AGGREGATE_PREDICTION_DUPLICATE",
    )
    checks.file_hash(
        run_dir / "predictions.jsonl",
        summary.get("predictions_sha256"),
        "AGGREGATE_PREDICTIONS",
    )
    checks.file_hash(
        run_dir / "failures.jsonl",
        summary.get("failures_sha256"),
        "AGGREGATE_FAILURES",
    )
    checks.require(
        not (run_dir / "failures.jsonl").read_text(encoding="utf-8").strip(),
        "FAILURE_LEDGER_NONEMPTY",
    )

    for task in selected:
        sample_id = str(task["sample_id"])
        task_root = run_dir / "tasks" / sample_id
        state_path = task_root / "state.json"
        prediction_path = task_root / "prediction.json"
        checks.require(state_path.is_file(), "TASK_STATE_MISSING", sample_id=sample_id)
        checks.require(
            prediction_path.is_file(), "TASK_PREDICTION_MISSING", sample_id=sample_id
        )
        if not state_path.is_file() or not prediction_path.is_file():
            continue
        state = read_json(state_path)
        prediction = read_json(prediction_path)
        checks.require(state.get("status") == "PASS", "TASK_STATE_NOT_PASS", sample_id=sample_id)
        checks.require(
            state.get("config_fingerprint") == config.get("config_fingerprint"),
            "TASK_CONFIG_MISMATCH",
            sample_id=sample_id,
        )
        checks.file_hash(
            prediction_path,
            state.get("prediction_sha256"),
            "TASK_PREDICTION",
            sample_id=sample_id,
        )
        prediction_errors = runner.render_schema_errors(
            prediction, prediction_schema, "PREDICTION"
        )
        checks.require(
            not prediction_errors,
            "TASK_PREDICTION_SCHEMA_INVALID",
            sample_id=sample_id,
            detail=";".join(prediction_errors),
        )
        try:
            runner.validate_prediction(prediction, prediction_schema, task)
            semantics_valid = True
            semantic_error = ""
        except ValueError as exc:
            semantics_valid = False
            semantic_error = str(exc)
        checks.require(
            semantics_valid,
            "TASK_PREDICTION_SEMANTICS_INVALID",
            sample_id=sample_id,
            detail=semantic_error,
        )
        checks.require(
            aggregate_by_id.get(sample_id) == prediction,
            "AGGREGATE_PREDICTION_CONTENT_MISMATCH",
            sample_id=sample_id,
        )

        attempt_root = Path(str(state.get("attempt_path") or ""))
        try:
            attempt_root.resolve().relative_to(task_root.resolve())
            attempt_inside_task = True
        except ValueError:
            attempt_inside_task = False
        checks.require(
            attempt_inside_task,
            "ATTEMPT_PATH_ESCAPES_TASK",
            sample_id=sample_id,
            detail=str(attempt_root),
        )
        input_audit_path = attempt_root / "input_audit.json"
        call_path = attempt_root / "model_call.json"
        checks.require(
            input_audit_path.is_file(), "INPUT_AUDIT_MISSING", sample_id=sample_id
        )
        checks.file_hash(
            call_path,
            state.get("model_call_sha256"),
            "MODEL_CALL",
            sample_id=sample_id,
        )
        if input_audit_path.is_file():
            input_audit = read_json(input_audit_path)
            checks.require(
                input_audit.get("status") == "PASS",
                "INPUT_AUDIT_NOT_PASS",
                sample_id=sample_id,
            )
            checks.require(
                not input_audit.get("forbidden_key_findings"),
                "INPUT_FORBIDDEN_KEY_LEAK",
                sample_id=sample_id,
            )
            for field in ("gold_present", "resolver_present", "web_evidence_present"):
                checks.require(
                    input_audit.get(field) is False,
                    f"INPUT_{field.upper()}",
                    sample_id=sample_id,
                )
            image = input_audit.get("image") or {}
            checks.file_hash(
                Path(str(image.get("path") or "")),
                image.get("sha256"),
                "TASK_IMAGE",
                sample_id=sample_id,
            )
        if call_path.is_file():
            call = read_json(call_path)
            checks.require(call.get("status") == "PASS", "MODEL_CALL_NOT_PASS", sample_id=sample_id)
            checks.require(
                call.get("requested_model") == config.get("requested_model"),
                "REQUESTED_MODEL_CHANGED",
                sample_id=sample_id,
            )
            checks.require(
                call.get("response_model")
                in set(
                    config.get("resolved_model_allowlist")
                    or [config.get("requested_model")]
                ),
                "RESOLVED_MODEL_CHANGED",
                sample_id=sample_id,
            )
            checks.require(
                call.get("request_protocol")
                == "riskchainbench_task1_multimodal_batch_v0.1",
                "REQUEST_PROTOCOL_CHANGED",
                sample_id=sample_id,
            )
            policy_errors = validate_generation_policy(
                call, str(config.get("requested_model") or "")
            )
            checks.require(
                not policy_errors,
                "GENERATION_POLICY_INVALID",
                sample_id=sample_id,
                detail=",".join(policy_errors),
            )
            attempts = call.get("attempts") or []
            checks.require(bool(attempts), "MODEL_ATTEMPTS_EMPTY", sample_id=sample_id)
            checks.require(
                bool(attempts) and attempts[-1].get("status") == "PASS",
                "MODEL_FINAL_ATTEMPT_NOT_PASS",
                sample_id=sample_id,
            )
            for attempt in attempts:
                checks.require(
                    bool(re.fullmatch(r"[0-9a-f]{64}", str(attempt.get("request_sha256") or ""))),
                    "MODEL_REQUEST_HASH_INVALID",
                    sample_id=sample_id,
                )
                response = attempt.get("response_content")
                if response is not None:
                    checks.require(
                        attempt.get("response_sha256") == runner.sha256_text(str(response)),
                        "MODEL_RESPONSE_HASH_MISMATCH",
                        sample_id=sample_id,
                    )

    return {
        "schema_version": "task1-multimodal-batch-validation/v0.1",
        "generated_at": utc_now(),
        "status": "PASS" if not checks.errors else "FAIL",
        "run_dir": str(run_dir),
        "validated_source_count": config.get("selected_source_count"),
        "validated_task_count": len(selected),
        "check_pass_count": checks.pass_count,
        "check_fail_count": len(checks.errors),
        "errors": checks.errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = validate_run(args.run_dir)
        output = args.out or args.run_dir / "audits/task1_artifact_validation.json"
        runner.atomic_json(output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["status"] == "PASS" else 2
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"status": "FAIL", "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
