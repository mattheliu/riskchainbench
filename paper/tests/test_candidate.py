import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S = load("candidate_scorer", "tools/score_obfuscated_reconstruction.py")
V = load("candidate_verifier", "verify_release.py")


def gold(site="fixture-1", text="hello"):
    return {"sample_id": site + "--v000", "session_id": site,
            "platform": "wechat", "intent": "navigate", "obfuscation_types": [],
            "source_session": {"messages": [{"message_id": "m1", "text": text, "transform": True}]},
            "gold_reconstruction": {"messages": [{"message_id": "m1", "text": text}],
                                    "entry_value": "fixture.example.test"}}


def prediction(record):
    return {"schema_version": "obfuscated-reconstruction-prediction/v0.1",
            "sample_id": record["sample_id"], "model_id": "synthetic-fixture",
            "run_id": "fixture-run", "input_view": "TOKEN_TEXT",
            "reconstructed_messages": copy.deepcopy(record["gold_reconstruction"]["messages"]),
            "intent": "navigate", "platform": "wechat", "abstain": False, "uncertainties": [],
            "entry_candidates": [{"rank": 1, "value": "fixture.example.test", "confidence": 1.0}]}


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def score_fixture(tmp_path, records, predictions, allow_missing=False):
    dataset, outputs = tmp_path / "synthetic_gold.jsonl", tmp_path / "synthetic_predictions.jsonl"
    write_rows(dataset, records)
    write_rows(outputs, predictions)
    return S.score_dataset(dataset, outputs, S.DEFAULT_PREDICTION_SCHEMA, 5, allow_missing, 20, 7)


def test_frozen_release_count_schema_and_hashes():
    assert V.verify()["tasks"] == 3600


def test_scorer_default_schema_is_bundled():
    assert S.DEFAULT_PREDICTION_SCHEMA.is_file()


def test_synthetic_oracle_scores_one_without_network(tmp_path):
    record = gold()
    report = score_fixture(tmp_path, [record], [prediction(record)])
    assert report["network_access_performed"] is False
    assert report["aggregate"]["entry_top1_exact_rate"] == 1
    assert report["aggregate"]["full_reconstruction_success_rate_top1"] == 1
    assert report["aggregate"]["character_error_rate"] == 0


def test_topk_does_not_repair_primary_entry():
    record = gold()
    pred = prediction(record)
    pred["entry_candidates"] = [{"rank": 1, "value": "wrong.example.test", "confidence": 0.6},
                                {"rank": 2, "value": "fixture.example.test", "confidence": 0.4}]
    case = S.score_case(record, pred, 5)
    assert case["entry_topk_exact"] is True
    assert case["entry_top1_exact"] is False
    assert case["full_reconstruction_success_top1"] is False


def test_missing_predictions_fail_by_default_and_stay_in_denominator(tmp_path):
    records = [gold("fixture-1"), gold("fixture-2")]
    with pytest.raises(ValueError, match="missing predictions"):
        score_fixture(tmp_path, records, [prediction(records[0])])
    report = score_fixture(tmp_path, records, [prediction(records[0])], True)
    assert report["aggregate"]["case_count"] == 2
    assert report["aggregate"]["entry_top1_exact_rate"] == 0.5


def test_cross_version_unknown_id_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown sample_id"):
        score_fixture(tmp_path, [gold("fixture-1")], [prediction(gold("different-version"))])


def test_duplicate_predictions_are_rejected(tmp_path):
    record = gold()
    with pytest.raises(ValueError, match="duplicate prediction"):
        score_fixture(tmp_path, [record], [prediction(record), prediction(record)])


def test_cer_is_micro_averaged_and_not_clipped():
    short, long = gold("short", "a"), gold("long", "abcdefghij")
    wrong = prediction(short)
    wrong["reconstructed_messages"][0]["text"] = "bbbb"
    case = S.score_case(short, wrong, 5)
    assert S.aggregate_cases([case])["character_error_rate"] == 4
    both = S.aggregate_cases([case, S.score_case(long, prediction(long), 5)])
    assert both["character_error_rate"] == pytest.approx(4 / 11)


def test_seed_reproducibility_and_site_clustering():
    records = [gold("fixture-1"), gold("fixture-2")]
    bad = prediction(records[1])
    bad["entry_candidates"] = []
    cases = [S.score_case(records[0], prediction(records[0]), 5)] * 6 + [S.score_case(records[1], bad, 5)] * 6
    first = S.bootstrap_site_clustered(cases, replicates=50, seed=17)
    assert first["site_count"] == 2
    assert first == S.bootstrap_site_clustered(cases, replicates=50, seed=17)


def test_concurrent_wrapper_loads_adjacent_harness():
    concurrent = load("candidate_concurrent", "tools/run_task1_concurrent_http.py")
    assert concurrent.HARNESS_PATH == ROOT / "tools/run_task1_http_harness.py"
    assert "/private/tmp/" not in (ROOT / "tools/run_task1_concurrent_http.py").read_text()


def test_run_dry_run_has_no_model_calls(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "run.py"), "--model", "synthetic-fixture",
                             "--run-id", "test-dry-run", "--output-dir", str(tmp_path / "runs"),
                             "--dry-run"], capture_output=True, text=True, check=True)
    output = json.loads(result.stdout)
    assert output["model_calls"] == 0
    assert not (tmp_path / "runs").exists()
    assert "8000" in output["command"] and "300" in output["command"]


def test_score_requires_explicit_seed():
    result = subprocess.run([sys.executable, str(ROOT / "score.py"), "--evaluator", "missing",
                             "--predictions", "missing", "--output", "missing"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "--bootstrap-seed" in result.stderr


def test_manifest_tamper_is_rejected(tmp_path):
    (tmp_path / "MANIFEST.sha256").write_text("0" * 64 + "  altered.txt\n")
    (tmp_path / "altered.txt").write_text("changed")
    with pytest.raises(ValueError, match="manifest mismatch"):
        V.verify(tmp_path)
