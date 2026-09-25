"""Verify the reviewed integration allowlist; does not certify old Git history."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = json.loads((ROOT / "docs/INTEGRATION_MANIFEST.json").read_text())
    names = set()
    for row in manifest["files"]:
        rel = Path(row["path"])
        if rel.is_absolute() or ".." in rel.parts or str(rel) in names:
            raise ValueError("unsafe or duplicate manifest entry")
        path = ROOT / rel
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT) or not path.is_file():
            raise ValueError("missing/unsafe integration file")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("hash mismatch: " + str(rel))
        names.add(str(rel))
    print(json.dumps({"status": "PASS", "integration_files_verified": len(names),
                      "scope": "reviewed 2026-09-25 integration, not full legacy history"}))


if __name__ == "__main__":
    main()
