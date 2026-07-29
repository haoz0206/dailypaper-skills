import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "skills"
    / "daily-papers"
    / "scripts"
    / "daily"
    / "download_note_images.py"
)
SPEC = importlib.util.spec_from_file_location("canonical_download_note_images", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
download_note_images = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = download_note_images
SPEC.loader.exec_module(download_note_images)


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
PDFIMAGES_LIST_ONE = b"""\
page   num  type   width height color comp bpc  enc interp  object ID x-ppi y-ppi size ratio
--------------------------------------------------------------------------------------------
   1     0 image       1     1  rgb     3   8  image  no         1  0    72    72    3B 0.0%
"""
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
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = io.BytesIO(body)
        self._headers = list((headers or {}).items())
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
    def __init__(self, responses: dict[str, list[FakeResponse] | FakeResponse]) -> None:
        self.responses = {
            url: value if isinstance(value, list) else [value]
            for url, value in responses.items()
        }
        self.requests: list[str] = []

    def request(self, target, _timeout: float, _headers=None):
        self.requests.append(target.url)
        queue = self.responses[target.url]
        return queue.pop(0)


class AlwaysFailFetcher:
    def new_budget(self):
        return object()

    def fetch_to_path(self, *_args, **_kwargs):
        raise download_note_images.DownloadError("network unavailable")


class FakePDFExtractor:
    def __init__(self, payload: bytes = PNG_BYTES) -> None:
        self.payload = payload
        self.calls: list[tuple[str, int]] = []

    def extract(self, arxiv_id, figure_number, work_dir, _budget):
        self.calls.append((arxiv_id, figure_number))
        extracted = work_dir / f"extract-{figure_number}.png"
        extracted.write_bytes(self.payload)
        return extracted


class FakePDFFetcher:
    def __init__(
        self,
        limits: download_note_images.FetchLimits | None = None,
    ) -> None:
        self.limits = limits or download_note_images.FetchLimits()
        self.urls: list[str] = []

    def new_budget(self):
        return object()

    def fetch_to_path(self, url, destination, *, kind, budget):
        self.urls.append(url)
        assert kind == "pdf"
        destination.write_bytes(PDF_BYTES)
        return download_note_images.FetchedFile(
            path=destination,
            final_url=url,
            media_type="application/pdf",
            extension=".pdf",
            bytes=len(PDF_BYTES),
            sha256=hashlib.sha256(PDF_BYTES).hexdigest(),
        )


class DownloadNoteImagesTests(unittest.TestCase):
    def test_url_policy_rejects_unsafe_destinations_and_mixed_dns(self) -> None:
        unsafe = (
            "ftp://example.com/a.png",
            "https://user:pass@example.com/a.png",
            "https://example.com:8443/a.png",
            "http://localhost/a.png",
            "http://localhost./a.png",
            "http://127.0.0.1/a.png",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/a.png",
            "http://224.0.0.1/a.png",
            "http://metadata.google.internal/a.png",
        )
        for url in unsafe:
            with self.subTest(url=url):
                with self.assertRaises(download_note_images.UnsafeURLError):
                    download_note_images.validate_remote_url(
                        url,
                        resolver=public_resolver,
                    )

        def mixed_resolver(host: str, port: int, **_kwargs):
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (PUBLIC_IPV4, port),
                ),
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("10.0.0.8", port),
                ),
            ]

        with self.assertRaisesRegex(
            download_note_images.UnsafeURLError,
            "non-public",
        ):
            download_note_images.validate_remote_url(
                "https://example.com/a.png",
                resolver=mixed_resolver,
            )

    def test_redirect_is_revalidated_before_second_request(self) -> None:
        transport = FakeTransport(
            {
                "https://example.com/a.png": FakeResponse(
                    302,
                    headers={"Location": "http://127.0.0.1/private.png"},
                ),
            }
        )
        fetcher = download_note_images.SafeFetcher(
            resolver=public_resolver,
            transport=transport,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "image.bin"
            with self.assertRaises(download_note_images.UnsafeURLError):
                fetcher.fetch_to_path(
                    "https://example.com/a.png",
                    destination,
                    kind="image",
                    budget=fetcher.new_budget(),
                )
            self.assertFalse(destination.exists())
        self.assertEqual(transport.requests, ["https://example.com/a.png"])

    def test_response_larger_than_limit_is_rejected_without_partial_file(self) -> None:
        limits = download_note_images.FetchLimits(
            max_image_bytes=32,
            max_pdf_bytes=64,
            max_total_bytes=64,
            request_timeout_seconds=2,
            run_timeout_seconds=5,
        )
        transport = FakeTransport(
            {
                "https://example.com/a.png": FakeResponse(
                    200,
                    PNG_BYTES,
                    headers={
                        "Content-Type": "image/png",
                        "Content-Length": str(len(PNG_BYTES)),
                    },
                )
            }
        )
        fetcher = download_note_images.SafeFetcher(
            resolver=public_resolver,
            transport=transport,
            limits=limits,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "image.bin"
            with self.assertRaises(download_note_images.ResponseTooLargeError):
                fetcher.fetch_to_path(
                    "https://example.com/a.png",
                    destination,
                    kind="image",
                    budget=fetcher.new_budget(),
                )
            self.assertFalse(destination.exists())

    def test_verified_download_reuses_stream_hash_without_second_full_read(
        self,
    ) -> None:
        transport = FakeTransport(
            {
                "https://example.com/a.png": FakeResponse(
                    200,
                    PNG_BYTES,
                    headers={
                        "Content-Type": "image/png",
                        "Content-Length": str(len(PNG_BYTES)),
                    },
                )
            }
        )
        fetcher = download_note_images.SafeFetcher(
            resolver=public_resolver,
            transport=transport,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "image.bin"
            with patch.object(
                download_note_images,
                "inspect_regular_file",
                side_effect=AssertionError(
                    "unexpected second full-file inspection"
                ),
            ):
                fetched = fetcher.fetch_to_path(
                    "https://example.com/a.png",
                    destination,
                    kind="image",
                    budget=fetcher.new_budget(),
                )

        self.assertEqual(
            fetched.sha256,
            hashlib.sha256(PNG_BYTES).hexdigest(),
        )

    def test_streamed_response_is_bounded_without_content_length(self) -> None:
        limits = download_note_images.FetchLimits(
            max_image_bytes=32,
            max_pdf_bytes=64,
            max_total_bytes=64,
            request_timeout_seconds=2,
            run_timeout_seconds=5,
        )
        transport = FakeTransport(
            {
                "https://example.com/a.png": FakeResponse(
                    200,
                    PNG_BYTES,
                    headers={"Content-Type": "image/png"},
                )
            }
        )
        fetcher = download_note_images.SafeFetcher(
            resolver=public_resolver,
            transport=transport,
            limits=limits,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "image.bin"
            with self.assertRaises(download_note_images.ResponseTooLargeError):
                fetcher.fetch_to_path(
                    "https://example.com/a.png",
                    destination,
                    kind="image",
                    budget=fetcher.new_budget(),
                )
            self.assertFalse(destination.exists())

    def test_declared_mime_must_match_verified_image_magic(self) -> None:
        transport = FakeTransport(
            {
                "https://example.com/a.png": FakeResponse(
                    200,
                    PNG_BYTES,
                    headers={"Content-Type": "image/jpeg"},
                )
            }
        )
        fetcher = download_note_images.SafeFetcher(
            resolver=public_resolver,
            transport=transport,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "image.bin"
            with self.assertRaises(download_note_images.UnsupportedMediaError):
                fetcher.fetch_to_path(
                    "https://example.com/a.png",
                    destination,
                    kind="image",
                    budget=fetcher.new_budget(),
                )
            self.assertFalse(destination.exists())

    def test_run_wide_response_budget_applies_across_files(self) -> None:
        limits = download_note_images.FetchLimits(
            max_image_bytes=128,
            max_pdf_bytes=128,
            max_total_bytes=len(PNG_BYTES) + 8,
            request_timeout_seconds=2,
            run_timeout_seconds=5,
        )
        transport = FakeTransport(
            {
                "https://example.com/a.png": FakeResponse(
                    200,
                    PNG_BYTES,
                    headers={"Content-Type": "image/png"},
                ),
                "https://example.com/b.png": FakeResponse(
                    200,
                    PNG_BYTES,
                    headers={"Content-Type": "image/png"},
                ),
            }
        )
        fetcher = download_note_images.SafeFetcher(
            resolver=public_resolver,
            transport=transport,
            limits=limits,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            budget = fetcher.new_budget()
            fetcher.fetch_to_path(
                "https://example.com/a.png",
                root / "a.bin",
                kind="image",
                budget=budget,
            )
            with self.assertRaises(download_note_images.ResponseTooLargeError):
                fetcher.fetch_to_path(
                    "https://example.com/b.png",
                    root / "b.bin",
                    kind="image",
                    budget=budget,
                )
            self.assertFalse((root / "b.bin").exists())

    def test_run_deadline_is_enforced_before_transport_request(self) -> None:
        times = iter((0.0, 10.0))
        transport = FakeTransport({})
        fetcher = download_note_images.SafeFetcher(
            resolver=public_resolver,
            transport=transport,
            limits=download_note_images.FetchLimits(
                request_timeout_seconds=2,
                run_timeout_seconds=5,
            ),
            clock=lambda: next(times),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "image.bin"
            budget = fetcher.new_budget()
            with self.assertRaisesRegex(
                download_note_images.DownloadError,
                "run deadline",
            ):
                fetcher.fetch_to_path(
                    "https://example.com/a.png",
                    destination,
                    kind="image",
                    budget=budget,
                )
            self.assertFalse(destination.exists())
        self.assertEqual(transport.requests, [])

    def test_reachable_image_is_left_unchanged_without_artifacts(self) -> None:
        url = "https://example.com/a.png"
        transport = FakeTransport(
            {
                url: FakeResponse(
                    200,
                    PNG_BYTES,
                    headers={"Content-Type": "image/png"},
                )
            }
        )
        fetcher = download_note_images.SafeFetcher(
            resolver=public_resolver,
            transport=transport,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            note = Path(temp_dir) / "Method.md"
            original = f"![reachable]({url})\n"
            note.write_text(original, encoding="utf-8")

            result = download_note_images.process_note(
                note,
                fetcher=fetcher,
                pdf_extractor=FakePDFExtractor(),
            )

            self.assertEqual(note.read_text(encoding="utf-8"), original)
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["reachable"], 1)
            self.assertEqual(result["localized"], 0)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["artifacts"], [])
            self.assertEqual(result["changed_paths"], [])

    def test_asset_publication_never_overwrites_hash_collision(self) -> None:
        digest = hashlib.sha256(PNG_BYTES).hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.png"
            source.write_bytes(PNG_BYTES)
            assets = root / "assets"
            assets.mkdir()
            collision = assets / f"{digest}.png"
            collision.write_bytes(b"user-owned-data")

            with self.assertRaises(download_note_images.AssetCollisionError):
                download_note_images.publish_asset(source, assets)

            self.assertEqual(collision.read_bytes(), b"user-owned-data")
            self.assertEqual(sorted(path.name for path in assets.iterdir()), [collision.name])

    def test_asset_publication_rejects_symlink_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            actual = root / "actual.png"
            actual.write_bytes(PNG_BYTES)
            source = root / "source.png"
            source.symlink_to(actual)
            assets = root / "assets"

            with self.assertRaises(download_note_images.UnsupportedMediaError):
                download_note_images.publish_asset(source, assets)

            self.assertEqual(list(assets.iterdir()), [])

    def test_asset_publication_enforces_source_byte_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.png"
            source.write_bytes(PNG_BYTES)
            assets = root / "assets"

            with self.assertRaises(download_note_images.ResponseTooLargeError):
                download_note_images.publish_asset(
                    source,
                    assets,
                    max_bytes=len(PNG_BYTES) - 1,
                )

            self.assertEqual(list(assets.iterdir()), [])

    def test_pdf_fallback_publishes_only_exact_content_addressed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = root / "Method.md"
            note.write_text(
                "---\nimage_source: online\n---\n\n"
                "![Figure 1](https://arxiv.org/html/2607.00001/fig1.png)\n",
                encoding="utf-8",
            )
            extractor = FakePDFExtractor()

            result = download_note_images.process_note(
                note,
                fetcher=AlwaysFailFetcher(),
                pdf_extractor=extractor,
            )

            digest = hashlib.sha256(PNG_BYTES).hexdigest()
            asset = root / "assets" / f"{digest}.png"
            self.assertEqual(extractor.calls, [("2607.00001", 1)])
            self.assertTrue(asset.is_file())
            self.assertEqual(asset.read_bytes(), PNG_BYTES)
            self.assertEqual(
                sorted(path.name for path in asset.parent.iterdir()),
                [asset.name],
            )
            self.assertIn(
                f"![[assets/{asset.name}|600]]",
                note.read_text(encoding="utf-8"),
            )
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["localized"], 1)
            self.assertEqual(
                result["changed_paths"],
                [str(asset.resolve()), str(note.resolve())],
            )
            self.assertEqual(
                result["artifacts"],
                [
                    {
                        "path": str(asset.resolve()),
                        "sha256": digest,
                        "media_type": "image/png",
                        "bytes": len(PNG_BYTES),
                        "created": True,
                    }
                ],
            )

    def test_vault_root_makes_artifacts_and_changes_vault_relative(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            vault = Path(temp_dir)
            notes_dir = vault / "论文笔记"
            notes_dir.mkdir()
            note = notes_dir / "Method.md"
            note.write_text(
                "![Figure 1](https://arxiv.org/html/2607.00001/fig1.png)\n",
                encoding="utf-8",
            )

            result = download_note_images.process_note(
                note,
                fetcher=AlwaysFailFetcher(),
                pdf_extractor=FakePDFExtractor(),
                vault_root=vault,
            )

            digest = hashlib.sha256(PNG_BYTES).hexdigest()
            asset_path = f"论文笔记/assets/{digest}.png"
            self.assertEqual(result["note"], "论文笔记/Method.md")
            self.assertEqual(
                result["changed_paths"],
                [asset_path, "论文笔记/Method.md"],
            )
            self.assertEqual(result["artifacts"][0]["path"], asset_path)

    def test_pdfimages_outputs_stay_in_the_unique_work_directory(self) -> None:
        fetcher = FakePDFFetcher()
        runner_calls: list[list[str]] = []

        def fake_runner(
            command,
            *,
            timeout,
            max_stdout_bytes,
            max_stderr_bytes,
            max_file_bytes,
        ):
            self.assertGreater(timeout, 0)
            self.assertGreater(max_stdout_bytes, 0)
            self.assertGreater(max_stderr_bytes, 0)
            runner_calls.append(command)
            if "-list" in command:
                self.assertIsNone(max_file_bytes)
                return download_note_images.ProcessResult(
                    0,
                    PDFIMAGES_LIST_ONE,
                    b"",
                )
            self.assertGreater(max_file_bytes, 0)
            prefix = Path(command[-1])
            (prefix.parent / f"{prefix.name}-000.png").write_bytes(PNG_BYTES)
            (prefix.parent / f"{prefix.name}-001.png").write_bytes(b"not-image")
            return download_note_images.ProcessResult(0, b"", b"")

        extractor = download_note_images.PDFExtractor(fetcher, runner=fake_runner)
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            selected = extractor.extract(
                "2607.00001",
                1,
                work_dir,
                fetcher.new_budget(),
            )

            self.assertTrue(selected.is_file())
            self.assertTrue(selected.resolve().is_relative_to(work_dir.resolve()))
            self.assertEqual(fetcher.urls, ["https://arxiv.org/pdf/2607.00001.pdf"])
            self.assertEqual(len(runner_calls), 2)
            self.assertIn("-list", runner_calls[0])
            self.assertIn("-png", runner_calls[1])
            self.assertEqual(
                sorted(path.name for path in work_dir.iterdir()),
                ["arxiv-2607.00001.pdf", "pdf-extract-2607.00001-1"],
            )

    def test_pdfimages_output_directory_is_bounded_before_sorting(self) -> None:
        fetcher = FakePDFFetcher()

        def fake_runner(
            command,
            *,
            timeout,
            max_stdout_bytes,
            max_stderr_bytes,
            max_file_bytes,
        ):
            if "-list" in command:
                return download_note_images.ProcessResult(
                    0,
                    PDFIMAGES_LIST_ONE,
                    b"",
                )
            prefix = Path(command[-1])
            for index in range(2):
                (prefix.parent / f"{prefix.name}-{index:03d}.png").write_bytes(
                    PNG_BYTES
                )
            return download_note_images.ProcessResult(0, b"", b"")

        extractor = download_note_images.PDFExtractor(fetcher, runner=fake_runner)
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(download_note_images, "MAX_EXTRACTED_FILES", 1),
            self.assertRaisesRegex(
                download_note_images.DownloadError,
                "too many image files",
            ),
        ):
            extractor.extract(
                "2607.00001",
                1,
                Path(temp_dir),
                fetcher.new_budget(),
            )

    def test_pdf_image_plan_blocks_decoded_bomb_before_extraction(
        self,
    ) -> None:
        fetcher = FakePDFFetcher(
            download_note_images.FetchLimits(max_extracted_bytes=1024)
        )
        calls: list[list[str]] = []
        oversized_plan = PDFIMAGES_LIST_ONE.replace(
            b"       1     1",
            b"    1000  1000",
        )

        def fake_runner(command, **_kwargs):
            calls.append(command)
            if "-list" not in command:
                self.fail("extraction ran before the oversized plan was rejected")
            return download_note_images.ProcessResult(
                0,
                oversized_plan,
                b"",
            )

        extractor = download_note_images.PDFExtractor(
            fetcher,
            runner=fake_runner,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(
                download_note_images.ResponseTooLargeError,
                "decoded-byte limit",
            ):
                extractor.extract(
                    "2607.00001",
                    1,
                    Path(temp_dir),
                    fetcher.new_budget(),
                )

        self.assertEqual(len(calls), 1)
        self.assertIn("-list", calls[0])

    def test_pdf_extraction_rechecks_actual_total_output_bytes(self) -> None:
        fetcher = FakePDFFetcher(
            download_note_images.FetchLimits(
                max_extracted_bytes=len(PNG_BYTES) + 1
            )
        )

        def fake_runner(command, **_kwargs):
            if "-list" in command:
                return download_note_images.ProcessResult(
                    0,
                    PDFIMAGES_LIST_ONE,
                    b"",
                )
            prefix = Path(command[-1])
            for index in range(2):
                (prefix.parent / f"{prefix.name}-{index:03d}.png").write_bytes(
                    PNG_BYTES
                )
            return download_note_images.ProcessResult(0, b"", b"")

        extractor = download_note_images.PDFExtractor(
            fetcher,
            runner=fake_runner,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(
                download_note_images.ResponseTooLargeError,
                "total byte limit",
            ):
                extractor.extract(
                    "2607.00001",
                    1,
                    Path(temp_dir),
                    fetcher.new_budget(),
                )

    def test_collision_failure_preserves_note_and_existing_asset(self) -> None:
        digest = hashlib.sha256(PNG_BYTES).hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = root / "Method.md"
            original_note = (
                "---\nimage_source: online\n---\n\n"
                "![Figure 1](https://arxiv.org/html/2607.00001/fig1.png)\n"
            )
            note.write_text(original_note, encoding="utf-8")
            assets = root / "assets"
            assets.mkdir()
            collision = assets / f"{digest}.png"
            collision.write_bytes(b"user-owned-data")

            result = download_note_images.process_note(
                note,
                fetcher=AlwaysFailFetcher(),
                pdf_extractor=FakePDFExtractor(),
            )

            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["changed_paths"], [])
            self.assertEqual(note.read_text(encoding="utf-8"), original_note)
            self.assertEqual(collision.read_bytes(), b"user-owned-data")

    def test_note_input_is_bounded_and_does_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "Target.md"
            target.write_text("# target\n", encoding="utf-8")
            link = root / "Linked.md"
            link.symlink_to(target)

            with self.assertRaisesRegex(
                download_note_images.ImageLocalizationError,
                "must not be a symlink",
            ):
                download_note_images.process_note(link)

            oversized = root / "Oversized.md"
            oversized.write_bytes(b"x" * 9)
            with patch.object(download_note_images, "MAX_NOTE_BYTES", 8):
                with self.assertRaisesRegex(
                    download_note_images.ImageLocalizationError,
                    "safety limit",
                ):
                    download_note_images.process_note(oversized)

    def test_atomic_note_update_rejects_symlink_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "Outside.md"
            target.write_text("# user data\n", encoding="utf-8")
            link = root / "Method.md"
            link.symlink_to(target)

            with self.assertRaises(
                download_note_images.ConcurrentNoteChangeError
            ):
                download_note_images._atomic_write_note(
                    link,
                    "# replacement\n",
                    expected_sha256=hashlib.sha256(
                        target.read_bytes()
                    ).hexdigest(),
                )

            self.assertEqual(target.read_text(encoding="utf-8"), "# user data\n")

    def test_atomic_note_replace_failure_reports_published_asset_and_preserves_note(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = root / "Method.md"
            original_note = (
                "---\nimage_source: online\n---\n\n"
                "![Figure 1](https://arxiv.org/html/2607.00001/fig1.png)\n"
            )
            note.write_text(original_note, encoding="utf-8")

            with patch.object(
                download_note_images.os,
                "replace",
                side_effect=OSError("simulated replace failure"),
            ):
                result = download_note_images.process_note(
                    note,
                    fetcher=AlwaysFailFetcher(),
                    pdf_extractor=FakePDFExtractor(),
                )

            digest = hashlib.sha256(PNG_BYTES).hexdigest()
            asset = root / "assets" / f"{digest}.png"
            self.assertEqual(note.read_text(encoding="utf-8"), original_note)
            self.assertTrue(asset.is_file())
            self.assertEqual(result["localized"], 0)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["changed_paths"], [str(asset.resolve())])
            self.assertEqual(result["artifacts"][0]["path"], str(asset.resolve()))

    def test_duplicate_content_reports_one_created_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            note = root / "Method.md"
            note.write_text(
                "![Figure 1](https://arxiv.org/html/2607.00001/fig1.png)\n"
                "![Figure 2](https://arxiv.org/html/2607.00001/fig2.png)\n",
                encoding="utf-8",
            )

            result = download_note_images.process_note(
                note,
                fetcher=AlwaysFailFetcher(),
                pdf_extractor=FakePDFExtractor(),
            )

            self.assertEqual(result["failed"], 0)
            self.assertEqual(len(result["artifacts"]), 1)
            self.assertTrue(result["artifacts"][0]["created"])
            self.assertEqual(len(result["changed_paths"]), 2)

    def test_cli_failure_is_machine_readable_and_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            note = Path(temp_dir) / "Unsafe.md"
            note.write_text(
                "![private](http://169.254.169.254/latest/meta-data)\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = download_note_images.main(
                    [str(note)],
                    fetcher=download_note_images.SafeFetcher(
                        resolver=public_resolver,
                        transport=FakeTransport({}),
                    ),
                    pdf_extractor=FakePDFExtractor(),
                )

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout.getvalue(), "")
            payload = json.loads(stderr.getvalue())
            self.assertEqual(payload["status"], "partial")
            self.assertEqual(payload["failed"], 1)
            self.assertEqual(payload["changed_paths"], [])

    def test_cli_argument_errors_are_machine_readable(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = download_note_images.main([])

        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout.getvalue(), "")
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["code"], "invalid-arguments")


if __name__ == "__main__":
    unittest.main()
