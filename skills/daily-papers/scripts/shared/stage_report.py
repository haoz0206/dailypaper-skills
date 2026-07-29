#!/usr/bin/env python3
"""Validate and resolve one DailyPaper stage report.

Stage reports are the stable hand-off between an AI-authored workflow stage and
the deterministic Run Coordinator.  Reports name artifacts by an explicit
``run`` or ``vault`` scope, so their paths never depend on the Harness working
directory.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from run_lifecycle import ArtifactCandidate
from safe_io import (
    SafeIOError,
    anchored_file_path,
    parse_json_object,
    read_regular_bytes,
)
from safe_path import SafePathError, relative_posix_path, resolve_within


REPORT_VERSION = 1
MAX_REPORT_BYTES = 1024 * 1024
RESULTS = frozenset(
    {
        "progress",
        "success",
        "recoverable",
        "attention",
        "deterministic-failure",
    }
)
STAGE_BY_PHASE = {
    "fetching": "fetch",
    "reviewing": "review",
    "writing-notes": "notes",
    "validated": "notes",
    "publishing": "notes",
}
ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
TOP_LEVEL_FIELDS = frozenset(
    {
        "version",
        "stage",
        "result",
        "artifacts",
        "changed_paths",
        "message",
        "retry_at",
        "metadata",
    }
)
ARTIFACT_FIELDS = frozenset({"role", "scope", "path"})


class StageReportError(RuntimeError):
    """A stage report is malformed, unsafe, or for the wrong Run phase."""


@dataclass(frozen=True)
class StageSubmission:
    """Validated coordinator arguments derived from one immutable report read."""

    report_path: Path
    report_sha256: str
    stage: str
    result: str
    message: str | None
    retry_at: str | None
    artifacts: tuple[ArtifactCandidate, ...]
    changed_paths: tuple[str, ...]
    metadata: Mapping[str, Any]

    def verify_unchanged(self) -> None:
        """Reject a report that changed after it was parsed."""
        try:
            current = read_regular_bytes(
                self.report_path,
                max_bytes=MAX_REPORT_BYTES,
                label="Stage report",
            )
            if current is None:
                raise SafeIOError(
                    f"Stage report file does not exist: {self.report_path}"
                )
        except SafeIOError as exc:
            raise StageReportError(
                f"Stage report became unreadable: {self.report_path}"
            ) from exc
        if hashlib.sha256(current).hexdigest() != self.report_sha256:
            raise StageReportError(
                f"Stage report changed while it was being submitted: {self.report_path}"
            )


def _relative_path(value: Any, label: str) -> PurePosixPath:
    try:
        return relative_posix_path(value, label=label)
    except SafePathError as exc:
        if exc.code in {"invalid-type", "empty"}:
            message = f"{label} must be a non-empty relative POSIX path"
        elif exc.code == "separator":
            message = f"{label} must use POSIX '/' separators"
        elif exc.code in {"root", "traversal"}:
            message = f"{label} must not be '.' or contain '..'"
        elif exc.code in {"absolute", "non-normalized"}:
            message = f"{label} must be a normalized relative POSIX path"
        else:
            message = str(exc)
        raise StageReportError(message) from exc


def _resolve_scoped_path(
    value: Any,
    *,
    scope: str,
    run_dir: Path,
    vault: Path,
    label: str,
) -> Path:
    relative = _relative_path(value, label)
    root = run_dir if scope == "run" else vault
    try:
        return resolve_within(root, relative.as_posix(), label=label)
    except SafePathError as exc:
        raise StageReportError(f"{label} escapes its {scope} scope") from exc


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise StageReportError(f"{label} must be null or a non-empty string")
    return value.strip()


def load_stage_report(
    report_path: Path,
    *,
    phase: str,
    run_dir: Path,
    vault: Path,
) -> StageSubmission:
    """Read, strictly validate, and resolve one stage report."""
    expected_stage = STAGE_BY_PHASE.get(phase)
    if expected_stage is None:
        raise StageReportError(f"Run phase {phase!r} does not accept a stage report")

    try:
        resolved_run_dir = run_dir.expanduser().resolve()
        resolved_report = report_path.expanduser()
        if not resolved_report.is_absolute():
            resolved_report = resolved_run_dir / resolved_report
        resolved_report = anchored_file_path(
            resolved_report,
            label="Stage report",
        )
    except (OSError, RuntimeError, SafeIOError) as exc:
        raise StageReportError("Stage report path cannot be resolved safely") from exc
    try:
        resolved_report.relative_to(resolved_run_dir)
    except ValueError as exc:
        raise StageReportError("Stage report must be inside the current Run directory") from exc
    try:
        raw = read_regular_bytes(
            resolved_report,
            max_bytes=MAX_REPORT_BYTES,
            label="Stage report",
        )
        if raw is None:
            raise SafeIOError(
                f"Stage report file does not exist: {resolved_report}"
            )
        data = parse_json_object(
            raw,
            max_bytes=MAX_REPORT_BYTES,
            label="Stage report",
        )
    except SafeIOError as exc:
        raise StageReportError(str(exc)) from exc

    unknown = set(data) - TOP_LEVEL_FIELDS
    if unknown:
        raise StageReportError(
            "Stage report contains unsupported fields: " + ", ".join(sorted(unknown))
        )
    version = data.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != REPORT_VERSION
    ):
        raise StageReportError(
            f"Stage report version must be {REPORT_VERSION}, got {version!r}"
        )
    stage = data.get("stage")
    if stage != expected_stage:
        raise StageReportError(
            f"Stage report {stage!r} does not match Run phase {phase!r} "
            f"(expected {expected_stage!r})"
        )
    result = data.get("result")
    if not isinstance(result, str) or result not in RESULTS:
        raise StageReportError(f"Unsupported stage result: {result!r}")

    message = _optional_text(data.get("message"), "message")
    retry_at = _optional_text(data.get("retry_at"), "retry_at")
    if result in {"recoverable", "attention", "deterministic-failure"} and not message:
        raise StageReportError(f"Stage result {result!r} requires a message")
    if retry_at is not None and result not in {"recoverable", "attention"}:
        raise StageReportError("retry_at is allowed only for recoverable or attention")

    raw_artifacts = data.get("artifacts", [])
    if not isinstance(raw_artifacts, list):
        raise StageReportError("artifacts must be a JSON array")
    artifacts: list[ArtifactCandidate] = []
    seen_artifacts: set[tuple[str, str, str]] = set()
    for index, artifact in enumerate(raw_artifacts):
        label = f"artifacts[{index}]"
        if not isinstance(artifact, dict) or set(artifact) != ARTIFACT_FIELDS:
            raise StageReportError(
                f"{label} must contain exactly role, scope, and path"
            )
        role = artifact["role"]
        scope = artifact["scope"]
        if not isinstance(role, str) or not ROLE_PATTERN.fullmatch(role):
            raise StageReportError(
                f"{label}.role must match {ROLE_PATTERN.pattern}"
            )
        if not isinstance(scope, str) or scope not in {"run", "vault"}:
            raise StageReportError(f"{label}.scope must be 'run' or 'vault'")
        relative = _relative_path(artifact["path"], f"{label}.path")
        identity = (role, scope, relative.as_posix())
        if identity in seen_artifacts:
            raise StageReportError(f"Duplicate artifact entry: {identity}")
        seen_artifacts.add(identity)
        artifacts.append(
            ArtifactCandidate(
                role=role,
                path=_resolve_scoped_path(
                    relative.as_posix(),
                    scope=scope,
                    run_dir=resolved_run_dir,
                    vault=vault,
                    label=f"{label}.path",
                ),
            )
        )

    raw_changes = data.get("changed_paths", [])
    if not isinstance(raw_changes, list):
        raise StageReportError("changed_paths must be a JSON array")
    changed_paths: list[str] = []
    for index, value in enumerate(raw_changes):
        relative = _relative_path(value, f"changed_paths[{index}]").as_posix()
        if relative not in changed_paths:
            changed_paths.append(relative)

    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        raise StageReportError("metadata must be a JSON object")

    report_artifact = ArtifactCandidate(
        role=f"{expected_stage}-report",
        path=resolved_report,
    )
    return StageSubmission(
        report_path=resolved_report,
        report_sha256=hashlib.sha256(raw).hexdigest(),
        stage=expected_stage,
        result=result,
        message=message,
        retry_at=retry_at,
        artifacts=(report_artifact, *artifacts),
        changed_paths=tuple(changed_paths),
        metadata=metadata,
    )
