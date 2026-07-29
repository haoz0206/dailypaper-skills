#!/usr/bin/env python3
"""Local lifecycle state for one DailyPaper Run.

This module deliberately does not arbitrate Vault ownership.  Its interface
owns the local Run Manifest, Run Checkpoints, and Run Artifacts; a higher-level
Run Coordinator owns Vault Task State and publication.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from safe_io import (
    SafeIOError,
    anchored_file_path,
    atomic_write_bytes,
    encode_json_value,
    load_json_object,
    sha256_regular_file,
)
from safe_path import SafePathError, relative_posix_path, resolve_within


MANIFEST_VERSION = 2
MANIFEST_LOCK_NAME = "manifest.lock"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
CONDITIONS = frozenset({"active", "interrupted", "attention-required"})
OUTCOMES = frozenset({"published", "failed", "cancelled"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class LifecycleError(RuntimeError):
    """Base class for safe, expected lifecycle failures."""


class ManifestCorrupt(LifecycleError):
    """Neither the current nor previous Run Manifest is valid."""


class SchemaError(LifecycleError):
    """A Run Manifest does not satisfy the v2 schema."""


class ContractMismatch(LifecycleError):
    """The caller's Workflow Contract differs from the Run Manifest."""


class ConfigurationMismatch(LifecycleError):
    """The caller's Configuration Fingerprint differs from the Run Manifest."""


class ManifestIdentityMismatch(LifecycleError):
    """A Run Manifest is not anchored to the expected Run directory and Vault."""


class InvalidTransition(LifecycleError):
    """A requested lifecycle transition is not allowed."""


class TerminalRun(LifecycleError):
    """A terminal DailyPaper Run cannot be changed."""


class CheckpointRequired(LifecycleError):
    """The current phase has not produced a verified Run Checkpoint."""


class ArtifactConflict(LifecycleError):
    """A Run Artifact no longer matches its verified content."""


class UnsafePath(LifecycleError):
    """A Run Artifact or Run Change Set path escapes its allowed root."""


class UnexpectedDirtyPaths(LifecycleError):
    """Resume observed Vault changes outside the Run Change Set."""


@dataclass(frozen=True)
class WorkflowContract:
    """Versioned ordering semantics shared by all Harnesses."""

    name: str
    version: int
    phases: tuple[str, ...]
    required_artifact_roles_by_phase: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Workflow Contract name must not be empty")
        if not isinstance(self.version, int) or self.version < 1:
            raise ValueError("Workflow Contract version must be a positive integer")
        if not self.phases or any(not value.strip() for value in self.phases):
            raise ValueError("Workflow Contract must define non-empty phases")
        if len(set(self.phases)) != len(self.phases):
            raise ValueError("Workflow Contract phases must be unique")
        required = self.required_artifact_roles_by_phase or {}
        normalized: dict[str, tuple[str, ...]] = {}
        for phase, roles in required.items():
            if phase not in self.phases:
                raise ValueError(
                    f"Required Artifact roles reference unknown Run Phase: {phase}"
                )
            role_tuple = tuple(roles)
            if (
                any(not isinstance(role, str) or not role.strip() for role in role_tuple)
                or len(role_tuple) != len(set(role_tuple))
            ):
                raise ValueError(
                    f"Required Artifact roles for {phase!r} must be unique and non-empty"
                )
            normalized[phase] = tuple(sorted(role_tuple))
        object.__setattr__(
            self,
            "required_artifact_roles_by_phase",
            MappingProxyType(normalized),
        )

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "version": self.version,
            "phases": list(self.phases),
            "required_artifact_roles_by_phase": {
                phase: list(self.required_artifact_roles_by_phase.get(phase, ()))
                for phase in self.phases
                if self.required_artifact_roles_by_phase.get(phase)
            },
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            **payload,
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }


DAILY_WORKFLOW_CONTRACT = WorkflowContract(
    name="daily-papers",
    version=2,
    phases=(
        "prepared",
        "fetching",
        "reviewing",
        "writing-notes",
        "validated",
        "publishing",
    ),
    required_artifact_roles_by_phase={
        "fetching": ("candidates", "enriched"),
        "reviewing": ("recommendation", "history"),
        "writing-notes": ("daily-note",),
        "validated": ("daily-note",),
    },
)


@dataclass(frozen=True)
class ArtifactCandidate:
    """A file offered for verification as a Run Artifact."""

    role: str
    path: Path


@dataclass(frozen=True)
class Interruption:
    """A recoverable interruption reported by the Run Coordinator."""

    message: str
    retry_at: str | None = None
    attention_required: bool = False


@dataclass(frozen=True)
class RunSnapshot:
    """Read-only projection returned through the module interface."""

    _data: Mapping[str, Any]

    @property
    def run_id(self) -> str:
        return str(self._data["run_id"])

    @property
    def window_days(self) -> int:
        return int(self._data["window_days"])

    @property
    def phase(self) -> str:
        return str(self._data["phase"])

    @property
    def condition(self) -> str | None:
        value = self._data["condition"]
        return None if value is None else str(value)

    @property
    def outcome(self) -> str | None:
        value = self._data["outcome"]
        return None if value is None else str(value)

    @property
    def revision(self) -> int:
        return int(self._data["revision"])

    @property
    def run_change_set(self) -> tuple[str, ...]:
        return tuple(self._data["run_change_set"])

    @property
    def manifest_path(self) -> Path:
        return Path(self._data["paths"]["run_dir"]) / "manifest.json"

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self._data))


class RunLifecycle:
    """Deep module for the local lifecycle of one DailyPaper Run."""

    def __init__(
        self,
        manifest_path: Path,
        contract: WorkflowContract,
        configuration_fingerprint: str,
        expected_vault: Path,
        expected_run_id: str,
        *,
        recovered_from_previous: bool = False,
    ) -> None:
        self._path = manifest_path
        self._contract = contract
        self._configuration_fingerprint = configuration_fingerprint
        self._expected_vault = expected_vault
        self._expected_run_id = expected_run_id
        self.recovered_from_previous = recovered_from_previous

    @classmethod
    def create(
        cls,
        manifest_path: Path,
        *,
        run_id: str,
        target_date: str,
        window_days: int = 1,
        timezone: str,
        vault: Path,
        contract: WorkflowContract,
        configuration_fingerprint: str,
    ) -> "RunLifecycle":
        """Create a v2 Run Manifest and return its sole local writer."""
        candidate = manifest_path.expanduser()
        path = anchored_file_path(candidate, label="Run Manifest")
        if path.name != "manifest.json":
            raise ManifestIdentityMismatch(
                "Run Manifest must be named manifest.json"
            )
        if candidate.is_symlink():
            raise ManifestIdentityMismatch("Run Manifest must not be a symlink")
        vault_path = vault.expanduser().resolve()
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        if not DATE_PATTERN.fullmatch(target_date):
            raise ValueError("target_date must use YYYY-MM-DD")
        if (
            isinstance(window_days, bool)
            or not isinstance(window_days, int)
            or not 1 <= window_days <= 31
        ):
            raise ValueError("window_days must be an integer from 1 to 31")
        if not timezone.strip():
            raise ValueError("timezone must not be empty")
        _require_sha256(configuration_fingerprint, "Configuration Fingerprint")
        path.parent.mkdir(parents=True, exist_ok=True)
        with _manifest_file_lock(path):
            if path.exists():
                raise LifecycleError(f"Run Manifest already exists: {path}")
            data: dict[str, Any] = {
                "version": MANIFEST_VERSION,
                "revision": 0,
                "run_id": run_id,
                "target_date": target_date,
                "window_days": window_days,
                "timezone": timezone,
                "phase": contract.phases[0],
                "condition": "active",
                "outcome": None,
                "terminal_reason": None,
                "workflow_contract": contract.as_dict(),
                "configuration_fingerprint": configuration_fingerprint,
                "paths": {
                    "vault": str(vault_path),
                    "run_dir": str(path.parent),
                    "candidates": str(path.parent / "candidates.json"),
                    "enriched": str(path.parent / "enriched.json"),
                    "result": str(path.parent / "result.json"),
                },
                "artifacts": {},
                "checkpoints": {},
                "run_change_set": [],
                "recovery": {
                    "attempts_by_phase": {},
                    "last_error": None,
                    "retry_at": None,
                },
                "publication": {
                    "acquisition_commit": None,
                    "content_commit": None,
                    "remote": None,
                    "branch": None,
                },
            }
            _validate_manifest(data)
            _validate_manifest_identity(
                data,
                manifest_path=path,
                expected_vault=vault_path,
                expected_run_id=run_id,
            )
            _atomic_write(path, _encode(data))
        return cls(
            path,
            contract,
            configuration_fingerprint,
            vault_path,
            run_id,
        )

    @classmethod
    def open(
        cls,
        manifest_path: Path,
        *,
        contract: WorkflowContract,
        configuration_fingerprint: str,
        expected_vault: Path,
        expected_run_id: str,
    ) -> "RunLifecycle":
        """Open a Run Manifest, falling back to its previous atomic snapshot."""
        path = anchored_file_path(manifest_path, label="Run Manifest")
        vault = expected_vault.expanduser().resolve()
        if path.name != "manifest.json":
            raise ManifestIdentityMismatch(
                "Run Manifest must be named manifest.json"
            )
        if not expected_run_id.strip():
            raise ValueError("expected_run_id must not be empty")
        _require_sha256(configuration_fingerprint, "Configuration Fingerprint")
        recovered = False
        with _manifest_file_lock(path):
            try:
                data = _read_and_validate(
                    path,
                    manifest_path=path,
                    expected_vault=vault,
                    expected_run_id=expected_run_id,
                )
            except (
                OSError,
                ValueError,
                json.JSONDecodeError,
                SchemaError,
                ManifestIdentityMismatch,
            ) as current_error:
                previous = path.with_name("manifest.prev.json")
                try:
                    data = _read_and_validate(
                        previous,
                        manifest_path=path,
                        expected_vault=vault,
                        expected_run_id=expected_run_id,
                    )
                except (
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    SchemaError,
                    ManifestIdentityMismatch,
                ) as prev_error:
                    raise ManifestCorrupt(
                        "Current and previous Run Manifest snapshots are invalid: "
                        f"current={current_error}; previous={prev_error}"
                    ) from prev_error
                _validate_open_expectations(
                    data,
                    contract=contract,
                    configuration_fingerprint=configuration_fingerprint,
                )
                # A symlinked current snapshot is invalid input, but once the
                # previous snapshot has been validated under the Manifest lock
                # it is safe to remove the link itself and restore the file.
                if path.is_symlink():
                    path.unlink()
                _atomic_write(path, _encode(data))
                recovered = True

        _validate_open_expectations(
            data,
            contract=contract,
            configuration_fingerprint=configuration_fingerprint,
        )
        return cls(
            path,
            contract,
            configuration_fingerprint,
            vault,
            expected_run_id,
            recovered_from_previous=recovered,
        )

    def snapshot(self) -> RunSnapshot:
        """Return the current validated lifecycle projection."""
        return _snapshot(self._load())

    def verify_publication_inputs(self) -> RunSnapshot:
        """Revalidate every registered artifact and claimed Vault path."""
        data = self._load()
        _require_mutable(data)
        if data["phase"] != self._contract.phases[-1]:
            raise InvalidTransition(
                "Publication inputs can be verified only in the final phase"
            )
        _verify_artifacts(data)
        _require_change_set_artifacts(data)
        return _snapshot(data)

    def checkpoint(
        self,
        *,
        artifacts: Iterable[ArtifactCandidate] = (),
        changed_paths: Iterable[Path | str] = (),
        allow_artifact_updates: bool = False,
        enforce_contract: bool = True,
    ) -> RunSnapshot:
        """Verify and atomically record work completed in the current phase."""
        data = self._load_mutable()
        _require_mutable(data)
        if data["condition"] != "active":
            raise InvalidTransition("Only an active Run can record a checkpoint")

        phase = str(data["phase"])
        vault = Path(data["paths"]["vault"])
        run_dir = Path(data["paths"]["run_dir"])
        proposed_artifacts = copy.deepcopy(data["artifacts"])
        artifact_keys: list[str] = []

        for candidate in artifacts:
            if not candidate.role.strip():
                raise ValueError("Run Artifact role must not be empty")
            reference = _artifact_reference(candidate.path, vault=vault, run_dir=run_dir)
            digest = _sha256_file(candidate.path)
            key = f"{candidate.role}:{reference['scope']}:{reference['path']}"
            existing = proposed_artifacts.get(key)
            record = {
                "role": candidate.role,
                **reference,
                "sha256": digest,
            }
            if (
                existing is not None
                and existing != record
                and not allow_artifact_updates
            ):
                raise ArtifactConflict(
                    f"Run Artifact changed after verification: {candidate.path}"
                )
            if allow_artifact_updates:
                for artifact in proposed_artifacts.values():
                    if (
                        artifact["scope"] == reference["scope"]
                        and artifact["path"] == reference["path"]
                    ):
                        artifact["sha256"] = digest
            proposed_artifacts[key] = record
            artifact_keys.append(key)

        proposed_change_set = list(data["run_change_set"])
        for candidate in changed_paths:
            relative = _vault_relative(candidate, vault)
            if relative not in proposed_change_set:
                proposed_change_set.append(relative)

        existing_checkpoint = data["checkpoints"].get(phase)
        checkpoint = {
            "phase": phase,
            "artifacts": sorted(set(artifact_keys)),
            "validated": True,
        }
        if existing_checkpoint is not None:
            existing_keys = set(existing_checkpoint["artifacts"])
            checkpoint["artifacts"] = sorted(existing_keys | set(artifact_keys))

        if enforce_contract:
            _require_checkpoint_roles(
                phase,
                checkpoint,
                proposed_artifacts,
                self._contract,
            )

        proposed = copy.deepcopy(data)
        proposed["artifacts"] = proposed_artifacts
        proposed["run_change_set"] = proposed_change_set
        _require_change_set_artifacts(proposed)

        if (
            proposed_artifacts == data["artifacts"]
            and proposed_change_set == data["run_change_set"]
            and existing_checkpoint == checkpoint
        ):
            return _snapshot(data)

        data["artifacts"] = proposed_artifacts
        data["run_change_set"] = proposed_change_set
        data["checkpoints"][phase] = checkpoint
        return self._commit(data)

    def advance(self, phase: str) -> RunSnapshot:
        """Advance exactly one phase, or safely resume the current phase."""
        data = self._load_mutable()
        _require_mutable(data)
        phases = self._contract.phases
        current = str(data["phase"])
        if phase not in phases:
            raise InvalidTransition(f"Unknown Run Phase: {phase}")

        if phase == current:
            _verify_artifacts(data)
            if data["condition"] == "active":
                return _snapshot(data)
            raise InvalidTransition(
                "Use resume() to reactivate an interrupted Run Phase"
            )

        current_index = phases.index(current)
        requested_index = phases.index(phase)
        if requested_index != current_index + 1:
            raise InvalidTransition(
                f"Run Phase must advance from {current!r} to "
                f"{phases[current_index + 1]!r}"
                if current_index + 1 < len(phases)
                else f"Run Phase {current!r} is already final"
            )
        if data["condition"] != "active":
            raise InvalidTransition("Resume the current Run Phase before advancing")
        if current_index > 0 and current not in data["checkpoints"]:
            raise CheckpointRequired(
                f"Run Phase {current!r} requires a verified checkpoint"
            )
        if current_index > 0:
            _require_checkpoint_roles(
                current,
                data["checkpoints"][current],
                data["artifacts"],
                self._contract,
            )
        _verify_artifacts(data)
        data["phase"] = phase
        return self._commit(data)

    def interrupt(self, interruption: Interruption) -> RunSnapshot:
        """Record recoverable interruption and retry metadata."""
        data = self._load_mutable()
        _require_mutable(data)
        if not interruption.message.strip():
            raise ValueError("Interruption message must not be empty")
        phase = str(data["phase"])
        attempts = data["recovery"]["attempts_by_phase"]
        attempts[phase] = int(attempts.get(phase, 0)) + 1
        data["recovery"]["last_error"] = {
            "phase": phase,
            "message": interruption.message,
            "attempt": attempts[phase],
        }
        data["recovery"]["retry_at"] = interruption.retry_at
        data["condition"] = (
            "attention-required"
            if interruption.attention_required
            else "interrupted"
        )
        return self._commit(data)

    def resume(
        self,
        *,
        observed_dirty_paths: Iterable[str],
        require_user_confirmation: bool = False,
    ) -> RunSnapshot:
        """Verify local evidence and reactivate the current Run Phase."""
        data = self._load_mutable()
        _require_mutable(data)
        if data["condition"] not in {"interrupted", "attention-required"}:
            if data["condition"] == "active":
                _verify_artifacts(data)
                return _snapshot(data)
            raise InvalidTransition("Only a non-terminal interrupted Run can resume")
        if (
            data["condition"] == "attention-required"
            and not require_user_confirmation
        ):
            raise InvalidTransition(
                "Attention-required Run needs explicit user confirmation to resume"
            )
        unexpected = set(observed_dirty_paths) - set(data["run_change_set"])
        if unexpected:
            raise UnexpectedDirtyPaths(
                "Vault contains changes outside this Run: "
                + ", ".join(sorted(unexpected))
            )
        _verify_artifacts(data)
        data["condition"] = "active"
        data["recovery"]["retry_at"] = None
        return self._commit(data)

    def finish(
        self,
        outcome: str,
        *,
        reason: str | None = None,
        content_commit: str | None = None,
    ) -> RunSnapshot:
        """Record the immutable Run Outcome."""
        data = self._load_mutable()
        _require_mutable(data)
        if outcome not in OUTCOMES:
            raise ValueError(f"Unknown Run Outcome: {outcome}")
        if outcome == "published":
            if data["phase"] != self._contract.phases[-1]:
                raise InvalidTransition(
                    "A Run can be published only from the final Workflow Contract phase"
                )
            if not content_commit or not content_commit.strip():
                raise ValueError("Published Run requires its content commit")
            existing_content_commit = data["publication"]["content_commit"]
            if (
                existing_content_commit is not None
                and existing_content_commit != content_commit
            ):
                raise InvalidTransition(
                    "Published Run must reuse its recorded content commit"
                )
            data["publication"]["content_commit"] = content_commit
        elif content_commit is not None:
            raise ValueError("Only a Published Run records a content commit")
        data["outcome"] = outcome
        data["condition"] = None
        data["terminal_reason"] = reason
        data["recovery"]["retry_at"] = None
        return self._commit(data)

    def record_acquisition(
        self,
        *,
        acquisition_commit: str,
        remote: str,
        branch: str,
    ) -> RunSnapshot:
        """Record the locally observed Vault ownership commit."""
        data = self._load_mutable()
        _require_mutable(data)
        values = {
            "acquisition_commit": acquisition_commit,
            "remote": remote,
            "branch": branch,
        }
        if any(not value.strip() for value in values.values()):
            raise ValueError("Acquisition metadata must not be empty")
        existing = {
            key: data["publication"][key]
            for key in values
        }
        if all(value is None for value in existing.values()):
            data["publication"].update(values)
            return self._commit(data)
        if existing == values:
            return _snapshot(data)
        raise InvalidTransition("Run acquisition metadata is immutable")

    def record_content_commit(self, content_commit: str) -> RunSnapshot:
        """Record one reusable local content commit before its push."""
        data = self._load_mutable()
        _require_mutable(data)
        if data["phase"] != self._contract.phases[-1]:
            raise InvalidTransition(
                "Content commit can be recorded only during publication"
            )
        if not content_commit.strip():
            raise ValueError("content_commit must not be empty")
        existing = data["publication"]["content_commit"]
        if existing is None:
            data["publication"]["content_commit"] = content_commit
            return self._commit(data)
        if existing == content_commit:
            return _snapshot(data)
        raise InvalidTransition("Run content commit is immutable")

    def _load(self) -> dict[str, Any]:
        data = _read_and_validate(
            self._path,
            manifest_path=self._path,
            expected_vault=self._expected_vault,
            expected_run_id=self._expected_run_id,
        )
        if data["workflow_contract"] != self._contract.as_dict():
            raise ContractMismatch("Workflow Contract changed after the Run started")
        if data["configuration_fingerprint"] != self._configuration_fingerprint:
            raise ConfigurationMismatch(
                "Output-affecting DailyPaper configuration changed after the Run started"
            )
        return data

    def _load_mutable(self) -> dict[str, Any]:
        return copy.deepcopy(self._load())

    def _commit(self, data: dict[str, Any]) -> RunSnapshot:
        with _manifest_file_lock(self._path):
            current = self._load()
            if data["revision"] != current["revision"]:
                raise LifecycleError(
                    "Run Manifest revision changed during concurrent update"
                )
            data["revision"] = int(current["revision"]) + 1
            _validate_manifest(data)
            _validate_manifest_identity(
                data,
                manifest_path=self._path,
                expected_vault=self._expected_vault,
                expected_run_id=self._expected_run_id,
            )
            previous = self._path.with_name("manifest.prev.json")
            _atomic_write(previous, _encode(current))
            _atomic_write(self._path, _encode(data))
            return _snapshot(data)


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _snapshot(data: Mapping[str, Any]) -> RunSnapshot:
    return RunSnapshot(copy.deepcopy(dict(data)))


def _encode(data: Mapping[str, Any]) -> bytes:
    try:
        return encode_json_value(
            data,
            max_bytes=MAX_MANIFEST_BYTES,
            label="Run Manifest",
        )
    except SafeIOError as exc:
        raise LifecycleError(str(exc)) from exc


@contextmanager
def _manifest_file_lock(manifest_path: Path) -> Iterator[None]:
    lock_path = manifest_path.parent / MANIFEST_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise LifecycleError(
            f"Run Manifest lock cannot be opened safely: {lock_path}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise LifecycleError(
                f"Run Manifest lock is not a regular file: {lock_path}"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    try:
        atomic_write_bytes(
            path,
            payload,
            mode=0o600,
            label="Run Manifest snapshot",
        )
    except SafeIOError as exc:
        raise LifecycleError(str(exc)) from exc


def _read_and_validate(
    source_path: Path,
    *,
    manifest_path: Path,
    expected_vault: Path,
    expected_run_id: str,
) -> dict[str, Any]:
    data = load_json_object(
        source_path,
        max_bytes=MAX_MANIFEST_BYTES,
        label="Run Manifest",
    )
    if data is None:  # Defensive: required=True must already reject this state.
        raise ValueError(f"Run Manifest file does not exist: {source_path}")
    # Pre-release Manifest v2 did not persist the acquisition window. Before
    # this contract existed all runs were one-day runs, so normalize only that
    # unambiguous legacy shape.
    data.setdefault("window_days", 1)
    _validate_manifest(data)
    _validate_manifest_identity(
        data,
        manifest_path=manifest_path,
        expected_vault=expected_vault,
        expected_run_id=expected_run_id,
    )
    return data


def _validate_manifest_identity(
    data: Mapping[str, Any],
    *,
    manifest_path: Path,
    expected_vault: Path,
    expected_run_id: str,
) -> None:
    path = anchored_file_path(manifest_path, label="Run Manifest")
    vault = expected_vault.expanduser().resolve()
    run_dir = path.parent
    if path.name != "manifest.json":
        raise ManifestIdentityMismatch("Run Manifest must be named manifest.json")
    if data["run_id"] != expected_run_id:
        raise ManifestIdentityMismatch(
            f"Expected Run {expected_run_id!r}, found {data['run_id']!r}"
        )
    expected_paths = {
        "vault": str(vault),
        "run_dir": str(run_dir),
        "candidates": str(run_dir / "candidates.json"),
        "enriched": str(run_dir / "enriched.json"),
        "result": str(run_dir / "result.json"),
    }
    if data["paths"] != expected_paths:
        raise ManifestIdentityMismatch(
            "Run Manifest paths are not anchored to its Run directory and Vault"
        )


def _validate_open_expectations(
    data: Mapping[str, Any],
    *,
    contract: WorkflowContract,
    configuration_fingerprint: str,
) -> None:
    if data["workflow_contract"] != contract.as_dict():
        raise ContractMismatch("Workflow Contract changed after the Run started")
    if data["configuration_fingerprint"] != configuration_fingerprint:
        raise ConfigurationMismatch(
            "Output-affecting DailyPaper configuration changed after the Run started"
        )


def _require_checkpoint_roles(
    phase: str,
    checkpoint: Mapping[str, Any],
    artifacts: Mapping[str, Mapping[str, Any]],
    contract: WorkflowContract,
) -> None:
    required_roles = set(
        contract.required_artifact_roles_by_phase.get(phase, ())
    )
    checkpoint_roles = {
        str(artifacts[key]["role"])
        for key in checkpoint["artifacts"]
    }
    missing_roles = required_roles - checkpoint_roles
    if missing_roles:
        raise CheckpointRequired(
            f"Run Phase {phase!r} is missing required Artifact roles: "
            + ", ".join(sorted(missing_roles))
        )


def _validate_manifest(data: Any) -> None:
    if not isinstance(data, dict):
        raise SchemaError("Run Manifest must be a JSON object")
    required = {
        "version",
        "revision",
        "run_id",
        "target_date",
        "window_days",
        "timezone",
        "phase",
        "condition",
        "outcome",
        "terminal_reason",
        "workflow_contract",
        "configuration_fingerprint",
        "paths",
        "artifacts",
        "checkpoints",
        "run_change_set",
        "recovery",
        "publication",
    }
    if set(data) != required:
        raise SchemaError("Run Manifest fields do not match schema v2")
    if data["version"] != MANIFEST_VERSION:
        raise SchemaError(f"Unsupported Run Manifest version: {data['version']}")
    if not isinstance(data["revision"], int) or data["revision"] < 0:
        raise SchemaError("Run Manifest revision must be a non-negative integer")
    if not isinstance(data["run_id"], str) or not data["run_id"]:
        raise SchemaError("Run Manifest run_id must not be empty")
    if (
        not isinstance(data["target_date"], str)
        or not DATE_PATTERN.fullmatch(data["target_date"])
    ):
        raise SchemaError("Run Manifest target_date must use YYYY-MM-DD")
    if (
        isinstance(data["window_days"], bool)
        or not isinstance(data["window_days"], int)
        or not 1 <= data["window_days"] <= 31
    ):
        raise SchemaError("Run Manifest window_days must be an integer from 1 to 31")
    if not isinstance(data["timezone"], str) or not data["timezone"]:
        raise SchemaError("Run Manifest timezone must not be empty")
    workflow_contract = data["workflow_contract"]
    if (
        not isinstance(workflow_contract, dict)
        or set(workflow_contract)
        != {
            "name",
            "version",
            "phases",
            "required_artifact_roles_by_phase",
            "sha256",
        }
        or not isinstance(workflow_contract["name"], str)
        or not workflow_contract["name"]
        or not isinstance(workflow_contract["version"], int)
        or workflow_contract["version"] < 1
        or not isinstance(workflow_contract["phases"], list)
        or not workflow_contract["phases"]
        or any(
            not isinstance(phase, str) or not phase
            for phase in workflow_contract["phases"]
        )
        or len(workflow_contract["phases"])
        != len(set(workflow_contract["phases"]))
    ):
        raise SchemaError("Run Manifest Workflow Contract is invalid")
    phases = workflow_contract["phases"]
    required_roles = workflow_contract["required_artifact_roles_by_phase"]
    if not isinstance(required_roles, dict):
        raise SchemaError("Workflow Contract required Artifact roles are invalid")
    for phase, roles in required_roles.items():
        if (
            phase not in phases
            or not isinstance(roles, list)
            or any(not isinstance(role, str) or not role for role in roles)
            or len(roles) != len(set(roles))
        ):
            raise SchemaError("Workflow Contract required Artifact roles are invalid")
    contract_payload = {
        "name": workflow_contract["name"],
        "version": workflow_contract["version"],
        "phases": phases,
        "required_artifact_roles_by_phase": required_roles,
    }
    expected_contract_sha = hashlib.sha256(
        json.dumps(
            contract_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if workflow_contract["sha256"] != expected_contract_sha:
        raise SchemaError("Workflow Contract fingerprint is invalid")
    if data["phase"] not in phases:
        raise SchemaError("Run Manifest contains an unknown Run Phase")
    if (
        not isinstance(data["configuration_fingerprint"], str)
        or not SHA256_PATTERN.fullmatch(data["configuration_fingerprint"])
    ):
        raise SchemaError("Run Manifest Configuration Fingerprint is invalid")
    if data["terminal_reason"] is not None and not isinstance(
        data["terminal_reason"], str
    ):
        raise SchemaError("Run terminal_reason must be a string or null")

    condition = data["condition"]
    outcome = data["outcome"]
    if outcome is None:
        if not isinstance(condition, str) or condition not in CONDITIONS:
            raise SchemaError("Non-terminal Run requires a valid Run Condition")
    else:
        if (
            not isinstance(outcome, str)
            or outcome not in OUTCOMES
            or condition is not None
        ):
            raise SchemaError("Terminal Run requires one outcome and no condition")

    paths = data["paths"]
    if (
        not isinstance(paths, dict)
        or set(paths)
        != {"vault", "run_dir", "candidates", "enriched", "result"}
        or any(
            not isinstance(value, str) or not Path(value).is_absolute()
            for value in paths.values()
        )
    ):
        raise SchemaError("Run Manifest paths must contain absolute Vault and run_dir")

    if not isinstance(data["artifacts"], dict):
        raise SchemaError("Run Manifest artifacts must be an object")
    for key, artifact in data["artifacts"].items():
        if not isinstance(key, str) or not isinstance(artifact, dict):
            raise SchemaError("Run Artifact records are invalid")
        if set(artifact) != {"role", "scope", "path", "sha256"}:
            raise SchemaError("Run Artifact fields do not match schema v2")
        if not isinstance(artifact["role"], str) or not artifact["role"]:
            raise SchemaError("Run Artifact role must not be empty")
        if (
            not isinstance(artifact["scope"], str)
            or artifact["scope"] not in {"run", "vault"}
        ):
            raise SchemaError("Run Artifact scope must be run or vault")
        if not _safe_relative_text(artifact["path"]):
            raise SchemaError("Run Artifact path must be safe and relative")
        if (
            not isinstance(artifact["sha256"], str)
            or not SHA256_PATTERN.fullmatch(artifact["sha256"])
        ):
            raise SchemaError("Run Artifact hash is invalid")

    if not isinstance(data["checkpoints"], dict):
        raise SchemaError("Run Manifest checkpoints must be an object")
    for phase, checkpoint in data["checkpoints"].items():
        if phase not in phases or not isinstance(checkpoint, dict):
            raise SchemaError("Run Checkpoint phase is invalid")
        if set(checkpoint) != {"phase", "artifacts", "validated"}:
            raise SchemaError("Run Checkpoint fields do not match schema v2")
        if (
            checkpoint["phase"] != phase
            or checkpoint["validated"] is not True
            or not isinstance(checkpoint["artifacts"], list)
            or any(not isinstance(key, str) for key in checkpoint["artifacts"])
            or any(key not in data["artifacts"] for key in checkpoint["artifacts"])
        ):
            raise SchemaError("Run Checkpoint references are invalid")
    current_phase_index = phases.index(data["phase"])
    for completed_phase in phases[1:current_phase_index]:
        if completed_phase not in data["checkpoints"]:
            raise SchemaError(
                f"Run Manifest skipped checkpoint for {completed_phase!r}"
            )
        checkpoint_roles = {
            data["artifacts"][key]["role"]
            for key in data["checkpoints"][completed_phase]["artifacts"]
        }
        missing_roles = (
            set(required_roles.get(completed_phase, [])) - checkpoint_roles
        )
        if missing_roles:
            raise SchemaError(
                f"Completed Run Checkpoint for {completed_phase!r} "
                "is missing required Artifact roles"
            )
    if any(phases.index(phase) > current_phase_index for phase in data["checkpoints"]):
        raise SchemaError("Run Manifest contains a future Run Checkpoint")

    change_set = data["run_change_set"]
    if (
        not isinstance(change_set, list)
        or any(not isinstance(value, str) for value in change_set)
        or len(change_set) != len(set(change_set))
        or any(not _safe_relative_text(value) for value in change_set)
    ):
        raise SchemaError("Run Change Set must contain unique safe relative paths")

    recovery = data["recovery"]
    if not isinstance(recovery, dict) or set(recovery) != {
        "attempts_by_phase",
        "last_error",
        "retry_at",
    }:
        raise SchemaError("Run recovery metadata does not match schema v2")
    attempts = recovery["attempts_by_phase"]
    if (
        not isinstance(attempts, dict)
        or any(
            phase not in phases
            or not isinstance(value, int)
            or value < 1
            for phase, value in attempts.items()
        )
    ):
        raise SchemaError("Run recovery attempts are invalid")
    if recovery["retry_at"] is not None and not isinstance(
        recovery["retry_at"], str
    ):
        raise SchemaError("Run retry_at must be a string or null")
    last_error = recovery["last_error"]
    if last_error is not None:
        if (
            not isinstance(last_error, dict)
            or set(last_error) != {"phase", "message", "attempt"}
            or last_error["phase"] not in phases
            or not isinstance(last_error["message"], str)
            or not isinstance(last_error["attempt"], int)
        ):
            raise SchemaError("Run last_error metadata is invalid")

    publication = data["publication"]
    if (
        not isinstance(publication, dict)
        or set(publication)
        != {"acquisition_commit", "content_commit", "remote", "branch"}
        or (
            publication["content_commit"] is not None
            and not isinstance(publication["content_commit"], str)
        )
        or (
            publication["acquisition_commit"] is not None
            and not isinstance(publication["acquisition_commit"], str)
        )
        or (
            publication["remote"] is not None
            and not isinstance(publication["remote"], str)
        )
        or (
            publication["branch"] is not None
            and not isinstance(publication["branch"], str)
        )
    ):
        raise SchemaError("Run publication metadata does not match schema v2")
    acquisition_values = (
        publication["acquisition_commit"],
        publication["remote"],
        publication["branch"],
    )
    if any(value is None for value in acquisition_values) and not all(
        value is None for value in acquisition_values
    ):
        raise SchemaError("Run acquisition metadata must be recorded together")
    if outcome == "published" and not publication["content_commit"]:
        raise SchemaError("Published Run requires a content commit")
    if outcome == "published" and data["phase"] != phases[-1]:
        raise SchemaError("Published Run must be in the final Run Phase")
    if publication["content_commit"] is not None:
        if not publication["content_commit"]:
            raise SchemaError("Run content commit must not be empty")
        if data["phase"] != phases[-1]:
            raise SchemaError(
                "Run content commit can exist only in the final Run Phase"
            )
    for key in ("acquisition_commit", "remote", "branch"):
        if publication[key] is not None and not publication[key]:
            raise SchemaError(f"Run publication {key} must not be empty")


def _require_mutable(data: Mapping[str, Any]) -> None:
    if data["outcome"] is not None:
        raise TerminalRun(
            f"Run has immutable terminal outcome {data['outcome']!r}"
        )


def _safe_relative_text(value: Any) -> bool:
    try:
        relative_posix_path(value, label="Run path")
    except SafePathError:
        return False
    return True


def _vault_relative(value: Path | str, vault: Path) -> str:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = vault / candidate
    candidate = candidate.resolve()
    try:
        relative = candidate.relative_to(vault.resolve())
    except ValueError as exc:
        raise UnsafePath(f"Run Change Set path is outside the Vault: {candidate}") from exc
    text = relative.as_posix()
    if not text or text == ".":
        raise UnsafePath("Run Change Set cannot contain the Vault root")
    return text


def _artifact_reference(path: Path, *, vault: Path, run_dir: Path) -> dict[str, str]:
    try:
        candidate = anchored_file_path(path, label="Run Artifact")
    except SafeIOError as exc:
        raise ArtifactConflict(str(exc)) from exc
    for scope, root in (("run", run_dir.resolve()), ("vault", vault.resolve())):
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        text = relative.as_posix()
        if text and text != ".":
            return {"scope": scope, "path": text}
    raise UnsafePath(f"Run Artifact is outside the Run directory and Vault: {candidate}")


def _sha256_file(path: Path) -> str:
    try:
        return sha256_regular_file(
            anchored_file_path(path, label="Run Artifact"),
            max_bytes=MAX_ARTIFACT_BYTES,
            label="Run Artifact",
        )
    except SafeIOError as exc:
        raise ArtifactConflict(str(exc)) from exc


def _verify_artifacts(data: Mapping[str, Any]) -> None:
    vault = Path(data["paths"]["vault"])
    run_dir = Path(data["paths"]["run_dir"])
    roots = {"vault": vault, "run": run_dir}
    for artifact in data["artifacts"].values():
        root = roots[artifact["scope"]].resolve()
        try:
            path = resolve_within(
                root,
                artifact["path"],
                label="Run Artifact",
            )
        except SafePathError as exc:
            raise ArtifactConflict(
                f"Run Artifact escaped its verified root: {artifact['path']}"
            ) from exc
        if _sha256_file(path) != artifact["sha256"]:
            raise ArtifactConflict(
                f"Run Artifact differs from its verified checkpoint: {path}"
            )


def _require_change_set_artifacts(data: Mapping[str, Any]) -> None:
    """Require every existing claimed Vault path to have a verified artifact."""
    vault = Path(data["paths"]["vault"]).resolve()
    artifact_paths = {
        artifact["path"]
        for artifact in data["artifacts"].values()
        if artifact["scope"] == "vault"
    }
    for relative in data["run_change_set"]:
        try:
            changed = resolve_within(
                vault,
                relative,
                label="Run Change Set path",
            )
        except SafePathError as exc:
            raise ArtifactConflict(
                f"Run Change Set path cannot be inspected safely: {relative}"
            ) from exc
        try:
            changed.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ArtifactConflict(
                f"Run Change Set path cannot be inspected safely: {relative}"
            ) from exc
        if relative not in artifact_paths:
            raise CheckpointRequired(
                "Existing Run Change Set path requires a verified Artifact: "
                f"{relative}"
            )
