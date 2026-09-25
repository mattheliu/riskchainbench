import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = load("reconstructed_gate", "reproduction/entry_gate.py")
PROTOCOL = load("exported_evidence", "research/evidence_protocol_v02/scripts/evidence_protocol_v02.py")


@pytest.mark.parametrize("value,expected", [
    ("alpha.beta.test", True), ("ALPHA.BETA.TEST", False),
    ("https://alpha.beta.test", False), ("alpha.beta.test/", False),
    (" alpha.beta.test", False), ("alpha.beta.test:443", False),
])
def test_strict_gate_does_not_normalize(value, expected):
    assert GATE.strict_top1_gate([{"rank": 1, "value": value}], "alpha.beta.test") is expected


def test_lower_rank_cannot_repair_gate():
    assert not GATE.strict_top1_gate([{"rank": 1, "value": "other.site.test"},
                                    {"rank": 2, "value": "alpha.beta.test"}], "alpha.beta.test")


@pytest.mark.parametrize("candidates", [None, [], [{"rank": True, "value": "alpha.beta.test"}],
    [{"rank": 2, "value": "alpha.beta.test"}],
    [{"rank": 1, "value": "alpha.beta.test"}, {"rank": 1, "value": "alpha.beta.test"}]])
def test_missing_or_ambiguous_candidates_fail(candidates):
    assert not GATE.strict_top1_gate(candidates, "alpha.beta.test")


def test_invalid_reference_rejected():
    with pytest.raises(ValueError):
        GATE.strict_top1_gate([], "https://alpha.beta.test")


def test_bottom_propagation_and_explicit_gate():
    assert GATE.apply_gate("VIOLATION", "TYPE", False) == ("__BOTTOM__", "__BOTTOM__")
    assert GATE.apply_gate("VIOLATION", "TYPE", True) == ("VIOLATION", "TYPE")
    with pytest.raises(ValueError):
        GATE.apply_gate("VIOLATION", "TYPE", None)


def test_exported_schemas_validate_themselves():
    for path in (ROOT / "research/evidence_protocol_v02/schemas").glob("*.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text()))


def test_empty_case_not_misreported_as_valid():
    report = PROTOCOL.audit_case({})
    assert report["status"] == "FAIL"
    assert report["recommended_outcome"] == "INVALID_EVIDENCE"
    assert report["protocol"]["is_gold_label"] is False


def test_cycles_and_shared_roots():
    assert PROTOCOL._cycle_nodes({"a": {"b"}, "b": {"a"}}) == {"a", "b"}
    assert not PROTOCOL._cycle_nodes({"a": {"b"}, "b": set()})
    assert PROTOCOL._root_observation_ids("I1", {"E1"}, {"I1": {"based_on_ids": ["E1", "E1"]}}, {}) == {"E1"}


def test_artifact_hash_and_root_boundary(tmp_path):
    artifact = tmp_path / "invented.txt"
    artifact.write_text("invented test artifact")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    errors = []
    assert PROTOCOL._verify_artifacts({"A": {"path": "invented.txt", "sha256": digest}}, tmp_path, errors) == (1, 1)
    assert not errors
    errors = []
    assert PROTOCOL._verify_artifacts({"A": {"path": "invented.txt", "sha256": "0"*64}}, tmp_path, errors) == (1, 0)
    assert "entity:A:artifact_sha256_mismatch" in errors
    assert not PROTOCOL._portable_path("../outside")
    assert not PROTOCOL._portable_path("/outside")


def test_imported_task2_manifest():
    root = ROOT / "task2/paper"
    for line in (root / "MANIFEST.sha256").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest


def test_public_web_cli_requires_explicit_inputs():
    result = subprocess.run([sys.executable, str(ROOT / "reproduction/web_only.py")],
                            text=True, capture_output=True)
    assert result.returncode != 0
    assert "--scored-zip" in result.stderr and "--ordinal-map" in result.stderr


def test_public_web_cli_rejects_overwriting_before_reading_inputs(tmp_path):
    output = tmp_path / "existing.json"
    output.write_text("preserve")
    result = subprocess.run([sys.executable, str(ROOT / "reproduction/web_only.py"),
        "--scored-zip", "absent.zip", "--ordinal-map", "absent.jsonl", "--output", str(output)],
        text=True, capture_output=True)
    assert result.returncode != 0 and "Refusing to overwrite" in result.stderr
    assert output.read_text() == "preserve"
