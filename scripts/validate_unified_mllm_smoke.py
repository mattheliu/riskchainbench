#!/usr/bin/env python3
"""Independently validate the fixed-MLLM end-to-end smoke artifacts."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESOLVER = (
    PROJECT_ROOT
    / "outputs/riskchainbench_30_100_600_v0.1/scale_600/private/resolver.json"
)
DEFAULT_TASKS = (
    PROJECT_ROOT
    / "outputs/riskchainbench_30_100_600_v0.1/scale_600/public/task1_inputs.jsonl"
)
DEFAULT_CASES = PROJECT_ROOT / "configs/unified_mllm_smoke_cases_v0.1.json"
DEFAULT_ROUTE_PROBE = (
    PROJECT_ROOT
    / "outputs/riskchainbench_30_100_600_v0.1/pilot_30/audits/"
    "libinfer_multimodal_route_probe_domestic_v0.3.json"
)
DEFAULT_CONFUSABLES = (
    PROJECT_ROOT / "data/derived/unicode_confusables_ascii_v17.0.0.json"
)
MUTATING_OPS = {"fill", "check", "click", "direct_submit", "reload"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    temporary.replace(path)


class Checks:
    def __init__(self) -> None:
        self.pass_count = 0
        self.errors: list[dict[str, str]] = []

    def require(
        self, condition: bool, code: str, *, case_ref: str = "RUN", detail: str = ""
    ) -> None:
        if condition:
            self.pass_count += 1
        else:
            self.errors.append(
                {"case_ref": case_ref, "code": code, "detail": detail[:500]}
            )

    def file_hash(
        self, path_value: Any, expected: Any, code: str, *, case_ref: str
    ) -> Path | None:
        path = Path(str(path_value or ""))
        exists = path.is_file()
        self.require(exists, f"{code}_MISSING", case_ref=case_ref, detail=str(path))
        if not exists:
            return None
        self.require(
            sha256_file(path) == expected,
            f"{code}_HASH_MISMATCH",
            case_ref=case_ref,
            detail=str(path),
        )
        return path


def selected_cases(
    resolver: dict[str, Any], tasks: list[dict[str, Any]], config: dict[str, Any]
) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    bindings = {row["site_id"]: row for row in resolver.get("bindings") or []}
    task_by_id = {row["sample_id"]: row for row in tasks}
    selected = []
    for spec in config.get("cases") or []:
        binding = bindings[spec["site_id"]]
        sample_id = binding["sample_ids"][int(spec.get("variant_index", 0))]
        selected.append((binding, task_by_id[sample_id], spec))
    return selected


def validate_judgment(
    value: Any, evidence_ids: set[str], visible_to_judge: set[str]
) -> list[str]:
    errors = []
    required = {
        "intent",
        "evidence",
        "risk_reason",
        "answer",
        "evidence_sufficiency",
        "counterevidence",
    }
    if not isinstance(value, dict) or set(value) != required:
        return ["JUDGMENT_SCHEMA_INVALID"]
    if value.get("answer") not in {"YES", "NO", "INSUFFICIENT"}:
        errors.append("JUDGMENT_ANSWER_INVALID")
    if value.get("evidence_sufficiency") not in {"SUFFICIENT", "INSUFFICIENT"}:
        errors.append("JUDGMENT_SUFFICIENCY_INVALID")
    citations = value.get("evidence")
    if not isinstance(citations, list) or not citations:
        errors.append("JUDGMENT_CITATIONS_EMPTY")
    else:
        for citation in citations:
            if not isinstance(citation, dict):
                errors.append("JUDGMENT_CITATION_INVALID")
                continue
            evidence_id = citation.get("evidence_id")
            if evidence_id not in evidence_ids:
                errors.append("JUDGMENT_CITATION_UNRESOLVED")
            if evidence_id not in visible_to_judge:
                errors.append("JUDGMENT_CITATION_NOT_MODEL_VISIBLE")
            if not str(citation.get("observation") or "").strip():
                errors.append("JUDGMENT_CITATION_OBSERVATION_EMPTY")
    return sorted(set(errors))


def validate_generation_policy(call: dict[str, Any], model: str) -> list[str]:
    """Validate the frozen route-specific generation parameter policy."""

    errors: list[str] = []
    options = call.get("model_request_options")
    if not isinstance(options, dict):
        return ["MODEL_REQUEST_OPTIONS_INVALID"]
    if model.startswith("gpt-5."):
        if call.get("temperature_policy") != "provider_default_omitted":
            errors.append("TEMPERATURE_POLICY_NOT_FROZEN")
        if call.get("reasoning_effort_policy") != "provider_default_omitted":
            errors.append("REASONING_EFFORT_POLICY_NOT_FROZEN")
        if "temperature" in options:
            errors.append("TEMPERATURE_MUST_BE_OMITTED")
        if "reasoning_effort" in options:
            errors.append("REASONING_EFFORT_MUST_BE_OMITTED")
    else:
        if call.get("temperature_policy") != "explicit_zero":
            errors.append("TEMPERATURE_POLICY_NOT_FROZEN")
        if call.get("reasoning_effort_policy") != "explicit_minimal":
            errors.append("REASONING_EFFORT_POLICY_NOT_FROZEN")
        if options.get("temperature") != 0:
            errors.append("TEMPERATURE_NOT_FROZEN")
        if options.get("reasoning_effort") != "minimal":
            errors.append("REASONING_EFFORT_NOT_FROZEN")
    expected_thinking = (
        "enable_thinking_false"
        if model.startswith("qwen")
        else "thinking_type_disabled"
        if model.startswith("kimi-")
        else "provider_default"
    )
    if call.get("thinking_policy", "provider_default") != expected_thinking:
        errors.append("THINKING_POLICY_NOT_FROZEN")
    if expected_thinking == "enable_thinking_false":
        if options.get("enable_thinking") is not False:
            errors.append("ENABLE_THINKING_NOT_FALSE")
    elif expected_thinking == "thinking_type_disabled":
        if options.get("thinking") != {"type": "disabled"}:
            errors.append("KIMI_THINKING_NOT_DISABLED")
    elif "enable_thinking" in options or "thinking" in options:
        errors.append("THINKING_OVERRIDE_UNEXPECTED")
    return errors


def validate_run(
    *,
    run_dir: Path,
    resolver_path: Path,
    tasks_path: Path,
    cases_path: Path,
    route_probe_path: Path,
    confusables_path: Path,
) -> dict[str, Any]:
    checks = Checks()
    summary = read_json(run_dir / "summary.json")
    resolver = read_json(resolver_path)
    tasks = read_jsonl(tasks_path)
    config = read_json(cases_path)
    route_probe = read_json(route_probe_path)
    confusables = read_json(confusables_path)
    selected = selected_cases(resolver, tasks, config)

    model = summary.get("requested_model")
    probe_rows = [
        row
        for row in route_probe.get("results") or []
        if row.get("model") == model and row.get("status") == "PASS_MULTIMODAL_ROUTE"
    ]
    pinned_resolved_models = {
        str(probe.get("response_model"))
        for row in probe_rows
        for probe in row.get("probes") or []
        if probe.get("response_model")
    }
    checks.require(summary.get("status") == "PASS", "SUMMARY_NOT_PASS")
    checks.require(summary.get("transport") == "libinfer-neo", "TRANSPORT_NOT_LIBINFER_NEO")
    checks.require(summary.get("oneapi_used") is False, "ONEAPI_NOT_DISABLED")
    # The probe's selected_fixed_mllm is the primary arm, not the only valid
    # comparator. Every experiment arm must still be explicitly present and
    # PASS in the same immutable multimodal route probe.
    checks.require(len(probe_rows) == 1, "MODEL_NOT_FROZEN_ROUTE")
    checks.require(len(pinned_resolved_models) == 1, "ROUTE_RESOLUTION_NOT_SINGLE")
    checks.require(summary.get("target_case_count") == len(selected), "TARGET_COUNT_MISMATCH")
    checks.require(summary.get("pass_case_count") == len(selected), "PASS_COUNT_MISMATCH")
    checks.require(summary.get("fail_case_count") == 0, "FAIL_COUNT_NONZERO")
    boundary = summary.get("claim_boundary") or {}
    checks.require(
        boundary.get("scale_600_model_evaluation_status") == "NOT_RUN",
        "FULL_600_MODEL_CLAIM_PRESENT",
    )
    checks.require(boundary.get("fraud_accuracy_f1_allowed") is False, "METRIC_CLAIM_ALLOWED")
    checks.require(boundary.get("human_gold_present") is False, "HUMAN_GOLD_MISSTATED")
    checks.require(
        boundary.get("judgments_are_protocol_valid_but_unscored") is True,
        "UNSCORED_BOUNDARY_MISSING",
    )

    summary_refs = {row.get("case_ref") for row in summary.get("case_results") or []}
    expected_refs = {row[0]["case_ref"] for row in selected}
    checks.require(summary_refs == expected_refs, "SUMMARY_CASE_SET_MISMATCH")

    for binding, task, spec in selected:
        case_ref = binding["case_ref"]
        result_path = run_dir / "cases" / case_ref / "case_result.json"
        checks.require(result_path.is_file(), "CASE_RESULT_MISSING", case_ref=case_ref)
        if not result_path.is_file():
            continue
        result = read_json(result_path)
        checks.require(result.get("status") == "PASS", "CASE_NOT_PASS", case_ref=case_ref)
        checks.require(result.get("sample_id") == task["sample_id"], "SAMPLE_ID_MISMATCH", case_ref=case_ref)
        checks.require(result.get("requested_model") == model, "CASE_MODEL_MISMATCH", case_ref=case_ref)
        checks.require(
            set(result.get("resolved_models") or []) == pinned_resolved_models,
            "CASE_RESOLVED_MODEL_MISMATCH",
            case_ref=case_ref,
        )

        entry = result.get("entry_reconstruction") or {}
        expected_entry_sha = binding["entry_value_sha256"]
        message_text = "\n\n".join(
            str(row.get("content") or "") for row in task.get("messages") or []
        )
        legend = entry.get("confusable_legend") or {}
        checks.require(entry.get("status") == "PASS", "ENTRY_NOT_PASS", case_ref=case_ref)
        checks.require(entry.get("setting") == "TAXONOMY_AWARE", "ENTRY_SETTING_INVALID", case_ref=case_ref)
        checks.require(entry.get("input_view") == "MULTIMODAL", "ENTRY_VIEW_NOT_MULTIMODAL", case_ref=case_ref)
        checks.require(entry.get("top1_exact_match") is True, "ENTRY_TOP1_NOT_EXACT", case_ref=case_ref)
        checks.require(entry.get("predicted_entry_sha256") == expected_entry_sha, "ENTRY_PREDICTION_HASH_MISMATCH", case_ref=case_ref)
        checks.require(entry.get("expected_entry_sha256") == expected_entry_sha, "ENTRY_GOLD_HASH_MISMATCH", case_ref=case_ref)
        checks.require(entry.get("message_token_text_sha256") == sha256_text(message_text), "ENTRY_TEXT_HASH_MISMATCH", case_ref=case_ref)
        checks.require(legend.get("setting") == "TAXONOMY_AWARE", "LEGEND_SETTING_INVALID", case_ref=case_ref)
        checks.require(legend.get("mapping_id") == confusables.get("mapping_id"), "LEGEND_MAPPING_ID_MISMATCH", case_ref=case_ref)
        checks.require(legend.get("unicode_version") == confusables.get("unicode_version"), "LEGEND_UNICODE_VERSION_MISMATCH", case_ref=case_ref)
        checks.require(legend.get("mapping_file_sha256") == sha256_file(confusables_path), "LEGEND_FILE_HASH_MISMATCH", case_ref=case_ref)
        task_image = entry.get("task_image") or {}
        checks.file_hash(task_image.get("path"), task_image.get("sha256"), "TASK_IMAGE", case_ref=case_ref)

        profile_path = PROJECT_ROOT / binding["profile"]["path"]
        profile = read_json(profile_path)
        scenario = next(
            row
            for row in (profile.get("verification") or {}).get("scenarios") or []
            if row.get("id") == spec["scenario_id"]
        )
        expected_actions = sum(row.get("op") in MUTATING_OPS for row in scenario["steps"])
        expected_assertions = sum(str(row.get("op") or "").startswith("expect_") for row in scenario["steps"])
        browser = result.get("browser_execution") or {}
        checks.require(browser.get("status") == "PASS", "BROWSER_NOT_PASS", case_ref=case_ref)
        checks.require(browser.get("scenario_id") == spec["scenario_id"], "SCENARIO_ID_MISMATCH", case_ref=case_ref)
        checks.require(browser.get("action_count") == expected_actions, "ACTION_COUNT_MISMATCH", case_ref=case_ref)
        checks.require(browser.get("scenario_assertions_passed") == expected_assertions, "ASSERTION_COUNT_MISMATCH", case_ref=case_ref)
        checks.require(browser.get("external_request_attempt_count") == 0, "EXTERNAL_REQUEST_ATTEMPTED", case_ref=case_ref)
        checks.require(not browser.get("external_request_attempt_sha256s"), "EXTERNAL_REQUEST_LEDGER_NONEMPTY", case_ref=case_ref)
        actions = browser.get("actions") or []
        checks.require(all(row.get("status") == "EXECUTED" for row in actions), "ACTION_NOT_EXECUTED", case_ref=case_ref)

        evidence = result.get("evidence") or []
        evidence_ids = {str(row.get("evidence_id")) for row in evidence}
        checks.require(len(evidence) == expected_actions + 1, "EVIDENCE_COUNT_MISMATCH", case_ref=case_ref)
        checks.require(len(evidence_ids) == len(evidence), "EVIDENCE_ID_DUPLICATE", case_ref=case_ref)
        for row in evidence:
            evidence_id = str(row.get("evidence_id"))
            checks.require(row.get("redaction_status") == "PASS", "REDACTION_NOT_PASS", case_ref=case_ref, detail=evidence_id)
            derived_path = checks.file_hash(row.get("model_visible_path"), row.get("model_visible_sha256"), "MODEL_VISIBLE_IMAGE", case_ref=case_ref)
            audit_path = Path(str(row.get("redaction_audit_path") or ""))
            checks.require(audit_path.is_file(), "REDACTION_AUDIT_MISSING", case_ref=case_ref, detail=evidence_id)
            if not audit_path.is_file():
                continue
            audit = read_json(audit_path)
            checks.require(audit.get("status") == "PASS", "REDACTION_AUDIT_NOT_PASS", case_ref=case_ref, detail=evidence_id)
            checks.require(audit.get("post_redaction_sensitive_count") == 0, "POST_REDACTION_LEAK", case_ref=case_ref, detail=evidence_id)
            checks.require((audit.get("derived") or {}).get("sha256") == row.get("model_visible_sha256"), "REDACTION_DERIVED_HASH_MISMATCH", case_ref=case_ref, detail=evidence_id)
            checks.require((audit.get("parent") or {}).get("sha256") == row.get("parent_sha256"), "REDACTION_PARENT_HASH_MISMATCH", case_ref=case_ref, detail=evidence_id)
            if derived_path is not None:
                checks.require(Path((audit.get("derived") or {}).get("path", "")) == derived_path, "REDACTION_DERIVED_PATH_MISMATCH", case_ref=case_ref, detail=evidence_id)
            checks.file_hash((audit.get("parent") or {}).get("path"), (audit.get("parent") or {}).get("sha256"), "RAW_PARENT_IMAGE", case_ref=case_ref)

        calls = result.get("model_calls") or []
        checks.require(len(calls) == expected_actions + 2, "MODEL_CALL_COUNT_MISMATCH", case_ref=case_ref)
        checks.require(bool(calls) and calls[0].get("phase") == "task1", "TASK1_CALL_MISSING", case_ref=case_ref)
        checks.require(bool(calls) and calls[-1].get("phase") == "final_judge", "FINAL_JUDGE_CALL_MISSING", case_ref=case_ref)
        visible_to_judge: set[str] = set()
        for call in calls:
            checks.require(call.get("status") == "PASS", "MODEL_CALL_NOT_PASS", case_ref=case_ref)
            checks.require(call.get("requested_model") == model, "MODEL_CALL_REQUEST_CHANGED", case_ref=case_ref)
            checks.require(call.get("response_model") in pinned_resolved_models, "MODEL_CALL_RESOLUTION_CHANGED", case_ref=case_ref)
            policy_errors = validate_generation_policy(call, str(model or ""))
            checks.require(
                not policy_errors,
                "GENERATION_POLICY_INVALID",
                case_ref=case_ref,
                detail=",".join(policy_errors),
            )
            checks.require(call.get("multimodal_input") is True, "MODEL_CALL_NOT_MULTIMODAL", case_ref=case_ref)
            images = call.get("images") or []
            checks.require(call.get("image_count") == len(images) and len(images) > 0, "MODEL_CALL_IMAGE_COUNT_INVALID", case_ref=case_ref)
            for image in images:
                checks.file_hash(image.get("path"), image.get("sha256"), "MODEL_CALL_IMAGE", case_ref=case_ref)
            for attempt in call.get("attempts") or []:
                resolved = attempt.get("response_model")
                if resolved:
                    checks.require(resolved in pinned_resolved_models, "MODEL_ATTEMPT_RESOLUTION_CHANGED", case_ref=case_ref)
            if call.get("phase") == "final_judge":
                visible_to_judge = {str(row.get("evidence_id")) for row in images}
        checks.require(visible_to_judge == evidence_ids, "JUDGE_DID_NOT_RECEIVE_ALL_EVIDENCE", case_ref=case_ref)

        judgment_errors = validate_judgment(result.get("judgment"), evidence_ids, visible_to_judge)
        checks.require(not judgment_errors, "JUDGMENT_PROTOCOL_INVALID", case_ref=case_ref, detail=",".join(judgment_errors))
        checks.require(result.get("judgment_status") == "PROTOCOL_VALID_UNSCORED_NO_HUMAN_GOLD", "JUDGMENT_SCORE_BOUNDARY_INVALID", case_ref=case_ref)
        case_boundary = result.get("claim_boundary") or {}
        checks.require(case_boundary.get("full_corpus_model_evaluation") is False, "CASE_FULL_CORPUS_CLAIM", case_ref=case_ref)
        checks.require(case_boundary.get("accuracy_or_f1_allowed") is False, "CASE_METRIC_CLAIM", case_ref=case_ref)
        checks.require(case_boundary.get("human_gold_present") is False, "CASE_HUMAN_GOLD_CLAIM", case_ref=case_ref)

    return {
        "schema_version": "unified-mllm-smoke-validation/v0.1",
        "generated_at": utc_now(),
        "status": "PASS" if not checks.errors else "FAIL",
        "run_dir": str(run_dir),
        "validated_case_count": len(selected),
        "check_pass_count": checks.pass_count,
        "check_fail_count": len(checks.errors),
        "errors": checks.errors,
        "claim_boundary": {
            "scope": "ENGINEERING_SMOKE_ONLY",
            "full_600_model_evaluation": False,
            "accuracy_or_f1_reported": False,
            "human_gold_present": False,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--resolver", type=Path, default=DEFAULT_RESOLVER)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--route-probe", type=Path, default=DEFAULT_ROUTE_PROBE)
    parser.add_argument("--confusables", type=Path, default=DEFAULT_CONFUSABLES)
    parser.add_argument("--out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = validate_run(
        run_dir=args.run_dir,
        resolver_path=args.resolver,
        tasks_path=args.tasks,
        cases_path=args.cases,
        route_probe_path=args.route_probe,
        confusables_path=args.confusables,
    )
    output = args.out or args.run_dir / "validation_report.json"
    atomic_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
