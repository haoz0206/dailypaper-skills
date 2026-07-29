#!/usr/bin/env python3
"""Deterministically prepare one machine for the DailyPaper suite.

The caller chooses an absolute Vault path.  This module owns the remaining
ordering contract: clone or validate the fixed repository, complete the
crash-resumable Vault bootstrap, and only then persist machine configuration.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

_SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from machine_config import (
    MachineConfigError,
    build_machine_config,
    load_machine_config,
    machine_config_path,
    write_machine_config,
)
from safe_git import SafeGitError, run_git_program
from vault_coordination import (
    CoordinationError,
    FIXED_BRANCH,
    FIXED_VAULT_URL,
    bootstrap_vault,
)


class OnboardingError(RuntimeError):
    """The selected machine Vault could not be prepared safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _normalize_target(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise OnboardingError(
            "invalid-vault-path",
            "DailyPaper Vault path must be absolute",
        )
    if not candidate.name or candidate.name in {".", ".."}:
        raise OnboardingError(
            "invalid-vault-path",
            f"DailyPaper Vault path has no safe directory name: {candidate}",
        )
    if candidate.is_symlink():
        raise OnboardingError(
            "unsafe-vault-path",
            f"DailyPaper Vault path must not be a symlink: {candidate}",
        )
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise OnboardingError(
            "invalid-vault-parent",
            f"DailyPaper Vault parent cannot be prepared: {candidate.parent}",
        ) from exc
    return parent / candidate.name


def _clone_fixed_vault(target: Path) -> None:
    """Clone into a unique sibling and rename only after Git succeeds."""
    temporary = target.parent / f".{target.name}.clone-{uuid4().hex}"
    command = [
        "git",
        "clone",
        "--branch",
        FIXED_BRANCH,
        "--single-branch",
        "--",
        FIXED_VAULT_URL,
        str(temporary),
    ]
    try:
        result = run_git_program(*command[1:])
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise OnboardingError(
                "clone-failed",
                f"Could not clone the fixed DailyPaper Vault: {detail}",
            )
        if temporary.is_symlink() or not temporary.is_dir():
            raise OnboardingError(
                "clone-failed",
                "Git clone did not produce a safe Vault directory",
            )
        if target.exists() or target.is_symlink():
            raise OnboardingError(
                "vault-path-raced",
                f"DailyPaper Vault path appeared during clone: {target}",
            )
        os.rename(temporary, target)
    except (OSError, SafeGitError) as exc:
        raise OnboardingError(
            "clone-failed",
            f"Could not install the cloned DailyPaper Vault at {target}",
        ) from exc
    finally:
        if temporary.is_dir() and not temporary.is_symlink():
            shutil.rmtree(temporary)
        elif temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def onboard_machine(
    vault_path: Path,
    *,
    zotero_database: Path | None = None,
    zotero_storage: Path | None = None,
) -> dict[str, Any]:
    """Prepare a fixed Vault and persist machine configuration last."""
    vault = _normalize_target(vault_path)
    if vault.exists():
        if vault.is_symlink() or not vault.is_dir():
            raise OnboardingError(
                "unsafe-vault-path",
                f"DailyPaper Vault path is not a regular directory: {vault}",
            )
        cloned = False
    else:
        _clone_fixed_vault(vault)
        cloned = True

    # bootstrap_vault validates Git root, fixed remote, fixed branch, clean
    # change set, and remote publication.  It is resumable after interruption.
    bootstrap = bootstrap_vault(vault)

    proposed = build_machine_config(
        vault_path=vault,
        zotero_database=zotero_database,
        zotero_storage=zotero_storage,
    )
    written = write_machine_config(proposed)
    verified = load_machine_config(required=True)
    if verified != written or verified.get("vault_path") != str(vault):
        raise OnboardingError(
            "machine-config-verification-failed",
            "Persisted machine configuration does not match the prepared Vault",
        )
    return {
        "version": 1,
        "status": "configured",
        "cloned": cloned,
        "vault": str(vault),
        "machine_config": str(machine_config_path()),
        "bootstrap": bootstrap,
        "config": verified,
    }


def _print_json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ),
        file=stream,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clone/validate, bootstrap, and configure one DailyPaper machine."
    )
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--zotero-db", type=Path)
    parser.add_argument("--zotero-storage", type=Path)
    args = parser.parse_args()
    try:
        result = onboard_machine(
            args.vault,
            zotero_database=args.zotero_db,
            zotero_storage=args.zotero_storage,
        )
    except OnboardingError as exc:
        _print_json(
            {
                "version": 1,
                "status": "blocked",
                "code": exc.code,
                "message": str(exc),
            },
            stream=sys.stderr,
        )
        return 2
    except MachineConfigError as exc:
        _print_json(
            {
                "version": 1,
                "status": "blocked",
                "code": "invalid-machine-config",
                "message": str(exc),
            },
            stream=sys.stderr,
        )
        return 2
    except CoordinationError as exc:
        _print_json(
            {
                "version": 1,
                "status": "blocked",
                "code": exc.code,
                "message": str(exc),
            },
            stream=sys.stderr,
        )
        return exc.exit_code
    _print_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
