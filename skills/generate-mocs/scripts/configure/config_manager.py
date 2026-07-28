#!/usr/bin/env python3
"""Safely inspect and update the shared DailyPaper Vault configuration."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[2]
SHARED_DIR = SKILL_ROOT / "scripts" / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

from user_config import DEFAULT_CONFIG


TASK_STATE_RELATIVE = Path(".dailypaper/tasks/daily-papers.json")
CONFIG_RELATIVE = Path(".dailypaper/config.json")
EDITABLE_DAILY_FIELDS = {
    "keywords",
    "negative_keywords",
    "domain_boost_keywords",
    "arxiv_categories",
    "min_score",
    "top_n",
}
EDITABLE_AUTOMATION_FIELDS = {"auto_refresh_indexes"}
ALLOWED_TOP_LEVEL_FIELDS = {
    "paths",
    "runtime",
    "repository",
    "daily_papers",
    "automation",
}
SHARED_PATH_FIELDS = {
    "obsidian_vault",
    "paper_notes_folder",
    "daily_papers_folder",
    "concepts_folder",
    "inbox_folder",
}
AUTOMATION_FIELDS = {"auto_refresh_indexes", "git_commit", "git_push"}
KEYWORD_FIELDS = {
    "keywords",
    "negative_keywords",
    "domain_boost_keywords",
}
CATEGORY_PATTERN = re.compile(r"^[a-z][a-z0-9-]*(?:\.[A-Za-z0-9-]+)?$")
FIXED_REPOSITORY_FIELDS = {
    "url",
    "remote",
    "branch",
    "task_state_file",
    "pull_before_run",
    "require_clean",
    "coordination_enabled",
    "same_day_policy",
}
REPOSITORY_FIELDS = FIXED_REPOSITORY_FIELDS | {"lease_hours"}


class ConfigError(ValueError):
    """The requested configuration is invalid or unsafe."""


class ActiveRunError(ConfigError):
    """A daily run currently owns the shared Vault."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def _load_json(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"Configuration file does not exist: {path}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"Expected a JSON object in {path}")
    return value


def resolve_config_path(vault: Path, configured: Path | None = None) -> Path:
    vault = vault.expanduser().resolve()
    expected = (vault / CONFIG_RELATIVE).resolve()
    if configured is not None and configured.expanduser().resolve() != expected:
        raise ConfigError(
            f"Shared configuration must be {expected}, not "
            f"{configured.expanduser().resolve()}"
        )
    return expected


def _base_config() -> dict[str, Any]:
    effective = copy.deepcopy(DEFAULT_CONFIG)
    tracked = _load_json(SHARED_DIR / "user-config.json", required=False)
    _deep_merge(effective, tracked)
    return effective


def load_effective_config(config_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    external = _load_json(config_path)
    effective = _base_config()
    _deep_merge(effective, external)
    return effective, external


def _normalize_string_list(
    value: Any,
    *,
    field: str,
    lowercase: bool,
    allow_empty: bool,
) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError(f"daily_papers.{field} must be an array")

    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(
                f"daily_papers.{field} must contain non-empty strings"
            )
        normalized = item.strip().lower() if lowercase else item.strip()
        dedup_key = normalized.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        result.append(normalized)

    if not allow_empty and not result:
        raise ConfigError(f"daily_papers.{field} must not be empty")
    return result


def normalize_daily_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("daily_papers must be an object")
    unknown = set(value) - EDITABLE_DAILY_FIELDS
    if unknown:
        raise ConfigError(
            "Unsupported daily_papers fields: " + ", ".join(sorted(unknown))
        )

    missing = EDITABLE_DAILY_FIELDS - set(value)
    if missing:
        raise ConfigError(
            "Effective daily_papers configuration is missing: "
            + ", ".join(sorted(missing))
        )

    normalized = copy.deepcopy(value)
    for field in KEYWORD_FIELDS:
        normalized[field] = _normalize_string_list(
            value[field],
            field=field,
            lowercase=True,
            allow_empty=True,
        )
    normalized["arxiv_categories"] = _normalize_string_list(
        value["arxiv_categories"],
        field="arxiv_categories",
        lowercase=False,
        allow_empty=False,
    )
    for category in normalized["arxiv_categories"]:
        if not CATEGORY_PATTERN.fullmatch(category):
            raise ConfigError(f"Invalid arXiv category: {category}")

    for field, upper_bound in (("min_score", 100), ("top_n", 200)):
        number = value[field]
        if isinstance(number, bool) or not isinstance(number, int):
            raise ConfigError(f"daily_papers.{field} must be an integer")
        minimum = 0 if field == "min_score" else 1
        if not minimum <= number <= upper_bound:
            raise ConfigError(
                f"daily_papers.{field} must be between {minimum} and {upper_bound}"
            )
        normalized[field] = number

    if not normalized["keywords"] and not normalized["domain_boost_keywords"]:
        raise ConfigError(
            "At least one positive keyword or domain boost keyword is required"
        )

    positive = set(normalized["keywords"]) | set(
        normalized["domain_boost_keywords"]
    )
    negative = set(normalized["negative_keywords"])
    conflicts = positive & negative
    if conflicts:
        raise ConfigError(
            "Positive and negative keyword lists conflict: "
            + ", ".join(sorted(conflicts))
        )
    return normalized


def _validate_relative_folder(value: Any, *, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"paths.{field} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"paths.{field} must be a safe relative path")


def _validate_external_safety(external: dict[str, Any]) -> None:
    unknown_top_level = set(external) - ALLOWED_TOP_LEVEL_FIELDS
    if unknown_top_level:
        raise ConfigError(
            "Unsupported shared configuration sections: "
            + ", ".join(sorted(unknown_top_level))
        )

    paths = external.get("paths", {})
    if not isinstance(paths, dict):
        raise ConfigError("paths must be an object")
    unknown_paths = set(paths) - SHARED_PATH_FIELDS
    if unknown_paths:
        raise ConfigError(
            "Unsupported shared paths fields: "
            + ", ".join(sorted(unknown_paths))
        )
    if "obsidian_vault" in paths and paths["obsidian_vault"] != ".":
        raise ConfigError(
            "Shared paths.obsidian_vault must remain '.'; use "
            "DAILYPAPER_VAULT for the per-machine absolute path"
        )
    for field in SHARED_PATH_FIELDS - {"obsidian_vault"}:
        if field in paths:
            _validate_relative_folder(paths[field], field=field)

    runtime = external.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ConfigError("runtime must be an object")
    unknown_runtime = set(runtime) - {"timezone"}
    if unknown_runtime:
        raise ConfigError(
            "Unsupported shared runtime fields: "
            + ", ".join(sorted(unknown_runtime))
        )
    if (
        "timezone" in runtime
        and runtime["timezone"] != DEFAULT_CONFIG["runtime"]["timezone"]
    ):
        raise ConfigError(
            "Shared runtime.timezone must remain "
            f"{DEFAULT_CONFIG['runtime']['timezone']!r}"
        )

    repository = external.get("repository", {})
    if not isinstance(repository, dict):
        raise ConfigError("repository must be an object")
    unknown_repository = set(repository) - REPOSITORY_FIELDS
    if unknown_repository:
        raise ConfigError(
            "Unsupported shared repository fields: "
            + ", ".join(sorted(unknown_repository))
        )
    expected = DEFAULT_CONFIG["repository"]
    for field in FIXED_REPOSITORY_FIELDS:
        if field in repository and repository[field] != expected[field]:
            raise ConfigError(
                f"Shared repository.{field} must remain {expected[field]!r}"
            )
    if "lease_hours" in repository:
        lease_hours = repository["lease_hours"]
        if (
            isinstance(lease_hours, bool)
            or not isinstance(lease_hours, int)
            or lease_hours < 1
            or lease_hours > 168
        ):
            raise ConfigError(
                "Shared repository.lease_hours must be an integer from 1 to 168"
            )

    daily = external.get("daily_papers", {})
    if not isinstance(daily, dict):
        raise ConfigError("daily_papers must be an object")
    unknown_daily = set(daily) - EDITABLE_DAILY_FIELDS
    if unknown_daily:
        raise ConfigError(
            "Unsupported daily_papers fields: "
            + ", ".join(sorted(unknown_daily))
        )

    automation = external.get("automation", {})
    if not isinstance(automation, dict):
        raise ConfigError("automation must be an object")
    unknown_automation = set(automation) - AUTOMATION_FIELDS
    if unknown_automation:
        raise ConfigError(
            "Unsupported automation fields: "
            + ", ".join(sorted(unknown_automation))
        )
    for field, value in automation.items():
        if not isinstance(value, bool):
            raise ConfigError(f"automation.{field} must be a boolean")
    effective_automation = copy.deepcopy(DEFAULT_CONFIG["automation"])
    effective_automation.update(automation)
    if effective_automation["git_push"] and not effective_automation["git_commit"]:
        raise ConfigError("automation.git_push requires automation.git_commit")


def validate_config(config_path: Path) -> dict[str, Any]:
    effective, external = load_effective_config(config_path)
    _validate_external_safety(external)
    normalized = normalize_daily_config(effective.get("daily_papers"))
    if normalized != effective["daily_papers"]:
        raise ConfigError(
            "daily_papers contains duplicate, untrimmed, or uppercase keywords; "
            "apply a normalized patch before running"
        )
    automation = effective.get("automation")
    if not isinstance(automation, dict):
        raise ConfigError("automation must be an object")
    unknown_automation = set(automation) - AUTOMATION_FIELDS
    if unknown_automation:
        raise ConfigError(
            "Unsupported effective automation fields: "
            + ", ".join(sorted(unknown_automation))
        )
    for field in AUTOMATION_FIELDS:
        if not isinstance(automation.get(field), bool):
            raise ConfigError(f"automation.{field} must be a boolean")
    if automation["git_push"] and not automation["git_commit"]:
        raise ConfigError("automation.git_push requires automation.git_commit")
    return effective


def _validate_patch(patch: dict[str, Any]) -> None:
    unknown_sections = set(patch) - {"daily_papers", "automation"}
    if unknown_sections:
        raise ConfigError(
            "Unsupported configuration sections: "
            + ", ".join(sorted(unknown_sections))
        )
    if not patch:
        raise ConfigError("Patch must not be empty")

    if "daily_papers" in patch:
        daily_patch = patch["daily_papers"]
        if not isinstance(daily_patch, dict):
            raise ConfigError("Patch daily_papers must be an object")
        unknown = set(daily_patch) - EDITABLE_DAILY_FIELDS
        if unknown:
            raise ConfigError(
                "Unsupported daily_papers patch fields: "
                + ", ".join(sorted(unknown))
            )
        if not daily_patch:
            raise ConfigError("Patch daily_papers must not be empty")

    if "automation" in patch:
        automation_patch = patch["automation"]
        if not isinstance(automation_patch, dict):
            raise ConfigError("Patch automation must be an object")
        unknown = set(automation_patch) - EDITABLE_AUTOMATION_FIELDS
        if unknown:
            raise ConfigError(
                "Unsupported automation patch fields: "
                + ", ".join(sorted(unknown))
            )
        if not automation_patch:
            raise ConfigError("Patch automation must not be empty")
        if not isinstance(automation_patch.get("auto_refresh_indexes"), bool):
            raise ConfigError(
                "automation.auto_refresh_indexes patch must be a boolean"
            )


def _changes(before: Any, after: Any, prefix: str = "") -> list[dict[str, Any]]:
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            path = f"{prefix}.{key}" if prefix else key
            changes.extend(_changes(before.get(key), after.get(key), path))
        return changes
    if before == after:
        return []
    return [{"path": prefix, "before": before, "after": after}]


def build_plan(config_path: Path, patch_path: Path) -> dict[str, Any]:
    effective, external = load_effective_config(config_path)
    patch = _load_json(patch_path)
    _validate_patch(patch)
    if not isinstance(effective.get("daily_papers"), dict):
        raise ConfigError("Existing effective daily_papers must be an object")
    if not isinstance(effective.get("automation"), dict):
        raise ConfigError("Existing effective automation must be an object")
    if not isinstance(
        effective["automation"].get("auto_refresh_indexes"),
        bool,
    ):
        raise ConfigError(
            "Existing automation.auto_refresh_indexes must be a boolean"
        )

    proposed_external = copy.deepcopy(external)

    if "daily_papers" in patch:
        daily = copy.deepcopy(effective["daily_papers"])
        daily.update(copy.deepcopy(patch["daily_papers"]))
        normalized_daily = normalize_daily_config(daily)
        proposed_external["daily_papers"] = normalized_daily

    if "automation" in patch:
        auto_refresh = patch["automation"]["auto_refresh_indexes"]
        external_automation = proposed_external.setdefault("automation", {})
        if not isinstance(external_automation, dict):
            raise ConfigError("Existing external automation must be an object")
        external_automation["auto_refresh_indexes"] = auto_refresh

    _validate_external_safety(proposed_external)
    proposed_effective = _base_config()
    _deep_merge(proposed_effective, proposed_external)
    normalize_daily_config(proposed_effective["daily_papers"])
    changes = _changes(
        {
            "daily_papers": effective["daily_papers"],
            "automation": {
                "auto_refresh_indexes": effective["automation"][
                    "auto_refresh_indexes"
                ]
            },
        },
        {
            "daily_papers": proposed_effective["daily_papers"],
            "automation": {
                "auto_refresh_indexes": proposed_effective["automation"][
                    "auto_refresh_indexes"
                ]
            },
        },
    )
    if not changes:
        raise ConfigError("Patch produces no effective configuration changes")

    return {
        "config_path": str(config_path),
        "changes": changes,
        "proposed": proposed_external,
    }


def apply_plan(vault: Path, config_path: Path, patch_path: Path) -> dict[str, Any]:
    vault = vault.expanduser().resolve()
    config_path = resolve_config_path(vault, config_path)
    plan = build_plan(config_path, patch_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=".config.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(plan["proposed"], temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        guard_active_run(vault)
        temporary_path.replace(config_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

    validate_config(config_path)
    return {
        "status": "applied",
        "config_path": str(config_path),
        "changes": plan["changes"],
    }


def guard_active_run(vault: Path) -> dict[str, Any]:
    state_path = vault.expanduser().resolve() / TASK_STATE_RELATIVE
    if not state_path.exists():
        return {"status": "safe", "task_state": "absent"}
    state = _load_json(state_path)
    if state.get("version") != 1:
        raise ConfigError(f"Unsupported task state version in {state_path}")
    if state.get("task") not in (None, "daily-papers"):
        raise ConfigError(f"Unexpected task state in {state_path}")
    if state.get("status") == "running":
        raise ActiveRunError(
            "DailyPaper run is active: "
            f"{state.get('harness', 'unknown')}/{state.get('owner', 'unknown')} "
            f"({state.get('run_id', 'unknown')}) until "
            f"{state.get('lease_until', 'unknown')}"
        )
    if state.get("status") not in {"success", "failed", "cancelled"}:
        raise ConfigError(
            f"Unexpected task status {state.get('status')!r} in {state_path}"
        )
    return {
        "status": "safe",
        "task_state": state.get("status", "unknown"),
        "run_id": state.get("run_id"),
    }


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("guard")
    subparsers.add_parser("show")
    subparsers.add_parser("validate")
    for command in ("plan", "apply"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--patch", type=Path, required=True)

    args = parser.parse_args()
    vault = args.vault.expanduser().resolve()
    config_path = resolve_config_path(vault, args.config)

    try:
        if args.command == "guard":
            _print_json(guard_active_run(vault))
        elif args.command == "show":
            effective = validate_config(config_path)
            _print_json(
                {
                    "config_path": str(config_path),
                    "daily_papers": effective["daily_papers"],
                    "automation": {
                        "auto_refresh_indexes": effective["automation"][
                            "auto_refresh_indexes"
                        ]
                    },
                }
            )
        elif args.command == "validate":
            validate_config(config_path)
            _print_json({"status": "valid", "config_path": str(config_path)})
        elif args.command == "plan":
            _print_json(build_plan(config_path, args.patch))
        elif args.command == "apply":
            _print_json(apply_plan(vault, config_path, args.patch))
    except ActiveRunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
