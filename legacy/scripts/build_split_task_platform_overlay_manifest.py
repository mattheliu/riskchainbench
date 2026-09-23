#!/usr/bin/env python3
"""Build a deterministic integrity manifest for split-task platform overlays."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any


EXPECTED = {
    "task1": {
        "contract_file": "task1_contract.json",
        "contract_sha256": (
            "28a2fe6bac429d902153f3d9f2b575b89439115a91e9cd882843d5e54e056240"
        ),
        "case_count": 600,
    },
    "task2": {
        "contract_file": "task2_contract.json",
        "contract_sha256": (
            "028109cd4966e685aa7e611472ef7bc6482a5b83f68f776f9547f7c8175fabed"
        ),
        "case_count": 600,
        "runtime_supplement_sha256": (
            "195a60585d726a6feb4dd986ffa324d0fac6a7746f1003ef669f392976689617"
        ),
    },
}
EXPECTED_HANDOFF_SHA256 = (
    "7c2f462ecc92ca57b69675afc4462a348e8c75614bcc5960465c6110fe4fcc4d"
)
EXPECTED_HANDOFF_MAP_SHA256 = (
    "78502501b787b892049e45587d1ece84547c960b35f66f4764dfb81f2e336177"
)
MANIFEST_NAME = "PORTABLE_OVERLAY_FILE_MANIFEST.jsonl"
RELEASE_NAME = "PORTABLE_OVERLAY_MANIFEST.json"
EXCLUDED_PATH_PARTS = {
    ".cache",
    ".git",
    ".hf_cache",
    ".ms_upload_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}
SECRET_PATTERNS = {
    "github_token": re.compile(rb"\bghp_[A-Za-z0-9]{20,}\b"),
    "huggingface_token": re.compile(rb"\bhf_[A-Za-z0-9]{20,}\b"),
    "modelscope_token": re.compile(
        rb"\bms-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        rb"[0-9a-f]{4}-[0-9a-f]{12}\b"
    ),
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def embedded_hash(value: dict[str, Any], field: str) -> str:
    unhashed = copy.deepcopy(value)
    unhashed.pop(field, None)
    return sha256_text(canonical_json(unhashed))


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def is_payload(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return (
        path.is_file()
        and path.name not in {MANIFEST_NAME, RELEASE_NAME}
        and not EXCLUDED_PATH_PARTS.intersection(relative.parts)
        and path.suffix != ".pyc"
    )


def validate_contracts(root: Path, task: str) -> dict[str, str]:
    expected = EXPECTED[task]
    contract = read_json(root / str(expected["contract_file"]))
    if contract.get("contract_sha256") != expected["contract_sha256"]:
        raise ValueError(f"{task} contract hash mismatch")
    handoff = read_json(root / "handoff_contract.json")
    if (
        handoff.get("contract_sha256") != EXPECTED_HANDOFF_SHA256
        or handoff.get("case_count") != expected["case_count"]
    ):
        raise ValueError("handoff contract mismatch")
    handoff_map = root / "evaluator_only/handoff_map.jsonl"
    if (
        not handoff_map.is_file()
        or sha256_file(handoff_map) != EXPECTED_HANDOFF_MAP_SHA256
    ):
        raise ValueError("handoff map is missing or invalid")
    hashes = {
        "task_contract_sha256": str(expected["contract_sha256"]),
        "handoff_contract_sha256": EXPECTED_HANDOFF_SHA256,
        "handoff_map_sha256": EXPECTED_HANDOFF_MAP_SHA256,
    }
    if task == "task2":
        supplement = read_json(root / "runtime_supplement/runtime_supplement.json")
        supplement_hash = str(supplement.get("supplement_sha256") or "")
        if supplement_hash != expected["runtime_supplement_sha256"]:
            raise ValueError("runtime supplement hash mismatch")
        bundle_ref = supplement.get("runtime_metadata_bundle") or {}
        bundle = root / "runtime_supplement" / str(bundle_ref.get("path") or "")
        if (
            not bundle.is_file()
            or bundle.stat().st_size != int(bundle_ref.get("bytes") or -1)
            or sha256_file(bundle) != bundle_ref.get("sha256")
        ):
            raise ValueError("runtime metadata bundle mismatch")
        hashes["runtime_supplement_sha256"] = supplement_hash
        hashes["runtime_metadata_bundle_sha256"] = str(bundle_ref["sha256"])
    return hashes


def scan_credentials(files: list[Path], root: Path) -> dict[str, Any]:
    hits: list[dict[str, str]] = []
    for path in files:
        content = path.read_bytes()
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                hits.append(
                    {"path": path.relative_to(root).as_posix(), "pattern": name}
                )
    if hits:
        raise ValueError(f"credential-like material detected: {hits}")
    return {"status": "PASS", "secret_hits": []}


def build(root: Path, task: str) -> dict[str, Any]:
    root = root.resolve()
    hashes = validate_contracts(root, task)
    files = [path for path in sorted(root.rglob("*")) if is_payload(path, root)]
    rows = [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in files
    ]
    manifest = root / MANIFEST_NAME
    manifest.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
        ),
        encoding="utf-8",
    )
    release: dict[str, Any] = {
        "schema_version": "riskchainbench-platform-overlay/v0.1",
        "status": "PASS_READY_FOR_UPLOAD",
        "task": task,
        "source_benchmark": "riskchainbench-balanced-600-v0.3",
        "case_count": int(EXPECTED[task]["case_count"]),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "file_count": len(rows),
        "total_bytes": sum(row["bytes"] for row in rows),
        "file_manifest": {
            "path": MANIFEST_NAME,
            "sha256": sha256_file(manifest),
        },
        "contracts": hashes,
        "visibility": "AUTHORIZED_EVALUATOR_OVERLAY",
        "contains_human_gold": False,
        "contains_credentials": False,
        "credential_scan": scan_credentials(files, root),
    }
    release["overlay_sha256"] = embedded_hash(release, "overlay_sha256")
    (root / RELEASE_NAME).write_text(
        json.dumps(release, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return release


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task", choices=sorted(EXPECTED), required=True)
    args = parser.parse_args()
    result = build(args.root, args.task)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
