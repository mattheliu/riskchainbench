#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any


SCOPE_KIND = "formal_gate_current_pass"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def profile_site_ids(profile: dict[str, Any]) -> set[str]:
    primary = str(profile.get("site_id") or "").strip()
    raw_aliases = profile.get("site_id_aliases") or []
    if not isinstance(raw_aliases, list):
        raise ValueError("profile site_id_aliases must be a list")
    aliases = {
        str(alias).strip()
        for alias in raw_aliases
        if isinstance(alias, str) and alias.strip()
    }
    return ({primary} if primary else set()) | aliases


def require_profile_site_output(profile: dict[str, Any], site_out: Path) -> None:
    allowed = profile_site_ids(profile)
    if site_out.name in allowed:
        return
    primary = str(profile.get("site_id") or "").strip()
    aliases = sorted(allowed - ({primary} if primary else set()))
    raise ValueError(
        f"profile site_id {primary!r} and aliases {aliases!r} "
        f"do not match {site_out.name!r}"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def site_ids_sha256(site_ids: list[str] | set[str]) -> str:
    canonical = "".join(f"{site_id}\n" for site_id in sorted(site_ids))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def scope_from_dashboard(path: Path) -> dict[str, Any]:
    dashboard = read_json(path)
    formal_gate = dashboard.get("formal_gate")
    if not isinstance(formal_gate, dict):
        raise ValueError("dashboard has no formal_gate object")
    raw_site_ids = formal_gate.get("current_gate_pass_sites")
    if not isinstance(raw_site_ids, list) or not raw_site_ids:
        raise ValueError("dashboard formal_gate has no current_gate_pass_sites")
    if any(not isinstance(value, str) or not value.strip() for value in raw_site_ids):
        raise ValueError("dashboard formal pass site IDs must be non-empty strings")
    site_ids = sorted(value.strip() for value in raw_site_ids)
    if len(site_ids) != len(set(site_ids)):
        raise ValueError("dashboard formal pass site IDs contain duplicates")

    declared_count = formal_gate.get("current_gate_pass")
    if not isinstance(declared_count, int) or declared_count != len(site_ids):
        raise ValueError("dashboard formal pass count does not match its site list")
    dashboard_gate_count = dashboard.get("seed_sites_gate_pass")
    if dashboard_gate_count is not None and dashboard_gate_count != len(site_ids):
        raise ValueError("dashboard seed gate pass count does not match formal pass scope")

    return {
        "schema_version": "1.0",
        "kind": SCOPE_KIND,
        "captured_at": utc_now(),
        "source_dashboard": str(path),
        "source_dashboard_generated_at": str(dashboard.get("generated_at") or ""),
        "source_dashboard_sha256": sha256_file(path),
        "declared_site_count": declared_count,
        "site_count": len(site_ids),
        "site_ids_sha256": site_ids_sha256(site_ids),
        "site_ids": site_ids,
    }


def load_scope(path: Path) -> dict[str, Any]:
    scope = read_json(path)
    if scope.get("kind") != SCOPE_KIND:
        raise ValueError(f"unsupported stateful scope kind: {scope.get('kind')!r}")
    raw_site_ids = scope.get("site_ids")
    if not isinstance(raw_site_ids, list) or not raw_site_ids:
        raise ValueError("stateful scope has no site IDs")
    if any(not isinstance(value, str) or not value.strip() for value in raw_site_ids):
        raise ValueError("stateful scope site IDs must be non-empty strings")
    site_ids = sorted(value.strip() for value in raw_site_ids)
    if len(site_ids) != len(set(site_ids)):
        raise ValueError("stateful scope site IDs contain duplicates")
    if scope.get("site_count") != len(site_ids):
        raise ValueError("stateful scope count does not match its site list")
    if scope.get("site_ids_sha256") != site_ids_sha256(site_ids):
        raise ValueError("stateful scope site ID hash mismatch")
    return {**scope, "site_ids": site_ids}


def scope_contract(scope: dict[str, Any], scope_path: Path) -> dict[str, Any]:
    return {
        "kind": scope["kind"],
        "scope_path": str(scope_path),
        "scope_sha256": sha256_file(scope_path),
        "source_dashboard": scope.get("source_dashboard", ""),
        "source_dashboard_generated_at": scope.get("source_dashboard_generated_at", ""),
        "source_dashboard_sha256": scope.get("source_dashboard_sha256", ""),
        "allowlist_site_count": scope["site_count"],
        "site_ids_sha256": scope["site_ids_sha256"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze the dashboard strict-PASS site IDs for stateful processing.")
    parser.add_argument("--dashboard", type=Path, default=Path("outputs/dashboard/dashboard.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    scope = scope_from_dashboard(args.dashboard)
    atomic_json(args.output, scope)
    print(
        json.dumps(
            {
                "kind": scope["kind"],
                "site_count": scope["site_count"],
                "site_ids_sha256": scope["site_ids_sha256"],
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
