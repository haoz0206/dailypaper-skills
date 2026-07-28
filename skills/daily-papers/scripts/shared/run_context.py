#!/usr/bin/env python3
"""Create isolated run manifests for the daily-paper workflow."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from user_config import obsidian_vault_path, timezone_name


MANIFEST_VERSION = 1
VALID_STATUSES = {
    "prepared",
    "fetching",
    "reviewing",
    "writing-notes",
    "validated",
    "failed",
}


def _target_date(value: str | None, timezone: str) -> str:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone}") from exc

    if value:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    return datetime.now(zone).date().isoformat()


def create_run(
    *,
    target_date: str | None = None,
    timezone: str | None = None,
    run_root: Path | None = None,
) -> Path:
    timezone = timezone or timezone_name()
    date = _target_date(target_date, timezone)
    vault = obsidian_vault_path()

    configured_root = os.environ.get("DAILYPAPER_RUN_ROOT")
    base = (
        Path(configured_root).expanduser().resolve()
        if configured_root
        else (run_root or vault / ".dailypaper" / "runs").resolve()
    )
    run_id = f"{date}-{uuid4().hex[:12]}"
    run_dir = base / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    manifest_path = run_dir / "manifest.json"
    manifest = {
        "version": MANIFEST_VERSION,
        "run_id": run_id,
        "status": "prepared",
        "target_date": date,
        "timezone": timezone,
        "changed_paths": [],
        "coordination": {
            "status": "not-acquired",
        },
        "paths": {
            "vault": str(vault),
            "run_dir": str(run_dir),
            "candidates": str(run_dir / "candidates.json"),
            "enriched": str(run_dir / "enriched.json"),
            "result": str(run_dir / "result.json"),
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def load_manifest(path: Path) -> dict:
    manifest_path = path.expanduser().resolve()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("version") != MANIFEST_VERSION:
        raise ValueError(f"Unsupported manifest version: {data.get('version')}")
    return data


def update_manifest(
    path: Path,
    *,
    status: str | None = None,
    changed_paths: list[Path] | None = None,
    coordination: dict | None = None,
) -> dict:
    """Update a run manifest and keep changed paths relative to the Vault."""
    manifest_path = path.expanduser().resolve()
    data = load_manifest(manifest_path)

    if status:
        if status not in VALID_STATUSES:
            valid = ", ".join(sorted(VALID_STATUSES))
            raise ValueError(f"Unknown status {status!r}; expected one of: {valid}")
        data["status"] = status

    vault = Path(data["paths"]["vault"]).resolve()
    recorded = list(data.get("changed_paths", []))
    for value in changed_paths or []:
        candidate = value.expanduser()
        if not candidate.is_absolute():
            candidate = vault / candidate
        candidate = candidate.resolve()
        try:
            relative = candidate.relative_to(vault).as_posix()
        except ValueError as exc:
            raise ValueError(f"Changed path is outside the Vault: {candidate}") from exc
        if relative not in recorded:
            recorded.append(relative)
    data["changed_paths"] = recorded
    if coordination:
        current_coordination = dict(data.get("coordination", {}))
        current_coordination.update(coordination)
        data["coordination"] = current_coordination

    temporary_path = manifest_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(manifest_path)
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--date", help="Target date in YYYY-MM-DD")
    create_parser.add_argument("--timezone", help="IANA timezone name")
    create_parser.add_argument("--run-root", type=Path)

    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("manifest", type=Path)

    update_parser = subparsers.add_parser("update")
    update_parser.add_argument("manifest", type=Path)
    update_parser.add_argument("--status", choices=sorted(VALID_STATUSES))
    update_parser.add_argument(
        "--changed-path",
        action="append",
        default=[],
        type=Path,
        help="Vault-relative or absolute path written by this run; repeat as needed",
    )

    args = parser.parse_args()
    if args.command == "create":
        path = create_run(
            target_date=args.date,
            timezone=args.timezone,
            run_root=args.run_root,
        )
        print(path)
        return

    if args.command == "update":
        data = update_manifest(
            args.manifest,
            status=args.status,
            changed_paths=args.changed_path,
        )
    else:
        data = load_manifest(args.manifest)
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
