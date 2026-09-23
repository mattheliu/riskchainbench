#!/usr/bin/env python3
"""Offline scoring using original scoring cores, without bundling private Gold."""
import argparse
import importlib.util
import json
from pathlib import Path

from verify_package import ROOT, sha256, verify

TASK1_EVALUATOR = "c4fdcb3a06b7d2834963750f999b38ea238e7900ce989087c62d9020737d8685"
TASK2_PAIRED = "da6bbdc314758baaa80dc5279a1adf65b4e8ab3d0b50e55a1e419a323ba2b7e0"


def load(relative):
    spec = importlib.util.spec_from_file_location("scoring_core", ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("task", choices=["task1", "task2"])
    p.add_argument("--mode", choices=["demo", "paper"], required=True)
    p.add_argument("--dataset", type=Path, help="Separately authorized matching evaluator/paired input")
    p.add_argument("--predictions", type=Path, help="Task 1 model predictions only")
    p.add_argument("--seed", type=int, required=True, help="Explicit seed: archived Task 1=20260726; Task 2 base=20260727")
    p.add_argument("--replicates", type=int, default=2000)
    p.add_argument("--output", type=Path, required=True, help="New aggregate report outside this package")
    return p


def score(args):
    verify()
    if args.replicates < 1:
        raise ValueError("replicates must be positive")
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("output must stay outside the public evaluation package")
    if output.exists():
        raise ValueError("refusing to overwrite an existing output")
    if args.mode == "demo":
        if args.dataset or args.predictions:
            raise ValueError("demo only accepts the bundled synthetic fixtures")
        dataset = ROOT / "examples" / f"{args.task}_synthetic.jsonl"
        predictions = ROOT / "examples/task1_predictions_synthetic.jsonl"
    else:
        if not args.dataset:
            raise ValueError("paper mode requires a separately authorized --dataset")
        dataset, predictions = args.dataset, args.predictions
        expected = TASK1_EVALUATOR if args.task == "task1" else TASK2_PAIRED
        if sha256(dataset) != expected:
            raise ValueError("dataset does not match the frozen paper snapshot")
        if args.task == "task1" and not predictions:
            raise ValueError("Task 1 paper mode requires --predictions")
    if args.task == "task2" and args.predictions:
        raise ValueError("Task 2 uses a paired case-level dataset, not --predictions")
    report = {"schema_version": "riskchainbench-public-evaluation/v0.1", "task": args.task,
              "mode": args.mode, "historical_reproduction_claim": False,
              "input_sha256": sha256(dataset), "base_seed": args.seed,
              "bootstrap_replicates": args.replicates, "model_calls": 0,
              "notice": "Synthetic smoke test, not benchmark results" if args.mode == "demo"
                        else "Matched input; does not prove historical model output reproduction. See README gaps."}
    if args.task == "task1":
        core = load("task1/tools/score_obfuscated_reconstruction.py")
        result = core.score_dataset(dataset, predictions, core.DEFAULT_PREDICTION_SCHEMA,
                                    5, False, args.replicates, args.seed)
        # Do not put per-case text, entries, or Gold into the convenient output.
        report.update(aggregate=result["aggregate"], aggregate_bootstrap=result["aggregate_bootstrap"],
                      predictions_sha256=sha256(predictions), missing_prediction_count=result["missing_prediction_count"])
    else:
        core = load("task2/bootstrap_task2_and_gated.py")
        rows = core.read_jsonl(dataset)
        if args.mode == "paper":
            grouped, labels = core.validate_and_group(rows)
        else:
            grouped = {"synthetic-fixture": rows}
            labels = tuple(sorted({r["gold_type"] for r in rows if r["gold_decision"] == "VIOLATION"}))
        report["models"] = {}
        report["type_labels"] = list(labels)
        for index, model in enumerate(sorted(grouped)):
            seed = args.seed + index
            report["models"][model] = {"seed": seed, "website_count": len(grouped[model]),
                                       **core.summarize_model(grouped[model], labels, seed, args.replicates)}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return {"status": "PASS", "mode": args.mode, "task": args.task, "output": str(output), "model_calls": 0}


if __name__ == "__main__":
    args = parser().parse_args()
    try:
        print(json.dumps(score(args), indent=2))
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(f"Evaluation failed: {exc}")
