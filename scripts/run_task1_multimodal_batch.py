#!/usr/bin/env python3
"""Run resumable, artifact-complete Task 1 inference through libinfer-neo."""

from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable

from jsonschema import Draft202012Validator


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import generate_obfuscated_session_dataset as safety  # noqa: E402
import score_obfuscated_reconstruction as scorer  # noqa: E402
from run_unified_mllm_smoke import (  # noqa: E402
    DEFAULT_CONFUSABLES,
    DEFAULT_ENV,
    DEFAULT_FONT,
    ModelCallError,
    atomic_json,
    build_confusable_legend,
    call_model_json,
    canonical_json,
    load_export_env,
    pinned_response_models,
    render_task_image,
    sha256_file,
    sha256_text,
    task_message_text,
    thinking_policy_for_model,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = Path(__file__).resolve()
SHARED_CLIENT_PATH = SCRIPT_DIR / "run_unified_mllm_smoke.py"
SAFETY_SCANNER_PATH = SCRIPT_DIR / "generate_obfuscated_session_dataset.py"
SCORER_PATH = SCRIPT_DIR / "score_obfuscated_reconstruction.py"
DEFAULT_SCALE_ROOT = (
    PROJECT_ROOT / "outputs/riskchainbench_balanced600_v0.3/scale_600"
)
DEFAULT_TASKS = DEFAULT_SCALE_ROOT / "public/task1_inputs.jsonl"
DEFAULT_TASK_MANIFEST = DEFAULT_SCALE_ROOT / "public/task1_inputs_manifest.json"
DEFAULT_TASK_SCHEMA = PROJECT_ROOT / "schemas/obfuscated_reconstruction_task_v0.1.schema.json"
DEFAULT_PREDICTION_SCHEMA = (
    PROJECT_ROOT / "schemas/obfuscated_reconstruction_prediction_v0.1.schema.json"
)
DEFAULT_PROMPT = PROJECT_ROOT / "configs/task1_multimodal_batch_prompt_v0.2.md"
DEFAULT_PLATFORM_EVIDENCE = PROJECT_ROOT / "configs/platform_token_evidence_v0.1.json"
FORBIDDEN_KEYS = {
    "entry",
    "gold",
    "gold_reconstruction",
    "label",
    "local_mirror_ref",
    "message_transformations",
    "obfuscation_types",
    "operations",
    "references",
    "resolver",
    "risk_type",
    "source_provenance",
    "source_session",
    "text_generation",
}
SEMANTIC_KEYS = {
    "reconstructed_messages",
    "intent_label",
    "platform",
    "entry_candidates",
    "abstain",
    "uncertainties",
}
INTENT_MAP = {
    "RECONSTRUCT_RESERVED_ENTRY_AND_INSPECT_CONTROLLED_LOCAL_PAGE": (
        "reconstruct the reserved entry and inspect the controlled local page"
    ),
    "OPEN_SITE": "open site",
    "ADD_ACCOUNT": "add account",
    "SEARCH_QUERY": "search query",
    "ENTER_CODE": "enter code",
    "CONTACT_SERVICE": "contact service",
    "UNKNOWN": "unknown",
}
PLATFORM_SYNTAX_HINTS = {
    "bilibili": "Bracketed platform shortcodes can be decorative tokens.",
    "douyin": "Bracketed platform shortcodes can be decorative tokens.",
    "wechat": "Slash-name or bracketed platform shortcodes can be decorative tokens.",
    "wechat_channels": "Slash-name or bracketed platform shortcodes can be decorative tokens.",
    "qq": "Slash-name or bracketed platform shortcodes can be decorative tokens.",
    "weibo": "Bracketed platform shortcodes can be decorative tokens.",
    "baidu_tieba": "Hash-parenthesis or bracketed shortcodes can be decorative tokens.",
    "tiktok": "Rendered emoji or bracketed shortcode text can be decorative tokens.",
}
SAMPLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"expected JSON object at {path}:{line_number}")
        rows.append(value)
    if not rows:
        raise ValueError(f"JSONL is empty: {path}")
    return rows


def passing_multimodal_models(route_probe: dict[str, Any]) -> set[str]:
    return {
        str(row["model"])
        for row in route_probe.get("results") or []
        if isinstance(row, dict)
        and isinstance(row.get("model"), str)
        and row.get("status") == "PASS_MULTIMODAL_ROUTE"
    }


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    atomic_text(path, "".join(canonical_json(row) + "\n" for row in rows))


def find_forbidden_keys(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in FORBIDDEN_KEYS:
                findings.append(child_path)
            findings.extend(find_forbidden_keys(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(find_forbidden_keys(child, f"{path}[{index}]"))
    return findings


def render_schema_errors(
    value: dict[str, Any], schema: dict[str, Any], label: str
) -> list[str]:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda row: list(row.absolute_path),
    )
    rendered = []
    for error in errors[:20]:
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        rendered.append(f"{label}:{location}:{error.message}")
    return rendered


def validate_task(
    task: dict[str, Any],
    schema: dict[str, Any],
    expected_manifest_row: dict[str, Any],
) -> None:
    errors = render_schema_errors(task, schema, "TASK")
    if errors:
        raise ValueError(";".join(errors))
    forbidden = find_forbidden_keys(task)
    if forbidden:
        raise ValueError("model-visible task contains forbidden keys: " + ",".join(forbidden))
    unhashed = copy.deepcopy(task)
    expected_hash = unhashed.pop("task_sha256")
    if sha256_text(canonical_json(unhashed)) != expected_hash:
        raise ValueError(f"task_sha256 mismatch: {task.get('sample_id')}")
    if expected_manifest_row.get("sample_id") != task.get("sample_id"):
        raise ValueError("task manifest sample_id mismatch")
    if expected_manifest_row.get("task_sha256") != expected_hash:
        raise ValueError("task manifest task_sha256 mismatch")


def source_id(sample_id: str) -> str:
    return sample_id.rsplit("--v", 1)[0]


def sample_variant_index(sample_id: str) -> int:
    match = re.search(r"--v([0-9]{3})$", sample_id)
    if match is None:
        raise ValueError(f"sample_id lacks a variant suffix: {sample_id}")
    return int(match.group(1))


def select_tasks(
    tasks: list[dict[str, Any]],
    source_offset: int,
    source_limit: int,
    variant_indices: set[int] | None = None,
) -> list[dict[str, Any]]:
    if source_offset < 0 or source_limit < 0:
        raise ValueError("source offset/limit cannot be negative")
    ordered_sources: list[str] = []
    seen: set[str] = set()
    for task in tasks:
        current = source_id(str(task["sample_id"]))
        if current not in seen:
            seen.add(current)
            ordered_sources.append(current)
    selected_sources = ordered_sources[source_offset:]
    if source_limit:
        selected_sources = selected_sources[:source_limit]
    allowed = set(selected_sources)
    selected = [
        task
        for task in tasks
        if source_id(str(task["sample_id"])) in allowed
        and (
            variant_indices is None
            or sample_variant_index(str(task["sample_id"])) in variant_indices
        )
    ]
    if not selected:
        raise ValueError("task selection is empty")
    return selected


def validate_semantic_response(value: dict[str, Any], task: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict) or set(value) != SEMANTIC_KEYS:
        return ["SEMANTIC_KEYS_INVALID"]
    candidates = value.get("entry_candidates")
    safe_candidate_literals: list[str] = []
    if isinstance(candidates, list):
        safe_candidate_literals = [
            str(row.get("value"))
            for row in candidates
            if isinstance(row, dict)
            and isinstance(row.get("value"), str)
            and scorer.is_reserved_candidate(row["value"])
        ]
    messages = value.get("reconstructed_messages")
    expected_ids = [str(row["message_id"]) for row in task["messages"]]
    if not isinstance(messages, list):
        errors.append("RECONSTRUCTED_MESSAGES_INVALID")
    else:
        observed_ids: list[str] = []
        for row in messages:
            if not isinstance(row, dict) or set(row) != {"message_id", "text"}:
                errors.append("RECONSTRUCTED_MESSAGE_SCHEMA_INVALID")
                continue
            observed_ids.append(str(row.get("message_id") or ""))
            if not isinstance(row.get("text"), str):
                errors.append("RECONSTRUCTED_MESSAGE_TEXT_INVALID")
            else:
                safety_view = row["text"]
                for candidate in safe_candidate_literals:
                    safety_view = safety_view.replace(candidate, "[MASKED_SITE]")
                if safety.scan_safety_findings(safety_view):
                    errors.append("RECONSTRUCTED_MESSAGE_SAFETY_INVALID")
        if observed_ids != expected_ids:
            errors.append("RECONSTRUCTED_MESSAGE_IDS_INVALID")
    if value.get("intent_label") not in INTENT_MAP:
        errors.append("INTENT_LABEL_INVALID")
    if value.get("platform") != task.get("platform"):
        errors.append("PLATFORM_INVALID")
    if not isinstance(candidates, list) or len(candidates) > 5:
        errors.append("ENTRY_CANDIDATES_INVALID")
    else:
        ranks: list[int] = []
        confidences: list[float] = []
        for row in candidates:
            if not isinstance(row, dict) or set(row) != {"rank", "value", "confidence"}:
                errors.append("ENTRY_CANDIDATE_SCHEMA_INVALID")
                continue
            rank = row.get("rank")
            confidence = row.get("confidence")
            candidate = row.get("value")
            if not isinstance(rank, int):
                errors.append("ENTRY_CANDIDATE_RANK_INVALID")
            else:
                ranks.append(rank)
            if (
                not isinstance(confidence, (int, float))
                or isinstance(confidence, bool)
                or not 0 <= float(confidence) <= 1
            ):
                errors.append("ENTRY_CANDIDATE_CONFIDENCE_INVALID")
            else:
                confidences.append(float(confidence))
            if not isinstance(candidate, str) or not scorer.is_reserved_candidate(candidate):
                errors.append("ENTRY_CANDIDATE_SAFETY_INVALID")
        if ranks != list(range(1, len(candidates) + 1)):
            errors.append("ENTRY_CANDIDATE_RANKS_NOT_CONTIGUOUS")
        if confidences != sorted(confidences, reverse=True):
            errors.append("ENTRY_CANDIDATE_CONFIDENCE_NOT_MONOTONIC")
    if not isinstance(value.get("abstain"), bool):
        errors.append("ABSTAIN_INVALID")
    elif not value["abstain"] and not candidates:
        errors.append("NON_ABSTAIN_REQUIRES_CANDIDATE")
    uncertainties = value.get("uncertainties")
    if not isinstance(uncertainties, list) or any(
        not isinstance(row, str) or not row.strip() for row in uncertainties
    ):
        errors.append("UNCERTAINTIES_INVALID")
    return sorted(set(errors))


def build_prediction(
    semantic: dict[str, Any],
    task: dict[str, Any],
    *,
    model: str,
    run_id: str,
) -> dict[str, Any]:
    prediction = {
        "schema_version": "obfuscated-reconstruction-prediction/v0.1",
        "sample_id": task["sample_id"],
        "model_id": model,
        "run_id": run_id,
        "input_view": "MULTIMODAL",
        "reconstructed_messages": [
            {
                "message_id": row["message_id"],
                "text": row["text"],
            }
            for row in semantic["reconstructed_messages"]
        ],
        "intent": INTENT_MAP[semantic["intent_label"]],
        "platform": semantic["platform"],
        "entry_candidates": [
            {
                "rank": int(row["rank"]),
                "value": row["value"].strip(),
                "confidence": round(float(row["confidence"]), 6),
            }
            for row in semantic["entry_candidates"]
        ],
        "abstain": semantic["abstain"],
        "uncertainties": [row.strip() for row in semantic["uncertainties"]],
    }
    return prediction


def validate_prediction(
    prediction: dict[str, Any], schema: dict[str, Any], task: dict[str, Any]
) -> None:
    errors = render_schema_errors(prediction, schema, "PREDICTION")
    if errors:
        raise ValueError(";".join(errors))
    scorer.validate_prediction(prediction, schema)
    if prediction["sample_id"] != task["sample_id"]:
        raise ValueError("prediction sample_id mismatch")
    if prediction["platform"] != task["platform"]:
        raise ValueError("prediction platform mismatch")
    if any(
        not scorer.is_reserved_candidate(row["value"])
        for row in prediction["entry_candidates"]
    ):
        raise ValueError("prediction contains a live entry")


def frozen_config(
    *,
    tasks_path: Path,
    manifest_path: Path,
    task_schema_path: Path,
    prediction_schema_path: Path,
    prompt_path: Path,
    route_probe_path: Path,
    font_path: Path,
    confusables_path: Path,
    platform_evidence_path: Path,
    model: str,
    run_id: str,
    setting: str,
    source_offset: int,
    source_limit: int,
    variant_indices: set[int] | None,
    selected: list[dict[str, Any]],
    max_tokens: int,
    max_attempts: int,
    timeout_seconds: float,
    resolved_model_allowlist: set[str],
) -> dict[str, Any]:
    immutable = {
        "schema_version": "task1-multimodal-batch-config/v0.1",
        "transport": "libinfer-neo",
        "oneapi_used": False,
        "requested_model": model,
        "resolved_model_allowlist": sorted(resolved_model_allowlist),
        "run_id": run_id,
        "evaluation_setting": setting,
        "input_view": "MULTIMODAL",
        "temperature_policy": (
            "provider_default_omitted" if model.startswith("gpt-5.") else "explicit_zero"
        ),
        "reasoning_effort_policy": (
            "provider_default_omitted"
            if model.startswith("gpt-5.")
            else "explicit_minimal"
        ),
        "thinking_policy": thinking_policy_for_model(model),
        "max_tokens": max_tokens,
        "max_attempts_per_call": max_attempts,
        "timeout_seconds": timeout_seconds,
        "source_offset": source_offset,
        "source_limit": source_limit,
        "variant_indices": (
            sorted(variant_indices) if variant_indices is not None else "ALL"
        ),
        "selected_source_count": len({source_id(row["sample_id"]) for row in selected}),
        "selected_task_count": len(selected),
        "selected_task_order_sha256": sha256_text(
            canonical_json([row["task_sha256"] for row in selected])
        ),
        "files": {
            "runner": {"path": str(RUNNER_PATH), "sha256": sha256_file(RUNNER_PATH)},
            "shared_model_client": {
                "path": str(SHARED_CLIENT_PATH),
                "sha256": sha256_file(SHARED_CLIENT_PATH),
            },
            "safety_scanner": {
                "path": str(SAFETY_SCANNER_PATH),
                "sha256": sha256_file(SAFETY_SCANNER_PATH),
            },
            "offline_scorer": {
                "path": str(SCORER_PATH),
                "sha256": sha256_file(SCORER_PATH),
            },
            "tasks": {"path": str(tasks_path), "sha256": sha256_file(tasks_path)},
            "task_manifest": {
                "path": str(manifest_path),
                "sha256": sha256_file(manifest_path),
            },
            "task_schema": {
                "path": str(task_schema_path),
                "sha256": sha256_file(task_schema_path),
            },
            "prediction_schema": {
                "path": str(prediction_schema_path),
                "sha256": sha256_file(prediction_schema_path),
            },
            "inference_prompt": {
                "path": str(prompt_path),
                "sha256": sha256_file(prompt_path),
            },
            "route_probe": {
                "path": str(route_probe_path),
                "sha256": sha256_file(route_probe_path),
            },
            "font": {"path": str(font_path), "sha256": sha256_file(font_path)},
            "confusables": {
                "path": str(confusables_path),
                "sha256": sha256_file(confusables_path),
            },
            "platform_token_evidence": {
                "path": str(platform_evidence_path),
                "sha256": sha256_file(platform_evidence_path),
            },
        },
    }
    immutable["config_fingerprint"] = sha256_text(canonical_json(immutable))
    return immutable


def prepare_run_config(run_root: Path, config: dict[str, Any]) -> None:
    path = run_root / "config.json"
    if path.exists():
        existing = read_json(path)
        if existing.get("config_fingerprint") != config["config_fingerprint"]:
            raise ValueError("run directory contains a different frozen configuration")
        return
    payload = dict(config)
    payload["created_at"] = utc_now()
    atomic_json(path, payload)


def next_attempt_dir(task_root: Path) -> Path:
    attempts_root = task_root / "attempts"
    attempts_root.mkdir(parents=True, exist_ok=True)
    indexes = [
        int(path.name)
        for path in attempts_root.iterdir()
        if path.is_dir() and path.name.isdigit()
    ]
    path = attempts_root / f"{(max(indexes, default=0) + 1):03d}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def completed_prediction(
    task_root: Path,
    *,
    config_fingerprint: str,
    prediction_schema: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any] | None:
    state_path = task_root / "state.json"
    prediction_path = task_root / "prediction.json"
    if not state_path.is_file() or not prediction_path.is_file():
        return None
    state = read_json(state_path)
    if state.get("status") != "PASS":
        return None
    if state.get("config_fingerprint") != config_fingerprint:
        raise ValueError(f"completed task has a different config: {task['sample_id']}")
    prediction = read_json(prediction_path)
    validate_prediction(prediction, prediction_schema, task)
    if state.get("prediction_sha256") != sha256_file(prediction_path):
        raise ValueError(f"completed prediction hash mismatch: {task['sample_id']}")
    return prediction


def build_user_payload(
    task: dict[str, Any],
    *,
    setting: str,
    confusables_path: Path,
    platform_evidence_sha256: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "phase": "TASK1_RECONSTRUCTION",
        "evaluation_setting": setting,
        "input_view": "MULTIMODAL",
        "task": task,
        "output_contract": {
            "keys": sorted(SEMANTIC_KEYS),
            "intent_labels": list(INTENT_MAP),
            "entry_candidate_limit": 5,
            "entry_policy": "RESERVED_OR_MASKED_ONLY",
        },
    }
    if setting == "TAXONOMY_AWARE":
        payload["input_scoped_confusable_legend"] = build_confusable_legend(
            task_message_text(task), confusables_path
        )
        payload["platform_syntax_hint"] = {
            "hint": PLATFORM_SYNTAX_HINTS.get(
                str(task["platform"]),
                "Rendered emoji or shortcode text can be decorative tokens.",
            ),
            "evidence_registry_sha256": platform_evidence_sha256,
            "status": "SYNTAX_HINT_ONLY_NOT_GOLD",
        }
    return payload


def run_one(
    task: dict[str, Any],
    *,
    run_root: Path,
    config: dict[str, Any],
    prediction_schema: dict[str, Any],
    prompt: str,
    base_url: str,
    api_key: str,
    model: str,
    run_id: str,
    setting: str,
    font_path: Path,
    confusables_path: Path,
    platform_evidence_sha256: str,
    max_tokens: int,
    max_attempts: int,
    timeout_seconds: float,
    resolved_model_allowlist: set[str],
) -> dict[str, Any]:
    sample_id = str(task["sample_id"])
    if not SAMPLE_ID_RE.fullmatch(sample_id):
        raise ValueError(f"unsafe sample_id: {sample_id}")
    task_root = run_root / "tasks" / sample_id
    task_root.mkdir(parents=True, exist_ok=True)
    existing = completed_prediction(
        task_root,
        config_fingerprint=config["config_fingerprint"],
        prediction_schema=prediction_schema,
        task=task,
    )
    if existing is not None:
        return {
            "sample_id": sample_id,
            "status": "PASS",
            "resumed": True,
            "prediction": existing,
        }

    attempt_root = next_attempt_dir(task_root)
    image_path = attempt_root / "task1_message.png"
    image_record = render_task_image(task, image_path, font_path)
    user_payload = build_user_payload(
        task,
        setting=setting,
        confusables_path=confusables_path,
        platform_evidence_sha256=platform_evidence_sha256,
    )
    atomic_json(
        attempt_root / "input_audit.json",
        {
            "schema_version": "task1-model-input-audit/v0.1",
            "sample_id": sample_id,
            "task_sha256": task["task_sha256"],
            "user_payload_sha256": sha256_text(canonical_json(user_payload)),
            "prompt_sha256": sha256_text(prompt),
            "image": image_record,
            "forbidden_key_findings": find_forbidden_keys(user_payload),
            "gold_present": False,
            "resolver_present": False,
            "web_evidence_present": False,
            "status": "PASS",
        },
    )
    try:
        semantic, audit = call_model_json(
            base_url=base_url,
            api_key=api_key,
            model=model,
            system_prompt=prompt,
            user_payload=user_payload,
            images=[("TASK1_MESSAGE", image_path)],
            phase="task1",
            case_ref=sample_id,
            max_tokens=max_tokens,
            call_index=1,
            validator=lambda value: validate_semantic_response(value, task),
            audit_path=attempt_root / "model_call.json",
            request_protocol="riskchainbench_task1_multimodal_batch_v0.1",
            notes_task_prefix="task1-multimodal-batch",
            notes_extra="frozen-task1-multimodal-evaluation-v0.1",
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
        )
        if audit.get("response_model") not in resolved_model_allowlist:
            raise ValueError("resolved model differs from the frozen requested model")
        prediction = build_prediction(semantic, task, model=model, run_id=run_id)
        validate_prediction(prediction, prediction_schema, task)
        attempt_prediction = attempt_root / "prediction.json"
        atomic_json(attempt_prediction, prediction)
        canonical_prediction = task_root / "prediction.json"
        if canonical_prediction.exists():
            raise ValueError("refusing to overwrite an existing canonical prediction")
        atomic_json(canonical_prediction, prediction)
        state = {
            "schema_version": "task1-batch-task-state/v0.1",
            "sample_id": sample_id,
            "status": "PASS",
            "finished_at": utc_now(),
            "config_fingerprint": config["config_fingerprint"],
            "attempt_path": str(attempt_root),
            "prediction_path": str(canonical_prediction),
            "prediction_sha256": sha256_file(canonical_prediction),
            "model_call_sha256": sha256_file(attempt_root / "model_call.json"),
            "requested_model": model,
            "resolved_model": audit.get("response_model"),
        }
        atomic_json(task_root / "state.json", state)
        return {
            "sample_id": sample_id,
            "status": "PASS",
            "resumed": False,
            "prediction": prediction,
        }
    except Exception as exc:  # noqa: BLE001 - every failed attempt is persisted.
        audit = exc.audit if isinstance(exc, ModelCallError) else None
        failure = {
            "schema_version": "task1-batch-attempt-failure/v0.1",
            "sample_id": sample_id,
            "status": "FAIL",
            "finished_at": utc_now(),
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "config_fingerprint": config["config_fingerprint"],
            "attempt_path": str(attempt_root),
            "model_call_status": audit.get("status") if isinstance(audit, dict) else None,
        }
        atomic_json(attempt_root / "failure.json", failure)
        atomic_json(task_root / "state.json", failure)
        return {
            "sample_id": sample_id,
            "status": "FAIL",
            "resumed": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
        }


def summarize_usage(run_root: Path, selected: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, int] = {}
    call_count = 0
    attempt_count = 0
    resolved_models: set[str] = set()
    for task in selected:
        task_root = run_root / "tasks" / task["sample_id"]
        state_path = task_root / "state.json"
        if not state_path.is_file():
            continue
        state = read_json(state_path)
        if state.get("status") != "PASS":
            continue
        resolved = state.get("resolved_model")
        if resolved:
            resolved_models.add(str(resolved))
        call_path = Path(str(state["attempt_path"])) / "model_call.json"
        if not call_path.is_file():
            continue
        call = read_json(call_path)
        call_count += 1
        for attempt in call.get("attempts") or []:
            attempt_count += 1
            usage = attempt.get("usage")
            if not isinstance(usage, dict):
                continue
            for key, value in usage.items():
                if isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
    return {
        "successful_model_call_count": call_count,
        "provider_attempt_count": attempt_count,
        "resolved_models": sorted(resolved_models),
        "token_usage": dict(sorted(totals.items())),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--task-manifest", type=Path, default=DEFAULT_TASK_MANIFEST)
    parser.add_argument("--task-schema", type=Path, default=DEFAULT_TASK_SCHEMA)
    parser.add_argument("--prediction-schema", type=Path, default=DEFAULT_PREDICTION_SCHEMA)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--route-probe", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT)
    parser.add_argument("--confusables", type=Path, default=DEFAULT_CONFUSABLES)
    parser.add_argument("--platform-evidence", type=Path, default=DEFAULT_PLATFORM_EVIDENCE)
    parser.add_argument("--model", required=True)
    parser.add_argument("--run-id")
    parser.add_argument(
        "--setting", choices=("BLIND", "TAXONOMY_AWARE"), default="TAXONOMY_AWARE"
    )
    parser.add_argument("--source-offset", type=int, default=0)
    parser.add_argument("--source-limit", type=int, default=0)
    parser.add_argument(
        "--variant-indices",
        help="Comma-separated variant indices, for example 0 or 1,2,3,4,5.",
    )
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=240)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not 1 <= args.concurrency <= 32:
            raise ValueError("concurrency must be between 1 and 32")
        if not 256 <= args.max_tokens <= 4096:
            raise ValueError("max_tokens must be between 256 and 4096")
        task_schema = read_json(args.task_schema)
        prediction_schema = read_json(args.prediction_schema)
        Draft202012Validator.check_schema(task_schema)
        Draft202012Validator.check_schema(prediction_schema)
        manifest = read_json(args.task_manifest)
        tasks = read_jsonl(args.tasks)
        if manifest.get("task_count") != len(tasks):
            raise ValueError("task manifest count mismatch")
        if manifest.get("output_sha256") != sha256_file(args.tasks):
            raise ValueError("task manifest output_sha256 mismatch")
        private_rows = manifest.get("tasks") or []
        if len(private_rows) != len(tasks):
            raise ValueError("task manifest row count mismatch")
        for task, manifest_row in zip(tasks, private_rows):
            validate_task(task, task_schema, manifest_row)
        variant_indices = None
        if args.variant_indices:
            try:
                variant_indices = {
                    int(value.strip())
                    for value in args.variant_indices.split(",")
                    if value.strip()
                }
            except ValueError as exc:
                raise ValueError("variant indices must be integers") from exc
            if not variant_indices or min(variant_indices) < 0:
                raise ValueError("variant indices must be non-negative")
        selected = select_tasks(
            tasks,
            args.source_offset,
            args.source_limit,
            variant_indices,
        )

        route_probe = read_json(args.route_probe)
        if route_probe.get("status") != "PASS_FIXED_MLLM_SELECTED":
            raise ValueError("route probe did not pass")
        if route_probe.get("transport") != "libinfer-neo" or route_probe.get("oneapi_used"):
            raise ValueError("route probe transport policy mismatch")
        if args.model not in passing_multimodal_models(route_probe):
            raise ValueError("requested model lacks a passing multimodal probe")
        resolved_model_allowlist = pinned_response_models(route_probe, args.model)

        environment = load_export_env(args.env_file)
        base_url = environment.get("LIBINFER_NEO_URL") or os.environ.get("LIBINFER_NEO_URL")
        api_key = environment.get("LIBINFER_SK") or os.environ.get("LIBINFER_SK")
        if not base_url or not api_key:
            raise ValueError("missing LIBINFER_NEO_URL or LIBINFER_SK")
        prompt = args.prompt.read_text(encoding="utf-8")
        run_id = args.run_id or (
            "task1-gpt56-" + sha256_text(
                f"{args.model}|{sha256_file(args.prompt)}|{args.setting}"
            )[:12]
        )
        if not SAMPLE_ID_RE.fullmatch(run_id):
            raise ValueError("run_id is not schema compatible")
        config = frozen_config(
            tasks_path=args.tasks,
            manifest_path=args.task_manifest,
            task_schema_path=args.task_schema,
            prediction_schema_path=args.prediction_schema,
            prompt_path=args.prompt,
            route_probe_path=args.route_probe,
            font_path=args.font,
            confusables_path=args.confusables,
            platform_evidence_path=args.platform_evidence,
            model=args.model,
            run_id=run_id,
            setting=args.setting,
            source_offset=args.source_offset,
            source_limit=args.source_limit,
            variant_indices=variant_indices,
            selected=selected,
            max_tokens=args.max_tokens,
            max_attempts=args.max_attempts,
            timeout_seconds=args.timeout_seconds,
            resolved_model_allowlist=resolved_model_allowlist,
        )
        args.out.mkdir(parents=True, exist_ok=True)
        prepare_run_config(args.out, config)
        platform_evidence_sha256 = sha256_file(args.platform_evidence)

        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = {
                executor.submit(
                    run_one,
                    task,
                    run_root=args.out,
                    config=config,
                    prediction_schema=prediction_schema,
                    prompt=prompt,
                    base_url=base_url,
                    api_key=api_key,
                    model=args.model,
                    run_id=run_id,
                    setting=args.setting,
                    font_path=args.font,
                    confusables_path=args.confusables,
                    platform_evidence_sha256=platform_evidence_sha256,
                    max_tokens=args.max_tokens,
                    max_attempts=args.max_attempts,
                    timeout_seconds=args.timeout_seconds,
                    resolved_model_allowlist=resolved_model_allowlist,
                ): task["sample_id"]
                for task in selected
            }
            for completed, future in enumerate(as_completed(futures), 1):
                result = future.result()
                results.append(result)
                if (
                    completed == 1
                    or completed == len(futures)
                    or (args.progress_every > 0 and completed % args.progress_every == 0)
                ):
                    print(
                        json.dumps(
                            {
                                "event": "progress",
                                "completed": completed,
                                "total": len(futures),
                                "pass": sum(row["status"] == "PASS" for row in results),
                                "fail": sum(row["status"] == "FAIL" for row in results),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

        result_by_id = {row["sample_id"]: row for row in results}
        predictions = [
            result_by_id[task["sample_id"]]["prediction"]
            for task in selected
            if result_by_id[task["sample_id"]]["status"] == "PASS"
        ]
        failures = [
            {
                "sample_id": row["sample_id"],
                "error_type": row.get("error_type"),
                "error": row.get("error"),
            }
            for row in results
            if row["status"] == "FAIL"
        ]
        write_jsonl(args.out / "predictions.jsonl", predictions)
        write_jsonl(args.out / "failures.jsonl", failures)
        usage = summarize_usage(args.out, selected)
        pass_count = len(predictions)
        summary = {
            "schema_version": "task1-multimodal-batch-summary/v0.1",
            "generated_at": utc_now(),
            "status": "PASS" if pass_count == len(selected) else "PARTIAL",
            "config_fingerprint": config["config_fingerprint"],
            "transport": "libinfer-neo",
            "oneapi_used": False,
            "requested_model": args.model,
            "run_id": run_id,
            "evaluation_setting": args.setting,
            "selected_source_count": config["selected_source_count"],
            "selected_task_count": len(selected),
            "valid_prediction_count": pass_count,
            "failed_prediction_count": len(failures),
            "resumed_prediction_count": sum(bool(row.get("resumed")) for row in results),
            "predictions_path": str(args.out / "predictions.jsonl"),
            "predictions_sha256": sha256_file(args.out / "predictions.jsonl"),
            "failures_path": str(args.out / "failures.jsonl"),
            "failures_sha256": sha256_file(args.out / "failures.jsonl"),
            "usage": usage,
            "claim_boundary": {
                "task2_run": False,
                "web_access_used_for_candidate_ranking": False,
                "accuracy_reported": False,
                "requires_offline_scorer": True,
            },
        }
        summary["summary_sha256"] = sha256_text(canonical_json(summary))
        atomic_json(args.out / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if summary["status"] == "PASS" else 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"status": "FAIL", "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
