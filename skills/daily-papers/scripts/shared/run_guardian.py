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
import os
import secrets
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SESSION_VERSION = 1
LOCK_NAME = "execution.lock"
SOCKET_NAME = "guardian.sock"
SESSION_NAME = "guardian-session.json"
DEFAULT_IDLE_TIMEOUT_SECONDS = 3600.0


class GuardianError(RuntimeError):
    """Base class for expected guardian failures."""


class GuardianAlreadyRunning(GuardianError):
    """The run directory already has a live execution lock holder."""


class GuardianUnavailable(GuardianError):
    """No responsive guardian is available for the run directory."""


class GuardianUnauthorized(GuardianError):
    """The session capability was rejected."""


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
        return f"linux:{fields_after_comm[19]}"
    except (FileNotFoundError, IndexError, OSError):
        pass

    # macOS (and other POSIX systems with ps): lstart is stable for a process
    # lifetime and sufficient to detect ordinary PID reuse.
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    marker = result.stdout.strip()
    return f"ps:{marker}" if result.returncode == 0 and marker else None


def process_start_marker(pid: int | None = None) -> str:
    """Return the current process marker for use with ``--owner-start-marker``."""
    target = pid if pid is not None else os.getpid()
    marker = _process_start_marker(target)
    if marker is None:
        raise GuardianUnavailable(f"Process {target} is not running")
    return marker


def _atomic_write_private_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_session(paths: GuardianPaths) -> dict[str, Any]:
    try:
        data = json.loads(paths.session.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
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
    except (FileNotFoundError, ConnectionError, socket.timeout, OSError) as exc:
        raise GuardianUnavailable(
            f"Guardian for {paths.run_dir} is not responding"
        ) from exc
    finally:
        client.close()

    try:
        response = json.loads(chunks.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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
    return request(run_dir, "stop", timeout=timeout)


class RunGuardian:
    """Long-lived, sole local holder of one run's execution lock."""

    def __init__(
        self,
        run_dir: Path,
        *,
        owner_pid: int | None = None,
        owner_start_marker: str | None = None,
        idle_timeout_seconds: float | None = DEFAULT_IDLE_TIMEOUT_SECONDS,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        if idle_timeout_seconds is not None and idle_timeout_seconds <= 0:
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
        if owner_pid is not None and self.owner_start_marker is None:
            raise GuardianUnavailable(f"Owner process {owner_pid} is not running")
        self.idle_timeout_seconds = idle_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.capability = secrets.token_urlsafe(32)
        self.started_monotonic = time.monotonic()
        self.last_activity = self.started_monotonic
        self._stopping = False
        self._lock_stream: Any = None
        self._server: socket.socket | None = None
        self._owns_lock = False

    def _acquire(self) -> None:
        self.paths.run_dir.mkdir(parents=True, exist_ok=True)
        self._lock_stream = self.paths.lock.open("a+", encoding="utf-8")
        try:
            fcntl.flock(
                self._lock_stream.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            self._lock_stream.close()
            self._lock_stream = None
            raise GuardianAlreadyRunning(
                f"Execution lock is already held for {self.paths.run_dir}"
            ) from exc
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
                "socket": str(self.paths.socket),
                "capability": self.capability,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "owner_pid": self.owner_pid,
                "owner_start_marker": self.owner_start_marker,
                "idle_timeout_seconds": self.idle_timeout_seconds,
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
            while not raw.endswith(b"\n") and len(raw) <= 65536:
                chunk = client.recv(65536)
                if not chunk:
                    break
                raw.extend(chunk)
            request_data = json.loads(raw.decode("utf-8"))
            supplied = request_data.get("capability")
            if not isinstance(supplied, str) or not secrets.compare_digest(
                supplied,
                self.capability,
            ):
                response = {"status": "unauthorized"}
            else:
                self.last_activity = time.monotonic()
                response = self._response(str(request_data.get("action", "")))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
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
        self._owns_lock = False

    def serve_forever(self) -> None:
        try:
            self._acquire()
            while not self._stopping:
                if not self._owner_is_alive() or self._idle_expired():
                    break
                assert self._server is not None
                try:
                    client, _ = self._server.accept()
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
    serve_parser.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT_SECONDS,
        help="Exit after this many seconds without an authenticated request",
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
