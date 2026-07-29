import hashlib
import importlib.util
import io
import socket
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "skills"
    / "daily-papers"
    / "scripts"
    / "shared"
    / "safe_http.py"
)
SPEC = importlib.util.spec_from_file_location("canonical_safe_http", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
safe_http = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = safe_http
SPEC.loader.exec_module(safe_http)


PUBLIC_IPV4 = "93.184.216.34"


def public_resolver(host: str, port: int, **_kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (PUBLIC_IPV4, port),
        )
    ]


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes = b"",
        *,
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.status = status
        self._body = io.BytesIO(body)
        self._headers = headers or []
        self.closed = False

    def getheaders(self):
        return list(self._headers)

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def set_timeout(self, _seconds: float) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, str]]] = []

    def request(self, target, _timeout: float, headers):
        self.requests.append((target.url, dict(headers)))
        return self.responses[target.url]


class SafeHTTPTests(unittest.TestCase):
    def test_aggregate_budget_is_atomic_across_concurrent_requests(self) -> None:
        budget = safe_http.FetchBudget(
            max_total_bytes=10,
            request_timeout_seconds=1,
            run_timeout_seconds=10,
        )

        def consume_one() -> bool:
            try:
                budget.consume(1)
            except safe_http.ResponseTooLargeError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=16) as executor:
            accepted = list(executor.map(lambda _index: consume_one(), range(64)))

        self.assertEqual(sum(accepted), 10)
        self.assertEqual(budget.consumed_bytes, 10)

    def client(self, responses: dict[str, FakeResponse]):
        transport = FakeTransport(responses)
        return (
            safe_http.SafeHTTPClient(
                resolver=public_resolver,
                transport=transport,
            ),
            transport,
        )

    @staticmethod
    def budget(client, max_total_bytes: int = 1024):
        return client.new_budget(
            max_total_bytes=max_total_bytes,
            request_timeout_seconds=2,
            run_timeout_seconds=5,
        )

    def test_fetch_bytes_returns_identity_and_sets_safe_headers(self) -> None:
        body = b'{"ok":true}'
        client, transport = self.client(
            {
                "https://example.com/data": FakeResponse(
                    200,
                    body,
                    headers=[
                        ("Content-Type", "application/json; charset=utf-8"),
                        ("Content-Length", str(len(body))),
                    ],
                )
            }
        )

        fetched = client.fetch_bytes(
            "https://example.com/data",
            max_bytes=128,
            budget=self.budget(client),
            accept="application/json",
            allowed_media_types={"application/json"},
        )

        self.assertEqual(fetched.body, body)
        self.assertEqual(fetched.bytes, len(body))
        self.assertEqual(fetched.sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(fetched.media_type, "application/json")
        self.assertEqual(fetched.final_url, "https://example.com/data")
        _, headers = transport.requests[0]
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertEqual(headers["Connection"], "close")
        self.assertEqual(headers["Accept"], "application/json")

    def test_redirect_to_private_address_is_rejected_before_request(self) -> None:
        client, transport = self.client(
            {
                "https://example.com/data": FakeResponse(
                    302,
                    headers=[("Location", "http://127.0.0.1/private")],
                )
            }
        )

        with self.assertRaises(safe_http.UnsafeURLError):
            client.fetch_bytes(
                "https://example.com/data",
                max_bytes=128,
                budget=self.budget(client),
            )

        self.assertEqual(
            [target for target, _headers in transport.requests],
            ["https://example.com/data"],
        )

    def test_stream_without_content_length_stops_at_byte_limit(self) -> None:
        response = FakeResponse(200, b"x" * 129)
        client, _transport = self.client(
            {"https://example.com/data": response}
        )

        with self.assertRaises(safe_http.ResponseTooLargeError):
            client.fetch_bytes(
                "https://example.com/data",
                max_bytes=128,
                budget=self.budget(client),
            )

        self.assertTrue(response.closed)

    def test_fetch_file_removes_partial_destination_on_failure(self) -> None:
        client, _transport = self.client(
            {"https://example.com/data": FakeResponse(200, b"x" * 129)}
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "download.bin"
            with self.assertRaises(safe_http.ResponseTooLargeError):
                client.fetch_file(
                    "https://example.com/data",
                    destination,
                    max_bytes=128,
                    budget=self.budget(client),
                )
            self.assertFalse(destination.exists())

    def test_fetched_file_prefix_reuses_identity_and_detects_mutation(
        self,
    ) -> None:
        body = b"\x89PNG\r\n\x1a\n" + b"x" * 32
        client, _transport = self.client(
            {
                "https://example.com/image": FakeResponse(
                    200,
                    body,
                    headers=[
                        ("Content-Type", "image/png"),
                        ("Content-Length", str(len(body))),
                    ],
                )
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "download.bin"
            fetched = client.fetch_file(
                "https://example.com/image",
                destination,
                max_bytes=128,
                budget=self.budget(client),
                allowed_media_types={"image/png"},
            )

            self.assertEqual(
                fetched.read_verified_prefix(8),
                b"\x89PNG\r\n\x1a\n",
            )
            destination.write_bytes(body[::-1])
            with self.assertRaisesRegex(
                safe_http.SafeHTTPError,
                "changed after",
            ):
                fetched.read_verified_prefix(8)

    def test_conflicting_content_length_is_rejected(self) -> None:
        client, _transport = self.client(
            {
                "https://example.com/data": FakeResponse(
                    200,
                    b"abc",
                    headers=[
                        ("Content-Length", "3"),
                        ("Content-Length", "4"),
                    ],
                )
            }
        )

        with self.assertRaisesRegex(
            safe_http.SafeHTTPError,
            "conflicting content-length",
        ):
            client.fetch_bytes(
                "https://example.com/data",
                max_bytes=128,
                budget=self.budget(client),
            )


if __name__ == "__main__":
    unittest.main()
