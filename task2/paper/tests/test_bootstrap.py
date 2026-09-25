import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("paper_bootstrap", ROOT / "bootstrap_task2_and_gated.py")
B = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(B)


def row(index=0, gate=True):
    return {"ordinal": index, "task2_case_ref": f"synthetic-{index}", "model": "fixture",
            "display_model": "Synthetic fixture", "gold_decision": "VIOLATION", "gold_type": "A",
            "web_pred_decision": "VIOLATION", "web_pred_type": "A", "gate_pass": gate,
            "gated_pred_decision": "VIOLATION" if gate else B.BOTTOM,
            "gated_pred_type": "A" if gate else B.BOTTOM}


def test_failed_gate_counts_as_failure_without_changing_web_result():
    rows = [row(0), row(1, False)]
    result = B.summarize_model(rows, ("A",), 7, 20)
    assert result["point"]["web_decision_accuracy"] == 1
    assert result["point"]["gated_decision_accuracy"] == 0.5
    assert result["point"]["gate_loss"] == 0.5


def test_invalid_web_decision_remains_in_denominator():
    rows = [row(0), row(1)]
    rows[1]["web_pred_decision"] = B.BOTTOM
    rows[1]["web_pred_type"] = B.BOTTOM
    assert B.score_rows(rows, "web", ("A",))["decision_accuracy"] == 0.5


def test_decision_macro_f1_keeps_three_fixed_labels():
    assert B.score_rows([row()], "web", ("A",))["decision_macro_f1"] == pytest.approx(1 / 3)


def test_type_f1_is_only_gold_violation_cases():
    control = row(1)
    control.update(gold_decision="NON_VIOLATION", gold_type="NONE",
                   web_pred_decision="VIOLATION", web_pred_type="A")
    assert B.score_rows([row(), control], "web", ("A",))["violation_type_macro_f1"] == 1


def test_paired_bootstrap_is_deterministic():
    rows = [row(0), row(1, False), row(2)]
    assert B.summarize_model(rows, ("A",), 27, 50) == B.summarize_model(rows, ("A",), 27, 50)


def full_fixture():
    rows = []
    for model in range(10):
        for index in range(600):
            item = row(index)
            item["model"] = f"synthetic-model-{model}"
            item["gold_type"] = f"type-{index % 6}"
            rows.append(item)
    return rows


def test_full_matrix_checks_dimensions_and_six_supported_types():
    grouped, labels = B.validate_and_group(full_fixture())
    assert len(grouped) == 10
    assert len(labels) == 6


def test_different_gold_between_models_is_rejected():
    rows = full_fixture()
    rows[601]["gold_type"] = "different"
    with pytest.raises(ValueError, match="alignment mismatch"):
        B.validate_and_group(rows)


def test_duplicate_website_is_rejected():
    rows = full_fixture()
    rows[1]["ordinal"] = 0
    with pytest.raises(ValueError, match="duplicate website ordinal"):
        B.validate_and_group(rows)


def test_recovered_aggregates_have_exact_seed_offsets_and_gate_identity():
    data = json.loads((ROOT / "results/web_and_gated_aggregates.json").read_text())
    assert len(data["models"]) == 10
    assert data["website_count_per_model"] == 600
    assert data["bootstrap"]["replicates"] == 2000
    for index, model in enumerate(data["models"]):
        assert model["seed"] == 20260727 + index
        point = model["point"]
        assert point["gate_loss"] == pytest.approx(point["web_decision_accuracy"] - point["gated_decision_accuracy"])
        assert set(point) == set(model["ci95"])
