"""Verify a standalone evaluation package against its complete allowlist."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify(root=ROOT):
    root = Path(root).resolve()
    names = set()
    for line in (root / "MANIFEST.sha256").read_text().splitlines():
        expected, name = line.split(maxsplit=1)
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or name in names:
            raise ValueError("unsafe or duplicate manifest path")
        target = root / relative
        if target.is_symlink() or not target.resolve().is_relative_to(root) or not target.is_file():
            raise ValueError("unsafe or missing package file")
        if sha256(target) != expected:
            raise ValueError(f"manifest mismatch: {name}")
        names.add(name)
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
              and not {"__pycache__", ".pytest_cache"}.intersection(p.parts)
              and p != root / "MANIFEST.sha256"}
    if actual != names:
        raise ValueError("unlisted or missing package files")
    return {"status": "PASS", "files": len(names), "model_calls": 0}


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
