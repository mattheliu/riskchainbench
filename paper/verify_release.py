#!/usr/bin/env python3
"""Verify the recovered, model-visible Task 1 snapshot without model calls."""
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASKS_HASH = "a3c7248d06caa6f2d978284d87fecf6c080f963b6a30473da338ba459e65b644"
PROMPT_HASH = "2ece83426010b6c5d6abc4ef005011f6780a60a42b467009a2cc4f753269373c"
EVALUATOR_HASH = "c4fdcb3a06b7d2834963750f999b38ea238e7900ce989087c62d9020737d8685"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify(root=ROOT):
    root = Path(root)
    checked = []
    for line in (root / "MANIFEST.sha256").read_text().splitlines():
        expected, name = line.split(maxsplit=1)
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe manifest path")
        target = root / relative
        if (target.is_symlink() or not target.resolve().is_relative_to(root.resolve())
                or not target.is_file() or sha256(target) != expected):
            raise ValueError(f"manifest mismatch: {name}")
        checked.append(name)
    if len(checked) != len(set(checked)):
        raise ValueError("duplicate manifest path")
    actual_files = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
                    and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
                    and p != root / "MANIFEST.sha256"}
    if set(checked) != actual_files:
        raise ValueError("unlisted or missing release files")
    tasks_path = root / "model_visible/tasks_3600.jsonl"
    if sha256(tasks_path) != TASKS_HASH or sha256(root / "spec/model_prompt.md") != PROMPT_HASH:
        raise ValueError("frozen scientific inputs changed")
    rows = [json.loads(line) for line in tasks_path.read_text().splitlines() if line.strip()]
    ids = [r["sample_id"] for r in rows]
    if len(ids) != 3600 or len(set(ids)) != 3600:
        raise ValueError("expected 3600 unique task IDs")
    if len({s.rsplit("--v", 1)[0] for s in ids}) != 600:
        raise ValueError("expected 600 source clusters")
    if Counter(s.rsplit("--v", 1)[1] for s in ids) != {f"{i:03d}": 600 for i in range(6)}:
        raise ValueError("each variant must contain 600 inputs")
    forbidden = {"gold", "gold_reconstruction", "evaluator", "evaluator_only", "resolver", "api_key", "authorization"}
    def walk(value):
        if isinstance(value, dict):
            if forbidden.intersection(str(k).lower() for k in value):
                raise ValueError("forbidden model-visible field")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for row in rows:
        walk(row)
    from jsonschema import Draft202012Validator
    schema = json.loads((root / "schemas/obfuscated_reconstruction_task_v0.1.schema.json").read_text())
    validator = Draft202012Validator(schema)
    for row in rows:
        validator.validate(row)
    return {"status": "PASS", "files": len(checked), "tasks": len(rows), "sources": 600, "network_calls": 0}


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
