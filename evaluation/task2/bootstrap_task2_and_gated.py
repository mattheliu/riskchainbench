#!/usr/bin/env python3
"""Bootstrap Task 2 web-only and entry-gated metrics by website."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


EXPECTED_CASE_LEVEL_SHA256 = (
    "da6bbdc314758baaa80dc5279a1adf65b4e8ab3d0b50e55a1e419a323ba2b7e0"
)
EXPECTED_MODELS = 10
EXPECTED_SITES = 600
BOTTOM = "__BOTTOM__"
DECISION_LABELS = (
    "VIOLATION",
    "NON_VIOLATION",
    "INSUFFICIENT_EVIDENCE",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-level", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--replicates", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20260727)
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def macro_f1(
    pairs: Iterable[tuple[str, str]], labels: tuple[str, ...]
) -> float:
    pairs = list(pairs)
    scores: list[float] = []
    for label in labels:
        tp = sum(gold == label and pred == label for gold, pred in pairs)
        fp = sum(gold != label and pred == label for gold, pred in pairs)
        fn = sum(gold == label and pred != label for gold, pred in pairs)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        scores.append(
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
    return sum(scores) / len(scores)


def score_rows(
    rows: list[dict[str, Any]],
    prediction_prefix: str,
    type_labels: tuple[str, ...],
) -> dict[str, float]:
    decision_key = f"{prediction_prefix}_pred_decision"
    type_key = f"{prediction_prefix}_pred_type"
    decisions = [(row["gold_decision"], row[decision_key]) for row in rows]
    violation_types = [
        (
            row["gold_type"],
            row[type_key] if row[decision_key] == "VIOLATION" else BOTTOM,
        )
        for row in rows
        if row["gold_decision"] == "VIOLATION"
    ]
    hierarchy = [
        row["gold_decision"] == row[decision_key]
        and row["gold_type"] == row[type_key]
        for row in rows
    ]
    return {
        "decision_accuracy": sum(gold == pred for gold, pred in decisions)
        / len(rows),
        "decision_macro_f1": macro_f1(decisions, DECISION_LABELS),
        "violation_type_macro_f1": macro_f1(
            violation_types, type_labels
        ),
        "hierarchical_exact_match": sum(hierarchy) / len(rows),
    }


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * probability)]


def summarize_model(
    rows: list[dict[str, Any]],
    type_labels: tuple[str, ...],
    seed: int,
    replicates: int,
) -> dict[str, Any]:
    web = score_rows(rows, "web", type_labels)
    gated = score_rows(rows, "gated", type_labels)
    point = {
        "entry_gate_pass": sum(row["gate_pass"] for row in rows)
        / len(rows),
        **{f"web_{key}": value for key, value in web.items()},
        **{f"gated_{key}": value for key, value in gated.items()},
        "gate_loss": web["decision_accuracy"]
        - gated["decision_accuracy"],
    }

    rng = random.Random(seed)
    sampled: dict[str, list[float]] = {
        key: [] for key in point
    }
    for _ in range(replicates):
        draw = [rows[rng.randrange(len(rows))] for _ in rows]
        draw_web = score_rows(draw, "web", type_labels)
        draw_gated = score_rows(draw, "gated", type_labels)
        values = {
            "entry_gate_pass": sum(row["gate_pass"] for row in draw)
            / len(draw),
            **{
                f"web_{key}": value
                for key, value in draw_web.items()
            },
            **{
                f"gated_{key}": value
                for key, value in draw_gated.items()
            },
            "gate_loss": draw_web["decision_accuracy"]
            - draw_gated["decision_accuracy"],
        }
        for key, value in values.items():
            sampled[key].append(value)

    intervals = {
        key: [
            percentile(values, 0.025),
            percentile(values, 0.975),
        ]
        for key, values in sampled.items()
    }
    return {"point": point, "ci95": intervals}


def validate_and_group(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], tuple[str, ...]]:
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_model[str(row["model"])].append(row)
    if len(by_model) != EXPECTED_MODELS:
        raise ValueError(
            f"expected {EXPECTED_MODELS} models, found {len(by_model)}"
        )

    reference_gold: dict[int, tuple[str, str, str]] | None = None
    for model, model_rows in by_model.items():
        model_rows.sort(key=lambda row: int(row["ordinal"]))
        if len(model_rows) != EXPECTED_SITES:
            raise ValueError(
                f"{model}: expected {EXPECTED_SITES} rows, "
                f"found {len(model_rows)}"
            )
        if len({int(row["ordinal"]) for row in model_rows}) != EXPECTED_SITES:
            raise ValueError(f"{model}: duplicate website ordinal")
        model_gold = {
            int(row["ordinal"]): (
                str(row["task2_case_ref"]),
                str(row["gold_decision"]),
                str(row["gold_type"]),
            )
            for row in model_rows
        }
        if reference_gold is None:
            reference_gold = model_gold
        elif model_gold != reference_gold:
            raise ValueError(f"{model}: website/gold alignment mismatch")

    assert reference_gold is not None
    type_labels = tuple(
        sorted(
            {
                gold_type
                for _, gold_decision, gold_type in reference_gold.values()
                if gold_decision == "VIOLATION"
            }
        )
    )
    if len(type_labels) != 6:
        raise ValueError(
            f"expected 6 supported violation types, found {type_labels}"
        )
    return dict(by_model), type_labels


def write_outputs(
    out_dir: Path,
    summary: dict[str, Any],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "task2_and_gated_bootstrap_2000.json"
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    csv_path = out_dir / "task2_and_gated_bootstrap_2000.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        fields = (
            "model",
            "display_model",
            "metric",
            "point",
            "ci95_low",
            "ci95_high",
            "sites",
            "replicates",
            "seed",
        )
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for model in summary["model_order"]:
            model_result = summary["models"][model]
            for metric, point in model_result["point"].items():
                low, high = model_result["ci95"][metric]
                writer.writerow(
                    {
                        "model": model,
                        "display_model": model_result["display_model"],
                        "metric": metric,
                        "point": f"{point:.12f}",
                        "ci95_low": f"{low:.12f}",
                        "ci95_high": f"{high:.12f}",
                        "sites": EXPECTED_SITES,
                        "replicates": summary["bootstrap"]["replicates"],
                        "seed": model_result["seed"],
                    }
                )

    manifest_path = out_dir / "ANALYSIS_MANIFEST.sha256"
    manifest_path.write_text(
        f"{sha256(json_path)}  {json_path.name}\n"
        f"{sha256(csv_path)}  {csv_path.name}\n",
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    actual_hash = sha256(args.case_level)
    if actual_hash != EXPECTED_CASE_LEVEL_SHA256:
        raise ValueError(
            "case-level input hash mismatch: "
            f"{actual_hash} != {EXPECTED_CASE_LEVEL_SHA256}"
        )

    by_model, type_labels = validate_and_group(
        read_jsonl(args.case_level)
    )
    models = sorted(by_model)
    results: dict[str, Any] = {}
    for model_index, model in enumerate(models):
        model_seed = args.seed + model_index
        result = summarize_model(
            by_model[model],
            type_labels,
            model_seed,
            args.replicates,
        )
        result["display_model"] = str(by_model[model][0]["display_model"])
        result["seed"] = model_seed
        results[model] = result

    summary = {
        "schema_version": "riskchainbench-bootstrap-summary/v0.1",
        "status": "PASS",
        "input": {
            "sha256": actual_hash,
            "rows": len(read_jsonl(args.case_level)),
        },
        "bootstrap": {
            "unit": "website",
            "replicates": args.replicates,
            "base_seed": args.seed,
            "central_interval": 0.95,
            "paired_web_and_gate_within_model": True,
            "fixed_decision_labels": list(DECISION_LABELS),
            "fixed_violation_type_labels": list(type_labels),
        },
        "model_order": models,
        "models": results,
    }
    write_outputs(args.out_dir, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
