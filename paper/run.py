#!/usr/bin/env python3
"""Run the pinned inputs; new results are not historical paper results."""
import argparse
import json
import subprocess
import sys
from pathlib import Path
from verify_release import ROOT, verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Your provider's exact model ID")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="Private run directory outside paper/")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true", help="Verify inputs and print command, with zero model calls")
    args = parser.parse_args()
    verify()
    output = args.output_dir.resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError("raw run artifacts must stay outside the public paper/ directory")
    if not 1 <= args.concurrency <= 32:
        raise ValueError("concurrency must be between 1 and 32")
    command = [sys.executable, str(ROOT / "tools/run_task1_concurrent_http.py"),
               "--tasks", str(ROOT / "model_visible/tasks_3600.jsonl"),
               "--prompt", str(ROOT / "spec/model_prompt.md"),
               "--model", args.model, "--run-id", args.run_id,
               "--output", str(output / "predictions.jsonl"),
               "--raw-output", str(output / "private_run_audit.json"),
               "--max-tokens", "8000", "--timeout-seconds", "300",
               "--max-attempts", "3", "--retry-delays", "2,5",
               "--concurrency", str(args.concurrency)]
    if args.dry_run:
        print(json.dumps({"command": command, "model_calls": 0}, indent=2))
        return 0
    output.mkdir(parents=True, exist_ok=True)
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
