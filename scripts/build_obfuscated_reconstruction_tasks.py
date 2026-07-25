#!/usr/bin/env python3
"""Project generated records into leak-checked Task 1 model inputs."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterable

from jsonschema import Draft202012Validator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = PROJECT_ROOT / "schemas/obfuscated_reconstruction_task_v0.1.schema.json"
DEFAULT_PROMPT = PROJECT_ROOT / "configs/obfuscated_reconstruction_prompt_v0.1.md"
FORBIDDEN_MODEL_KEYS = {
    "entry",
    "gold_reconstruction",
    "local_mirror_ref",
    "message_transformations",
    "obfuscation_types",
    "operations",
    "record_seed_sha256",
    "rule_id",
    "rule_source",
    "source_provenance",
    "source_session",
    "text_generation",
    "transform",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(canonical_json(value) + "\n")


def find_forbidden_keys(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in FORBIDDEN_MODEL_KEYS:
                findings.append(child_path)
            findings.extend(find_forbidden_keys(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(find_forbidden_keys(child, f"{path}[{index}]"))
    return findings


def validate_task(task: dict[str, Any], schema: dict[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(task),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        rendered = []
        for error in errors[:10]:
            location = ".".join(str(item) for item in error.absolute_path) or "$"
            rendered.append(f"{location}: {error.message}")
        raise ValueError("task schema validation failed: " + "; ".join(rendered))
    forbidden = find_forbidden_keys(task)
    if forbidden:
        raise ValueError("model task contains forbidden keys: " + ", ".join(forbidden))
    unhashed = copy.deepcopy(task)
    expected = unhashed.pop("task_sha256")
    if sha256_text(canonical_json(unhashed)) != expected:
        raise ValueError(f"task hash mismatch: {task.get('sample_id')}")


def build_task(record: dict[str, Any], prompt_id: str, prompt_sha256: str) -> dict[str, Any]:
    context = record["client_context"]
    task: dict[str, Any] = {
        "schema_version": "obfuscated-reconstruction-task/v0.1",
        "sample_id": record["sample_id"],
        "platform": record["platform"],
        "surface": record["surface"],
        "client_context": {
            "client_type": context["client_type"],
            "os": context["os"],
            "client_version": context["client_version"],
            "locale": context["locale"],
        },
        "input_view": "TOKEN_TEXT",
        "messages": [
            {
                "message_id": message["message_id"],
                "role": message["role"],
                "content": message["text"],
            }
            for message in record["transformed_session"]["messages"]
        ],
        "prompt_id": prompt_id,
        "prompt_sha256": prompt_sha256,
    }
    task["task_sha256"] = sha256_text(canonical_json(task))
    return task


def build_tasks(
    dataset_path: Path,
    output_path: Path,
    manifest_path: Path,
    prompt_path: Path,
    schema_path: Path,
    prompt_id: str,
) -> dict[str, Any]:
    records = read_jsonl(dataset_path)
    if not records:
        raise ValueError("dataset is empty")
    schema = read_json(schema_path)
    Draft202012Validator.check_schema(schema)
    prompt_sha256 = sha256_file(prompt_path)
    tasks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for record in records:
        if record["sample_id"] in seen_ids:
            raise ValueError(f"duplicate sample_id: {record['sample_id']}")
        seen_ids.add(record["sample_id"])
        task = build_task(record, prompt_id, prompt_sha256)
        validate_task(task, schema)
        serialized = canonical_json(task)
        forbidden_literals = [
            str(record["entry"]["local_mirror_ref"]),
            str(record.get("source_provenance", {}).get("source_ref") or ""),
        ]
        for literal in forbidden_literals:
            if literal and literal in serialized:
                raise ValueError(
                    f"private literal leaked into model task {record['sample_id']}: {literal}"
                )
        tasks.append(task)

    write_jsonl(output_path, tasks)
    manifest = {
        "schema_version": "obfuscated-reconstruction-task-manifest/v0.1",
        "dataset_name": dataset_path.name,
        "dataset_sha256": sha256_file(dataset_path),
        "prompt_name": prompt_path.name,
        "prompt_id": prompt_id,
        "prompt_sha256": prompt_sha256,
        "schema_sha256": sha256_file(schema_path),
        "output_name": output_path.name,
        "output_sha256": sha256_file(output_path),
        "task_count": len(tasks),
        "tasks": [
            {
                "line_number": index,
                "sample_id": task["sample_id"],
                "task_sha256": task["task_sha256"],
            }
            for index, task in enumerate(tasks, 1)
        ],
    }
    write_json(manifest_path, manifest)
    return {
        "status": "PASS",
        "task_count": len(tasks),
        "output": str(output_path),
        "manifest": str(manifest_path),
        "output_sha256": manifest["output_sha256"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build leak-checked model-visible Task 1 inputs."
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--prompt-id", default="covert-entry-reconstruction-v0.1")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_tasks(
            args.dataset,
            args.output,
            args.manifest,
            args.prompt,
            args.schema,
            args.prompt_id,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
