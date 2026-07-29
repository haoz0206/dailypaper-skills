#!/usr/bin/env python3
"""Bounded HTTP fetching with public-address validation and DNS pinning.

The interface deliberately has two operations: fetch a bounded response into
memory, or stream it into a new local file.  Both share the same redirect,
header, encoding, deadline, byte-budget, and destination policy.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import os
import socket
import ssl
import stat
import threading
import time
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit, urlunsplit


CHUNK_BYTES = 64 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_URL_LENGTH = 8192
MAX_RESOLVED_ADDRESSES = 32
DEFAULT_MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
STANDARD_PORTS = {"http": 80, "https": 443}
BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata.aws.internal",
    }
)
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home")


class SafeHTTPError(RuntimeError):
    """A remote response failed a deterministic safety invariant."""


class UnsafeURLError(SafeHTTPError):
    """A URL or one of its DNS answers is not safe to request."""


class ResponseTooLargeError(SafeHTTPError):
    """A response exceeded its per-request or aggregate byte budget."""


@dataclass(frozen=True)
class ValidatedURL:
    """One URL whose complete DNS answer set contains only public addresses."""

    url: str
    scheme: str
    host: str
    port: int
    request_target: str
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class FetchedBytes:
    body: bytes
    final_url: str
    media_type: str | None
    headers: Mapping[str, tuple[str, ...]]
    bytes: int
    sha256: str


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    links: int

    @classmethod
    def capture(cls, metadata: os.stat_result) -> "_FileIdentity":
        if not stat.S_ISREG(metadata.st_mode):
            raise SafeHTTPError("Downloaded artifact is no longer a regular file")
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
            links=metadata.st_nlink,
        )


def _open_verified_file(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise SafeHTTPError(
            "This platform cannot safely reopen downloaded artifacts"
        )
    try:
        return os.open(path, flags | nofollow)
    except OSError as exc:
        raise SafeHTTPError(
            f"Downloaded artifact cannot be reopened safely: {path}"
        ) from exc


@dataclass(frozen=True)
class FetchedFile:
    path: Path
    final_url: str
    media_type: str | None
    headers: Mapping[str, tuple[str, ...]]
    bytes: int
    sha256: str
    _identity: _FileIdentity = field(repr=False)

    def read_verified_prefix(self, max_bytes: int) -> bytes:
        """Read a small prefix only if this is still the exact fetched file."""
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
            or max_bytes > MAX_HEADER_BYTES
        ):
            raise ValueError(
                f"max_bytes must be between 1 and {MAX_HEADER_BYTES}"
            )
        descriptor = _open_verified_file(self.path)
        try:
            before = _FileIdentity.capture(os.fstat(descriptor))
            if before != self._identity or before.size != self.bytes:
                raise SafeHTTPError(
                    "Downloaded artifact changed after the fetch completed"
                )
            chunks = bytearray()
            while len(chunks) < max_bytes:
                block = os.read(descriptor, max_bytes - len(chunks))
                if not block:
                    break
                chunks.extend(block)
            after = _FileIdentity.capture(os.fstat(descriptor))
            if after != before:
                raise SafeHTTPError(
                    "Downloaded artifact changed while it was inspected"
                )
            return bytes(chunks)
        except OSError as exc:
            raise SafeHTTPError(
                f"Downloaded artifact cannot be inspected safely: {self.path}"
            ) from exc
        finally:
            os.close(descriptor)


class FetchBudget:
    """One aggregate byte budget and monotonic deadline shared by requests."""

    def __init__(
        self,
        *,
        max_total_bytes: int,
        request_timeout_seconds: float,
        run_timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(max_total_bytes, bool)
            or not isinstance(max_total_bytes, int)
            or max_total_bytes <= 0
        ):
            raise ValueError("max_total_bytes must be a positive integer")
        if request_timeout_seconds <= 0 or run_timeout_seconds <= 0:
            raise ValueError("HTTP timeouts must be positive")
        self.max_total_bytes = max_total_bytes
        self.request_timeout_seconds = request_timeout_seconds
        self.run_timeout_seconds = run_timeout_seconds
        self._clock = clock
        self._started = clock()
        self._consumed_bytes = 0
        self._lock = threading.Lock()

    @property
    def consumed_bytes(self) -> int:
        with self._lock:
            return self._consumed_bytes

    def remaining_seconds(self) -> float:
        remaining = self.run_timeout_seconds - (self._clock() - self._started)
        if remaining <= 0:
            raise SafeHTTPError("HTTP fetch exceeded its run deadline")
        return remaining

    def request_timeout(self) -> float:
        return min(self.request_timeout_seconds, self.remaining_seconds())

    def ensure_available(self, count: int) -> None:
        if count < 0:
            raise SafeHTTPError("Remote response declared a negative size")
        with self._lock:
            if self._consumed_bytes + count > self.max_total_bytes:
                raise ResponseTooLargeError(
                    "Remote responses exceed the aggregate byte budget"
                )

    def consume(self, count: int) -> None:
        if count < 0:
            raise SafeHTTPError("Remote response declared a negative size")
        with self._lock:
            if self._consumed_bytes + count > self.max_total_bytes:
                raise ResponseTooLargeError(
                    "Remote responses exceed the aggregate byte budget"
                )
            self._consumed_bytes += count


def _is_public_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    return bool(address.is_global) and not any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _normalized_host(host: str) -> str:
    candidate = host.rstrip(".").casefold()
    if not candidate:
        raise UnsafeURLError("Remote URL requires a hostname")
    try:
        return candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeURLError("Remote URL hostname is not valid IDNA") from exc


def _resolved_addresses(
    host: str,
    port: int,
    *,
    resolver: Callable[..., Iterable[tuple[Any, ...]]],
) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        try:
            results = resolver(
                host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except OSError as exc:
            raise UnsafeURLError(
                f"Cannot safely resolve remote hostname {host!r}"
            ) from exc
        addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        for result in results:
            try:
                raw_address = str(result[4][0]).split("%", 1)[0]
                addresses.add(ipaddress.ip_address(raw_address))
            except (IndexError, TypeError, ValueError) as exc:
                raise UnsafeURLError(
                    f"Resolver returned an invalid address for {host!r}"
                ) from exc
        if not addresses:
            raise UnsafeURLError(
                f"Remote hostname {host!r} resolved to no addresses"
            )
    else:
        addresses = {literal}

    unsafe = sorted(
        str(address) for address in addresses if not _is_public_address(address)
    )
    if unsafe:
        raise UnsafeURLError(
            f"Remote hostname {host!r} resolves to non-public address(es): "
            + ", ".join(unsafe)
        )
    if len(addresses) > MAX_RESOLVED_ADDRESSES:
        raise UnsafeURLError(
            f"Remote hostname {host!r} resolves to too many addresses"
        )
    return tuple(sorted(str(address) for address in addresses))


def validate_remote_url(
    value: str,
    *,
    resolver: Callable[..., Iterable[tuple[Any, ...]]] = socket.getaddrinfo,
) -> ValidatedURL:
    """Validate one hop and pin its complete public DNS answer set."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise UnsafeURLError("Remote URL must be a non-empty trimmed string")
    if len(value) > MAX_URL_LENGTH:
        raise UnsafeURLError("Remote URL exceeds the length limit")
    if "\\" in value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise UnsafeURLError("Remote URL contains unsafe characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("Remote URL is malformed") from exc
    scheme = parsed.scheme.casefold()
    if scheme not in STANDARD_PORTS:
        raise UnsafeURLError("Remote URL scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("Remote URL must not contain credentials")
    if parsed.fragment:
        raise UnsafeURLError("Remote URL must not contain a fragment")
    if not parsed.hostname:
        raise UnsafeURLError("Remote URL requires a hostname")
    host = _normalized_host(parsed.hostname)
    if host in BLOCKED_HOSTS or any(
        host.endswith(suffix) for suffix in BLOCKED_HOST_SUFFIXES
    ):
        raise UnsafeURLError(f"Remote hostname {host!r} is blocked")
    expected_port = STANDARD_PORTS[scheme]
    actual_port = expected_port if port is None else port
    if actual_port != expected_port:
        raise UnsafeURLError(
            f"Remote URL must use the standard {scheme} port {expected_port}"
        )
    addresses = _resolved_addresses(host, actual_port, resolver=resolver)
    path = parsed.path or "/"
    if not path.startswith("/"):
        raise UnsafeURLError("Remote URL path must use origin form")
    request_target = path + (f"?{parsed.query}" if parsed.query else "")
    display_host = f"[{host}]" if ":" in host else host
    normalized = urlunsplit((scheme, display_host, path, parsed.query, ""))
    return ValidatedURL(
        url=normalized,
        scheme=scheme,
        host=host,
        port=actual_port,
        request_target=request_target,
        addresses=addresses,
    )


class _ResponseHandle:
    def __init__(
        self,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
    ) -> None:
        self._connection = connection
        self._response = response
        self.status = response.status

    def getheaders(self) -> list[tuple[str, str]]:
        return self._response.getheaders()

    def set_timeout(self, seconds: float) -> None:
        if self._connection.sock is not None:
            self._connection.sock.settimeout(seconds)

    def read(self, size: int = -1) -> bytes:
        return self._response.read(size)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


class PinnedHTTPTransport:
    """Production adapter: connect to a validated address without re-resolving."""

    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl_context or ssl.create_default_context()

    def request(
        self,
        target: ValidatedURL,
        timeout: float,
        headers: Mapping[str, str],
    ) -> _ResponseHandle:
        errors: list[str] = []
        deadline = time.monotonic() + timeout
        for address in target.addresses:
            raw_socket: socket.socket | None = None
            connection: http.client.HTTPConnection | None = None
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SafeHTTPError(
                        f"Connections to {target.host!r} exceeded the timeout"
                    )
                raw_socket = socket.create_connection(
                    (address, target.port),
                    timeout=remaining,
                )
                raw_socket.settimeout(max(deadline - time.monotonic(), 0.001))
                if target.scheme == "https":
                    raw_socket = self._ssl_context.wrap_socket(
                        raw_socket,
                        server_hostname=target.host,
                    )
                    raw_socket.settimeout(
                        max(deadline - time.monotonic(), 0.001)
                    )
                connection = http.client.HTTPConnection(
                    target.host,
                    target.port,
                    timeout=remaining,
                )
                connection.sock = raw_socket
                raw_socket = None
                connection.request(
                    "GET",
                    target.request_target,
                    headers=dict(headers),
                )
                return _ResponseHandle(connection, connection.getresponse())
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                errors.append(f"{address}: {exc}")
                if connection is not None:
                    connection.close()
                if raw_socket is not None:
                    raw_socket.close()
        raise SafeHTTPError(
            f"Could not connect to validated host {target.host!r}: "
            + "; ".join(errors)
        )


def _normalized_headers(response: Any) -> dict[str, tuple[str, ...]]:
    collected: dict[str, list[str]] = {}
    size = 0
    try:
        raw_headers = response.getheaders()
    except (AttributeError, http.client.HTTPException) as exc:
        raise SafeHTTPError("Remote response headers cannot be read") from exc
    for name, value in raw_headers:
        key = str(name).strip().casefold()
        item = str(value).strip()
        size += len(key.encode("utf-8")) + len(item.encode("utf-8")) + 4
        if size > MAX_HEADER_BYTES:
            raise SafeHTTPError("Remote response headers exceed the safety limit")
        collected.setdefault(key, []).append(item)
    return {key: tuple(values) for key, values in collected.items()}


def _single_header(
    headers: Mapping[str, tuple[str, ...]],
    name: str,
    *,
    required: bool = False,
) -> str | None:
    values = headers.get(name, ())
    if not values:
        if required:
            raise SafeHTTPError(f"Remote response requires the {name} header")
        return None
    if len(set(values)) != 1:
        raise SafeHTTPError(
            f"Remote response contains conflicting {name} headers"
        )
    return values[0]


def _media_type(
    headers: Mapping[str, tuple[str, ...]],
) -> str | None:
    declared = _single_header(headers, "content-type")
    if declared is None:
        return None
    normalized = declared.split(";", 1)[0].strip().casefold()
    return "image/jpeg" if normalized == "image/jpg" else normalized


def _content_length(headers: Mapping[str, tuple[str, ...]]) -> int | None:
    value = _single_header(headers, "content-length")
    if value is None:
        return None
    try:
        length = int(value, 10)
    except ValueError as exc:
        raise SafeHTTPError("Remote Content-Length must be an integer") from exc
    if length < 0:
        raise SafeHTTPError("Remote Content-Length must not be negative")
    return length


class SafeHTTPClient:
    """Deep interface for bounded in-memory and file HTTP responses."""

    def __init__(
        self,
        *,
        resolver: Callable[..., Iterable[tuple[Any, ...]]] = socket.getaddrinfo,
        transport: Any | None = None,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ) -> None:
        if (
            isinstance(max_redirects, bool)
            or not isinstance(max_redirects, int)
            or max_redirects < 0
        ):
            raise ValueError("max_redirects must be a non-negative integer")
        self.resolver = resolver
        self.transport = transport or PinnedHTTPTransport()
        self.max_redirects = max_redirects

    def new_budget(
        self,
        *,
        max_total_bytes: int,
        request_timeout_seconds: float,
        run_timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> FetchBudget:
        return FetchBudget(
            max_total_bytes=max_total_bytes,
            request_timeout_seconds=request_timeout_seconds,
            run_timeout_seconds=run_timeout_seconds,
            clock=clock,
        )

    def fetch_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        budget: FetchBudget,
        accept: str = "*/*",
        allowed_media_types: Iterable[str] | None = None,
    ) -> FetchedBytes:
        buffer = io.BytesIO()
        result = self._fetch_stream(
            url,
            max_bytes=max_bytes,
            budget=budget,
            accept=accept,
            allowed_media_types=allowed_media_types,
            open_sink=lambda: nullcontext(buffer),
        )
        return FetchedBytes(body=buffer.getvalue(), **result)

    def fetch_file(
        self,
        url: str,
        destination: Path,
        *,
        max_bytes: int,
        budget: FetchBudget,
        accept: str = "*/*",
        allowed_media_types: Iterable[str] | None = None,
    ) -> FetchedFile:
        candidate = destination.expanduser()
        try:
            parent = candidate.parent.resolve(strict=True)
        except OSError as exc:
            raise SafeHTTPError(
                f"Fetch destination parent is unavailable: {candidate.parent}"
            ) from exc
        target = parent / candidate.name
        if target.exists() or target.is_symlink():
            raise SafeHTTPError(
                f"Fetch destination already exists: {target}"
            )
        try:
            result = self._fetch_stream(
                url,
                max_bytes=max_bytes,
                budget=budget,
                accept=accept,
                allowed_media_types=allowed_media_types,
                open_sink=lambda: target.open("xb"),
            )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        try:
            descriptor = _open_verified_file(target)
            try:
                identity = _FileIdentity.capture(os.fstat(descriptor))
            finally:
                os.close(descriptor)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        if identity.size != result["bytes"]:
            target.unlink(missing_ok=True)
            raise SafeHTTPError(
                "Downloaded artifact size changed after the fetch completed"
            )
        return FetchedFile(path=target, _identity=identity, **result)

    def _fetch_stream(
        self,
        url: str,
        *,
        max_bytes: int,
        budget: FetchBudget,
        accept: str,
        allowed_media_types: Iterable[str] | None,
        open_sink: Callable[[], AbstractContextManager[Any]],
    ) -> dict[str, Any]:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer")
        allowed = (
            {value.casefold() for value in allowed_media_types}
            if allowed_media_types is not None
            else None
        )
        current_url = url
        request_headers = {
            "Accept": accept,
            "Accept-Encoding": "identity",
            "Connection": "close",
            "User-Agent": "dailypaper-skills/1",
        }
        for redirect_count in range(self.max_redirects + 1):
            target = validate_remote_url(current_url, resolver=self.resolver)
            response = None
            try:
                response = self.transport.request(
                    target,
                    budget.request_timeout(),
                    request_headers,
                )
                headers = _normalized_headers(response)
                if response.status in REDIRECT_STATUSES:
                    location = _single_header(
                        headers,
                        "location",
                        required=True,
                    )
                    if location is None:
                        raise SafeHTTPError(
                            "Redirect response is missing one Location header"
                        )
                    if redirect_count >= self.max_redirects:
                        raise SafeHTTPError(
                            "Remote response exceeded the redirect limit"
                        )
                    current_url = urljoin(target.url, location)
                    continue
                if response.status != 200:
                    raise SafeHTTPError(
                        f"Remote server returned HTTP {response.status}"
                    )
                encoding = _single_header(headers, "content-encoding")
                if encoding is not None and encoding.casefold() not in {
                    "",
                    "identity",
                }:
                    raise SafeHTTPError(
                        "Compressed HTTP response bodies are not accepted"
                    )
                media_type = _media_type(headers)
                if allowed is not None and media_type not in allowed:
                    raise SafeHTTPError(
                        f"Remote Content-Type {media_type!r} is not allowed"
                    )
                declared_length = _content_length(headers)
                if declared_length is not None:
                    if declared_length > max_bytes:
                        raise ResponseTooLargeError(
                            "Remote response exceeds the per-request byte limit"
                        )
                    budget.ensure_available(declared_length)

                written = 0
                digest = hashlib.sha256()
                with open_sink() as output:
                    while True:
                        response.set_timeout(budget.request_timeout())
                        block = response.read(CHUNK_BYTES)
                        if not block:
                            break
                        written += len(block)
                        if written > max_bytes:
                            raise ResponseTooLargeError(
                                "Remote response exceeds the per-request byte limit"
                            )
                        budget.consume(len(block))
                        digest.update(block)
                        output.write(block)
                    if hasattr(output, "flush"):
                        output.flush()
                    try:
                        descriptor = output.fileno()
                    except (AttributeError, io.UnsupportedOperation):
                        pass
                    else:
                        os.fsync(descriptor)
                if written <= 0:
                    raise SafeHTTPError("Remote response body is empty")
                if declared_length is not None and written != declared_length:
                    raise SafeHTTPError(
                        "Remote response body length does not match Content-Length"
                    )
                return {
                    "final_url": target.url,
                    "media_type": media_type,
                    "headers": headers,
                    "bytes": written,
                    "sha256": digest.hexdigest(),
                }
            except SafeHTTPError:
                raise
            except (OSError, TimeoutError, http.client.HTTPException) as exc:
                raise SafeHTTPError(f"Remote request failed: {exc}") from exc
            finally:
                if response is not None:
                    response.close()
        raise SafeHTTPError("Remote response exceeded the redirect limit")
