#!/usr/bin/env python3
"""Score Task 1 reconstruction predictions without resolving or visiting URLs."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import random
import re
import sys
from typing import Any, Iterable, Sequence
import unicodedata2 as unicodedata
from urllib.parse import urlsplit, urlunsplit

from jsonschema import Draft202012Validator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PREDICTION_SCHEMA = (
    PROJECT_ROOT / "schemas/obfuscated_reconstruction_prediction_v0.1.schema.json"
)
MASKED_ENTRY_RE = re.compile(r"^\[MASKED_(?:SITE|URL|ACCOUNT|CODE)(?:_[A-Z0-9_-]+)?\]$")
MIXED_TOKEN_RE = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9]+|[^\s]", re.UNICODE)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"expected JSON object at {path}:{line_number}")
        values.append(value)
    return values


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def validate_prediction(value: dict[str, Any], schema: dict[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        rendered = []
        for error in errors[:10]:
            location = ".".join(str(item) for item in error.absolute_path) or "$"
            rendered.append(f"{location}: {error.message}")
        raise ValueError("prediction schema validation failed: " + "; ".join(rendered))
    ranks = [row["rank"] for row in value["entry_candidates"]]
    if len(ranks) != len(set(ranks)):
        raise ValueError(f"duplicate entry candidate rank for {value['sample_id']}")
    message_ids = [row["message_id"] for row in value["reconstructed_messages"]]
    if len(message_ids) != len(set(message_ids)):
        raise ValueError(f"duplicate reconstructed message_id for {value['sample_id']}")


def edit_distance(left: Sequence[Any], right: Sequence[Any]) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_item in enumerate(left, 1):
        current = [left_index]
        for right_index, right_item in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_item != right_item),
                )
            )
        previous = current
    return previous[-1]


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    normalized = re.sub(r"[ \t\r\f\v]+", " ", normalized)
    normalized = re.sub(r" *\n *", "\n", normalized)
    return normalized.strip()


def normalize_intent(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def parse_entry(value: str) -> dict[str, str]:
    normalized = unicodedata.normalize("NFC", value).strip()
    if MASKED_ENTRY_RE.fullmatch(normalized):
        return {
            "canonical": normalized,
            "scheme": "",
            "host": normalized,
            "port": "",
            "path": "",
            "query": "",
            "fragment": "",
        }
    candidate = normalized if "://" in normalized else f"https://{normalized}"
    try:
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        # A model may echo a defanged host such as example[.]invalid, which urlsplit
        # rejects as a malformed IPv6 literal.  Treat it as an opaque entry rather
        # than aborting the whole scoring run.
        return {
            "canonical": normalized,
            "scheme": "",
            "host": normalized.casefold(),
            "port": "UNPARSEABLE",
            "path": "",
            "query": "",
            "fragment": "",
        }
    try:
        port = str(parsed.port or "")
    except ValueError:
        port = "INVALID"
    scheme = parsed.scheme.casefold()
    if (scheme == "https" and port == "443") or (scheme == "http" and port == "80"):
        port = ""
    netloc = host + (f":{port}" if port else "")
    path = parsed.path or "/"
    canonical = urlunsplit((scheme, netloc, path, parsed.query, parsed.fragment))
    return {
        "canonical": canonical,
        "scheme": scheme,
        "host": host,
        "port": port,
        "path": path,
        "query": parsed.query,
        "fragment": parsed.fragment,
    }


def is_reserved_candidate(value: str) -> bool:
    normalized = value.strip()
    if MASKED_ENTRY_RE.fullmatch(normalized):
        return True
    try:
        parsed = urlsplit(normalized if "://" in normalized else f"//{normalized}")
        host = (parsed.hostname or "").rstrip(".").casefold()
    except ValueError:
        # Defanged hosts are unparseable; they are not reserved, so a model that
        # echoes one is still flagged as predicting a non-reserved entry.
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True
    except ValueError:
        pass
    if host in {"example.com", "example.net", "example.org"}:
        return True
    if host.endswith((".example.com", ".example.net", ".example.org")):
        return True
    return host.endswith((".test", ".invalid", ".example"))


def target_gold_messages(record: dict[str, Any]) -> dict[str, str]:
    target_ids = {
        row["message_id"]
        for row in record["source_session"]["messages"]
        if row["transform"]
    }
    return {
        row["message_id"]: normalize_text(row["text"])
        for row in record["gold_reconstruction"]["messages"]
        if row["message_id"] in target_ids
    }


def score_case(
    record: dict[str, Any],
    prediction: dict[str, Any],
    top_k: int,
) -> dict[str, Any]:
    gold_messages = target_gold_messages(record)
    predicted_messages = {
        row["message_id"]: normalize_text(row["text"])
        for row in prediction["reconstructed_messages"]
    }
    char_edits = 0
    char_references = 0
    token_edits = 0
    token_references = 0
    exact_messages = 0
    message_rows: list[dict[str, Any]] = []
    for message_id, gold in gold_messages.items():
        predicted = predicted_messages.get(message_id, "")
        gold_tokens = MIXED_TOKEN_RE.findall(gold)
        predicted_tokens = MIXED_TOKEN_RE.findall(predicted)
        char_error = edit_distance(list(gold), list(predicted))
        token_error = edit_distance(gold_tokens, predicted_tokens)
        char_edits += char_error
        char_references += len(gold)
        token_edits += token_error
        token_references += len(gold_tokens)
        exact = predicted == gold
        exact_messages += int(exact)
        message_rows.append(
            {
                "message_id": message_id,
                "exact": exact,
                "char_edits": char_error,
                "reference_characters": len(gold),
                "mixed_token_edits": token_error,
                "reference_mixed_tokens": len(gold_tokens),
            }
        )

    gold_entry = parse_entry(record["gold_reconstruction"]["entry_value"])
    candidates = sorted(prediction["entry_candidates"], key=lambda row: row["rank"])
    evaluated_candidates = candidates[:top_k]
    candidate_rows: list[dict[str, Any]] = []
    component_names = ("scheme", "host", "port", "path", "query", "fragment")
    for candidate in evaluated_candidates:
        parsed = parse_entry(candidate["value"])
        component_matches = {
            component: parsed[component] == gold_entry[component]
            for component in component_names
        }
        candidate_rows.append(
            {
                "rank": candidate["rank"],
                "confidence": candidate["confidence"],
                "canonical_exact": parsed["canonical"] == gold_entry["canonical"],
                "component_matches": component_matches,
                "component_accuracy": sum(component_matches.values()) / len(component_matches),
                "reserved_or_masked": is_reserved_candidate(candidate["value"]),
            }
        )

    top1_exact = bool(candidate_rows and candidate_rows[0]["canonical_exact"])
    topk_exact = any(row["canonical_exact"] for row in candidate_rows)
    all_messages_exact = exact_messages == len(gold_messages)
    intent_exact = normalize_intent(prediction["intent"]) == normalize_intent(record["intent"])
    platform_exact = prediction["platform"] == record["platform"]
    full_success = all_messages_exact and intent_exact and platform_exact and topk_exact
    # The committed-restoration protocol consumes entry_candidates[0], so the
    # top-1 variant is primary; top-k stays as a ranking diagnostic.
    full_success_top1 = (
        all_messages_exact and intent_exact and platform_exact and top1_exact
    )
    return {
        "sample_id": record["sample_id"],
        "site_id": record["session_id"],
        "platform": record["platform"],
        "input_view": prediction["input_view"],
        "abstain": prediction["abstain"],
        "messages": message_rows,
        "char_edits": char_edits,
        "reference_characters": char_references,
        "mixed_token_edits": token_edits,
        "reference_mixed_tokens": token_references,
        "all_target_messages_exact": all_messages_exact,
        "target_message_count": len(gold_messages),
        "exact_message_count": exact_messages,
        "intent_exact": intent_exact,
        "platform_exact": platform_exact,
        "entry_top1_exact": top1_exact,
        "entry_topk_exact": topk_exact,
        "local_resolution_success_at_k": topk_exact,
        "entry_candidates": candidate_rows,
        "predicted_live_entry_count": sum(
            not row["reserved_or_masked"] for row in candidate_rows
        ),
        "full_reconstruction_success": full_success,
        "full_reconstruction_success_top1": full_success_top1,
        "obfuscation_types": record["obfuscation_types"],
    }


def aggregate_cases(cases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(cases)
    count = len(rows)
    char_edits = sum(row["char_edits"] for row in rows)
    char_references = sum(row["reference_characters"] for row in rows)
    token_edits = sum(row["mixed_token_edits"] for row in rows)
    token_references = sum(row["reference_mixed_tokens"] for row in rows)
    target_messages = sum(row["target_message_count"] for row in rows)
    exact_messages = sum(row["exact_message_count"] for row in rows)

    def rate(field: str) -> float:
        return sum(bool(row[field]) for row in rows) / count if count else 0.0

    return {
        "case_count": count,
        "target_message_count": target_messages,
        "character_error_rate": char_edits / char_references if char_references else 0.0,
        "mixed_token_error_rate": token_edits / token_references if token_references else 0.0,
        # Per-message exact match, the reported EM_msg.  message_exact_rate below is
        # the stricter per-case conjunction (every target message in the session
        # correct) and is kept for backward comparability.
        "message_exact_rate_per_message": (
            exact_messages / target_messages if target_messages else 0.0
        ),
        "message_exact_rate": rate("all_target_messages_exact"),
        "intent_accuracy": rate("intent_exact"),
        "platform_accuracy": rate("platform_exact"),
        "entry_top1_exact_rate": rate("entry_top1_exact"),
        "entry_topk_recall": rate("entry_topk_exact"),
        "local_resolution_success_rate_at_k": rate("local_resolution_success_at_k"),
        "abstain_rate": rate("abstain"),
        "full_reconstruction_success_rate": rate("full_reconstruction_success"),
        "full_reconstruction_success_rate_top1": rate("full_reconstruction_success_top1"),
        "predicted_live_entry_count": sum(row["predicted_live_entry_count"] for row in rows),
    }


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    fraction = index - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


INTEGER_AGGREGATE_FIELDS = frozenset(
    {"case_count", "target_message_count", "predicted_live_entry_count"}
)


def site_clusters(cases: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group cases by website so a replicate resamples sites, not variants.

    Message variants of one website are not independent samples; resampling them
    individually would shrink the interval by roughly the variant count.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in cases:
        grouped.setdefault(row["site_id"], []).append(row)
    return [grouped[key] for key in sorted(grouped)]


def bootstrap_site_clustered(
    cases: Sequence[dict[str, Any]],
    *,
    replicates: int,
    seed: int,
) -> dict[str, Any]:
    clusters = site_clusters(cases)
    if not clusters or replicates <= 0:
        return {
            "replicates": 0,
            "site_count": len(clusters),
            "seed": seed,
            "ci95": {},
        }
    rng = random.Random(seed)
    samples: dict[str, list[float]] = {}
    for _ in range(replicates):
        drawn: list[dict[str, Any]] = []
        for _ in clusters:
            drawn.extend(clusters[rng.randrange(len(clusters))])
        for field, value in aggregate_cases(drawn).items():
            if field in INTEGER_AGGREGATE_FIELDS:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(value):
                continue
            samples.setdefault(field, []).append(float(value))
    return {
        "replicates": replicates,
        "site_count": len(clusters),
        "seed": seed,
        "ci95": {
            field: [percentile(values, 0.025), percentile(values, 0.975)]
            for field, values in sorted(samples.items())
        },
    }


def score_dataset(
    dataset_path: Path,
    prediction_path: Path,
    schema_path: Path,
    top_k: int,
    allow_missing: bool,
    bootstrap_replicates: int = 0,
    bootstrap_seed: int = 20260726,
) -> dict[str, Any]:
    if top_k <= 0 or top_k > 10:
        raise ValueError("top-k must be in [1, 10]")
    records = read_jsonl(dataset_path)
    predictions = read_jsonl(prediction_path)
    schema = read_json(schema_path)
    Draft202012Validator.check_schema(schema)
    prediction_by_id: dict[str, dict[str, Any]] = {}
    for prediction in predictions:
        validate_prediction(prediction, schema)
        sample_id = prediction["sample_id"]
        if sample_id in prediction_by_id:
            raise ValueError(f"duplicate prediction sample_id: {sample_id}")
        prediction_by_id[sample_id] = prediction

    record_ids = {record["sample_id"] for record in records}
    extra = sorted(set(prediction_by_id) - record_ids)
    if extra:
        raise ValueError("predictions contain unknown sample_id: " + ", ".join(extra[:10]))
    missing = sorted(record_ids - set(prediction_by_id))
    if missing and not allow_missing:
        raise ValueError("missing predictions: " + ", ".join(missing[:10]))

    cases: list[dict[str, Any]] = []
    for record in records:
        prediction = prediction_by_id.get(record["sample_id"])
        if prediction is None:
            prediction = {
                "schema_version": "obfuscated-reconstruction-prediction/v0.1",
                "sample_id": record["sample_id"],
                "model_id": "MISSING",
                "run_id": "missing-prediction",
                "input_view": "TOKEN_TEXT",
                "reconstructed_messages": [],
                "intent": "",
                "platform": "unknown",
                "entry_candidates": [],
                "abstain": True,
                "uncertainties": ["missing_prediction"],
            }
        cases.append(score_case(record, prediction, top_k))

    platforms = sorted({row["platform"] for row in cases})
    obfuscation_types = sorted(
        {kind for row in cases for kind in row["obfuscation_types"]}
    )
    return {
        "schema_version": "obfuscated-reconstruction-score/v0.1",
        "dataset_name": dataset_path.name,
        "dataset_sha256": sha256_file(dataset_path),
        "predictions_name": prediction_path.name,
        "predictions_sha256": sha256_file(prediction_path),
        "top_k": top_k,
        "network_access_performed": False,
        "missing_prediction_count": len(missing),
        "aggregate": aggregate_cases(cases),
        "aggregate_bootstrap": bootstrap_site_clustered(
            cases,
            replicates=bootstrap_replicates,
            seed=bootstrap_seed,
        ),
        "by_platform": {
            platform: aggregate_cases(row for row in cases if row["platform"] == platform)
            for platform in platforms
        },
        "by_obfuscation_type": {
            kind: aggregate_cases(row for row in cases if kind in row["obfuscation_types"])
            for kind in obfuscation_types
        },
        "cases": cases,
        "report_sha256": "",
    }


def finalize_report(report: dict[str, Any]) -> dict[str, Any]:
    report = dict(report)
    report["report_sha256"] = ""
    report["report_sha256"] = hashlib.sha256(
        canonical_json(report).encode("utf-8")
    ).hexdigest()
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score obfuscated-session reconstruction without URL access."
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schema", type=Path, default=DEFAULT_PREDICTION_SCHEMA)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument(
        "--bootstrap-replicates",
        type=int,
        default=2000,
        help="site-clustered bootstrap replicates for 95%% CIs; 0 disables",
    )
    parser.add_argument("--bootstrap-seed", type=int, default=20260726)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = finalize_report(
            score_dataset(
                args.dataset,
                args.predictions,
                args.schema,
                args.top_k,
                args.allow_missing,
                args.bootstrap_replicates,
                args.bootstrap_seed,
            )
        )
        write_json(args.output, report)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": "PASS",
                "output": str(args.output),
                "aggregate": report["aggregate"],
                "report_sha256": report["report_sha256"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
