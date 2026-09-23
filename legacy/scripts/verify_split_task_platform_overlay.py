#!/usr/bin/env python3
"""Verify a downloaded RiskChainBench split-task platform overlay."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any


MANIFEST_NAME = "PORTABLE_OVERLAY_FILE_MANIFEST.jsonl"
RELEASE_NAME = "PORTABLE_OVERLAY_MANIFEST.json"


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


def checked_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"unsafe overlay path: {value}")
    return path


def verify(root: Path) -> dict[str, Any]:
    root = root.resolve()
    release = read_json(root / RELEASE_NAME)
    if (
        release.get("schema_version") != "riskchainbench-platform-overlay/v0.1"
        or release.get("status") != "PASS_READY_FOR_UPLOAD"
        or release.get("case_count") != 600
        or release.get("source_benchmark") != "riskchainbench-balanced-600-v0.3"
        or release.get("contains_human_gold") is not False
        or release.get("contains_credentials") is not False
        or release.get("overlay_sha256")
        != embedded_hash(release, "overlay_sha256")
    ):
        raise ValueError("portable overlay manifest failed its embedded gate")
    manifest_ref = release.get("file_manifest") or {}
    manifest = root / checked_relative_path(str(manifest_ref.get("path") or ""))
    if (
        not manifest.is_file()
        or sha256_file(manifest) != manifest_ref.get("sha256")
    ):
        raise ValueError("portable overlay file manifest hash mismatch")
    rows = [
        json.loads(line)
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(rows) != release.get("file_count"):
        raise ValueError("portable overlay file count mismatch")
    paths = [str(row.get("path") or "") for row in rows]
    if len(paths) != len(set(paths)):
        raise ValueError("portable overlay contains duplicate paths")
    total_bytes = 0
    for row in rows:
        path = root / checked_relative_path(str(row["path"]))
        expected_bytes = int(row["bytes"])
        if (
            not path.is_file()
            or path.stat().st_size != expected_bytes
            or sha256_file(path) != row["sha256"]
        ):
            raise ValueError(f"portable overlay file mismatch: {row['path']}")
        total_bytes += expected_bytes
    if total_bytes != release.get("total_bytes"):
        raise ValueError("portable overlay byte count mismatch")
    return {
        "status": "PASS",
        "task": release["task"],
        "case_count": release["case_count"],
        "file_count": len(rows),
        "total_bytes": total_bytes,
        "overlay_sha256": release["overlay_sha256"],
        "contracts": release["contracts"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.root), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
