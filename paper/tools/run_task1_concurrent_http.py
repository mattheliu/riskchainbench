#!/usr/bin/env python3
"""Concurrent, resumable wrapper around the plain Task 1 HTTP harness.

Each worker still executes one ordinary chat-completions request at a time
through ``run_task1_http_harness.run_task``. The wrapper only schedules
independent samples concurrently and atomically checkpoints after every result.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any


LOCAL_HARNESS_PATH = Path(__file__).resolve().with_name(
    "run_task1_http_harness.py"
)
# Packaging-only change (2026-09-21): require the bundled harness rather than
# falling back to a developer's temporary checkout. Model-call logic is unchanged.
HARNESS_PATH = LOCAL_HARNESS_PATH
if not HARNESS_PATH.is_file():
    raise FileNotFoundError("bundled run_task1_http_harness.py is missing")
SPEC = importlib.util.spec_from_file_location("task1_http_harness", HARNESS_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load harness: {HARNESS_PATH}")
H = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(H)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw-output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-delays", default="2,5")
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=1,
        help="Atomically persist after this many newly completed tasks.",
    )
    args = parser.parse_args()

    if not 1 <= args.concurrency <= 32:
        raise ValueError("concurrency must be between 1 and 32")
    if not 1 <= args.max_attempts <= 5:
        raise ValueError("max-attempts must be between 1 and 5")
    if not 1 <= args.checkpoint_every <= 100:
        raise ValueError("checkpoint-every must be between 1 and 100")
    if not H.SAMPLE_ID_RE.fullmatch(args.run_id):
        raise ValueError("run-id is not schema compatible")
    base_url = os.environ.get("LIBINFER_NEO_URL")
    api_key = os.environ.get("LIBINFER_SK")
    if not base_url or not api_key:
        raise ValueError("missing LIBINFER_NEO_URL or LIBINFER_SK")

    tasks = H.read_jsonl(args.tasks)
    task_order = [str(row["sample_id"]) for row in tasks]
    if len(task_order) != len(set(task_order)):
        raise ValueError("duplicate task sample_id")
    task_by_id = {str(row["sample_id"]): row for row in tasks}
    system_prompt = args.prompt.read_text(encoding="utf-8")
    retry_delays = H.parse_retry_delays(args.retry_delays)
    system_prompt_sha256 = sha256_text(system_prompt)
    tasks_sha256 = sha256_file(args.tasks)
    request_options = H.request_options(args.model, args.max_tokens)

    predictions_by_id: dict[str, dict[str, Any]] = {}
    raw_by_id: dict[str, dict[str, Any]] = {}
    if args.output.exists() != args.raw_output.exists():
        raise ValueError("resume requires both output and raw-output")
    if args.output.exists():
        predictions = H.read_jsonl(args.output)
        raw_document = read_json(args.raw_output)
        if raw_document.get("model") != args.model:
            raise ValueError("resume model differs from existing raw audit")
        if raw_document.get("run_id") != args.run_id:
            raise ValueError("resume run-id differs from existing raw audit")
        if raw_document.get("system_prompt_sha256") != system_prompt_sha256:
            raise ValueError("resume prompt differs from existing raw audit")
        recorded_tasks_sha256 = raw_document.get("tasks_sha256")
        if (
            recorded_tasks_sha256 is not None
            and recorded_tasks_sha256 != tasks_sha256
        ):
            raise ValueError("resume tasks differ from existing raw audit")
        if raw_document.get("request_options") != request_options:
            raise ValueError("resume request options differ from existing raw audit")
        predictions_by_id = {
            str(row["sample_id"]): row for row in predictions
        }
        raw_by_id = {
            str(row["sample_id"]): row
            for row in raw_document.get("tasks", [])
        }
        if set(predictions_by_id) != set(raw_by_id):
            raise ValueError("resume prediction/raw sample IDs differ")
        unknown = set(predictions_by_id) - set(task_by_id)
        if unknown:
            raise ValueError(f"resume contains unknown sample IDs: {sorted(unknown)[:3]}")
        if recorded_tasks_sha256 is None:
            for sample_id, raw_row in raw_by_id.items():
                expected_request_sha256 = sha256_text(
                    H.canonical_json(
                        {
                            "model": args.model,
                            "system_prompt_sha256": system_prompt_sha256,
                            "user_payload": H.build_user_payload(
                                task_by_id[sample_id]
                            ),
                            "request_options": request_options,
                        }
                    )
                )
                if raw_row.get("request_sha256") != expected_request_sha256:
                    raise ValueError(
                        "legacy resume task/request fingerprint mismatch: "
                        f"{sample_id}"
                    )
        preflight = raw_document.get("preflight") or {}
    else:
        preflight = H.run_preflight(
            base_url=base_url,
            api_key=api_key,
            model=args.model,
            timeout_seconds=min(args.timeout_seconds, 120),
            max_attempts=args.max_attempts,
            retry_delays=retry_delays,
            max_tokens=256,
        )
        if preflight["status"] != "PASS":
            raise RuntimeError("preflight did not pass")

    raw_document = {
        "schema_version": "task1-concurrent-http-harness-audit/v0.1",
        "run_id": args.run_id,
        "model": args.model,
        "system_prompt_sha256": system_prompt_sha256,
        "tasks_sha256": tasks_sha256,
        "task_count": len(tasks),
        "request_options": request_options,
        "max_attempts": args.max_attempts,
        "retry_delays_seconds": retry_delays,
        "timeout_seconds": args.timeout_seconds,
        "concurrency": args.concurrency,
        "preflight": preflight,
        "tasks": [],
    }
    write_lock = threading.Lock()

    def checkpoint() -> None:
        ordered_ids = [
            sample_id for sample_id in task_order if sample_id in predictions_by_id
        ]
        raw_document["tasks"] = [raw_by_id[sample_id] for sample_id in ordered_ids]
        H.write_jsonl(
            args.output,
            [predictions_by_id[sample_id] for sample_id in ordered_ids],
        )
        H.write_json(args.raw_output, raw_document)

    checkpoint()
    remaining = [
        task for task in tasks if str(task["sample_id"]) not in predictions_by_id
    ]
    print(
        H.canonical_json(
            {
                "event": "start",
                "model": args.model,
                "task_count": len(tasks),
                "already_completed": len(predictions_by_id),
                "remaining": len(remaining),
                "concurrency": args.concurrency,
                "preflight_status": preflight.get("status"),
            }
        ),
        flush=True,
    )

    def run_one(task: dict[str, Any]):
        return H.run_task(
            task,
            base_url=base_url,
            api_key=api_key,
            model=args.model,
            run_id=args.run_id,
            system_prompt=system_prompt,
            max_tokens=args.max_tokens,
            timeout_seconds=args.timeout_seconds,
            max_attempts=args.max_attempts,
            retry_delays=retry_delays,
        )

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        future_to_id = {
            executor.submit(run_one, task): str(task["sample_id"])
            for task in remaining
        }
        try:
            for future in as_completed(future_to_id):
                sample_id = future_to_id[future]
                prediction, raw = future.result()
                with write_lock:
                    predictions_by_id[sample_id] = prediction
                    raw_by_id[sample_id] = raw
                    completed = len(predictions_by_id)
                    if completed % args.checkpoint_every == 0:
                        checkpoint()
                print(
                    H.canonical_json(
                        {
                            "event": "progress",
                            "model": args.model,
                            "completed": completed,
                            "total": len(tasks),
                            "sample_id": sample_id,
                            "status": raw["status"],
                            "attempt_count": len(raw["attempts"]),
                        }
                    ),
                    flush=True,
                )
        except BaseException:
            for pending in future_to_id:
                pending.cancel()
            with write_lock:
                checkpoint()
            raise

    with write_lock:
        checkpoint()
    failures = sum(row["status"] != "PASS" for row in raw_by_id.values())
    retries = sum(
        max(len(row.get("attempts", [])) - 1, 0)
        for row in raw_by_id.values()
    )
    print(
        H.canonical_json(
            {
                "status": "COMPLETE" if failures == 0 else "COMPLETE_WITH_FAILURES",
                "model": args.model,
                "task_count": len(tasks),
                "failure_count": failures,
                "retry_count": retries,
                "concurrency": args.concurrency,
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            H.canonical_json(
                {
                    "status": "FAIL",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                }
            ),
            file=sys.stderr,
        )
        raise
