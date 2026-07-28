#!/usr/bin/env python3
"""Manage DailyPaper settings that must remain local to one machine."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MACHINE_CONFIG_ENV = "DAILYPAPER_MACHINE_CONFIG"
ALLOWED_TOP_LEVEL_FIELDS = {"version", "vault_path", "zotero"}
ALLOWED_ZOTERO_FIELDS = {"database_path", "storage_path"}


class MachineConfigError(ValueError):
    """The per-machine configuration is missing, invalid, or unsafe."""


def machine_config_path() -> Path:
    explicit = os.environ.get(MACHINE_CONFIG_ENV)
    if explicit:
        return Path(explicit).expanduser().resolve()
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        base = Path(xdg_config_home).expanduser().resolve()
    else:
        base = Path.home() / ".config"
    return base / "dailypaper" / "config.json"


def _normalize_absolute_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MachineConfigError(f"{field} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise MachineConfigError(f"{field} must be an absolute path")
    return str(path.resolve())


def normalize_machine_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MachineConfigError("Machine configuration must be a JSON object")
    unknown = set(value) - ALLOWED_TOP_LEVEL_FIELDS
    if unknown:
        raise MachineConfigError(
            "Unsupported machine configuration fields: "
            + ", ".join(sorted(unknown))
        )
    if value.get("version") != SCHEMA_VERSION:
        raise MachineConfigError(
            f"Machine configuration version must be {SCHEMA_VERSION}"
        )

    normalized: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "vault_path": _normalize_absolute_path(
            value.get("vault_path"),
            field="vault_path",
        ),
    }
    zotero = value.get("zotero")
    if zotero is not None:
        if not isinstance(zotero, dict):
            raise MachineConfigError("zotero must be an object")
        unknown_zotero = set(zotero) - ALLOWED_ZOTERO_FIELDS
        if unknown_zotero:
            raise MachineConfigError(
                "Unsupported machine Zotero fields: "
                + ", ".join(sorted(unknown_zotero))
            )
        normalized_zotero: dict[str, str] = {}
        for field in sorted(ALLOWED_ZOTERO_FIELDS):
            if field in zotero:
                normalized_zotero[field] = _normalize_absolute_path(
                    zotero[field],
                    field=f"zotero.{field}",
                )
        if normalized_zotero:
            normalized["zotero"] = normalized_zotero
    return normalized


def load_machine_config(*, required: bool = False) -> dict[str, Any]:
    path = machine_config_path()
    if not path.exists():
        if required:
            raise MachineConfigError(
                f"Machine configuration does not exist: {path}"
            )
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MachineConfigError(f"Invalid JSON in {path}: {exc}") from exc
    return normalize_machine_config(value)


def write_machine_config(value: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_machine_config(value)
    path = machine_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".config.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(normalized, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return normalized


def build_machine_config(
    *,
    vault_path: Path,
    zotero_database: Path | None = None,
    zotero_storage: Path | None = None,
) -> dict[str, Any]:
    try:
        existing = load_machine_config(required=False)
    except MachineConfigError:
        # An explicit `set` is the recovery path for a malformed machine file.
        # Preserve optional values only when the existing file is valid.
        existing = {}
    proposed: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "vault_path": str(vault_path),
    }
    existing_zotero = existing.get("zotero")
    if isinstance(existing_zotero, dict):
        proposed["zotero"] = dict(existing_zotero)
    if zotero_database is not None:
        proposed.setdefault("zotero", {})["database_path"] = str(
            zotero_database
        )
    if zotero_storage is not None:
        proposed.setdefault("zotero", {})["storage_path"] = str(zotero_storage)
    return normalize_machine_config(proposed)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("path")
    subparsers.add_parser("show")
    subparsers.add_parser("validate")
    set_parser = subparsers.add_parser("set")
    set_parser.add_argument("--vault", required=True, type=Path)
    set_parser.add_argument("--zotero-db", type=Path)
    set_parser.add_argument("--zotero-storage", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "path":
            _print_json({"config_path": str(machine_config_path())})
        elif args.command == "show":
            config = load_machine_config(required=False)
            _print_json(
                {
                    "status": "configured" if config else "unconfigured",
                    "config_path": str(machine_config_path()),
                    "config": config,
                }
            )
        elif args.command == "validate":
            config = load_machine_config(required=True)
            _print_json(
                {
                    "status": "valid",
                    "config_path": str(machine_config_path()),
                    "config": config,
                }
            )
        elif args.command == "set":
            proposed = build_machine_config(
                vault_path=args.vault,
                zotero_database=args.zotero_db,
                zotero_storage=args.zotero_storage,
            )
            written = write_machine_config(proposed)
            _print_json(
                {
                    "status": "configured",
                    "config_path": str(machine_config_path()),
                    "config": written,
                }
            )
    except (MachineConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
