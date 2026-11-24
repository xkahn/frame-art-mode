import ast
from datetime import datetime
import hashlib
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from frame_art_mode.models import (
    ART_STORE_FAVORITES_CATEGORY,
    ART_STORE_ID_PREFIX,
    NORMALIZATION_VERSION,
    OCCASION_PREFIX,
    ROOT_SET,
    AbortRequested,
    CachedImage,
    CurrentDisplay,
    FrameArtError,
    IPCError,
    ImageError,
    LibraryError,
    Manifest,
    ManifestEntry,
    OperationResult,
    OperationState,
    PreparedImage,
    ServiceError,
    SetSettings,
    SourceImage,
    TVArtClient,
    TVServiceError,
    UploadError,
    atomic_write_bytes,
    atomic_write_json,
    close_tv_client,
    ensure_private_dir,
    tv_key_for_ip,
    utc_now_iso,
)


class _FakeArtClientWithClose:
    def __init__(self, fail_close: bool = False) -> None:
        self.closed = False
        self.fail_close = fail_close

    def get_artmode(self) -> str:
        return "on"

    def available(self, category: str | None = None) -> list[dict[str, str]]:
        return []

    def upload(self, file: bytes, file_type: str = "JPEG") -> str:
        return "MY_F0001"

    def delete(self, content_id: str) -> bool:
        return True

    def select_image(
        self,
        content_id: str,
        category: str | None = None,
        show: bool = True,
    ) -> bool:
        return True

    def get_current(self) -> dict[str, str]:
        return {"content_id": "MY_F0001"}

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("close error")


class _FakeArtClientWithoutClose:
    def get_artmode(self) -> str:
        return "on"

    def available(self, category: str | None = None) -> list[dict[str, str]]:
        return []

    def upload(self, file: bytes, file_type: str = "JPEG") -> str:
        return "MY_F0001"

    def delete(self, content_id: str) -> bool:
        return True

    def select_image(
        self,
        content_id: str,
        category: str | None = None,
        show: bool = True,
    ) -> bool:
        return True

    def get_current(self) -> dict[str, str]:
        return {"content_id": "MY_F0001"}


class ModelsTests(unittest.TestCase):
    def test_constants_and_exception_hierarchy(self):
        self.assertEqual(ROOT_SET, "root")
        self.assertEqual(OCCASION_PREFIX, "occasion:")
        self.assertEqual(ART_STORE_FAVORITES_CATEGORY, "MY-C0004")
        self.assertEqual(ART_STORE_ID_PREFIX, "SAM-")
        self.assertEqual(NORMALIZATION_VERSION, 1)

        self.assertTrue(issubclass(LibraryError, FrameArtError))
        self.assertTrue(issubclass(ImageError, ValueError))
        self.assertTrue(issubclass(ImageError, FrameArtError))
        self.assertTrue(issubclass(TVServiceError, FrameArtError))
        self.assertIs(ServiceError, TVServiceError)
        self.assertTrue(issubclass(UploadError, TVServiceError))
        self.assertTrue(issubclass(AbortRequested, FrameArtError))
        self.assertTrue(issubclass(IPCError, FrameArtError))

        upload_err = UploadError("failed", "MY_F0002")
        self.assertEqual(upload_err.content_id, "MY_F0002")
        self.assertIsNone(UploadError("failed").content_id)

        ipc_err = IPCError("busy", status_code=409, code="conflict", offline=False)
        self.assertEqual(ipc_err.status_code, 409)
        self.assertEqual(ipc_err.code, "conflict")
        self.assertFalse(ipc_err.offline)
        self.assertEqual(ipc_err.message, "busy")

    def test_tv_art_client_protocol_and_close_helper(self):
        client_with_close = _FakeArtClientWithClose()
        client_without_close = _FakeArtClientWithoutClose()
        self.assertIsInstance(client_with_close, TVArtClient)
        self.assertIsInstance(client_without_close, TVArtClient)

        close_tv_client(client_with_close)
        self.assertTrue(client_with_close.closed)
        close_tv_client(client_without_close)
        close_tv_client(None)

        failing_close = _FakeArtClientWithClose(fail_close=True)
        close_tv_client(failing_close)
        self.assertTrue(failing_close.closed)

    def test_operation_state_public_vs_full_serialization(self):
        op = OperationState(
            kind="sync",
            phase="uploading",
            message="Uploading sunset.jpg",
            current_item="Party/sunset.jpg",
            completed_ops=4,
            total_ops=14,
            abort_requested=False,
            started_at="2026-11-23T23:00:00+00:00",
            known_uploaded=("MY_F0001", "MY_F0002"),
        )
        full = op.to_dict()
        public = op.to_public_dict()
        self.assertEqual(full["known_uploaded"], ["MY_F0001", "MY_F0002"])
        self.assertNotIn("known_uploaded", public)
        self.assertEqual(public["completed_ops"], 4)
        self.assertEqual(public["total_ops"], 14)

        round_trip = OperationState.from_dict(full)
        self.assertEqual(round_trip, op)

        with self.assertRaises(ValueError):
            OperationState.from_dict({**full, "completed_ops": True})
        with self.assertRaises(ValueError):
            OperationState.from_dict({**full, "known_uploaded": [""]})

    def test_operation_result_public_vs_full_and_state_validation(self):
        for valid_state in ("completed", "aborted", "failed", "interrupted"):
            result = OperationResult(
                kind="sync",
                state=valid_state,
                message="done",
                timestamp="2026-11-23T23:05:00+00:00",
                recovery_pending=("MY_F0001",),
            )
            full = result.to_dict()
            public = result.to_public_dict()
            self.assertEqual(full["recovery_pending"], ["MY_F0001"])
            self.assertNotIn("recovery_pending", public)
            self.assertEqual(OperationResult.from_dict(full), result)

        with self.assertRaises(ValueError):
            OperationResult(
                kind="sync",
                state="running",
                message="invalid",
                timestamp="2026-11-23T23:05:00+00:00",
            )

    def test_set_settings_validation_and_defaults(self):
        default_settings = SetSettings.from_dict(None)
        self.assertEqual(
            default_settings,
            SetSettings(random=True, mix_art_store_favorites=False, interval_minutes=15),
        )
        for bad_interval in (0, -5, True, False, "15"):
            with self.subTest(interval_minutes=bad_interval):
                with self.assertRaises(ValueError):
                    SetSettings(interval_minutes=bad_interval)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SetSettings.from_dict({"random": "yes"})

    def test_image_models_and_current_display_round_trip(self):
        source = SourceImage(
            path=Path("/photos/sunset.jpg"),
            set_id="root",
            source_rel="sunset.jpg",
            name_rel="sunset.jpg",
            signature=(1, 2, 1024, 1700000000),
        )
        self.assertEqual(SourceImage.from_dict(source.to_dict()), source)

        cached = CachedImage(
            set_id="occasion:Party",
            source_rel="Party/sunset.jpg",
            name_rel="sunset.jpg",
            signature=(1, 2, 1024, 1700000000),
            fingerprint="abc123",
            tv_path="tv/abc123.jpg",
            thumb_path="thumbs/abc123.jpg",
            skip_reason=None,
            normalization_version=NORMALIZATION_VERSION,
            max_image_bytes=5242880,
            byte_size=400000,
            width=3840,
            height=2160,
        )
        self.assertEqual(CachedImage.from_dict(cached.to_dict()), cached)

        prepared = PreparedImage(
            source_rel="Party/sunset.jpg",
            filename="sunset.jpg",
            fingerprint="abc123",
            cache_path=Path("/state/cache/tv/abc123.jpg"),
            byte_size=400000,
        )
        self.assertEqual(PreparedImage.from_dict(prepared.to_dict()), prepared)

        display = CurrentDisplay(
            content_id="SAM-1234",
            category=ART_STORE_FAVORITES_CATEGORY,
            source_rel=None,
            is_art_store=True,
        )
        self.assertEqual(CurrentDisplay.from_dict(display.to_dict()), display)

    def test_manifest_round_trip_and_validation(self):
        manifest = Manifest(
            schema_version=1,
            tv_ip="192.168.1.50",
            active_set="occasion:Party",
            entries=(
                ManifestEntry(
                    source_rel="Party/sunset.jpg",
                    filename="sunset.jpg",
                    fingerprint="sha256hex",
                    content_id="MY_F0001",
                ),
            ),
            cleanup_pending=("MY_F0000",),
            settings={
                "occasion:Party": SetSettings(
                    random=False,
                    mix_art_store_favorites=True,
                    interval_minutes=30,
                )
            },
            current_content_id="MY_F0001",
        )
        serialized = manifest.to_dict()
        restored = Manifest.from_dict(serialized, expected_tv_ip="192.168.1.50")
        self.assertEqual(restored, manifest)
        self.assertEqual(restored.settings_for("root"), SetSettings())
        self.assertEqual(restored.settings_for("occasion:Party").interval_minutes, 30)

        with self.assertRaises(ValueError):
            Manifest.from_dict(serialized, expected_tv_ip="192.168.1.99")

        with self.assertRaises(ValueError):
            Manifest(
                tv_ip="192.168.1.50",
                entries=(
                    ManifestEntry("a.jpg", "a.jpg", "fp1", "MY_F0001"),
                    ManifestEntry("b.jpg", "b.jpg", "fp2", "MY_F0001"),
                ),
            )

        with self.assertRaises(ValueError):
            Manifest.from_dict({**serialized, "schema_version": 2})

    def test_helpers_and_atomic_writes_with_private_permissions(self):
        parsed_time = datetime.fromisoformat(utc_now_iso())
        self.assertIsNotNone(parsed_time.tzinfo)
        self.assertEqual(
            tv_key_for_ip("192.168.1.50"),
            hashlib.sha256(b"192.168.1.50").hexdigest()[:16],
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_dir = ensure_private_dir(root / "state" / "tv")
            self.assertEqual(stat.S_IMODE(private_dir.stat().st_mode), 0o700)

            json_path = private_dir / "manifest.json"
            atomic_write_json(json_path, {"ok": True, "count": 2})
            self.assertEqual(stat.S_IMODE(json_path.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(json_path.read_text(encoding="utf-8")),
                {"ok": True, "count": 2},
            )

            bytes_path = private_dir / "image.jpg"
            atomic_write_bytes(bytes_path, b"\xff\xd8\xff\xd9", mode=0o600, fsync=False)
            self.assertEqual(stat.S_IMODE(bytes_path.stat().st_mode), 0o600)
            self.assertEqual(bytes_path.read_bytes(), b"\xff\xd8\xff\xd9")

            # Verify no temp files left behind
            self.assertEqual(
                sorted(p.name for p in private_dir.iterdir()),
                ["image.jpg", "manifest.json"],
            )

    def test_atomic_write_bytes_cleans_up_temp_file_on_close_and_replace_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "manifest.json"
            target.write_bytes(b"original")

            real_ntf = tempfile.NamedTemporaryFile

            class _FailingCloseContext:
                def __init__(self, inner):
                    self._inner = inner

                def __enter__(self):
                    return self._inner.__enter__()

                def __exit__(self, exc_type, exc, tb):
                    self._inner.__exit__(exc_type, exc, tb)
                    raise OSError("disk full on close")

            def ntf_with_close_failure(*args, **kwargs):
                return _FailingCloseContext(real_ntf(*args, **kwargs))

            with mock.patch(
                "frame_art_mode.models.tempfile.NamedTemporaryFile",
                side_effect=ntf_with_close_failure,
            ):
                with self.assertRaisesRegex(OSError, "disk full on close"):
                    atomic_write_bytes(target, b"updated", fsync=False)

            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual([p.name for p in root.iterdir()], ["manifest.json"])

            with mock.patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    atomic_write_bytes(target, b"updated", fsync=False)

            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual([p.name for p in root.iterdir()], ["manifest.json"])

    def test_models_module_imports_stdlib_only(self):
        path = Path(__file__).resolve().parents[1] / "frame_art_mode" / "models.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    self.assertIn(top, sys.stdlib_module_names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".")[0]
                self.assertIn(top, sys.stdlib_module_names)


if __name__ == "__main__":
    unittest.main()
