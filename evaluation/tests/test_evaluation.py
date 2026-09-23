import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(tmp_path, task="task1", *extra):
    out = tmp_path / (task + ".json")
    cmd = [sys.executable, str(ROOT / "evaluate.py"), task, "--mode", "demo",
           "--seed", "7", "--replicates", "30", "--output", str(out), *extra]
    return subprocess.run(cmd, text=True, capture_output=True), out


def test_manifest():
    p = subprocess.run([sys.executable, str(ROOT / "verify_package.py")], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def test_task1_demo_is_aggregate_only(tmp_path):
    p, out = run(tmp_path)
    assert p.returncode == 0, p.stderr
    report = json.loads(out.read_text())
    assert report["mode"] == "demo" and report["model_calls"] == 0
    assert not report["historical_reproduction_claim"]
    assert report["aggregate"]["entry_top1_exact_rate"] == 0.5
    assert "cases" not in report


def test_task2_paired_gate_and_invalid_denominator(tmp_path):
    p, out = run(tmp_path, "task2")
    assert p.returncode == 0, p.stderr
    report = json.loads(out.read_text())
    model = report["models"]["synthetic-fixture"]
    assert model["website_count"] == 8
    assert len(report["type_labels"]) == 6
    assert model["point"]["web_decision_accuracy"] == 7 / 8
    assert model["point"]["gated_decision_accuracy"] == 6 / 8
    assert model["point"]["gate_loss"] == 1 / 8


@pytest.mark.parametrize("task", ["task1", "task2"])
def test_seed_determinism(tmp_path, task):
    a, first = run(tmp_path / "first", task)
    b, second = run(tmp_path / "second", task)
    assert a.returncode == b.returncode == 0
    assert first.read_bytes() == second.read_bytes()


def test_output_overwrite_rejected(tmp_path):
    p, out = run(tmp_path)
    original = out.read_bytes()
    second, _ = run(tmp_path)
    assert p.returncode == 0 and second.returncode != 0
    assert out.read_bytes() == original


def test_demo_cannot_accept_external_gold(tmp_path):
    p, out = run(tmp_path, "task1", "--dataset", str(tmp_path / "other"))
    assert p.returncode != 0 and not out.exists()
    assert "bundled synthetic" in p.stderr


@pytest.mark.parametrize("task", ["task1", "task2"])
def test_paper_mode_rejects_synthetic_as_real_gold(tmp_path, task):
    out = tmp_path / "no-output.json"
    p = subprocess.run([sys.executable, str(ROOT / "evaluate.py"), task, "--mode", "paper",
                        "--seed", "7", "--dataset", str(ROOT / "examples" / f"{task}_synthetic.jsonl"),
                        "--output", str(out)], capture_output=True, text=True)
    assert p.returncode != 0 and not out.exists()
    assert "frozen paper snapshot" in p.stderr


def test_seed_required(tmp_path):
    p = subprocess.run([sys.executable, str(ROOT / "evaluate.py"), "task1", "--mode", "demo",
                        "--output", str(tmp_path / "missing.json")], capture_output=True, text=True)
    assert p.returncode == 2 and "--seed" in p.stderr


def test_nonpositive_bootstrap_rejected(tmp_path):
    p, out = run(tmp_path, "task2", "--replicates", "0")
    assert p.returncode != 0 and not out.exists()


def test_output_cannot_pollute_package(tmp_path):
    p, _ = run(tmp_path, "task1", "--output", str(ROOT / "should-not-exist.json"))
    assert p.returncode != 0 and not (ROOT / "should-not-exist.json").exists()
