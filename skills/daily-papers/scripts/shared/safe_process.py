#!/usr/bin/env python3
"""Run one local argument-vector tool with bounded resources and output.

This module is for document-processing tools such as ``pdftotext`` and
``pdfimages``.  Git coordination has different durability semantics and does
not use this interface.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence


CHUNK_BYTES = 64 * 1024
READER_JOIN_SECONDS = 0.5


class SafeProcessError(RuntimeError):
    """A local tool could not complete within its safety contract."""


class ProcessTimeoutError(SafeProcessError):
    """A local tool exceeded its wall-clock deadline."""


class ProcessOutputLimitError(SafeProcessError):
    """A local tool exceeded a captured stream byte limit."""


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _positive_number(value: int | float, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{label} must be positive")


def _validated_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    if (
        isinstance(arguments, (str, bytes))
        or not arguments
        or any(
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            for argument in arguments
        )
    ):
        raise ValueError(
            "arguments must be a non-empty sequence of non-empty strings"
        )
    return tuple(arguments)


def _file_size_limiter(max_bytes: int | None) -> Callable[[], None] | None:
    if max_bytes is None:
        return None
    _positive_number(max_bytes, "max_file_bytes")
    if not isinstance(max_bytes, int):
        raise ValueError("max_file_bytes must be an integer")
    try:
        import resource
    except ImportError as exc:
        raise SafeProcessError(
            "This platform cannot enforce child output-file limits"
        ) from exc

    def apply_limit() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_bytes, max_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply_limit


def _validated_environment(
    environment: Mapping[str, str] | None,
) -> dict[str, str] | None:
    if environment is None:
        return None
    if not isinstance(environment, Mapping):
        raise ValueError("environment must be a string mapping")
    result: dict[str, str] = {}
    for key, value in environment.items():
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\x00" in key
            or not isinstance(value, str)
            or "\x00" in value
        ):
            raise ValueError("environment must contain safe string keys and values")
        result[key] = value
    return result


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError):
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass


def run_bounded_tool(
    arguments: Sequence[str],
    *,
    timeout: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    max_file_bytes: int | None = None,
    environment: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Run one tool without a shell and return bounded byte streams.

    The child starts in its own process group.  A timeout or stream overflow
    kills the complete group, drains both pipes, and waits for the child before
    returning an error.
    """
    command = _validated_arguments(arguments)
    _positive_number(timeout, "timeout")
    _positive_number(max_stdout_bytes, "max_stdout_bytes")
    _positive_number(max_stderr_bytes, "max_stderr_bytes")
    if not isinstance(max_stdout_bytes, int) or not isinstance(
        max_stderr_bytes, int
    ):
        raise ValueError("captured stream limits must be integers")
    limiter = _file_size_limiter(max_file_bytes)
    child_environment = _validated_environment(environment)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=limiter,
            env=child_environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SafeProcessError(f"Could not start local tool: {exc}") from exc
    assert process.stdout is not None and process.stderr is not None

    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    limits = {
        "stdout": max_stdout_bytes,
        "stderr": max_stderr_bytes,
    }
    overflows: set[str] = set()
    reader_errors: list[OSError] = []
    state_lock = threading.Lock()

    def drain(name: str, stream: object) -> None:
        try:
            while True:
                block = stream.read(CHUNK_BYTES)  # type: ignore[attr-defined]
                if not block:
                    return
                with state_lock:
                    remaining = limits[name] - len(outputs[name])
                    if remaining > 0:
                        outputs[name].extend(block[:remaining])
                    if len(block) > remaining:
                        overflows.add(name)
                        _kill_process_group(process)
        except OSError as exc:
            with state_lock:
                reader_errors.append(exc)
                _kill_process_group(process)
        finally:
            try:
                stream.close()  # type: ignore[attr-defined]
            except OSError:
                pass

    readers = [
        threading.Thread(
            target=drain,
            args=("stdout", process.stdout),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=("stderr", process.stderr),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(process)
        process.wait()
    finally:
        for reader in readers:
            reader.join(timeout=READER_JOIN_SECONDS)
        readers_lingered = any(reader.is_alive() for reader in readers)
        if readers_lingered:
            _kill_process_group(process)
            process.wait()
            for reader in readers:
                reader.join(timeout=READER_JOIN_SECONDS)
            if any(reader.is_alive() for reader in readers):
                raise SafeProcessError(
                    "Local tool output readers did not terminate"
                )

    if readers_lingered:
        raise SafeProcessError(
            "A descendant process outlived the local tool"
        )
    if reader_errors:
        raise SafeProcessError(
            f"Could not read local tool output: {reader_errors[0]}"
        ) from reader_errors[0]
    if overflows:
        names = ", ".join(sorted(overflows))
        raise ProcessOutputLimitError(
            f"Local tool exceeded its {names} byte limit"
        )
    if timed_out:
        raise ProcessTimeoutError(
            f"Local tool exceeded its {timeout:g}-second timeout"
        )
    return ProcessResult(
        returncode=process.returncode,
        stdout=bytes(outputs["stdout"]),
        stderr=bytes(outputs["stderr"]),
    )
