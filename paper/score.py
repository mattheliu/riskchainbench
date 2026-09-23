#!/usr/bin/env python3
"""Score with a separately authorized evaluator and an explicitly chosen seed."""
import argparse
import subprocess
import sys
from pathlib import Path
from verify_release import ROOT, EVALUATOR_HASH, sha256, verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluator", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-seed", type=int, required=True,
                        help="Required: archived Task 1 results use 20260726; see KNOWN_GAPS.md")
    args = parser.parse_args()
    verify()
    if sha256(args.evaluator) != EVALUATOR_HASH:
        raise ValueError("evaluator does not match this frozen snapshot")
    if args.output.exists():
        raise ValueError("refusing to overwrite an existing score report")
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError("case-level score reports must stay outside the public paper/ directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.call([sys.executable, str(ROOT / "tools/score_obfuscated_reconstruction.py"),
                            "--dataset", str(args.evaluator), "--predictions", str(args.predictions),
                            "--output", str(output), "--schema", str(ROOT / "schemas/obfuscated_reconstruction_prediction_v0.1.schema.json"),
                            "--bootstrap-replicates", "2000", "--bootstrap-seed", str(args.bootstrap_seed)])


if __name__ == "__main__":
    raise SystemExit(main())
