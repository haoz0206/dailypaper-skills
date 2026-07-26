#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import os
from functools import lru_cache
from pathlib import Path


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


def _deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


@lru_cache(maxsize=1)
def load_user_config() -> dict:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config_dir = Path(__file__).resolve().parent

    for filename in ("user-config.json", "user-config.local.json"):
        config_path = config_dir / filename
        if not config_path.exists():
            continue
        with config_path.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            _deep_merge(config, loaded)

    external_config = os.environ.get("DAILYPAPER_CONFIG")
    if external_config:
        config_path = Path(external_config).expanduser().resolve()
        with config_path.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            _deep_merge(config, loaded)

    return config


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
    config = load_user_config()["automation"]
    if config.get("git_push") and not config.get("git_commit"):
        config = copy.deepcopy(config)
        config["git_push"] = False
    return config


def runtime_config() -> dict:
    return load_user_config()["runtime"]


def obsidian_vault_path() -> Path:
    explicit_vault = os.environ.get("DAILYPAPER_VAULT")
    if explicit_vault:
        return _expand(explicit_vault)
    return _expand(paths_config()["obsidian_vault"])


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
    load_user_config.cache_clear()


def auto_refresh_indexes_enabled() -> bool:
    return bool(automation_config()["auto_refresh_indexes"])


def git_commit_enabled() -> bool:
    return bool(automation_config()["git_commit"])


def git_push_enabled() -> bool:
    return bool(automation_config()["git_push"])
