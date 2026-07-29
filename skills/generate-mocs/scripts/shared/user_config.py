#!/usr/bin/env python3

from __future__ import annotations

import copy
import os
from functools import lru_cache
from pathlib import Path

import config_schema
from machine_config import load_machine_config
from safe_io import anchored_file_path


DEFAULT_CONFIG = {
    "paths": {
        "obsidian_vault": ".",
        "paper_notes_folder": "论文笔记",
        "daily_papers_folder": "DailyPapers",
        "concepts_folder": "_概念",
        "inbox_folder": "_待整理",
        "zotero_db": "~/Zotero/zotero.sqlite",
        "zotero_storage": "~/Zotero/storage",
    },
    "runtime": {
        "timezone": "Asia/Shanghai",
    },
    "repository": {
        "url": "git@github.com:haoz0206/dailypaper-vault.git",
        "remote": "origin",
        "branch": "main",
        "task_state_file": ".dailypaper/tasks/daily-papers.json",
        "pull_before_run": True,
        "require_clean": True,
        "coordination_enabled": True,
        "lease_hours": 24,
        "same_day_policy": "skip",
    },
    "daily_papers": {
        "keywords": [
            "world model",
            "diffusion model",
            "embodied ai",
            "3d gaussian splatting",
            "4d gaussian splatting",
            "sim-to-real",
            "sim2real",
            "robot simulation",
        ],
        "negative_keywords": [
            "medical imaging",
            "weather forecast",
            "climate",
            "pet restoration",
            "mri",
            "ct scan",
            "pathology",
            "diagnosis",
            "protein",
            "drug discovery",
            "molecular",
            "audio generation",
            "music generation",
            "speech synthesis",
            "text-to-speech",
            "speech recognition",
            "voice cloning",
            "coding agent",
            "code agent",
            "code generation",
            "software engineering agent",
            "gui agent",
            "computer use",
            "web agent",
            "browser agent",
            "document parsing",
            "document understanding",
            "ocr",
            "rag framework",
            "retrieval augmented",
            "retrieval-augmented",
            "llm memory",
            "long-term memory for llm",
            "text-to-sql",
            "code repair",
            "code review",
            "trading",
            "financial",
        ],
        "domain_boost_keywords": [
            "robot",
            "manipulation",
            "grasping",
            "locomotion",
            "navigation",
            "planning",
            "reinforcement learning",
            "policy learning",
            "visuomotor",
            "action prediction",
        ],
        "arxiv_categories": ["cs.RO", "cs.CV", "cs.AI", "cs.LG"],
        "min_score": 2,
        "top_n": 30,
    },
    "automation": {
        "auto_refresh_indexes": True,
        "git_commit": False,
        "git_push": False,
    },
}


@lru_cache(maxsize=1)
def _load_user_config() -> dict:
    config_dir = Path(__file__).resolve().parent
    bundled = config_schema.load_json_object(
        config_dir / "user-config.json",
        label="Bundled configuration",
    )
    config_schema.validate_effective_config(bundled, DEFAULT_CONFIG)
    overlays: list[dict] = []

    machine = load_machine_config(required=False)
    external_config = os.environ.get("DAILYPAPER_CONFIG")
    if external_config:
        config_path = anchored_file_path(
            Path(external_config),
            label="Shared Vault configuration",
        )
    else:
        configured_vault = _configured_vault_override(machine)
        config_path = (
            configured_vault / ".dailypaper" / "config.json"
            if configured_vault is not None
            else None
        )
    if config_path is not None and config_path.exists():
        overlays.append(
            config_schema.load_json_object(
                config_path,
                label="Shared Vault configuration",
            )
        )

    config = config_schema.merge_validated_overlays(
        bundled,
        overlays,
        DEFAULT_CONFIG,
    )
    machine_zotero = machine.get("zotero", {})
    if isinstance(machine_zotero, dict):
        if "database_path" in machine_zotero:
            config["paths"]["zotero_db"] = machine_zotero["database_path"]
        if "storage_path" in machine_zotero:
            config["paths"]["zotero_storage"] = machine_zotero["storage_path"]
    config_schema.validate_effective_config(config, DEFAULT_CONFIG)
    return config


def load_user_config() -> dict:
    """Return an isolated copy of the validated effective configuration."""
    return copy.deepcopy(_load_user_config())


def _configured_vault_override(
    machine: dict | None = None,
) -> Path | None:
    explicit_vault = os.environ.get("DAILYPAPER_VAULT")
    if explicit_vault:
        return _expand(explicit_vault)
    machine_config = (
        machine if machine is not None else load_machine_config(required=False)
    )
    machine_vault = machine_config.get("vault_path")
    if isinstance(machine_vault, str) and machine_vault:
        return Path(machine_vault).expanduser().resolve()
    return None


def _find_workspace_root() -> Path:
    explicit_root = os.environ.get("DAILYPAPER_WORKSPACE")
    if explicit_root:
        return Path(explicit_root).expanduser().resolve()

    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def _expand(path_value: str, *, relative_to: Path | None = None) -> Path:
    expanded = Path(path_value).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return ((relative_to or _find_workspace_root()) / expanded).resolve()


def paths_config() -> dict:
    return load_user_config()["paths"]


def daily_papers_config() -> dict:
    return load_user_config()["daily_papers"]


def automation_config() -> dict:
    return load_user_config()["automation"]


def repository_config() -> dict:
    return load_user_config()["repository"]


def runtime_config() -> dict:
    return load_user_config()["runtime"]


def obsidian_vault_path() -> Path:
    configured = _configured_vault_override()
    if configured is not None:
        return configured
    return _expand(paths_config()["obsidian_vault"])


def shared_config_path() -> Path:
    explicit = os.environ.get("DAILYPAPER_CONFIG")
    if explicit:
        return anchored_file_path(
            Path(explicit),
            label="Shared Vault configuration",
        )
    return obsidian_vault_path() / ".dailypaper" / "config.json"


def paper_notes_dir() -> Path:
    return obsidian_vault_path() / paths_config()["paper_notes_folder"]


def daily_papers_dir() -> Path:
    return obsidian_vault_path() / paths_config()["daily_papers_folder"]


def concepts_dir() -> Path:
    return paper_notes_dir() / paths_config()["concepts_folder"]


def paper_inbox_dir() -> Path:
    return paper_notes_dir() / paths_config().get("inbox_folder", "_待整理")


def zotero_db_path() -> Path:
    return _expand(paths_config()["zotero_db"])


def zotero_storage_dir() -> Path:
    return _expand(paths_config()["zotero_storage"])


def timezone_name() -> str:
    return str(runtime_config().get("timezone", "Asia/Shanghai"))


def clear_config_cache() -> None:
    """Clear cached configuration after tests or environment changes."""
    _load_user_config.cache_clear()


def auto_refresh_indexes_enabled() -> bool:
    return bool(automation_config()["auto_refresh_indexes"])


def git_commit_enabled() -> bool:
    return bool(automation_config()["git_commit"])


def git_push_enabled() -> bool:
    return bool(automation_config()["git_push"])
