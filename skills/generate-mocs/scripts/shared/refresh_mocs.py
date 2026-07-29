#!/usr/bin/env python3
"""Refresh paper and concept MOCs through one deterministic interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


_SHARED_DIR = Path(__file__).resolve().parent
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from moc_builder import (
    MOCApplyError,
    MOCConflictError,
    apply_moc_plans,
    plan_tree_mocs,
)
from safe_git import SafeGitError, repository_dirty_paths
from user_config import (
    obsidian_vault_path,
    paper_notes_dir,
    paths_config,
)


SCOPES = frozenset({"all", "concepts", "papers"})


def _dirty_paths(vault: Path) -> set[str]:
    try:
        return repository_dirty_paths(vault)
    except SafeGitError as exc:
        raise MOCConflictError(
            f"Could not inspect dirty Vault paths: {exc}"
        ) from exc


def refresh_mocs(
    scope: str = "all",
    *,
    vault_root: Path | None = None,
    notes_root: Path | None = None,
    concepts_root: Path | None = None,
    concepts_folder_name: str | None = None,
    protect_dirty: bool = False,
) -> dict[str, Any]:
    """Refresh the requested MOC trees and return one combined summary."""
    if not isinstance(scope, str) or scope not in SCOPES:
        raise ValueError(f"scope must be one of {sorted(SCOPES)}, got {scope!r}")

    vault = (vault_root or obsidian_vault_path()).expanduser().resolve()
    notes = (notes_root or paper_notes_dir()).expanduser().resolve()
    concept_folder = (
        concepts_folder_name
        or (Path(concepts_root).name if concepts_root is not None else None)
        or str(paths_config()["concepts_folder"])
    )
    if (
        not concept_folder
        or Path(concept_folder).is_absolute()
        or Path(concept_folder).name != concept_folder
        or concept_folder in {".", ".."}
    ):
        raise ValueError("concepts_folder must be one relative directory name")
    concepts = (concepts_root or (notes / concept_folder)).expanduser().resolve()
    for label, root in (("notes_root", notes), ("concepts_root", concepts)):
        try:
            root.relative_to(vault)
        except ValueError as exc:
            raise ValueError(f"{label} must be inside vault_root") from exc

    summaries: dict[str, dict[str, Any]] = {}
    changed_paths: list[str] = []
    protected_paths = _dirty_paths(vault) if protect_dirty else set()
    planned: list[tuple[str, Any]] = []

    if scope in {"all", "concepts"}:
        concept_plan = plan_tree_mocs(
            vault_root=vault,
            root_dir=concepts,
            title_prefix="概念目录页",
            intro="用于浏览概念笔记和对应分类入口。",
            protected_paths=protected_paths,
        )
        planned.append(("concepts", concept_plan))

    if scope in {"all", "papers"}:
        paper_plan = plan_tree_mocs(
            vault_root=vault,
            root_dir=notes,
            title_prefix="论文目录页",
            intro="用于浏览论文笔记、分类目录和子主题入口。",
            exclude_dir_names={concept_folder},
            protected_paths=protected_paths,
        )
        planned.append(("papers", paper_plan))

    applied = apply_moc_plans(plan for _name, plan in planned)
    for (name, _plan), summary in zip(planned, applied, strict=True):
        summaries[name] = summary.to_dict()
        changed_paths.extend(summary.changed_paths)

    return {
        "version": 1,
        "scope": scope,
        "summaries": summaries,
        "changed_paths": list(dict.fromkeys(changed_paths)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh deterministic DailyPaper MOC pages."
    )
    parser.add_argument(
        "--scope",
        choices=sorted(SCOPES),
        default="all",
        help="MOC tree to refresh (default: all).",
    )
    parser.add_argument(
        "--protect-dirty",
        action="store_true",
        help="Refuse to replace a MOC path that was already dirty.",
    )
    parser.add_argument("--vault-root", type=Path)
    parser.add_argument("--notes-root", type=Path)
    parser.add_argument("--concepts-root", type=Path)
    parser.add_argument("--concepts-folder")
    args = parser.parse_args()
    explicit_values = (
        args.vault_root,
        args.notes_root,
        args.concepts_root,
    )
    if any(value is not None for value in explicit_values) and not all(
        value is not None for value in explicit_values
    ):
        print(
            json.dumps(
                {
                    "version": 1,
                    "status": "blocked",
                    "code": "incomplete-runtime-paths",
                    "message": (
                        "--vault-root, --notes-root and --concepts-root "
                        "must be provided together"
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        result = refresh_mocs(
            args.scope,
            vault_root=args.vault_root,
            notes_root=args.notes_root,
            concepts_root=args.concepts_root,
            concepts_folder_name=args.concepts_folder,
            protect_dirty=args.protect_dirty,
        )
    except (MOCConflictError, OSError, ValueError) as exc:
        partial = (
            list(exc.changed_paths)
            if isinstance(exc, MOCApplyError)
            else []
        )
        print(
            json.dumps(
                {
                    "version": 1,
                    "status": "blocked",
                    "code": "moc-conflict",
                    "message": str(exc),
                    "changed_paths": partial,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
