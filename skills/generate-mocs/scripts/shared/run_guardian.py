#!/usr/bin/env python3
"""Hold a run-wide local execution lock across short-lived CLI commands.

The guardian is intentionally independent from Run Manifest and Git state.  It
owns a Unix ``flock`` for its lifetime and exposes a small Unix socket interface
for cooperating local callers.  Its capability prevents accidental cross-run
requests; it is not an OS security boundary between processes running as the
same Unix user.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import secrets
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from safe_git import SafeGitError, run_git_command
from safe_io import (
    SafeIOError,
    anchored_file_path,
    atomic_write_json,
    load_json_object,
    parse_json_object,
)
from safe_process import SafeProcessError, run_bounded_tool


SESSION_VERSION = 1
LOCK_NAME = "execution.lock"
SOCKET_NAME = "guardian.sock"
SESSION_NAME = "guardian-session.json"
# Coordinated production runs must retain ownership until an explicit terminal
# transition or process death.  A finite timeout remains available to direct
# low-level callers and tests, but is never the default.
DEFAULT_IDLE_TIMEOUT_SECONDS: float | None = None
DEFAULT_READY_TIMEOUT_SECONDS = 5.0
READY_PROBE_TIMEOUT_SECONDS = 0.1
READY_POLL_INTERVAL_SECONDS = 0.02
MAX_SESSION_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 64 * 1024
_STARTED_GUARDIANS: list[subprocess.Popen[bytes]] = []


class GuardianError(RuntimeError):
    """Base class for expected guardian failures."""


class GuardianAlreadyRunning(GuardianError):
    """The run directory already has a live execution lock holder."""


class GuardianUnavailable(GuardianError):
    """No responsive guardian is available for the run directory."""


class GuardianUnauthorized(GuardianError):
    """The session capability was rejected."""


def vault_writer_lock_path(vault: Path) -> Path:
    """Return one lock path shared by every writer in the same Git clone."""
    resolved = vault.expanduser().resolve()
    try:
        result = run_git_command(
            resolved,
            "rev-parse",
            "--git-common-dir",
        )
    except SafeGitError as exc:
        raise GuardianUnavailable(
            f"Could not resolve the Vault Git directory: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise GuardianUnavailable(
            f"Could not resolve the Vault Git directory: {detail}"
        )
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = (resolved / common).resolve()
    return common / "dailypaper" / "vault-writer.lock"


def _open_exclusive_lock(
    path: Path,
    *,
    busy_message: str,
    label: str = "Vault writer lock",
):
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise GuardianUnavailable(
            f"{label} must not be a symlink: {candidate}"
        )
    try:
        resolved = anchored_file_path(candidate, label=label)
    except SafeIOError as exc:
        raise GuardianUnavailable(str(exc)) from exc
    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            resolved,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise GuardianUnavailable(
            f"{label} cannot be opened safely: {resolved}"
        ) from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise GuardianUnavailable(
            f"{label} is not a regular file: {resolved}"
        )
    stream = os.fdopen(descriptor, "a+", encoding="utf-8")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        stream.close()
        raise GuardianAlreadyRunning(busy_message) from exc
    return stream


@contextmanager
def hold_vault_writer_lock(vault: Path):
    """Hold the same non-blocking writer lock used by every Run guardian."""
    stream = _open_exclusive_lock(
        vault_writer_lock_path(vault),
        busy_message="Another DailyPaper writer already owns this Vault",
    )
    try:
        yield
    finally:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


@dataclass(frozen=True)
class GuardianPaths:
    run_dir: Path
    lock: Path
    socket: Path
    session: Path

    @classmethod
    def for_run(cls, run_dir: Path) -> "GuardianPaths":
        resolved = run_dir.expanduser().resolve()
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:32]
        socket_name = f"dpg-{os.getuid()}-{digest}.sock"
        return cls(
            run_dir=resolved,
            lock=resolved / LOCK_NAME,
            socket=Path("/tmp") / socket_name,
            session=resolved / SESSION_NAME,
        )


def _process_start_marker(pid: int) -> str | None:
    """Return a marker that changes when a PID is reused."""
    if pid <= 0:
        return None

    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        # Linux: field 22 is the process start time in clock ticks.  The second
        # field may contain spaces, so split only after the final right paren.
        suffix = proc_stat.read_text(encoding="utf-8").rsplit(")", 1)[1]
        fields_after_comm = suffix.split()
        # A zombie has already released every OS lock and only awaits reaping
        # by its parent, so it is no longer a live guardian owner.
        if fields_after_comm[0] == "Z":
            return None
        return f"linux:{fields_after_comm[19]}"
    except (FileNotFoundError, IndexError, OSError):
        pass

    # macOS (and other POSIX systems with ps): lstart is stable for a process
    # lifetime and sufficient to detect ordinary PID reuse.
    try:
        result = run_bounded_tool(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            timeout=2,
            max_stdout_bytes=4096,
            max_stderr_bytes=4096,
        )
        marker = result.stdout.decode("utf-8").strip()
    except (SafeProcessError, UnicodeDecodeError):
        return None
    return f"ps:{marker}" if result.returncode == 0 and marker else None


def process_start_marker(pid: int | None = None) -> str:
    """Return the current process marker for use with ``--owner-start-marker``."""
    target = pid if pid is not None else os.getpid()
    marker = _process_start_marker(target)
    if marker is None:
        raise GuardianUnavailable(f"Process {target} is not running")
    return marker


def _atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        atomic_write_json(
            path,
            payload,
            max_bytes=MAX_SESSION_BYTES,
            mode=0o600,
            label="Guardian session",
        )
    except SafeIOError as exc:
        raise GuardianUnavailable(str(exc)) from exc


def _read_session(paths: GuardianPaths) -> dict[str, Any]:
    try:
        data = load_json_object(
            paths.session,
            max_bytes=MAX_SESSION_BYTES,
            label="Guardian session",
        )
        if data is None:  # Defensive: required=True must already reject this state.
            raise SafeIOError("Guardian session file does not exist")
    except SafeIOError as exc:
        raise GuardianUnavailable(
            f"No readable guardian session for {paths.run_dir}"
        ) from exc
    if data.get("version") != SESSION_VERSION:
        raise GuardianUnavailable(
            f"Unsupported guardian session version: {data.get('version')}"
        )
    if not isinstance(data.get("capability"), str):
        raise GuardianUnavailable("Guardian session has no capability")
    return data


def request(
    run_dir: Path,
    action: str,
    *,
    capability: str | None = None,
    timeout: float = 1.0,
) -> dict[str, Any]:
    """Send one authenticated request to a live guardian."""
    paths = GuardianPaths.for_run(run_dir)
    session = _read_session(paths)
    token = capability or str(session["capability"])
    message = json.dumps(
        {"action": action, "capability": token},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"

    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(paths.socket))
        client.sendall(message)
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_MESSAGE_BYTES:
                raise GuardianUnavailable(
                    "Guardian response exceeds the safety limit"
                )
    except (FileNotFoundError, ConnectionError, socket.timeout, OSError) as exc:
        raise GuardianUnavailable(
            f"Guardian for {paths.run_dir} is not responding"
        ) from exc
    finally:
        client.close()

    try:
        response = parse_json_object(
            bytes(chunks),
            max_bytes=MAX_MESSAGE_BYTES,
            label="Guardian response",
        )
    except SafeIOError as exc:
        raise GuardianUnavailable("Guardian returned an invalid response") from exc
    if response.get("status") == "unauthorized":
        raise GuardianUnauthorized("Guardian capability was rejected")
    if response.get("status") != "ok":
        raise GuardianError(str(response.get("message", "Guardian request failed")))
    return response


def probe_guardian(run_dir: Path, *, timeout: float = 1.0) -> dict[str, Any]:
    """Probe a guardian from a short-lived CLI helper."""
    return request(run_dir, "ping", timeout=timeout)


def guardian_status(run_dir: Path, *, timeout: float = 1.0) -> dict[str, Any]:
    return request(run_dir, "status", timeout=timeout)


def stop_guardian(run_dir: Path, *, timeout: float = 1.0) -> dict[str, Any]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive")
    deadline = time.monotonic() + timeout
    response = request(run_dir, "stop", timeout=timeout)
    stopped_pid = response.get("pid")
    stopped_marker = response.get("process_start_marker")
    for process in tuple(_STARTED_GUARDIANS):
        if process.pid != stopped_pid:
            continue
        try:
            process.wait(timeout=max(deadline - time.monotonic(), 0.001))
        except subprocess.TimeoutExpired:
            pass
        break
    while True:
        try:
            status_result = guardian_status(
                run_dir,
                timeout=min(
                    READY_PROBE_TIMEOUT_SECONDS,
                    max(deadline - time.monotonic(), 0.001),
                ),
            )
        except GuardianError:
            if isinstance(stopped_pid, int):
                observed_marker = _process_start_marker(stopped_pid)
                if observed_marker is not None and (
                    not isinstance(stopped_marker, str)
                    or observed_marker == stopped_marker
                ):
                    if time.monotonic() >= deadline:
                        raise GuardianUnavailable(
                            f"Guardian process {stopped_pid} did not stop "
                            "before the deadline"
                        )
                    time.sleep(
                        min(
                            READY_POLL_INTERVAL_SECONDS,
                            max(deadline - time.monotonic(), 0.001),
                        )
                    )
                    continue
            _reap_started_guardians()
            return response
        if status_result.get("pid") != stopped_pid:
            raise GuardianAlreadyRunning(
                "A replacement guardian acquired the Run before stop completed"
            )
        if time.monotonic() >= deadline:
            raise GuardianUnavailable(
                f"Guardian process {stopped_pid} did not stop before the deadline"
            )
        time.sleep(
            min(
                READY_POLL_INTERVAL_SECONDS,
                max(deadline - time.monotonic(), 0.001),
            )
        )


def _reap_started_guardians() -> None:
    _STARTED_GUARDIANS[:] = [
        process for process in _STARTED_GUARDIANS if process.poll() is None
    ]


def _terminate_started_guardian(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired as exc:
                raise GuardianUnavailable(
                    "Timed-out guardian process could not be stopped"
                ) from exc
    _reap_started_guardians()


def _validate_guardian_status(
    run_dir: Path,
    *,
    vault_lock: Path,
    status_result: dict[str, Any],
) -> dict[str, Any]:
    if status_result.get("run_dir") != str(run_dir):
        raise GuardianUnavailable("Guardian reported an unexpected Run directory")
    if status_result.get("vault_lock_path") != str(vault_lock):
        raise GuardianUnavailable(
            "Guardian is bound to a different Vault writer lock"
        )
    return status_result


def ensure_guardian_running(
    run_dir: Path,
    *,
    vault: Path,
    idle_timeout_seconds: float | None = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ready_timeout_seconds: float = DEFAULT_READY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Return one ready guardian, launching and cleaning up a child if needed."""
    if idle_timeout_seconds is not None and (
        not math.isfinite(idle_timeout_seconds) or idle_timeout_seconds <= 0
    ):
        raise ValueError("idle_timeout_seconds must be positive or None")
    if not math.isfinite(ready_timeout_seconds) or ready_timeout_seconds <= 0:
        raise ValueError("ready_timeout_seconds must be positive")

    resolved_run = GuardianPaths.for_run(run_dir).run_dir
    vault_lock = vault_writer_lock_path(vault)
    try:
        status_result = guardian_status(
            resolved_run,
            timeout=min(READY_PROBE_TIMEOUT_SECONDS, ready_timeout_seconds),
        )
    except GuardianError:
        pass
    else:
        return _validate_guardian_status(
            resolved_run,
            vault_lock=vault_lock,
            status_result=status_result,
        )
    _reap_started_guardians()
    try:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "serve",
            str(resolved_run),
            "--vault-lock",
            str(vault_lock),
        ]
        if idle_timeout_seconds is not None:
            command.extend(["--idle-timeout", str(idle_timeout_seconds)])
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise GuardianUnavailable(f"Guardian process could not be started: {exc}") from exc
    _STARTED_GUARDIANS.append(process)

    deadline = time.monotonic() + ready_timeout_seconds
    last_error: GuardianError | None = None
    while True:
        try:
            status_result = guardian_status(
                resolved_run,
                timeout=min(
                    READY_PROBE_TIMEOUT_SECONDS,
                    max(deadline - time.monotonic(), 0.001),
                ),
            )
            return _validate_guardian_status(
                resolved_run,
                vault_lock=vault_lock,
                status_result=status_result,
            )
        except GuardianError as exc:
            last_error = exc

        return_code = process.poll()
        if return_code is not None:
            _reap_started_guardians()
            raise GuardianUnavailable(
                "Guardian process exited before readiness "
                f"(status {return_code}): {last_error}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_started_guardian(process)
            raise GuardianUnavailable(
                f"Guardian did not become ready before the deadline: {last_error}"
            )
        time.sleep(min(READY_POLL_INTERVAL_SECONDS, remaining))


class RunGuardian:
    """Long-lived, sole local holder of one run's execution lock."""

    def __init__(
        self,
        run_dir: Path,
        *,
        owner_pid: int | None = None,
        owner_start_marker: str | None = None,
        vault_lock_path: Path | None = None,
        idle_timeout_seconds: float | None = DEFAULT_IDLE_TIMEOUT_SECONDS,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        if idle_timeout_seconds is not None and (
            not math.isfinite(idle_timeout_seconds) or idle_timeout_seconds <= 0
        ):
            raise ValueError("idle_timeout_seconds must be positive or None")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if owner_start_marker is not None and owner_pid is None:
            raise ValueError("owner_start_marker requires owner_pid")

        self.paths = GuardianPaths.for_run(run_dir)
        self.owner_pid = owner_pid
        self.owner_start_marker = (
            owner_start_marker
            if owner_start_marker is not None
            else (_process_start_marker(owner_pid) if owner_pid is not None else None)
        )
        if vault_lock_path is None:
            self.vault_lock_path = None
        else:
            candidate = vault_lock_path.expanduser()
            if candidate.is_symlink():
                raise GuardianUnavailable(
                    f"Vault writer lock must not be a symlink: {candidate}"
                )
            try:
                self.vault_lock_path = anchored_file_path(
                    candidate,
                    label="Vault writer lock",
                )
            except SafeIOError as exc:
                raise GuardianUnavailable(str(exc)) from exc
        if owner_pid is not None and self.owner_start_marker is None:
            raise GuardianUnavailable(f"Owner process {owner_pid} is not running")
        self.idle_timeout_seconds = idle_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.capability = secrets.token_urlsafe(32)
        self.process_start_marker = _process_start_marker(os.getpid())
        if self.process_start_marker is None:
            raise GuardianUnavailable("Could not identify the guardian process")
        self.started_monotonic = time.monotonic()
        self.last_activity = self.started_monotonic
        self._stopping = False
        self._lock_stream: Any = None
        self._vault_lock_stream: Any = None
        self._server: socket.socket | None = None
        self._owns_lock = False

    def _acquire(self) -> None:
        self.paths.run_dir.mkdir(parents=True, exist_ok=True)
        if self.vault_lock_path is not None:
            self._vault_lock_stream = _open_exclusive_lock(
                self.vault_lock_path,
                busy_message="Another DailyPaper writer already owns this Vault",
            )
        self._lock_stream = _open_exclusive_lock(
            self.paths.lock,
            busy_message=(
                f"Execution lock is already held for {self.paths.run_dir}"
            ),
            label="Run execution lock",
        )
        self._owns_lock = True

        # Stale endpoints can remain after SIGKILL.  They are safe to remove
        # only after this process has acquired the execution lock.
        for endpoint in (self.paths.socket, self.paths.session):
            try:
                endpoint.unlink()
            except FileNotFoundError:
                pass

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.paths.socket))
        os.chmod(self.paths.socket, 0o600)
        self._server.listen(8)
        self._server.settimeout(self.poll_interval_seconds)

        _atomic_write_private_json(
            self.paths.session,
            {
                "version": SESSION_VERSION,
                "pid": os.getpid(),
                "process_start_marker": self.process_start_marker,
                "socket": str(self.paths.socket),
                "capability": self.capability,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "owner_pid": self.owner_pid,
                "owner_start_marker": self.owner_start_marker,
                "idle_timeout_seconds": self.idle_timeout_seconds,
                "vault_lock_path": (
                    str(self.vault_lock_path)
                    if self.vault_lock_path is not None
                    else None
                ),
            },
        )

    def _owner_is_alive(self) -> bool:
        if self.owner_pid is None:
            return True
        marker = _process_start_marker(self.owner_pid)
        if marker is not None:
            return marker == self.owner_start_marker
        try:
            os.kill(self.owner_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _idle_expired(self) -> bool:
        # Owner liveness, checked by the serve loop, is authoritative.  Idle is
        # only a fallback for guardians launched without an owner identity.
        if self.owner_pid is not None:
            return False
        return (
            self.idle_timeout_seconds is not None
            and time.monotonic() - self.last_activity >= self.idle_timeout_seconds
        )

    def _response(self, action: str) -> dict[str, Any]:
        now = time.monotonic()
        common = {
            "status": "ok",
            "action": action,
            "pid": os.getpid(),
            "process_start_marker": self.process_start_marker,
            "run_dir": str(self.paths.run_dir),
        }
        if action == "ping":
            return common
        if action == "status":
            return {
                **common,
                "uptime_seconds": now - self.started_monotonic,
                "idle_seconds": now - self.last_activity,
                "owner_pid": self.owner_pid,
                "owner_alive": self._owner_is_alive(),
                "idle_timeout_seconds": self.idle_timeout_seconds,
                "vault_lock_path": (
                    str(self.vault_lock_path)
                    if self.vault_lock_path is not None
                    else None
                ),
            }
        if action == "stop":
            self._stopping = True
            return common
        return {
            "status": "error",
            "action": action,
            "message": f"Unknown guardian action: {action}",
        }

    def _serve_client(self, client: socket.socket) -> None:
        client.settimeout(1.0)
        raw = bytearray()
        try:
            while not raw.endswith(b"\n") and len(raw) <= MAX_MESSAGE_BYTES:
                chunk = client.recv(MAX_MESSAGE_BYTES + 1 - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            request_data = parse_json_object(
                bytes(raw),
                max_bytes=MAX_MESSAGE_BYTES,
                label="Guardian request",
            )
            supplied = request_data.get("capability")
            if not isinstance(supplied, str) or not secrets.compare_digest(
                supplied,
                self.capability,
            ):
                response = {"status": "unauthorized"}
            else:
                self.last_activity = time.monotonic()
                response = self._response(str(request_data.get("action", "")))
        except (SafeIOError, OSError):
            response = {"status": "error", "message": "Invalid guardian request"}
        try:
            client.sendall(
                json.dumps(response, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
        except OSError:
            pass

    def _cleanup(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._owns_lock:
            for endpoint in (self.paths.socket, self.paths.session):
                try:
                    endpoint.unlink()
                except (FileNotFoundError, OSError):
                    pass
        if self._lock_stream is not None:
            fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_UN)
            self._lock_stream.close()
            self._lock_stream = None
        if self._vault_lock_stream is not None:
            fcntl.flock(self._vault_lock_stream.fileno(), fcntl.LOCK_UN)
            self._vault_lock_stream.close()
            self._vault_lock_stream = None
        self._owns_lock = False

    def serve_forever(self) -> None:
        try:
            self._acquire()
            while not self._stopping:
                if not self._owner_is_alive() or self._idle_expired():
                    break
                server = self._server
                if server is None:
                    raise GuardianUnavailable(
                        "Guardian server disappeared after lock acquisition"
                    )
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
                with client:
                    self._serve_client(client)
        finally:
            self._cleanup()


def _print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("run_dir", type=Path)
    serve_parser.add_argument("--owner-pid", type=int)
    serve_parser.add_argument("--owner-start-marker")
    serve_parser.add_argument("--vault-lock", type=Path)
    serve_parser.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT_SECONDS,
        help=(
            "Optional low-level fallback: exit after this many seconds without "
            "an authenticated request. Coordinated runs omit this option."
        ),
    )

    for command in ("probe", "status", "stop"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("run_dir", type=Path)
        command_parser.add_argument("--timeout", type=float, default=1.0)

    marker_parser = subparsers.add_parser("process-start-marker")
    marker_parser.add_argument("--pid", type=int)

    args = parser.parse_args()
    try:
        if args.command == "serve":
            RunGuardian(
                args.run_dir,
                owner_pid=args.owner_pid,
                owner_start_marker=args.owner_start_marker,
                vault_lock_path=args.vault_lock,
                idle_timeout_seconds=args.idle_timeout,
            ).serve_forever()
            return
        if args.command == "process-start-marker":
            print(process_start_marker(args.pid))
            return
        helper = {
            "probe": probe_guardian,
            "status": guardian_status,
            "stop": stop_guardian,
        }[args.command]
        _print_json(helper(args.run_dir, timeout=args.timeout))
    except GuardianAlreadyRunning as exc:
        _print_json({"status": "already-running", "message": str(exc)})
        raise SystemExit(3) from exc
    except GuardianError as exc:
        _print_json({"status": "unavailable", "message": str(exc)})
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
