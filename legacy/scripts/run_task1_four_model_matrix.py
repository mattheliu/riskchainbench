#!/usr/bin/env python3
"""Run the independent six-variant Task 1 benchmark on four libinfer-neo models."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RELEASE = ROOT / "outputs/riskchainbench_split_tasks_v0.1/task1"
DEFAULT_ROUTE_PROBE = (
    ROOT / "outputs/libinfer_neo_600_eval_20260725/preflight/all_routes.json"
)
DEFAULT_OUT = ROOT / "outputs/libinfer_neo_task1_text_v04_eval_20260725"
# Convenience default for the current paper experiment; never an allowlist.
DEFAULT_MODELS = (
    "gpt-5.4",
    "claude-opus-4-8-kiro",
    "kimi-k2.6",
    "gemini-3.5-flash",
)
EXPECTED_SOURCE_BENCHMARK_ID = "riskchainbench-balanced-600-v0.3"
EXPECTED_TASK1_CONTRACT_SHA256 = (
    "28a2fe6bac429d902153f3d9f2b575b89439115a91e9cd882843d5e54e056240"
)
TRACK_VARIANTS = {
    "primary": {0},
    "robustness": {1, 2, 3, 4, 5},
}
RUNNER = ROOT / "scripts/run_task1_text_batch.py"
VALIDATOR = ROOT / "scripts/validate_task1_text_batch.py"
SCORER = ROOT / "scripts/score_obfuscated_reconstruction.py"


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


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        "".join(canonical_json(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def embedded_hash(value: dict[str, Any], field: str) -> str:
    unhashed = copy.deepcopy(value)
    unhashed.pop(field, None)
    return sha256_text(canonical_json(unhashed))


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "model"


def passing_models(route_probe: dict[str, Any]) -> set[str]:
    return {
        str(row["model"])
        for row in route_probe.get("results") or []
        if row.get("status") == "PASS_MULTIMODAL_ROUTE"
    }


def parse_models(value: str) -> list[str]:
    models = [row.strip() for row in value.split(",") if row.strip()]
    if not models or len(models) != len(set(models)):
        raise ValueError("models must be a unique non-empty list")
    return models


def parse_tracks(value: str) -> list[str]:
    tracks = [row.strip() for row in value.split(",") if row.strip()]
    if not tracks or len(tracks) != len(set(tracks)):
        raise ValueError("tracks must be a unique non-empty list")
    if any(track not in TRACK_VARIANTS for track in tracks):
        raise ValueError("tracks must be primary and/or robustness")
    return tracks


def verify_release(release: Path, route_probe_path: Path, models: list[str]) -> dict[str, Any]:
    contract = read_json(release / "task1_contract.json")
    if contract.get("contract_sha256") != embedded_hash(contract, "contract_sha256"):
        raise ValueError("Task 1 contract embedded hash mismatch")
    if (
        contract.get("status") != "PASS_READY_FOR_INFERENCE"
        or contract.get("benchmark_id")
        != "riskchainbench-task1-obfuscated-reconstruction-v0.4"
        or contract.get("source_benchmark_id") != EXPECTED_SOURCE_BENCHMARK_ID
        or contract.get("contract_sha256") != EXPECTED_TASK1_CONTRACT_SHA256
        or contract.get("site_count") != 600
        or contract.get("variants_per_site") != 6
        or contract.get("task_count") != 3600
        or contract.get("primary_variant_index") != 0
    ):
        raise ValueError("Task 1 release contract is not the frozen v0.4 contract")
    for name, reference in (contract.get("files") or {}).items():
        path = release / str(reference["path"])
        if not path.is_file() or sha256_file(path) != reference["sha256"]:
            raise ValueError(f"Task 1 contract file mismatch: {name}")
    route_probe = read_json(route_probe_path)
    if (
        route_probe.get("status") != "PASS_FIXED_MLLM_SELECTED"
        or route_probe.get("transport") != "libinfer-neo"
        or route_probe.get("oneapi_used") is not False
    ):
        raise ValueError("libinfer-neo multimodal route probe is not valid")
    missing = sorted(set(models) - passing_models(route_probe))
    if missing:
        raise ValueError("models lack passing multimodal routes: " + ",".join(missing))
    return contract


def filter_gold(release: Path, out: Path, variants: set[int]) -> Path:
    source = release / "evaluator_only/generated_sessions.jsonl"
    selected = [
        row for row in read_jsonl(source) if int(row["variant_index"]) in variants
    ]
    expected = 600 * len(variants)
    if len(selected) != expected:
        raise ValueError(f"gold variant subset count mismatch: {len(selected)} != {expected}")
    write_jsonl(out, selected)
    return out


def run_logged(command: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            canonical_json(
                {"event": "command_start", "timestamp": utc_now(), "command": command}
            )
            + "\n"
        )
        handle.flush()
        result = subprocess.run(
            command,
            cwd=ROOT,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        )
        handle.write(
            canonical_json(
                {
                    "event": "command_end",
                    "timestamp": utc_now(),
                    "returncode": result.returncode,
                }
            )
            + "\n"
        )
        return result.returncode


class Ledger:
    def __init__(self, root: Path, models: list[str], tracks: list[str]) -> None:
        self.root = root
        self.models = models
        self.tracks = tracks
        self.lock = threading.Lock()

    def update(self, model: str, track: str, state: str, **extra: Any) -> None:
        with self.lock:
            path = self.root / "models" / safe_slug(model) / track / "matrix_status.json"
            prior = read_json(path) if path.is_file() else {}
            payload = {
                "schema_version": "riskchainbench-task1-track-status/v0.1",
                "model": model,
                "track": track,
                "state": state,
                "created_at": prior.get("created_at") or utc_now(),
                "updated_at": utc_now(),
                **extra,
            }
            write_json(path, payload)
            self.refresh()

    def refresh(self) -> None:
        rows = []
        for model in self.models:
            for track in self.tracks:
                path = (
                    self.root
                    / "models"
                    / safe_slug(model)
                    / track
                    / "matrix_status.json"
                )
                rows.append(
                    read_json(path)
                    if path.is_file()
                    else {
                        "model": model,
                        "track": track,
                        "state": "PENDING",
                    }
                )
        terminal = all(row["state"] in {"COMPLETE", "PARTIAL", "BLOCKED"} for row in rows)
        write_json(
            self.root / "matrix_summary.json",
            {
                "schema_version": "riskchainbench-task1-matrix-summary/v0.1",
                "status": (
                    "COMPLETE"
                    if terminal and all(row["state"] == "COMPLETE" for row in rows)
                    else "COMPLETE_WITH_PARTIAL"
                    if terminal
                    else "RUNNING"
                ),
                "updated_at": utc_now(),
                "rows": rows,
            },
        )


def run_track(
    *,
    model: str,
    track: str,
    release: Path,
    route_probe: Path,
    root: Path,
    gold_subset: Path,
    concurrency: int,
    retry_rounds: int,
    ledger: Ledger,
) -> dict[str, Any]:
    track_root = root / "models" / safe_slug(model) / track
    run_root = track_root / "run"
    log_path = track_root / "runner.log"
    variants = TRACK_VARIANTS[track]
    command = [
        "/usr/bin/python3.10",
        str(RUNNER),
        "--tasks",
        str(release / "model_visible/task1_inputs.jsonl"),
        "--task-manifest",
        str(release / "model_visible/task1_inputs_manifest.json"),
        "--prompt",
        str(release / "spec/model_prompt.md"),
        "--route-probe",
        str(route_probe),
        "--model",
        model,
        "--source-limit",
        "600",
        "--variant-indices",
        ",".join(str(value) for value in sorted(variants)),
        "--concurrency",
        str(concurrency),
        "--progress-every",
        "20",
        "--out",
        str(run_root),
    ]
    final_code = 1
    for round_index in range(retry_rounds + 1):
        ledger.update(
            model,
            track,
            "RUNNING",
            retry_round=round_index,
            run_root=str(run_root),
        )
        final_code = run_logged(command, log_path)
        summary_path = run_root / "summary.json"
        if summary_path.is_file() and read_json(summary_path).get("status") == "PASS":
            break

    summary = read_json(run_root / "summary.json")
    audit_dir = track_root / "audits"
    audit_dir.mkdir(parents=True, exist_ok=True)
    validation_code = None
    if summary.get("status") == "PASS":
        validation_code = run_logged(
            [
                "/usr/bin/python3.10",
                str(VALIDATOR),
                "--run-dir",
                str(run_root),
                "--out",
                str(audit_dir / "validation.json"),
            ],
            track_root / "validator.log",
        )
    score_command = [
        "/usr/bin/python3.10",
        str(SCORER),
        "--dataset",
        str(gold_subset),
        "--predictions",
        str(run_root / "predictions.jsonl"),
        "--output",
        str(audit_dir / "score.json"),
        "--allow-missing",
    ]
    score_code = run_logged(score_command, track_root / "scorer.log")
    state = (
        "COMPLETE"
        if summary.get("status") == "PASS"
        and validation_code == 0
        and score_code == 0
        else "PARTIAL"
    )
    ledger.update(
        model,
        track,
        state,
        runner_returncode=final_code,
        validation_returncode=validation_code,
        score_returncode=score_code,
        selected_task_count=summary.get("selected_task_count"),
        valid_prediction_count=summary.get("valid_prediction_count"),
        failed_prediction_count=summary.get("failed_prediction_count"),
    )
    return read_json(track_root / "matrix_status.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, default=DEFAULT_RELEASE)
    parser.add_argument("--route-probe", type=Path, default=DEFAULT_ROUTE_PROBE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--tracks", default="primary,robustness")
    parser.add_argument("--model-workers", type=int, default=4)
    parser.add_argument("--task-concurrency", type=int, default=3)
    parser.add_argument("--system-retry-rounds", type=int, default=2)
    args = parser.parse_args(argv)
    lock_handle = None
    try:
        models = parse_models(args.models)
        tracks = parse_tracks(args.tracks)
        if not 1 <= args.model_workers <= 8:
            raise ValueError("model-workers must be between 1 and 8")
        if not 1 <= args.task_concurrency <= 8:
            raise ValueError("task-concurrency must be between 1 and 8")
        contract = verify_release(args.release, args.route_probe, models)
        args.out.mkdir(parents=True, exist_ok=True)
        lock_handle = (args.out / ".matrix.lock").open("w", encoding="utf-8")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        write_json(
            args.out / "matrix_config.json",
            {
                "schema_version": "riskchainbench-task1-matrix-config/v0.1",
                "benchmark_id": contract["benchmark_id"],
                "task1_contract_sha256": contract["contract_sha256"],
                "models": models,
                "tracks": tracks,
                "model_workers": args.model_workers,
                "task_concurrency": args.task_concurrency,
                "system_retry_rounds": args.system_retry_rounds,
                "transport": "libinfer-neo",
                "oneapi_used": False,
                "created_at": utc_now(),
            },
        )
        gold_paths = {
            track: filter_gold(
                args.release,
                args.out / "inputs" / f"{track}_gold.jsonl",
                TRACK_VARIANTS[track],
            )
            for track in tracks
        }
        ledger = Ledger(args.out, models, tracks)
        ledger.refresh()
        results = []
        # Finish v000 for all models before starting robustness variants. This
        # lets the independently launched Task 2 matrix consume primary results
        # without waiting for the additional 3,000 Task 1 cases.
        for track in tracks:
            with ThreadPoolExecutor(max_workers=args.model_workers) as executor:
                futures = {
                    executor.submit(
                        run_track,
                        model=model,
                        track=track,
                        release=args.release,
                        route_probe=args.route_probe,
                        root=args.out,
                        gold_subset=gold_paths[track],
                        concurrency=args.task_concurrency,
                        retry_rounds=args.system_retry_rounds,
                        ledger=ledger,
                    ): model
                    for model in models
                }
                for future in as_completed(futures):
                    model = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:  # noqa: BLE001
                        ledger.update(
                            model,
                            track,
                            "BLOCKED",
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
        ledger.refresh()
        final = read_json(args.out / "matrix_summary.json")
        print(json.dumps(final, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if final["status"] == "COMPLETE" else 2
    except (BlockingIOError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"status": "FAIL", "error_type": type(exc).__name__, "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    finally:
        if lock_handle is not None:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())
