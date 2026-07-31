#!/usr/bin/env python3
"""Single schema and normalization interface for DailyPaper configuration."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from safe_io import (
    SafeIOError,
    load_json_object as load_safe_json_object,
    parse_json_object as parse_safe_json_object,
)
from safe_path import SafePathError, relative_posix_path


MAX_CONFIG_BYTES = 1024 * 1024
SHARED_CONFIG_VERSION = 1
TOP_LEVEL_FIELDS = frozenset(
    {"paths", "runtime", "repository", "daily_papers", "automation"}
)
SHARED_DOCUMENT_FIELDS = TOP_LEVEL_FIELDS | {"schema_version"}
PATH_FIELDS = frozenset(
    {
        "obsidian_vault",
        "paper_notes_folder",
        "daily_papers_folder",
        "concepts_folder",
        "inbox_folder",
        "zotero_db",
        "zotero_storage",
    }
)
SHARED_PATH_FIELDS = frozenset(
    {
        "obsidian_vault",
        "paper_notes_folder",
        "daily_papers_folder",
        "concepts_folder",
        "inbox_folder",
    }
)
FOLDER_FIELDS = frozenset(
    {
        "paper_notes_folder",
        "daily_papers_folder",
        "concepts_folder",
        "inbox_folder",
    }
)
DAILY_FIELDS = frozenset(
    {
        "keywords",
        "negative_keywords",
        "domain_boost_keywords",
        "arxiv_categories",
        "min_score",
        "top_n",
    }
)
KEYWORD_FIELDS = frozenset(
    {"keywords", "negative_keywords", "domain_boost_keywords"}
)
AUTOMATION_FIELDS = frozenset(
    {"auto_refresh_indexes", "git_commit", "git_push"}
)
FIXED_REPOSITORY_FIELDS = frozenset(
    {
        "url",
        "remote",
        "branch",
        "task_state_file",
        "pull_before_run",
        "require_clean",
        "coordination_enabled",
        "same_day_policy",
    }
)
REPOSITORY_FIELDS = FIXED_REPOSITORY_FIELDS | {"lease_hours"}
CATEGORY_PATTERN = re.compile(r"^[a-z][a-z0-9-]*(?:\.[A-Za-z0-9-]+)?$")
RESERVED_VAULT_ROOTS = frozenset({".git", ".dailypaper"})


class ConfigurationError(ValueError):
    """Configuration data is malformed, unsupported, or unsafe."""


class ConfigurationMigrationRequired(ConfigurationError):
    """A legacy shared configuration must be explicitly migrated."""


def load_json_object(
    path: Path,
    *,
    required: bool = True,
    label: str = "Configuration",
) -> dict[str, Any]:
    """Race-safely load one bounded regular UTF-8 JSON object."""
    try:
        value = load_safe_json_object(
            path,
            max_bytes=MAX_CONFIG_BYTES,
            required=required,
            label=label,
        )
    except SafeIOError as exc:
        raise ConfigurationError(str(exc)) from exc
    return {} if value is None else value


def parse_json_object(
    raw: bytes,
    *,
    label: str = "Configuration",
) -> dict[str, Any]:
    """Parse one bounded strict UTF-8 JSON object from immutable bytes."""
    try:
        return parse_safe_json_object(
            raw,
            max_bytes=MAX_CONFIG_BYTES,
            label=label,
        )
    except SafeIOError as exc:
        raise ConfigurationError(str(exc)) from exc


def deep_merge(
    base: dict[str, Any],
    override: dict[str, Any],
) -> dict[str, Any]:
    """Merge a validated overlay into base without retaining caller references."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def _require_exact_fields(
    value: Any,
    expected: frozenset[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be a JSON object")
    actual = set(value)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise ConfigurationError(
            f"{label} is missing required fields: " + ", ".join(sorted(missing))
        )
    if unknown:
        raise ConfigurationError(
            f"{label} contains unsupported fields: " + ", ".join(sorted(unknown))
        )
    return value


def _validate_relative_folder(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(
            f"{label} must be a non-empty normalized safe relative POSIX path"
        )
    try:
        pure = relative_posix_path(value, label=label)
    except SafePathError as exc:
        raise ConfigurationError(
            f"{label} must be a non-empty normalized safe relative POSIX path"
        ) from exc
    if pure.parts[0] in RESERVED_VAULT_ROOTS:
        raise ConfigurationError(
            f"{label} must be a non-empty normalized safe relative POSIX path"
        )
    return pure.as_posix()


def _paths_overlap(first: PurePosixPath, second: PurePosixPath) -> bool:
    return first == second or first in second.parents or second in first.parents


def _validate_folder_relationships(paths: dict[str, Any]) -> None:
    notes = PurePosixPath(str(paths["paper_notes_folder"]))
    daily = PurePosixPath(str(paths["daily_papers_folder"]))
    concepts = PurePosixPath(str(paths["concepts_folder"]))
    inbox = PurePosixPath(str(paths["inbox_folder"]))
    if _paths_overlap(notes, daily):
        raise ConfigurationError(
            "paths.paper_notes_folder and paths.daily_papers_folder must not overlap"
        )
    if _paths_overlap(concepts, inbox):
        raise ConfigurationError(
            "paths.concepts_folder and paths.inbox_folder must not overlap"
        )


def _normalize_string_list(
    value: Any,
    label: str,
    *,
    allow_empty: bool,
    lowercase: bool,
) -> list[str]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{label} must be a JSON array")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigurationError(f"{label} must contain non-empty strings")
        cleaned = item.strip().lower() if lowercase else item.strip()
        if len(cleaned) > 256:
            raise ConfigurationError(f"{label} entries must not exceed 256 characters")
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(cleaned)
    if len(normalized) > 500:
        raise ConfigurationError(f"{label} must not contain more than 500 entries")
    if not allow_empty and not normalized:
        raise ConfigurationError(f"{label} must not be empty")
    return normalized


def normalize_daily_config(value: Any) -> dict[str, Any]:
    """Return the canonical daily-paper research configuration."""
    daily = _require_exact_fields(value, DAILY_FIELDS, "daily_papers")
    normalized = copy.deepcopy(daily)
    for field in KEYWORD_FIELDS:
        normalized[field] = _normalize_string_list(
            daily[field],
            f"daily_papers.{field}",
            allow_empty=True,
            lowercase=True,
        )
    normalized["arxiv_categories"] = _normalize_string_list(
        daily["arxiv_categories"],
        "daily_papers.arxiv_categories",
        allow_empty=False,
        lowercase=False,
    )
    for category in normalized["arxiv_categories"]:
        if not CATEGORY_PATTERN.fullmatch(category):
            raise ConfigurationError(f"Invalid arXiv category: {category}")
    for field, minimum, maximum in (
        ("min_score", 0, 100),
        ("top_n", 1, 200),
    ):
        number = daily[field]
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not minimum <= number <= maximum
        ):
            raise ConfigurationError(
                f"daily_papers.{field} must be an integer from "
                f"{minimum} to {maximum}"
            )
        normalized[field] = number
    if not normalized["keywords"] and not normalized["domain_boost_keywords"]:
        raise ConfigurationError(
            "At least one positive keyword or domain boost keyword is required"
        )
    positive = set(normalized["keywords"]) | set(
        normalized["domain_boost_keywords"]
    )
    conflicts = positive & set(normalized["negative_keywords"])
    if conflicts:
        raise ConfigurationError(
            "Positive and negative keyword lists conflict: "
            + ", ".join(sorted(conflicts))
        )
    return normalized


def validate_effective_config(
    config: Any,
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """Validate and return one complete canonical effective configuration."""
    effective = _require_exact_fields(config, TOP_LEVEL_FIELDS, "configuration")
    paths = _require_exact_fields(effective["paths"], PATH_FIELDS, "paths")
    if paths["obsidian_vault"] != ".":
        raise ConfigurationError(
            "paths.obsidian_vault must remain '.'; configure the machine Vault instead"
        )
    for field in FOLDER_FIELDS:
        _validate_relative_folder(paths[field], f"paths.{field}")
    _validate_folder_relationships(paths)
    for field in ("zotero_db", "zotero_storage"):
        if not isinstance(paths[field], str) or not paths[field].strip():
            raise ConfigurationError(f"paths.{field} must be a non-empty string")

    runtime = _require_exact_fields(
        effective["runtime"],
        frozenset({"timezone"}),
        "runtime",
    )
    timezone = runtime["timezone"]
    if not isinstance(timezone, str) or not timezone:
        raise ConfigurationError("runtime.timezone must be a non-empty string")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"Unknown runtime.timezone: {timezone}") from exc
    expected_timezone = defaults["runtime"]["timezone"]
    if timezone != expected_timezone:
        raise ConfigurationError(
            f"runtime.timezone must remain {expected_timezone!r}"
        )

    repository = _require_exact_fields(
        effective["repository"],
        REPOSITORY_FIELDS,
        "repository",
    )
    expected_repository = defaults["repository"]
    for field in FIXED_REPOSITORY_FIELDS:
        if repository[field] != expected_repository[field]:
            raise ConfigurationError(
                f"repository.{field} must remain {expected_repository[field]!r}"
            )
    lease_hours = repository["lease_hours"]
    if (
        isinstance(lease_hours, bool)
        or not isinstance(lease_hours, int)
        or not 1 <= lease_hours <= 168
    ):
        raise ConfigurationError(
            "repository.lease_hours must be an integer from 1 to 168"
        )

    normalized_daily = normalize_daily_config(effective["daily_papers"])
    if normalized_daily != effective["daily_papers"]:
        raise ConfigurationError(
            "daily_papers contains duplicate, untrimmed, or uppercase keywords"
        )

    automation = _require_exact_fields(
        effective["automation"],
        AUTOMATION_FIELDS,
        "automation",
    )
    for field, value in automation.items():
        if not isinstance(value, bool):
            raise ConfigurationError(f"automation.{field} must be a boolean")
    if automation["git_push"] != automation["git_commit"]:
        raise ConfigurationError(
            "automation.git_commit and automation.git_push must be enabled "
            "or disabled together for recoverable coordinated publication"
        )
    return effective


def validate_overlay(
    overlay: Any,
    base: dict[str, Any],
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """Validate a partial portable overlay and its merged effective result."""
    if not isinstance(overlay, dict):
        raise ConfigurationError("Configuration overlay must be a JSON object")
    unknown_top = set(overlay) - TOP_LEVEL_FIELDS
    if unknown_top:
        raise ConfigurationError(
            "Configuration overlay contains unsupported sections: "
            + ", ".join(sorted(unknown_top))
        )
    paths = overlay.get("paths", {})
    if not isinstance(paths, dict):
        raise ConfigurationError("paths must be a JSON object")
    unknown_paths = set(paths) - SHARED_PATH_FIELDS
    if unknown_paths:
        raise ConfigurationError(
            "Configuration overlay contains unsupported paths fields: "
            + ", ".join(sorted(unknown_paths))
        )
    runtime = overlay.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ConfigurationError("runtime must be a JSON object")
    if set(runtime) - {"timezone"}:
        raise ConfigurationError("Configuration overlay contains unsupported runtime fields")
    repository = overlay.get("repository", {})
    if not isinstance(repository, dict):
        raise ConfigurationError("repository must be a JSON object")
    unknown_repository = set(repository) - REPOSITORY_FIELDS
    if unknown_repository:
        raise ConfigurationError(
            "Configuration overlay contains unsupported repository fields: "
            + ", ".join(sorted(unknown_repository))
        )
    daily = overlay.get("daily_papers", {})
    if not isinstance(daily, dict):
        raise ConfigurationError("daily_papers must be a JSON object")
    unknown_daily = set(daily) - DAILY_FIELDS
    if unknown_daily:
        raise ConfigurationError(
            "Configuration overlay contains unsupported daily_papers fields: "
            + ", ".join(sorted(unknown_daily))
        )
    automation = overlay.get("automation", {})
    if not isinstance(automation, dict):
        raise ConfigurationError("automation must be a JSON object")
    unknown_automation = set(automation) - AUTOMATION_FIELDS
    if unknown_automation:
        raise ConfigurationError(
            "Configuration overlay contains unsupported automation fields: "
            + ", ".join(sorted(unknown_automation))
        )

    candidate = deep_merge(copy.deepcopy(base), overlay)
    validate_effective_config(candidate, defaults)
    return overlay


def shared_config_version(document: Any) -> int:
    """Return 0 for a legacy overlay or the declared shared schema version."""
    if not isinstance(document, dict):
        raise ConfigurationError("Shared Vault configuration must be a JSON object")
    if "schema_version" not in document:
        return 0
    version = document["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigurationError(
            "Shared Vault configuration schema_version must be an integer"
        )
    if version != SHARED_CONFIG_VERSION:
        raise ConfigurationError(
            "Unsupported shared Vault configuration schema_version "
            f"{version}; this Skill supports {SHARED_CONFIG_VERSION}"
        )
    return version


def materialize_shared_config(
    effective: dict[str, Any],
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """Create one complete, versioned, portable user-owned configuration."""
    validate_effective_config(effective, defaults)
    document = {
        "schema_version": SHARED_CONFIG_VERSION,
        "paths": {
            field: copy.deepcopy(effective["paths"][field])
            for field in sorted(SHARED_PATH_FIELDS)
        },
        "runtime": copy.deepcopy(effective["runtime"]),
        "repository": copy.deepcopy(effective["repository"]),
        "daily_papers": copy.deepcopy(effective["daily_papers"]),
        "automation": copy.deepcopy(effective["automation"]),
    }
    validate_shared_config(document, effective, defaults)
    return document


def validate_shared_config(
    document: Any,
    base: dict[str, Any],
    defaults: dict[str, Any],
    *,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    """Validate a shared document and return its portable configuration payload."""
    version = shared_config_version(document)
    if version == 0:
        validate_overlay(document, base, defaults)
        if not allow_legacy:
            raise ConfigurationMigrationRequired(
                "Shared Vault configuration uses the legacy unversioned overlay "
                "format; run configure-dailypaper and explicitly approve migration"
            )
        return copy.deepcopy(document)

    if not isinstance(document, dict):
        raise ConfigurationError("Shared Vault configuration must be a JSON object")
    unknown_top = set(document) - SHARED_DOCUMENT_FIELDS
    if unknown_top:
        raise ConfigurationError(
            "Shared Vault configuration contains unsupported sections: "
            + ", ".join(sorted(unknown_top))
        )
    shared = _require_exact_fields(
        document,
        SHARED_DOCUMENT_FIELDS,
        "Shared Vault configuration",
    )
    if not isinstance(shared["paths"], dict):
        raise ConfigurationError("Shared Vault configuration paths must be an object")
    unknown_paths = set(shared["paths"]) - SHARED_PATH_FIELDS
    if unknown_paths:
        raise ConfigurationError(
            "Shared Vault configuration contains unsupported paths fields: "
            + ", ".join(sorted(unknown_paths))
        )
    _require_exact_fields(
        shared["paths"],
        SHARED_PATH_FIELDS,
        "Shared Vault configuration paths",
    )
    _require_exact_fields(
        shared["runtime"],
        frozenset({"timezone"}),
        "Shared Vault configuration runtime",
    )
    _require_exact_fields(
        shared["repository"],
        REPOSITORY_FIELDS,
        "Shared Vault configuration repository",
    )
    _require_exact_fields(
        shared["daily_papers"],
        DAILY_FIELDS,
        "Shared Vault configuration daily_papers",
    )
    _require_exact_fields(
        shared["automation"],
        AUTOMATION_FIELDS,
        "Shared Vault configuration automation",
    )
    payload = {
        key: copy.deepcopy(value)
        for key, value in shared.items()
        if key != "schema_version"
    }
    candidate = deep_merge(copy.deepcopy(base), payload)
    validate_effective_config(candidate, defaults)
    return payload


def merge_validated_overlays(
    base: dict[str, Any],
    overlays: Iterable[dict[str, Any]],
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """Merge portable overlays in order and validate after every layer."""
    current = copy.deepcopy(base)
    validate_effective_config(current, defaults)
    for overlay in overlays:
        validate_overlay(overlay, current, defaults)
        deep_merge(current, overlay)
    validate_effective_config(current, defaults)
    return current


def configuration_fingerprint(config: dict[str, Any]) -> str:
    """Hash output-affecting portable configuration only."""
    payload = copy.deepcopy(config)
    for machine_local_key in (
        "obsidian_vault",
        "zotero_db",
        "zotero_storage",
    ):
        payload["paths"].pop(machine_local_key, None)
    payload["repository"].pop("remote", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
