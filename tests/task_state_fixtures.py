from __future__ import annotations

from typing import Any


def make_task_state(
    status: str = "running",
    *,
    run_id: str = "2026-07-29-test",
    harness: str = "codex",
    owner: str = "test-host",
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "version": 1,
        "task": "daily-papers",
        "target_date": "2026-07-29",
        "window_days": 1,
        "status": status,
        "run_id": run_id,
        "harness": harness,
        "owner": owner,
        "started_at": "2026-07-29T08:00:00+08:00",
        "updated_at": "2026-07-29T09:00:00+08:00",
        "base_commit": "a" * 40,
        "config_sha256": "b" * 64,
        "outputs": {
            "daily_note": "DailyPapers/2026-07-29-论文推荐.md",
        },
    }
    if status == "running":
        state["lease_until"] = "2026-07-30T08:00:00+08:00"
    elif status in {"success", "published"}:
        state["completed_at"] = "2026-07-29T09:00:00+08:00"
        state["changed_paths"] = [
            "DailyPapers/2026-07-29-论文推荐.md",
        ]
    elif status == "failed":
        state["failed_at"] = "2026-07-29T09:00:00+08:00"
        state["message"] = "deterministic failure"
    elif status == "cancelled":
        state["cancelled_at"] = "2026-07-29T09:00:00+08:00"
    else:
        raise ValueError(f"Unsupported fixture status: {status}")
    return state
