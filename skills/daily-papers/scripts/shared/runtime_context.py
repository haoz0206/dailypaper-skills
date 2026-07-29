#!/usr/bin/env python3
"""Resolve and validate one Harness-independent DailyPaper runtime context."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import active_run_guard
import config_schema
import user_config
from machine_config import (
    MachineConfigError,
    load_machine_config,
    machine_config_path,
)


CONTEXT_VERSION = 1
RuntimeContextError = config_schema.ConfigurationError


def _resolve_machine_and_vault() -> tuple[dict[str, Any], Path]:
    explicit_vault = os.environ.get("DAILYPAPER_VAULT")
    machine = load_machine_config(required=not bool(explicit_vault))
    if explicit_vault:
        candidate = Path(explicit_vault).expanduser()
        if not candidate.is_absolute():
            raise RuntimeContextError("DAILYPAPER_VAULT must be an absolute path")
        vault = candidate.resolve()
    else:
        vault = Path(str(machine["vault_path"])).expanduser().resolve()
    if not vault.is_dir():
        raise RuntimeContextError(f"Configured Vault is not a directory: {vault}")
    return machine, vault


def resolve_vault_path() -> Path:
    """Resolve the one machine-local Vault root without reading shared config."""
    _machine, vault = _resolve_machine_and_vault()
    return vault


def resolve_shared_config_path(vault: Path) -> Path:
    """Resolve the shared configuration source without depending on cwd."""
    explicit_config = os.environ.get("DAILYPAPER_CONFIG")
    if explicit_config:
        candidate = Path(explicit_config).expanduser()
        if not candidate.is_absolute():
            raise RuntimeContextError("DAILYPAPER_CONFIG must be an absolute path")
        return candidate.resolve()
    return vault / ".dailypaper" / "config.json"


def _inside_vault(path: Path, vault: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(vault)
    except ValueError as exc:
        raise RuntimeContextError(f"{label} escapes the configured Vault") from exc
    return resolved


def resolve_runtime_context(
    *,
    guard_active_run: bool = False,
    guard_remote: bool = True,
    prepare_standalone: bool = False,
) -> dict[str, Any]:
    """Return one validated context for all public and internal workflows."""
    machine, vault = _resolve_machine_and_vault()

    preparation: dict[str, Any] = {"prepared": False}
    if prepare_standalone:
        repository_defaults = user_config.DEFAULT_CONFIG["repository"]
        prepared = active_run_guard.prepare_standalone_vault(
            vault,
            repository_url=str(repository_defaults["url"]),
            remote=str(repository_defaults["remote"]),
            branch=str(repository_defaults["branch"]),
        )
        preparation = {"prepared": True, **prepared}

    config_path = resolve_shared_config_path(vault)
    if not config_path.is_file():
        raise RuntimeContextError(
            f"Shared Vault configuration does not exist: {config_path}"
        )
    user_config.clear_config_cache()
    effective = user_config.load_user_config()

    notes = _inside_vault(user_config.paper_notes_dir(), vault, "paper notes path")
    concepts = _inside_vault(user_config.concepts_dir(), vault, "concepts path")
    inbox = _inside_vault(user_config.paper_inbox_dir(), vault, "inbox path")
    daily = _inside_vault(user_config.daily_papers_dir(), vault, "daily papers path")

    guard_result: dict[str, Any] = {"checked": False}
    if guard_active_run or prepare_standalone:
        repository = effective["repository"]
        if guard_remote:
            checked = active_run_guard.guard_remote_active_run(
                vault,
                repository_url=str(repository["url"]),
                remote=str(repository["remote"]),
                branch=str(repository["branch"]),
                task_state_file=str(repository["task_state_file"]),
                fetch_remote=not prepare_standalone,
            )
        else:
            checked = active_run_guard.guard_active_run(
                vault,
                task_state_file=str(repository["task_state_file"]),
            )
        guard_result = {"checked": True, **checked}

    machine_zotero = machine.get("zotero")
    zotero: dict[str, Any] = {
        "enabled": isinstance(machine_zotero, dict) and bool(machine_zotero),
        "database_path": None,
        "storage_path": None,
    }
    if isinstance(machine_zotero, dict):
        zotero["database_path"] = machine_zotero.get("database_path")
        zotero["storage_path"] = machine_zotero.get("storage_path")

    fingerprint = config_schema.configuration_fingerprint(effective)

    return {
        "version": CONTEXT_VERSION,
        "status": "ready",
        "sources": {
            "machine_config": str(machine_config_path()),
            "shared_config": str(config_path),
        },
        "paths": {
            "vault": str(vault),
            "paper_notes": str(notes),
            "concepts": str(concepts),
            "inbox": str(inbox),
            "daily_papers": str(daily),
        },
        "runtime": copy.deepcopy(effective["runtime"]),
        "repository": copy.deepcopy(effective["repository"]),
        "daily_papers": copy.deepcopy(effective["daily_papers"]),
        "automation": copy.deepcopy(effective["automation"]),
        "zotero": zotero,
        "guard": guard_result,
        "preparation": preparation,
        "configuration_fingerprint": fingerprint,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve one validated DailyPaper runtime context."
    )
    parser.add_argument(
        "--guard-active-run",
        action="store_true",
        help="Reject standalone writes while a coordinated run owns the Vault.",
    )
    parser.add_argument(
        "--prepare-standalone",
        action="store_true",
        help=(
            "Fast-forward a clean Vault (or prove a dirty Vault is current), "
            "then reject an active coordinated run."
        ),
    )
    args = parser.parse_args()
    try:
        context = resolve_runtime_context(
            guard_active_run=args.guard_active_run,
            prepare_standalone=args.prepare_standalone,
        )
    except active_run_guard.ActiveRunError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "code": "active-run",
                    "message": str(exc),
                    "run": exc.state,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except (
        MachineConfigError,
        RuntimeContextError,
        active_run_guard.GuardError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "code": "invalid-runtime-context",
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(context, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
