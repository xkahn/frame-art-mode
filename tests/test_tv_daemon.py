from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from frame_art_mode.config import Config
from frame_art_mode.ipc import (
    HTTP_ACCEPTED,
    HTTP_BAD_REQUEST,
    HTTP_CONFLICT,
    HTTP_NOT_FOUND,
    HTTP_OK,
    call_json,
    request_json,
)
from frame_art_mode.models import (
    ART_STORE_FAVORITES_CATEGORY,
    IPCError,
    Manifest,
    ManifestEntry,
    OperationResult,
    OperationState,
    PreparedImage,
    SetSettings,
    TVServiceError,
)
from frame_art_mode.tv_daemon import (
    TVDaemon,
    build_tv_ipc_server,
    main,
    serve_daemon,
)


class FakeArtClient:
    """In-memory fake implementing TVArtClient + close() lifecycle tracking."""

    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}
        self.upload_count = 0
        self.fail_upload = False
        self.fail_upload_message = "upload rejected"
        self.fail_on_upload_number: int | None = None
        self.fail_delete: set[str] = set()
        self.fail_delete_raise: set[str] = set()
        self.fail_select: set[str] = set()
        self.mode: str | bool = "on"
        self.fail_artmode = False
        self.current: str | None = None
        self.current_category: str | None = None
        self.current_raw_override: dict | None = None
        self.calls: list[tuple] = []
        self.favorites: list[dict] = []
        self.fail_favorites = False
        self.return_none = False
        self.duplicate_upload_id: str | None = None
        self.unverified_uploads: set[str] = set()
        self.on_upload = None
        self.available_gate: tuple[threading.Event, threading.Event] | None = None
        self.delete_gate: tuple[threading.Event, threading.Event] | None = None
        self.on_favorites = None
        self.on_select = None
        self.closed = False
        self.closed_count = 0

    def get_artmode(self):
        self.calls.append(("get_artmode",))
        if self.fail_artmode:
            raise RuntimeError("TV unreachable")
        return self.mode

    def available(self, category: str | None = None):
        self.calls.append(("available", category))
        if self.available_gate is not None:
            entered, release = self.available_gate
            self.available_gate = None
            entered.set()
            release.wait(timeout=5.0)
        if category == ART_STORE_FAVORITES_CATEGORY:
            if self.on_favorites is not None:
                callback, self.on_favorites = self.on_favorites, None
                callback()
            if self.fail_favorites:
                self.fail_favorites = False
                raise RuntimeError("favorites unavailable")
            return list(self.favorites)
        return [
            {"content_id": cid}
            for cid in self.items
            if cid not in self.unverified_uploads
        ]

    def upload(self, file: bytes, file_type: str = "JPEG"):
        self.calls.append(("upload", file_type))
        self.upload_count += 1
        if self.fail_upload or self.upload_count == self.fail_on_upload_number:
            self.fail_upload = False
            raise RuntimeError(self.fail_upload_message)
        if self.duplicate_upload_id is not None:
            return self.duplicate_upload_id
        content_id = f"APP-{self.upload_count}"
        self.items[content_id] = file
        if self.on_upload is not None:
            callback, self.on_upload = self.on_upload, None
            callback()
        return None if self.return_none else content_id

    def delete(self, content_id: str):
        self.calls.append(("delete", content_id))
        if self.delete_gate is not None:
            entered, release = self.delete_gate
            self.delete_gate = None
            entered.set()
            release.wait(timeout=5.0)
        if content_id in self.fail_delete_raise:
            raise RuntimeError(f"delete failed for {content_id}")
        if content_id in self.fail_delete:
            return False
        self.items.pop(content_id, None)
        return True

    def select_image(self, content_id: str, category: str | None = None, show: bool = True):
        self.calls.append(("select", content_id, category))
        if content_id in self.fail_select:
            raise RuntimeError(f"cannot select missing item {content_id}")
        self.current = content_id
        self.current_category = category
        if self.on_select is not None:
            callback = self.on_select
            callback()
        return True

    def get_current(self):
        self.calls.append(("get_current",))
        if self.current_raw_override is not None:
            return dict(self.current_raw_override)
        return {"content_id": self.current, "category": self.current_category}

    def close(self) -> None:
        self.closed = True
        self.closed_count += 1


class FakeClock:
    def __init__(self, initial_mono: float = 1000.0) -> None:
        self.mono = initial_mono
        self.wall = datetime(2026, 11, 24, 5, 0, 0, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.mono

    def utcnow(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self.mono += seconds
        self.wall = self.wall + timedelta(seconds=seconds)


class FakeLibraryService:
    """In-memory fake for library.sock /prepare-set and /release-lease."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir = cache_dir.parent / "thumbs"
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)
        self.sets: dict[str, list[dict]] = {}
        self.skipped: dict[str, list[dict]] = {}
        self.warnings: list[str] = []
        self.active_leases: dict[str, str] = {}
        self.released_leases: list[str] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_prepare_with: Exception | None = None
        self.fail_release = False
        self.raw_prepare_override: dict | None = None

    def put_image(
        self,
        set_id: str,
        source_rel: str,
        payload: bytes,
        *,
        name_rel: str | None = None,
    ) -> dict:
        digest = hashlib.sha256(payload).hexdigest()
        cache_path = self.cache_dir / f"{digest}.jpg"
        cache_path.write_bytes(payload)
        thumb_path = self.thumbs_dir / f"{digest}.jpg"
        thumb_path.write_bytes(payload)
        computed_name_rel = name_rel
        if computed_name_rel is None:
            computed_name_rel = (
                source_rel.split("/", 1)[1]
                if set_id.startswith("occasion:") and "/" in source_rel
                else source_rel
            )
        entry = {
            "source_rel": source_rel,
            "name_rel": computed_name_rel,
            "filename": Path(source_rel).name,
            "fingerprint": digest,
            "cache_path": str(cache_path),
            "thumb_path": str(thumb_path),
            "byte_size": len(payload),
        }
        self.sets.setdefault(set_id, []).append(entry)
        return entry

    def clear_set(self, set_id: str) -> None:
        self.sets[set_id] = []
        self.skipped[set_id] = []

    def __call__(
        self,
        socket_path: Path,
        method: str,
        path: str,
        body: dict | None = None,
        timeout: float = 5.0,
    ) -> dict:
        payload = dict(body or {})
        self.calls.append((method, path, payload))
        if path == "/prepare-set":
            if self.fail_prepare_with is not None:
                err, self.fail_prepare_with = self.fail_prepare_with, None
                raise err
            if self.raw_prepare_override is not None:
                return dict(self.raw_prepare_override)
            set_id = payload["set_id"]
            lease_id = payload["lease_id"]
            self.active_leases[lease_id] = set_id
            return {
                "ok": True,
                "set_id": set_id,
                "lease_id": lease_id,
                "images": list(self.sets.get(set_id, [])),
                "skipped": list(self.skipped.get(set_id, [])),
                "warnings": list(self.warnings),
            }
        if path == "/release-lease":
            lease_id = payload["lease_id"]
            self.released_leases.append(lease_id)
            self.active_leases.pop(lease_id, None)
            if self.fail_release:
                raise IPCError("release failed", status_code=503, offline=True)
            return {"ok": True}
        raise AssertionError(f"Unexpected library IPC call: {method} {path}")


class TVDaemonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.images = self.base / "images"
        self.images.mkdir()
        self.state = self.base / "state"
        self.config = Config(
            script_dir=self.base,
            image_root=self.images,
            state_dir=self.state,
            tv_ip="192.168.1.50",
            bind_ip="192.168.1.20",
            preview_seconds=15,
            tv_op_delay_seconds=3.0,
            tv_batch_settle_seconds=12.0,
            tv_timeout_seconds=30.0,
            tv_verify_retries=4,
            tv_verify_interval_seconds=3.0,
            max_set_images_warning=50,
        )
        self.art = FakeArtClient()
        self.opened_clients: list[FakeArtClient] = []
        self.library = FakeLibraryService(self.base / "cache" / "tv")
        self.library.put_image("root", "photo.jpg", b"\xff\xd8\xff\xe0red\xff\xd9")
        self.sleeps: list[float] = []
        self.clock = FakeClock()
        self.daemon = self._make_daemon()

    def tearDown(self) -> None:
        self.daemon.stop()
        self.temp.cleanup()

    def _tv_factory(self, _cfg: Config, _token: Path) -> FakeArtClient:
        self.opened_clients.append(self.art)
        return self.art

    def _make_daemon(self, *, use_real_sleep: bool = False) -> TVDaemon:
        return TVDaemon(
            self.config,
            tv_factory=self._tv_factory,
            library_caller=self.library,
            sleeper=None if use_real_sleep else self.sleeps.append,
            clock=self.clock.monotonic,
            wall_clock=self.clock.utcnow,
        )

    def _sync_and_wait(self, daemon: TVDaemon, set_id: str = "root", settings=None) -> dict:
        daemon.start_sync(set_id, settings=settings)
        self.assertTrue(daemon.wait_for_idle(timeout=5.0), "daemon did not finish sync in time")
        return daemon.status()

    # ------------------------------------------------------------------
    # Import Boundaries, Process Hygiene, Locking & Socket Lifecycle
    # ------------------------------------------------------------------

    def test_import_boundaries_and_no_image_root_access(self):
        path = Path(__file__).resolve().parents[1] / "frame_art_mode" / "tv_daemon.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        forbidden = {
            "PIL",
            "Pillow",
            "flask",
            "images",
            "library_daemon",
            "web_server",
            "service",
            "web",
        }
        top_level_imported: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level_imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_level_imported.add(node.module.split(".")[0])
        self.assertNotIn("samsungframetv", top_level_imported)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], forbidden)
            elif isinstance(node, ast.ImportFrom) and node.module:
                for part in node.module.split("."):
                    self.assertNotIn(part, forbidden)
        self.assertNotIn("image_root", source)

    def test_second_process_for_same_tv_is_rejected_and_permissions_private(self):
        self.assertEqual(stat.S_IMODE(self.config.tv_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.daemon.lock_path.stat().st_mode), 0o600)
        with self.assertRaisesRegex(TVServiceError, "already managing TV"):
            TVDaemon(self.config, tv_factory=self._tv_factory, library_caller=self.library)
        self.assertEqual(serve_daemon(self.config), 2)

    def test_cli_serve_exits_2_on_config_error(self):
        bad_cfg = self.base / "bad.json"
        bad_cfg.write_text("{not-json", encoding="utf-8")
        with mock.patch("sys.stderr", new=io.StringIO()):
            code = main(["--config", str(bad_cfg), "serve"])
        self.assertEqual(code, 2)

    def test_fresh_tv_client_closed_in_finally_on_success_and_error(self):
        clients: list[FakeArtClient] = []

        def factory(_cfg: Config, _token: Path) -> FakeArtClient:
            client = FakeArtClient()
            client.items.update(self.art.items)
            clients.append(client)
            return client

        self.daemon.stop()
        self.daemon = TVDaemon(
            self.config,
            tv_factory=factory,
            library_caller=self.library,
            sleeper=self.sleeps.append,
            clock=self.clock.monotonic,
            wall_clock=self.clock.utcnow,
        )
        self.daemon.check_tv()
        self.assertEqual(len(clients), 1)
        self.assertTrue(clients[0].closed)

        self._sync_and_wait(self.daemon, "root")
        self.assertEqual(len(clients), 2)
        self.assertTrue(clients[1].closed)

        # Error path still closes client
        def failing_factory(_cfg: Config, _token: Path) -> FakeArtClient:
            client = FakeArtClient()
            client.fail_artmode = True
            clients.append(client)
            return client

        self.daemon._tv_factory = failing_factory
        res = self.daemon.check_tv()
        self.assertFalse(res["tv_ready"])
        self.assertEqual(len(clients), 3)
        self.assertTrue(clients[2].closed)

    # ------------------------------------------------------------------
    # Legacy Sync Tests Ported & Updated for ADR 0001-0004
    # ------------------------------------------------------------------

    def test_sync_reuses_unchanged_uploads_and_preserves_untracked_art(self):
        self.art.items["SAMSUNG-ART"] = b"existing"
        first = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(first["last_result"]["state"], "completed")
        content_id = first["entries"][0]["content_id"]
        self.assertEqual(self.art.upload_count, 1)

        second = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(second["entries"][0]["content_id"], content_id)
        self.assertEqual(self.art.upload_count, 1)
        self.assertIn("SAMSUNG-ART", self.art.items)
        self.assertEqual(self.art.current, content_id)
        self.assertEqual(second["current_display"]["content_id"], content_id)
        self.assertEqual(second["current_display"]["source_rel"], "photo.jpg")
        self.assertFalse(second["current_display"]["is_art_store"])
        # File permissions on manifest.json and operation.json
        self.assertEqual(stat.S_IMODE(self.daemon.manifest_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.daemon.operation_path.stat().st_mode), 0o600)

    def test_missing_tv_item_is_reuploaded_and_restart_does_not_sync(self):
        content_id = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]
        self.art.items.pop(content_id)
        result = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(self.art.upload_count, 2)
        active_id = result["entries"][0]["content_id"]
        self.assertNotEqual(active_id, content_id)

        self.daemon.stop()
        reopened = self._make_daemon()
        try:
            snap = reopened.status()
            self.assertEqual(snap["active_set"], "root")
            self.assertEqual(snap["entries"][0]["content_id"], active_id)
            self.assertEqual(snap["last_result"]["state"], "completed")
            self.assertEqual(self.art.upload_count, 2)
        finally:
            reopened.stop()

    def test_none_upload_confirmation_is_verified_through_available_list(self):
        self.art.return_none = True
        result = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(result["last_result"]["state"], "completed")
        self.assertEqual(result["entries"][0]["content_id"], "APP-1")

    def test_duplicate_upload_id_is_rejected_without_corrupting_manifest(self):
        self.art.items["EXISTING-1"] = b"existing"
        self.art.duplicate_upload_id = "EXISTING-1"
        result = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(result["last_result"]["state"], "failed")
        self.assertIn("duplicate content ID", result["last_result"]["message"])
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["cleanup_pending"], [])

    def test_failed_upload_fails_fast_with_no_rollback_delete_storm(self):
        initial = self._sync_and_wait(self.daemon, "root")
        old_id = initial["entries"][0]["content_id"]
        self.library.clear_set("root")
        self.library.put_image("root", "photo.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")
        self.art.fail_upload = True

        result = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(result["last_result"]["state"], "failed")
        self.assertIn("previously active TV set was preserved", result["last_result"]["message"])
        self.assertEqual(result["entries"][0]["content_id"], old_id)
        self.assertIn(old_id, self.art.items)
        self.assertFalse(any(call[0] == "delete" for call in self.art.calls))

    def test_tv_timeout_records_failure_and_requires_explicit_retry(self):
        old_id = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]
        self.library.clear_set("root")
        self.library.put_image("root", "photo.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")
        self.art.fail_upload = True
        self.art.fail_upload_message = "TV request timed out"

        failed = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(failed["last_result"]["state"], "failed")
        self.assertIn("TV request timed out", failed["last_result"]["message"])
        self.assertTrue(failed["last_result"]["timestamp"])
        self.assertEqual(failed["entries"][0]["content_id"], old_id)
        self.assertIn(old_id, self.art.items)

        retried = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(retried["last_result"]["state"], "completed")
        self.assertNotEqual(retried["entries"][0]["content_id"], old_id)

    def test_later_upload_failure_stops_immediately_and_queues_partial_to_cleanup_pending(self):
        old_id = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]
        self.library.clear_set("root")
        self.library.put_image("root", "photo.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0green\xff\xd9")
        self.library.put_image("root", "photo3.jpg", b"\xff\xd8\xff\xe0yellow\xff\xd9")
        self.art.fail_on_upload_number = self.art.upload_count + 2

        result = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(result["last_result"]["state"], "failed")
        # Fail-fast: photo3 was never attempted
        self.assertEqual(self.art.upload_count, 3)
        # ADR 0001: NO rollback delete storm!
        self.assertFalse(any(call[0] == "delete" for call in self.art.calls))
        # Old set preserved and partial APP-2 moved to cleanup_pending
        self.assertEqual(result["entries"][0]["content_id"], old_id)
        self.assertIn(old_id, self.art.items)
        self.assertIn("APP-2", self.art.items)
        self.assertEqual(result["cleanup_pending"], ["APP-2"])

        # Next explicit sync reconciles APP-2 and old_id
        self.art.fail_on_upload_number = None
        retried = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(retried["last_result"]["state"], "completed")
        self.assertEqual(retried["cleanup_pending"], [])
        self.assertNotIn("APP-2", self.art.items)
        self.assertNotIn(old_id, self.art.items)

    def test_empty_valid_set_does_not_change_tv_or_manifest_and_releases_lease(self):
        active = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]
        self.library.clear_set("root")

        result = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(result["last_result"]["state"], "failed")
        self.assertIn("no valid images", result["last_result"]["message"])
        self.assertEqual(result["entries"][0]["content_id"], active)
        self.assertIn(active, self.art.items)
        self.assertEqual(self.library.active_leases, {})
        self.assertEqual(len(self.library.released_leases), 2)

    def test_library_lease_always_released_on_prepare_error_and_release_error_logged(self):
        self.library.fail_prepare_with = IPCError("library offline", status_code=503, offline=True)
        res = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(res["last_result"]["state"], "failed")
        self.assertEqual(len(self.library.released_leases), 1)

        # Even if release-lease fails, original exception is not masked
        self.library.fail_release = True
        self.art.mode = "on"
        self.library.clear_set("root")
        with self.assertLogs("frame_art_mode.tv_daemon", level="WARNING"):
            res2 = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(res2["last_result"]["state"], "failed")
        self.assertIn("no valid images", res2["last_result"]["message"])

    def test_large_set_warning_is_emitted(self):
        self.library.clear_set("root")
        for i in range(self.config.max_set_images_warning + 1):
            self.library.put_image("root", f"img_{i}.jpg", f"jpeg-{i}".encode())
        res = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(res["last_result"]["state"], "completed")
        self.assertTrue(
            any("exceeds warning threshold" in w for w in res["warnings"])
        )

    def test_partial_cleanup_fail_fast_keeps_new_set_active_and_retries(self):
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0green\xff\xd9")
        first = self._sync_and_wait(self.daemon, "root")
        old_ids = [entry["content_id"] for entry in first["entries"]]

        self.library.put_image("occasion:Party", "Party/p1.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")
        self.art.fail_delete.add(old_ids[0])
        self.art.calls.clear()

        result = self._sync_and_wait(self.daemon, "occasion:Party")
        new_id = result["entries"][0]["content_id"]
        self.assertEqual(result["last_result"]["state"], "failed")
        self.assertEqual(result["active_set"], "occasion:Party")
        # Fail-fast: stopped on first delete error, did not attempt second delete
        delete_calls = [c for c in self.art.calls if c[0] == "delete"]
        self.assertEqual(delete_calls, [("delete", old_ids[0])])
        self.assertEqual(result["cleanup_pending"], old_ids)
        self.assertIn(old_ids[0], self.art.items)
        self.assertIn(old_ids[1], self.art.items)

        self.art.fail_delete.clear()
        retried = self._sync_and_wait(self.daemon, "occasion:Party")
        self.assertEqual(retried["last_result"]["state"], "completed")
        self.assertEqual(retried["cleanup_pending"], [])
        self.assertNotIn(old_ids[0], self.art.items)
        self.assertNotIn(old_ids[1], self.art.items)
        self.assertIn(new_id, self.art.items)

    def test_delete_returning_false_is_verified_via_available(self):
        old_id = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]
        self.library.put_image("occasion:Party", "Party/p1.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")
        # Simulate TV returning False even though item was already gone on TV
        self.art.items.pop(old_id)
        self.art.fail_delete.add(old_id)
        result = self._sync_and_wait(self.daemon, "occasion:Party")
        self.assertEqual(result["last_result"]["state"], "completed")
        self.assertEqual(result["cleanup_pending"], [])

    def test_sync_checks_art_mode_and_records_configured_delays_and_select_before_delete(self):
        self.art.mode = "off"
        off_res = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(off_res["last_result"]["state"], "failed")
        self.assertIn("not in Art Mode", off_res["last_result"]["message"])
        self.assertFalse(any(call[0] == "upload" for call in self.art.calls))

        self.art.mode = "on"
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0green\xff\xd9")
        self.sleeps.clear()
        self._sync_and_wait(self.daemon, "root")
        self.assertIn(self.config.tv_op_delay_seconds, self.sleeps)
        self.assertIn(self.config.tv_batch_settle_seconds, self.sleeps)

        self.library.put_image("occasion:Party", "Party/p1.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")
        self.sleeps.clear()
        self.art.calls.clear()
        res = self._sync_and_wait(self.daemon, "occasion:Party")
        self.assertEqual(res["last_result"]["state"], "completed")

        # Select-before-delete ordering check
        select_idx = next(i for i, c in enumerate(self.art.calls) if c[0] == "select")
        delete_indices = [i for i, c in enumerate(self.art.calls) if c[0] == "delete"]
        self.assertEqual(len(delete_indices), 2)
        self.assertTrue(all(select_idx < idx for idx in delete_indices))
        self.assertNotIn(self.art.current, [self.art.calls[i][1] for i in delete_indices])
        self.assertGreaterEqual(self.sleeps.count(self.config.tv_op_delay_seconds), 1)
        self.assertGreaterEqual(self.sleeps.count(self.config.tv_batch_settle_seconds), 2)

    # ------------------------------------------------------------------
    # Unified Step-Count Progress & Single-Lane Concurrency
    # ------------------------------------------------------------------

    def test_background_sync_step_count_progress_and_409_conflicts(self):
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0green\xff\xd9")
        entered = threading.Event()
        release = threading.Event()
        self.art.on_upload = lambda: setattr(
            self.art, "available_gate", (entered, release)
        )
        start_resp = self.daemon.start_sync("root")
        self.assertTrue(start_resp["ok"])
        try:
            self.assertTrue(entered.wait(timeout=2.0))
            # GET /status is non-blocking and reports unified step count without known_uploaded
            snap = self.daemon.status()
            op = snap["operation"]
            self.assertIsNotNone(op)
            self.assertEqual(op["kind"], "sync")
            self.assertEqual(op["phase"], "verifying")
            self.assertEqual(op["current_item"], "photo.jpg")
            self.assertEqual(op["completed_ops"], 1)
            # U=2, D=0 => 1 + 2 + 1 + 1 + 0 + 0 = 5
            self.assertEqual(op["total_ops"], 5)
            self.assertNotIn("known_uploaded", op)

            # Mutually exclusive operations reject with conflict (409)
            with self.assertRaises(TVServiceError):
                self.daemon.start_sync("root")
            with self.assertRaises(TVServiceError):
                self.daemon.check_tv()
            with self.assertRaises(TVServiceError):
                self.daemon.save_settings("root", interval_minutes=30)
        finally:
            release.set()
            self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))

        completed = self.daemon.status()
        self.assertIsNone(completed["operation"])
        self.assertEqual(completed["last_result"]["state"], "completed")

    # ------------------------------------------------------------------
    # Cooperative Abort (Pre-commit, During Delete, During Preview)
    # ------------------------------------------------------------------

    def test_cooperative_abort_pre_commit_preserves_old_set_and_queues_partial(self):
        old_id = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]
        self.library.clear_set("root")
        self.library.put_image("root", "photo.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0green\xff\xd9")

        entered = threading.Event()
        release = threading.Event()
        self.art.on_upload = lambda: setattr(
            self.art, "available_gate", (entered, release)
        )
        self.art.calls.clear()

        self.daemon.start_sync("root")
        self.assertTrue(entered.wait(timeout=2.0))
        abort_res = self.daemon.request_abort()
        self.assertEqual(abort_res, {"ok": True, "aborted": True})
        self.assertTrue(self.daemon.status()["operation"]["abort_requested"])
        release.set()
        self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))

        snap = self.daemon.status()
        self.assertEqual(snap["last_result"]["state"], "aborted")
        self.assertEqual(snap["active_set"], "root")
        self.assertEqual(snap["entries"][0]["content_id"], old_id)
        self.assertEqual(snap["cleanup_pending"], ["APP-2"])
        self.assertFalse(any(c[0] == "delete" for c in self.art.calls))
        # photo2 was never uploaded
        self.assertEqual(self.art.upload_count, 2)

    def test_cooperative_abort_wakes_real_settle_sleep_immediately(self):
        self.daemon.stop()
        self.config = Config(
            script_dir=self.base,
            image_root=self.images,
            state_dir=self.state,
            tv_ip="192.168.1.50",
            bind_ip="192.168.1.20",
            preview_seconds=30,
            tv_op_delay_seconds=0.5,
            tv_batch_settle_seconds=30.0,
            tv_timeout_seconds=30.0,
            tv_verify_retries=2,
            tv_verify_interval_seconds=0.5,
        )
        self.daemon = self._make_daemon(use_real_sleep=True)
        self.daemon.start_sync("root")

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            op = self.daemon.status()["operation"]
            if op and op["phase"] == "settling-uploads":
                break
            time.sleep(0.01)
        else:
            self.fail("did not reach settling-uploads phase")

        t0 = time.monotonic()
        self.daemon.request_abort()
        self.assertTrue(self.daemon.wait_for_idle(timeout=2.0))
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(self.daemon.status()["last_result"]["state"], "aborted")
        self.assertEqual(self.daemon.status()["cleanup_pending"], ["APP-1"])

    def test_cooperative_abort_during_delete_keeps_new_set_and_remaining_cleanup(self):
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0green\xff\xd9")
        first = self._sync_and_wait(self.daemon, "root")
        old_ids = [e["content_id"] for e in first["entries"]]

        self.library.put_image("occasion:Party", "Party/p1.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")
        entered = threading.Event()
        release = threading.Event()
        self.art.delete_gate = (entered, release)

        self.daemon.start_sync("occasion:Party")
        self.assertTrue(entered.wait(timeout=2.0))
        self.daemon.request_abort()
        release.set()
        self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))

        snap = self.daemon.status()
        self.assertEqual(snap["last_result"]["state"], "aborted")
        self.assertEqual(snap["active_set"], "occasion:Party")
        # First delete completed before abort check stopped the second delete
        self.assertEqual(snap["cleanup_pending"], [old_ids[1]])
        self.assertIn(old_ids[1], self.art.items)
        self.assertEqual(self.daemon.request_abort(), {"ok": True, "aborted": False})

    # ------------------------------------------------------------------
    # Manifest Integrity & Restart Recovery
    # ------------------------------------------------------------------

    def test_unreadable_or_wrong_tv_manifest_fails_closed(self):
        active = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]
        manifest_path = self.daemon.manifest_path
        self.daemon.stop()
        manifest_path.write_text("{", encoding="utf-8")

        reopened = self._make_daemon()
        try:
            snap = reopened.status()
            self.assertIsNotNone(snap["manifest_error"])
            with self.assertRaises(TVServiceError):
                reopened.start_sync("root")
            with self.assertRaises(TVServiceError):
                reopened.save_settings("root", interval_minutes=10)
            self.assertIn(active, self.art.items)
            self.assertFalse(any(c[0] == "delete" for c in self.art.calls))
        finally:
            reopened.stop()

        # Wrong tv_ip also fails closed
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "tv_ip": "10.0.0.99",
                    "active_set": "root",
                    "entries": [],
                    "cleanup_pending": [],
                    "settings": {},
                    "current_content_id": None,
                }
            ),
            encoding="utf-8",
        )
        reopened2 = self._make_daemon()
        try:
            self.assertIsNotNone(reopened2.status()["manifest_error"])
            with self.assertRaises(TVServiceError):
                reopened2.start_sync("root")
        finally:
            reopened2.stop()

    def test_manifest_removed_while_running_prevents_sync_and_deletion(self):
        active = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]
        self.daemon.manifest_path.unlink()
        with self.assertRaisesRegex(TVServiceError, "manifest disappeared"):
            self.daemon.start_sync("root")
        self.assertIn(active, self.art.items)
        self.assertFalse(any(c[0] == "delete" for c in self.art.calls))

    def test_manifest_loss_during_upload_fails_fast_without_delete_storm(self):
        active = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]
        self.library.clear_set("root")
        self.library.put_image("root", "photo.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")
        self.art.on_upload = self.daemon.manifest_path.unlink

        res = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(res["last_result"]["state"], "failed")
        self.assertIsNotNone(res["manifest_error"])
        self.assertIn(active, self.art.items)
        self.assertIn("APP-2", self.art.items)
        self.assertFalse(any(c[0] == "delete" for c in self.art.calls))
        op_data = json.loads(self.daemon.operation_path.read_text(encoding="utf-8"))
        self.assertEqual(op_data["last_result"]["recovery_pending"], ["APP-2"])

    def test_restart_marks_sync_interrupted_and_recovers_partial_uploads_and_check_preserves(self):
        active_id = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]
        partial_id = "APP-PARTIAL"
        self.art.items[partial_id] = b"partial"
        self.daemon._begin_operation("sync", "uploading", "Uploading photo.jpg")
        self.daemon._remember_uploaded(partial_id)
        self.daemon.stop()

        reopened = self._make_daemon()
        try:
            interrupted = reopened.status()
            self.assertIsNone(interrupted["operation"])
            self.assertEqual(interrupted["last_result"]["state"], "interrupted")
            self.assertEqual(interrupted["entries"][0]["content_id"], active_id)
            self.assertEqual(interrupted["cleanup_pending"], [partial_id])
            self.assertIn(partial_id, self.art.items)

            # POST /check preserves existing interrupted last_result
            check_res = reopened.check_tv()
            self.assertTrue(check_res["tv_ready"])
            self.assertEqual(reopened.status()["last_result"]["state"], "interrupted")

            retried = self._sync_and_wait(reopened, "root")
            self.assertEqual(retried["last_result"]["state"], "completed")
            self.assertEqual(retried["cleanup_pending"], [])
            self.assertNotIn(partial_id, self.art.items)
            self.assertIn(active_id, self.art.items)
        finally:
            reopened.stop()

    # ------------------------------------------------------------------
    # Art Store Favorites (ADR 0002), Preview, Slideshow & Drift
    # ------------------------------------------------------------------

    def test_art_store_favorites_setting_defaults_off_and_saves_per_set(self):
        self.assertEqual(
            self.daemon._manifest.settings_for("root").mix_art_store_favorites,
            False,
        )
        self.daemon.save_settings(
            "occasion:Party",
            random=True,
            interval_minutes=9,
            mix_art_store_favorites=True,
        )
        snap = self.daemon.status()
        self.assertTrue(snap["settings"]["occasion:Party"]["mix_art_store_favorites"])
        self.assertEqual(snap["settings"]["occasion:Party"]["interval_minutes"], 9)
        self.assertFalse(self.daemon._manifest.settings_for("root").mix_art_store_favorites)

    def test_random_pick_mixes_filtered_favorites_and_deduplicates_nas_ids(self):
        self.art.favorites = [
            {"content_id": "SAM-ART-1"},
            {"content_id": "APP-1"},
            {"content_id": "UNKNOWN-ART"},
        ]
        picked: list = []
        self.daemon._chooser = lambda candidates: (
            picked.extend(candidates)
            or next(item for item in candidates if item.category == ART_STORE_FAVORITES_CATEGORY)
        )
        self.daemon.save_settings(
            "root",
            random=True,
            interval_minutes=15,
            mix_art_store_favorites=True,
        )

        res = self._sync_and_wait(self.daemon, "root")
        self.assertEqual([item.content_id for item in picked], ["APP-1", "SAM-ART-1"])
        self.assertEqual(self.art.current, "SAM-ART-1")
        self.assertEqual(self.art.current_category, ART_STORE_FAVORITES_CATEGORY)
        self.assertTrue(res["current_display"]["is_art_store"])
        self.assertEqual(res["current_display"]["category"], ART_STORE_FAVORITES_CATEGORY)
        self.assertTrue(any("UNKNOWN-ART" in w for w in res["warnings"]))
        self.assertIn(("available", ART_STORE_FAVORITES_CATEGORY), self.art.calls)

    def test_favorites_are_not_queried_when_random_mode_is_off(self):
        self.daemon.save_settings(
            "root",
            random=False,
            interval_minutes=15,
            mix_art_store_favorites=True,
        )
        self._sync_and_wait(self.daemon, "root")
        self.assertFalse(
            any(c == ("available", ART_STORE_FAVORITES_CATEGORY) for c in self.art.calls)
        )
        self.assertIsNone(self.art.current_category)

    def test_empty_favorites_and_query_errors_fall_back_then_retry_next_pick(self):
        entries = (ManifestEntry("photo.jpg", "photo.jpg", "fp1", "APP-1"),)
        self.daemon._chooser = lambda candidates: candidates[0]
        self.art.favorites = []
        self.assertEqual(self.daemon._random_pick(self.art, entries, True).content_id, "APP-1")

        self.art.fail_favorites = True
        self.assertEqual(self.daemon._random_pick(self.art, entries, True).content_id, "APP-1")
        self.assertTrue(
            any("favorites unavailable" in w for w in self.daemon.status()["warnings"])
        )

        self.art.favorites = [{"content_id": "SAM-ART-2"}]
        retry = self.daemon._random_pick(self.art, entries, True)
        self.assertEqual(retry.content_id, "APP-1")
        self.assertEqual(
            sum(c == ("available", ART_STORE_FAVORITES_CATEGORY) for c in self.art.calls),
            3,
        )
        self.assertFalse(
            any("favorites unavailable" in w for w in self.daemon.status()["warnings"])
        )

    def test_preview_is_limited_to_active_set_and_restores_favorite_category_and_timer(self):
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0green\xff\xd9")
        synced = self._sync_and_wait(self.daemon, "root")
        first_id = synced["entries"][0]["content_id"]

        with self.assertRaises(TVServiceError):
            self.daemon.start_preview("SAMSUNG-ART")

        # Test nested/camelCase get_current response variant
        self.art.current = "SAM-ART-RESTORE"
        self.art.current_category = ART_STORE_FAVORITES_CATEGORY
        self.art.current_raw_override = {
            "content": {"contentId": "SAM-ART-RESTORE", "categoryId": ART_STORE_FAVORITES_CATEGORY}
        }

        self.clock.advance(100.0)
        start_preview = self.daemon.start_preview(first_id)
        self.assertTrue(start_preview["preview"]["active"])
        self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))

        self.assertIn(float(self.config.preview_seconds), self.sleeps)
        self.assertEqual(self.art.current, "SAM-ART-RESTORE")
        self.assertEqual(self.art.current_category, ART_STORE_FAVORITES_CATEGORY)
        snap = self.daemon.status()
        self.assertFalse(snap["preview"]["active"])
        self.assertEqual(snap["last_result"]["state"], "completed")
        self.assertEqual(snap["current_display"]["content_id"], "SAM-ART-RESTORE")
        self.assertTrue(snap["current_display"]["is_art_store"])
        self.assertEqual(self.daemon._next_at_mono, 1100.0 + 15 * 60)

    def test_preview_abort_immediately_restores_previous_image_and_category(self):
        synced = self._sync_and_wait(self.daemon, "root")
        first_id = synced["entries"][0]["content_id"]
        self.art.current = "SAM-ART-ORIG"
        self.art.current_category = ART_STORE_FAVORITES_CATEGORY

        self.daemon.stop()
        self.config = Config(
            script_dir=self.base,
            image_root=self.images,
            state_dir=self.state,
            tv_ip="192.168.1.50",
            bind_ip="192.168.1.20",
            preview_seconds=30,
        )
        self.daemon = self._make_daemon(use_real_sleep=True)
        self.daemon.start_preview(first_id)

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            op = self.daemon.status()["operation"]
            if op and op["phase"] == "previewing":
                break
            time.sleep(0.01)
        else:
            self.fail("preview did not enter previewing phase")

        t0 = time.monotonic()
        self.daemon.request_abort()
        self.assertTrue(self.daemon.wait_for_idle(timeout=2.0))
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(self.art.current, "SAM-ART-ORIG")
        self.assertEqual(self.art.current_category, ART_STORE_FAVORITES_CATEGORY)
        self.assertEqual(self.daemon.status()["last_result"]["state"], "aborted")

    def test_slideshow_and_favorites_queries_are_visible_and_zero_idle_tv_polling(self):
        self.art.favorites = [{"content_id": "SAM-ART-1"}]
        self.daemon.save_settings(
            "root",
            random=True,
            interval_minutes=15,
            mix_art_store_favorites=True,
        )
        self._sync_and_wait(self.daemon, "root")
        observed_phases: list[str] = []
        self.art.on_favorites = lambda: observed_phases.append(
            self.daemon.status()["operation"]["phase"]
        )
        self.art.calls.clear()

        # GET /status performs zero TV I/O
        self.daemon.status()
        self.assertEqual(self.art.calls, [])

        self.assertTrue(self.daemon.run_slideshow_tick())
        self.assertEqual(observed_phases, ["querying-favorites"])
        snap = self.daemon.status()
        self.assertEqual(snap["last_result"]["kind"], "slideshow-selection")
        self.assertEqual(snap["last_result"]["state"], "completed")

    def test_drift_tolerance_skips_to_next_candidate_and_clears_on_sync(self):
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0green\xff\xd9")
        self.daemon.save_settings(
            "root",
            random=False,
            interval_minutes=15,
            mix_art_store_favorites=False,
        )
        synced = self._sync_and_wait(self.daemon, "root")
        first_id, second_id = [e["content_id"] for e in synced["entries"]]
        self.assertEqual(self.art.current, first_id)

        # Second image disappeared from TV (drift), first is still valid
        self.art.fail_select.add(second_id)
        self.assertTrue(self.daemon.run_slideshow_tick())
        snap = self.daemon.status()
        self.assertFalse(snap["cycling_paused"])
        self.assertEqual(len(snap["drift_warnings"]), 1)
        self.assertIn(second_id, snap["drift_warnings"][0])
        self.assertEqual(self.art.current, first_id)

        # Next sync clears drift_warnings
        self.art.fail_select.clear()
        resynced = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(resynced["drift_warnings"], [])

    def test_cycling_pauses_on_unreachable_tv_with_zero_idle_polling_until_check(self):
        self._sync_and_wait(self.daemon, "root")
        self.art.fail_artmode = True
        self.assertFalse(self.daemon.run_slideshow_tick())
        snap = self.daemon.status()
        self.assertTrue(snap["cycling_paused"])
        self.assertFalse(snap["tv_ready"])

        # Start actor loop when paused and confirm zero idle TV polling
        self.art.calls.clear()
        self.daemon.start()
        time.sleep(0.05)
        self.assertEqual(self.art.calls, [])

        # Explicit POST /check when TV returns resumes cycling
        self.art.fail_artmode = False
        check_res = self.daemon.check_tv()
        self.assertTrue(check_res["tv_ready"])
        self.assertFalse(self.daemon.status()["cycling_paused"])

    # ------------------------------------------------------------------
    # Unix Domain Socket IPC Endpoints & CLI Subcommands
    # ------------------------------------------------------------------

    def test_all_tv_sock_endpoints_and_error_envelopes(self):
        server = build_tv_ipc_server(self.daemon)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.daemon.start()

        def _cleanup() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

        self.addCleanup(_cleanup)

        sock = self.config.tv_sock

        # 1. GET /health
        health = call_json(sock, "GET", "/health")
        self.assertTrue(health["ok"])
        self.assertEqual(health["tv_key"], self.config.tv_key)
        self.assertIsInstance(health["pid"], int)

        # 2. GET /status (exact schema keys)
        status = call_json(sock, "GET", "/status")
        expected_keys = {
            "tv_ready",
            "tv_checked_at",
            "active_set",
            "entries",
            "current_display",
            "next_rotation_at",
            "cycling_paused",
            "preview",
            "cleanup_pending",
            "settings",
            "operation",
            "last_result",
            "warnings",
            "drift_warnings",
            "manifest_error",
            "last_error",
        }
        self.assertEqual(set(status.keys()), expected_keys)

        # 3. POST /check
        check = call_json(sock, "POST", "/check", body={})
        self.assertTrue(check["ok"])
        self.assertTrue(check["tv_ready"])

        # 4. POST /settings (200 + 400 validation)
        bad_status, bad_body = request_json(
            sock, "POST", "/settings", body={"set_id": "", "interval_minutes": 5}
        )
        self.assertEqual(bad_status, HTTP_BAD_REQUEST)
        self.assertEqual(bad_body["code"], "bad_request")

        bad_interval_status, _ = request_json(
            sock,
            "POST",
            "/settings",
            body={"set_id": "root", "random": True, "mix_art_store_favorites": False, "interval_minutes": 0},
        )
        self.assertEqual(bad_interval_status, HTTP_BAD_REQUEST)

        saved = call_json(
            sock,
            "POST",
            "/settings",
            body={
                "set_id": "root",
                "random": False,
                "mix_art_store_favorites": True,
                "interval_minutes": 10,
            },
        )
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["settings"]["interval_minutes"], 10)

        # 5. POST /sync (400 + 202)
        bad_sync_status, _ = request_json(sock, "POST", "/sync", body={"set_id": ""})
        self.assertEqual(bad_sync_status, HTTP_BAD_REQUEST)

        sync_status, sync_body = request_json(
            sock,
            "POST",
            "/sync",
            body={"set_id": "root", "settings": {"random": True, "interval_minutes": 12}},
        )
        self.assertEqual(sync_status, HTTP_ACCEPTED)
        self.assertTrue(sync_body["ok"])
        self.assertEqual(sync_body["operation"]["kind"], "sync")
        self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))

        # 6. POST /preview (400 + 404 + 202)
        bad_prev_status, _ = request_json(sock, "POST", "/preview", body={})
        self.assertEqual(bad_prev_status, HTTP_BAD_REQUEST)

        miss_status, miss_body = request_json(
            sock, "POST", "/preview", body={"content_id": "NON-EXISTENT"}
        )
        self.assertEqual(miss_status, HTTP_NOT_FOUND)
        self.assertEqual(miss_body["code"], "not_found")

        active_id = call_json(sock, "GET", "/status")["entries"][0]["content_id"]
        prev_status, prev_body = request_json(
            sock, "POST", "/preview", body={"content_id": active_id}
        )
        self.assertEqual(prev_status, HTTP_ACCEPTED)
        self.assertTrue(prev_body["preview"]["active"])
        self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))

        # 7. POST /abort (200)
        abort_status, abort_body = request_json(sock, "POST", "/abort", body={})
        self.assertEqual(abort_status, HTTP_OK)
        self.assertEqual(abort_body, {"ok": True, "aborted": False})

        # 409 conflict during active sync
        entered = threading.Event()
        release = threading.Event()
        self.library.clear_set("root")
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0green\xff\xd9")
        self.art.on_upload = lambda: setattr(self.art, "available_gate", (entered, release))
        try:
            call_json(sock, "POST", "/sync", body={"set_id": "root"})
            self.assertTrue(entered.wait(timeout=2.0))
            c_status, c_body = request_json(sock, "POST", "/check", body={})
            self.assertEqual(c_status, HTTP_CONFLICT)
            self.assertEqual(c_body["code"], "conflict")
        finally:
            release.set()
            self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))

    def test_cli_client_subcommands_talk_to_tv_sock(self):
        server = build_tv_ipc_server(self.daemon)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.daemon.start()

        def _cleanup() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

        self.addCleanup(_cleanup)

        cfg_file = self.base / "config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "tv_ip": self.config.tv_ip,
                    "bind_ip": self.config.bind_ip,
                    "state_dir": str(self.state),
                    "image_root": str(self.images),
                }
            ),
            encoding="utf-8",
        )

        for argv in (
            ["--config", str(cfg_file), "status"],
            ["check", "--config", str(cfg_file)],
            ["--config", str(cfg_file), "sync", "root"],
        ):
            stdout = io.StringIO()
            with mock.patch("sys.stdout", new=stdout):
                code = main(argv)
            self.assertEqual(code, 0)
            parsed = json.loads(stdout.getvalue())
            self.assertIsInstance(parsed, dict)

        self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))
        active_id = self.daemon.status()["entries"][0]["content_id"]

        for argv in (
            ["--config", str(cfg_file), "preview", active_id],
            ["--config", str(cfg_file), "abort"],
        ):
            stdout = io.StringIO()
            with mock.patch("sys.stdout", new=stdout):
                code = main(argv)
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(stdout.getvalue())["ok"])
        self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))

    def test_default_tv_factory_lazy_imports_samsungframetv(self):
        from frame_art_mode.tv_daemon import default_tv_factory

        fake_art = mock.MagicMock()
        fake_tv = mock.MagicMock()
        fake_tv.art.return_value = fake_art
        fake_cls = mock.MagicMock(return_value=fake_tv)
        fake_module = mock.MagicMock(SamsungTVWS=fake_cls)
        token_path = self.config.tv_dir / "tv-token.txt"

        with mock.patch.dict(sys.modules, {"samsungframetv": fake_module}):
            result = default_tv_factory(self.config, token_path)

        self.assertIs(result, fake_art)
        fake_cls.assert_called_once_with(
            self.config.tv_ip,
            token_file=str(token_path),
            timeout=self.config.tv_timeout_seconds,
        )
        fake_tv.art.assert_called_once_with(timeout=self.config.tv_timeout_seconds)

    def test_upload_verification_retries_and_unverified_upload_queues_cleanup(self):
        # Succeeds after delayed appearance in available()
        avail_checks = 0
        orig_available = self.art.available

        def delayed_available(category=None):
            nonlocal avail_checks
            if category is None:
                avail_checks += 1
                if avail_checks == 2:
                    self.art.unverified_uploads.add("APP-1")
                else:
                    self.art.unverified_uploads.discard("APP-1")
            return orig_available(category)

        self.art.available = delayed_available  # type: ignore[method-assign]
        res = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(res["last_result"]["state"], "completed")
        self.assertIn(self.config.tv_verify_interval_seconds, self.sleeps)

        # Fails after tv_verify_retries and queues partial upload to cleanup_pending
        self.art.available = orig_available  # type: ignore[method-assign]
        self.library.clear_set("root")
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")
        self.art.unverified_uploads.add("APP-2")
        self.sleeps.clear()
        failed = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(failed["last_result"]["state"], "failed")
        self.assertIn("did not verify", failed["last_result"]["message"])
        self.assertEqual(
            self.sleeps.count(self.config.tv_verify_interval_seconds),
            self.config.tv_verify_retries - 1,
        )
        self.assertEqual(failed["cleanup_pending"], ["APP-2"])
        self.assertFalse(any(c[0] == "delete" for c in self.art.calls))

    def test_lost_art_mode_after_upload_settle_fails_fast_pre_commit(self):
        old_id = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]
        self.library.clear_set("root")
        self.library.put_image("root", "photo.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")

        def drop_artmode_on_settle(seconds: float) -> None:
            self.sleeps.append(seconds)
            if seconds == self.config.tv_batch_settle_seconds:
                self.art.mode = "off"

        self.daemon._sleeper = drop_artmode_on_settle
        res = self._sync_and_wait(self.daemon, "root")
        self.assertEqual(res["last_result"]["state"], "failed")
        self.assertIn("not in Art Mode", res["last_result"]["message"])
        self.assertEqual(res["entries"][0]["content_id"], old_id)
        self.assertEqual(res["cleanup_pending"], ["APP-2"])
        self.assertFalse(any(c[0] == "delete" for c in self.art.calls))

    def test_step7_select_failure_aborts_before_delete_loop_and_records_drift(self):
        old_id = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]
        self.library.put_image("occasion:Party", "Party/p1.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")
        self.art.fail_select.add("APP-2")
        self.art.calls.clear()

        res = self._sync_and_wait(self.daemon, "occasion:Party")
        self.assertEqual(res["last_result"]["state"], "failed")
        self.assertEqual(res["active_set"], "occasion:Party")
        # Select-before-delete: old_id must NOT be deleted when select failed!
        self.assertFalse(any(c[0] == "delete" for c in self.art.calls))
        self.assertEqual(res["cleanup_pending"], [old_id])
        self.assertIn(old_id, self.art.items)
        self.assertEqual(len(res["drift_warnings"]), 1)

    def test_settings_allowed_during_preview_and_sync_saves_settings_atomically_first(self):
        self._sync_and_wait(self.daemon, "root")
        # Settings passed in POST /sync are persisted even if sync fails on Art Mode off
        self.art.mode = "off"
        res = self._sync_and_wait(
            self.daemon,
            "root",
            settings={"random": False, "mix_art_store_favorites": True, "interval_minutes": 45},
        )
        self.assertEqual(res["last_result"]["state"], "failed")
        self.assertEqual(res["settings"]["root"]["interval_minutes"], 45)
        self.assertFalse(res["settings"]["root"]["random"])

        # Settings allowed during preview (only rejected during sync)
        self.art.mode = "on"
        active_id = res["entries"][0]["content_id"]
        self.daemon.stop()
        self.config = Config(
            script_dir=self.base,
            image_root=self.images,
            state_dir=self.state,
            tv_ip="192.168.1.50",
            bind_ip="192.168.1.20",
            preview_seconds=30,
        )
        self.daemon = self._make_daemon(use_real_sleep=True)
        self.daemon.start_preview(active_id)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            op = self.daemon.status()["operation"]
            if op and op["phase"] == "previewing":
                break
            time.sleep(0.01)
        saved = self.daemon.save_settings("root", interval_minutes=20)
        self.assertEqual(saved["settings"]["interval_minutes"], 20)
        self.daemon.request_abort()
        self.assertTrue(self.daemon.wait_for_idle(timeout=2.0))

    def test_recovery_pending_reconciled_after_manifest_repaired(self):
        active_id = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]
        valid_manifest_text = self.daemon.manifest_path.read_text(encoding="utf-8")
        self.daemon.stop()

        # Simulate interrupted sync with unreadable manifest -> recovery_pending stored in operation.json
        self.daemon.manifest_path.write_text("{corrupt", encoding="utf-8")
        self.daemon.operation_path.write_text(
            json.dumps(
                {
                    "current": OperationState(
                        kind="sync",
                        phase="uploading",
                        message="Uploading",
                        known_uploaded=("APP-ORPHAN",),
                    ).to_dict(),
                    "last_result": None,
                }
            ),
            encoding="utf-8",
        )
        with self.assertLogs("frame_art_mode.tv_daemon", level="ERROR"):
            broken = self._make_daemon()
        try:
            self.assertIsNotNone(broken.status()["manifest_error"])
            self.assertEqual(broken.status()["last_result"]["state"], "interrupted")
            op_json = json.loads(broken.operation_path.read_text(encoding="utf-8"))
            self.assertEqual(op_json["last_result"]["recovery_pending"], ["APP-ORPHAN"])
        finally:
            broken.stop()

        # Repair manifest and restart -> recovery_pending merges into cleanup_pending
        self.daemon.manifest_path.write_text(valid_manifest_text, encoding="utf-8")
        repaired = self._make_daemon()
        try:
            snap = repaired.status()
            self.assertIsNone(snap["manifest_error"])
            self.assertEqual(snap["entries"][0]["content_id"], active_id)
            self.assertEqual(snap["cleanup_pending"], ["APP-ORPHAN"])
            op_json2 = json.loads(repaired.operation_path.read_text(encoding="utf-8"))
            self.assertEqual(op_json2["last_result"]["recovery_pending"], [])
        finally:
            repaired.stop()

    # ------------------------------------------------------------------
    # Aligned PreparedImage Schema & Step-Count Matrix
    # ------------------------------------------------------------------

    def test_prepared_image_from_dict_accepts_aligned_library_extra_fields(self):
        entry = self.library.put_image(
            "occasion:Party",
            "Party/sub/pic.jpg",
            b"\xff\xd8\xff\xe0party\xff\xd9",
        )
        self.assertIn("name_rel", entry)
        self.assertIn("thumb_path", entry)
        self.assertIn("byte_size", entry)
        prepared = PreparedImage.from_dict(entry)
        self.assertEqual(prepared.source_rel, "Party/sub/pic.jpg")
        self.assertEqual(prepared.filename, "pic.jpg")
        self.assertEqual(prepared.byte_size, entry["byte_size"])

    def test_total_ops_and_monotonic_completed_ops_matrix(self):
        snapshots: list[tuple[str, int, int]] = []
        orig_persist = self.daemon._persist_operation

        def capture_persist() -> None:
            orig_persist()
            op = self.daemon._current_op
            if op is not None and op.kind == "sync":
                snapshots.append((op.phase, op.completed_ops, op.total_ops))

        self.daemon._persist_operation = capture_persist  # type: ignore[method-assign]

        def assert_monotonic_and_total(expected_total: int) -> None:
            self.assertTrue(snapshots)
            planning_phases = [
                (p, c, t)
                for p, c, t in snapshots
                if t == 0
            ]
            self.assertTrue(
                all(p in {"starting", "checking-tv", "preparing", "planning"} and c == 0 for p, c, _ in planning_phases),
                f"unexpected total_ops=0 snapshots: {planning_phases}",
            )
            counted = [(p, c, t) for p, c, t in snapshots if t > 0]
            self.assertTrue(counted)
            self.assertTrue(all(t == expected_total for _, _, t in counted), counted)
            completed_seq = [c for _, c, _ in counted]
            self.assertEqual(completed_seq, sorted(completed_seq))
            self.assertEqual(completed_seq[-1], expected_total)

        # Case 1: U=1, D=0 -> total_ops = 1 + 1 + 1 + 1 + 0 + 0 = 4
        snapshots.clear()
        self.sleeps.clear()
        self._sync_and_wait(self.daemon, "root")
        assert_monotonic_and_total(4)
        self.assertEqual(self.sleeps.count(self.config.tv_batch_settle_seconds), 1)

        # Case 2: U=0, D=0 -> total_ops = 1 + 0 + 0 + 1 + 0 + 0 = 2 (no settle sleeps)
        snapshots.clear()
        self.sleeps.clear()
        self._sync_and_wait(self.daemon, "root")
        assert_monotonic_and_total(2)
        self.assertEqual(self.sleeps.count(self.config.tv_batch_settle_seconds), 0)

        # Case 3: U=2, D=1 -> total_ops = 1 + 2 + 1 + 1 + 1 + 1 = 7
        self.library.put_image("occasion:Party", "Party/a.jpg", b"\xff\xd8\xff\xe0a\xff\xd9")
        self.library.put_image("occasion:Party", "Party/b.jpg", b"\xff\xd8\xff\xe0b\xff\xd9")
        snapshots.clear()
        self.sleeps.clear()
        self._sync_and_wait(self.daemon, "occasion:Party")
        assert_monotonic_and_total(7)
        self.assertEqual(self.sleeps.count(self.config.tv_batch_settle_seconds), 2)

        # Case 4: U=0, D=2 -> switching back to "root" when root's image was deleted from TV?
        # Keep root image NOT on TV -> let's put an extra cleanup_pending item while reusing Party/a.jpg & Party/b.jpg?
        # Better: upload root image while Party has 2 items, or add 2 cleanup_pending items and reuse Party's 2 items!
        self.daemon.stop()
        manifest_data = json.loads(self.daemon.manifest_path.read_text(encoding="utf-8"))
        manifest_data["cleanup_pending"] = ["ORPHAN-1", "ORPHAN-2"]
        self.daemon.manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
        self.art.items["ORPHAN-1"] = b"x"
        self.art.items["ORPHAN-2"] = b"y"
        self.daemon = self._make_daemon()
        self.daemon._persist_operation = capture_persist  # type: ignore[method-assign]
        snapshots.clear()
        self.sleeps.clear()
        # U=0 (reuses Party/a.jpg & Party/b.jpg), D=2 (ORPHAN-1, ORPHAN-2) -> 1 + 0 + 0 + 1 + 2 + 1 = 5
        self._sync_and_wait(self.daemon, "occasion:Party")
        assert_monotonic_and_total(5)
        self.assertEqual(self.sleeps.count(self.config.tv_batch_settle_seconds), 1)

    # ------------------------------------------------------------------
    # Manifest Mutation & Unexpected Creation Fail-Closed
    # ------------------------------------------------------------------

    def test_manifest_external_mutation_and_unexpected_creation_fail_closed(self):
        # Case 1: unexpected manifest creation on disk when none existed at startup
        self.assertFalse(self.daemon._manifest_persisted)
        self.daemon.manifest_path.write_text(
            json.dumps(Manifest(tv_ip=self.config.tv_ip).to_dict()),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(TVServiceError, "appeared unexpectedly"):
            self.daemon.start_sync("root")
        self.assertIsNotNone(self.daemon.status()["manifest_error"])

        # Reset cleanly and sync once so manifest is persisted
        self.daemon.stop()
        self.daemon.manifest_path.unlink()
        self.daemon = self._make_daemon()
        active_id = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]

        # Case 2: external mutation on disk (disk_manifest != self._manifest)
        tampered = json.loads(self.daemon.manifest_path.read_text(encoding="utf-8"))
        tampered["cleanup_pending"] = ["ROGUE-ID"]
        self.daemon.manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
        self.art.items["ROGUE-ID"] = b"rogue"

        with self.assertRaisesRegex(TVServiceError, "changed while the service was running"):
            self.daemon.start_sync("root")
        self.assertIn(active_id, self.art.items)
        self.assertIn("ROGUE-ID", self.art.items)
        self.assertFalse(any(c[0] == "delete" for c in self.art.calls))

    # ------------------------------------------------------------------
    # Preview Failures, Shutdown Interruption & Error Recovery
    # ------------------------------------------------------------------

    def test_preview_failures_and_shutdown_interruption(self):
        active_id = self._sync_and_wait(self.daemon, "root")["entries"][0]["content_id"]

        # 1. get_current() returning missing content_id
        self.art.current = None
        self.art.current_raw_override = {}
        self.daemon.start_preview(active_id)
        self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))
        snap = self.daemon.status()
        self.assertFalse(snap["preview"]["active"])
        self.assertEqual(snap["last_result"]["state"], "failed")
        self.assertIn("did not identify its current image", snap["last_result"]["message"])

        # 2. get_current() raising an exception
        self.art.current_raw_override = None
        orig_get_current = self.art.get_current
        self.art.get_current = mock.MagicMock(side_effect=RuntimeError("ws get_current failed"))  # type: ignore[method-assign]
        self.daemon.start_preview(active_id)
        self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))
        snap = self.daemon.status()
        self.assertEqual(snap["last_result"]["state"], "failed")
        self.assertIn("could not read the current TV image", snap["last_result"]["message"])
        self.art.get_current = orig_get_current  # type: ignore[method-assign]

        # 3. select_image failure on preview start while Art Mode is still on -> records drift warning
        self.art.current = active_id
        self.art.fail_select.add(active_id)
        self.daemon.start_preview(active_id)
        self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))
        snap = self.daemon.status()
        self.assertEqual(snap["last_result"]["state"], "failed")
        self.assertFalse(snap["preview"]["active"])
        self.assertEqual(len(snap["drift_warnings"]), 1)
        self.art.fail_select.clear()

        # 4. Phase 3 restore failure pauses cycling; subsequent check_tv() clears last_error
        self.art.current = "APP-RESTORE"
        self.art.fail_select.add("APP-RESTORE")
        self.daemon.start_preview(active_id)
        self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))
        failed_restore = self.daemon.status()
        self.assertEqual(failed_restore["last_result"]["state"], "failed")
        self.assertTrue(failed_restore["cycling_paused"])
        self.assertFalse(failed_restore["tv_ready"])
        self.assertTrue(failed_restore["last_error"].startswith("preview restore failed:"))

        self.art.fail_select.clear()
        check_res = self.daemon.check_tv()
        self.assertTrue(check_res["tv_ready"])
        recovered = self.daemon.status()
        self.assertFalse(recovered["cycling_paused"])
        self.assertIsNone(recovered["last_error"])

        # 5. stop() interrupting Phase 2 records state="interrupted"
        self.daemon.stop()
        self.config = Config(
            script_dir=self.base,
            image_root=self.images,
            state_dir=self.state,
            tv_ip="192.168.1.50",
            bind_ip="192.168.1.20",
            preview_seconds=30,
        )
        self.daemon = self._make_daemon(use_real_sleep=True)
        self.art.current = active_id
        self.daemon.start_preview(active_id)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            op = self.daemon.status()["operation"]
            if op and op["phase"] == "previewing":
                break
            time.sleep(0.01)
        else:
            self.fail("preview did not enter previewing phase")

        self.daemon.stop()
        self.assertEqual(self.daemon.status()["last_result"]["state"], "interrupted")
        self.assertFalse(self.daemon.status()["preview"]["active"])

    # ------------------------------------------------------------------
    # Slideshow Wrap-Around, Failures & Actor Loop Automation
    # ------------------------------------------------------------------

    def test_slideshow_sequential_wrap_around_all_fail_and_art_mode_loss(self):
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0green\xff\xd9")
        self.library.put_image("root", "photo3.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")
        self.daemon.save_settings("root", random=False, interval_minutes=15)
        synced = self._sync_and_wait(self.daemon, "root")
        id1, id2, id3 = [e["content_id"] for e in synced["entries"]]
        self.assertEqual(self.art.current, id1)

        # Sequential progression + wrap-around: id1 -> id2 -> id3 -> id1
        self.assertTrue(self.daemon.run_slideshow_tick())
        self.assertEqual(self.art.current, id2)
        self.assertTrue(self.daemon.run_slideshow_tick())
        self.assertEqual(self.art.current, id3)
        self.assertTrue(self.daemon.run_slideshow_tick())
        self.assertEqual(self.art.current, id1)

        # Art Mode lost mid-select_image pauses cycling WITHOUT adding drift_warnings
        def drop_art_and_fail(cid: str, category: str | None = None, show: bool = True):
            self.art.mode = "off"
            raise RuntimeError("TV entered standby")

        orig_select = self.art.select_image
        self.art.select_image = drop_art_and_fail  # type: ignore[method-assign]
        self.assertFalse(self.daemon.run_slideshow_tick())
        snap = self.daemon.status()
        self.assertTrue(snap["cycling_paused"])
        self.assertFalse(snap["tv_ready"])
        self.assertEqual(snap["drift_warnings"], [])
        self.assertIn("not in Art Mode", snap["last_error"])

        # Recover via check_tv
        self.art.select_image = orig_select  # type: ignore[method-assign]
        self.art.mode = "on"
        self.daemon.check_tv()
        self.assertFalse(self.daemon.status()["cycling_paused"])

        # All candidates fail select_image while still in Art Mode -> drift warnings + paused cycling
        self.art.fail_select.update({id1, id2, id3})
        self.assertFalse(self.daemon.run_slideshow_tick())
        snap2 = self.daemon.status()
        self.assertTrue(snap2["cycling_paused"])
        self.assertEqual(len(snap2["drift_warnings"]), 3)
        self.assertIn("all managed artwork candidates failed", snap2["last_error"])

    def test_actor_loop_fires_scheduled_rotation_tick(self):
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0green\xff\xd9")
        self.daemon.save_settings("root", random=False, interval_minutes=1)
        synced = self._sync_and_wait(self.daemon, "root")
        id1, id2 = [e["content_id"] for e in synced["entries"]]
        self.assertEqual(self.art.current, id1)

        # Advance clock past the 60s rotation deadline and wake actor
        self.clock.advance(61.0)
        self.daemon._actor_wake_event.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self.art.current == id2 and self.daemon.wait_for_idle(timeout=0.1):
                break
            time.sleep(0.01)
        self.assertEqual(self.art.current, id2)

    def test_paused_cycling_auto_resumes_on_next_interval_when_tv_returns(self):
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0green\xff\xd9")
        self.daemon.save_settings("root", random=False, interval_minutes=1)
        synced = self._sync_and_wait(self.daemon, "root")
        id1, id2 = [e["content_id"] for e in synced["entries"]]
        self.assertEqual(self.art.current, id1)

        # TV drops out of Art Mode overnight -> cycling pauses and hides next_rotation_at
        self.art.mode = "off"
        self.assertFalse(self.daemon.run_slideshow_tick())
        paused = self.daemon.status()
        self.assertTrue(paused["cycling_paused"])
        self.assertFalse(paused["tv_ready"])
        self.assertIsNone(paused["next_rotation_at"])

        # Zero busy/tight TV polling while paused
        self.art.calls.clear()
        self.daemon.start()
        time.sleep(0.05)
        self.assertEqual(self.art.calls, [])

        # TV returns to Art Mode; once the interval elapses, actor auto-resumes without check_tv()
        self.art.mode = "on"
        self.clock.advance(61.0)
        self.daemon._actor_wake_event.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            snap = self.daemon.status()
            if (
                not snap["cycling_paused"]
                and snap["tv_ready"] is True
                and self.art.current == id2
                and self.daemon.wait_for_idle(timeout=0.05)
            ):
                break
            time.sleep(0.01)

        resumed = self.daemon.status()
        self.assertEqual(self.art.current, id2)
        self.assertFalse(resumed["cycling_paused"])
        self.assertTrue(resumed["tv_ready"])
        self.assertIsNone(resumed["last_error"])
        self.assertIsNotNone(resumed["next_rotation_at"])

    # ------------------------------------------------------------------
    # IPC Validation, Concurrency, Signals & CLI Edge Cases
    # ------------------------------------------------------------------

    def test_ipc_whitespace_null_partial_settings_and_manifest_tamper_409(self):
        server = build_tv_ipc_server(self.daemon)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.daemon.start()

        def _cleanup() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

        self.addCleanup(_cleanup)

        sock = self.config.tv_sock
        self.assertEqual(stat.S_IMODE(sock.stat().st_mode), 0o600)

        # Whitespace-only IDs rejected with 400
        for path, body in (
            ("/settings", {"set_id": "   ", "interval_minutes": 5}),
            ("/sync", {"set_id": "\t\n"}),
            ("/preview", {"content_id": "   "}),
        ):
            status_code, err = request_json(sock, "POST", path, body=body)
            self.assertEqual(status_code, HTTP_BAD_REQUEST, (path, body, err))
            self.assertEqual(err["code"], "bad_request")

        # Explicit null in POST /settings rejected with 400
        for field in ("random", "mix_art_store_favorites", "interval_minutes"):
            status_code, err = request_json(
                sock,
                "POST",
                "/settings",
                body={"set_id": "root", field: None},
            )
            self.assertEqual(status_code, HTTP_BAD_REQUEST, (field, err))

        # Unexpected body keys in /check and /abort rejected with 400
        for path in ("/check", "/abort"):
            status_code, err = request_json(sock, "POST", path, body={"extra": 1})
            self.assertEqual(status_code, HTTP_BAD_REQUEST, (path, err))

        # Invalid settings payloads in POST /sync rejected with 400
        for bad_settings in (
            "not-a-dict",
            {"unknown": True},
            {"random": None},
            {"interval_minutes": 0},
        ):
            status_code, err = request_json(
                sock,
                "POST",
                "/sync",
                body={"set_id": "root", "settings": bad_settings},
            )
            self.assertEqual(status_code, HTTP_BAD_REQUEST, (bad_settings, err))

        # Partial settings in POST /sync preserve prior per-set settings
        call_json(
            sock,
            "POST",
            "/settings",
            body={
                "set_id": "root",
                "random": False,
                "mix_art_store_favorites": True,
                "interval_minutes": 25,
            },
        )
        s_code, _ = request_json(
            sock,
            "POST",
            "/sync",
            body={"set_id": "root", "settings": {"interval_minutes": 12}},
        )
        self.assertEqual(s_code, HTTP_ACCEPTED)
        self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))
        saved_root = call_json(sock, "GET", "/status")["settings"]["root"]
        self.assertEqual(
            saved_root,
            {"random": False, "mix_art_store_favorites": True, "interval_minutes": 12},
        )

        # Live manifest tamper returns 409 with code="manifest_error" on POST /settings
        tampered = json.loads(self.daemon.manifest_path.read_text(encoding="utf-8"))
        tampered["cleanup_pending"] = ["TAMPERED"]
        self.daemon.manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
        c_status, c_err = request_json(
            sock,
            "POST",
            "/settings",
            body={"set_id": "root", "interval_minutes": 8},
        )
        self.assertEqual(c_status, HTTP_CONFLICT)
        self.assertEqual(c_err["code"], "manifest_error")

    def test_delete_aborted_clears_stale_last_error(self):
        self.library.put_image("root", "photo2.jpg", b"\xff\xd8\xff\xe0green\xff\xd9")
        self._sync_and_wait(self.daemon, "root")

        # Introduce a stale last_error from a failed check_tv()
        self.art.fail_artmode = True
        self.daemon.check_tv()
        self.assertTrue(self.daemon.status()["last_error"])
        self.art.fail_artmode = False

        # Start sync to occasion:Party and abort during delete loop
        self.library.put_image("occasion:Party", "Party/p1.jpg", b"\xff\xd8\xff\xe0blue\xff\xd9")
        entered = threading.Event()
        release = threading.Event()
        self.art.delete_gate = (entered, release)
        self.daemon.start_sync("occasion:Party")
        self.assertTrue(entered.wait(timeout=2.0))
        self.daemon.request_abort()
        release.set()
        self.assertTrue(self.daemon.wait_for_idle(timeout=5.0))

        snap = self.daemon.status()
        self.assertEqual(snap["last_result"]["state"], "aborted")
        self.assertIsNone(snap["last_error"])

    def test_check_tv_and_concurrent_stop_do_not_deadlock(self):
        # Hold actor busy on a blocking task, queue check_tv(), then call stop()
        blocker_entered = threading.Event()
        blocker_release = threading.Event()

        def blocking_task() -> None:
            blocker_entered.set()
            blocker_release.wait(timeout=5.0)

        self.daemon._submit_actor_task("sync", blocking_task, wait=False)
        self.assertTrue(blocker_entered.wait(timeout=2.0))

        check_error: list[BaseException] = []
        check_done = threading.Event()

        def run_check() -> None:
            try:
                self.daemon.check_tv()
            except BaseException as exc:  # noqa: BLE001
                check_error.append(exc)
            finally:
                check_done.set()

        t = threading.Thread(target=run_check, daemon=True)
        t.start()
        # Wait until check_tv has enqueued its reachability task
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            op = self.daemon.status()["operation"]
            if op and op["kind"] == "reachability" and not self.daemon._task_queue.empty():
                break
            time.sleep(0.01)

        # Call stop() before releasing blocker so _actor_loop exits without running check_tv
        stop_done = threading.Event()

        def run_stop() -> None:
            self.daemon.stop()
            stop_done.set()

        stop_thread = threading.Thread(target=run_stop, daemon=True)
        stop_thread.start()
        time.sleep(0.02)
        blocker_release.set()

        self.assertTrue(stop_done.wait(timeout=3.0), "stop() deadlocked")
        self.assertTrue(check_done.wait(timeout=3.0), "check_tv() deadlocked")
        self.assertEqual(len(check_error), 1)
        self.assertIsInstance(check_error[0], TVServiceError)
        self.assertIsNone(self.daemon.status()["operation"])

    def test_token_file_permissions_init_failure_unlock_and_cli_offline_exit_1(self):
        # Pre-existing tv-token.txt with loose permissions is tightened to 0o600
        self.daemon.stop()
        token_path = self.config.tv_dir / "tv-token.txt"
        token_path.write_text("secret-token", encoding="utf-8")
        os.chmod(token_path, 0o644)
        self.daemon = self._make_daemon()
        self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)

        # Corrupt operation.json -> TVDaemon.__init__ fails and releases tv.lock (no FD leak)
        self.daemon.stop()
        self.daemon.operation_path.write_text("{corrupt", encoding="utf-8")
        with self.assertRaisesRegex(TVServiceError, "operation status is unreadable"):
            self._make_daemon()
        self.daemon.operation_path.unlink()
        # Lock must have been released so a fresh daemon succeeds
        self.daemon = self._make_daemon()

        # CLI status/check/sync/abort/preview return exit code 1 when daemon is offline
        cfg_file = self.base / "offline_config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "tv_ip": self.config.tv_ip,
                    "bind_ip": self.config.bind_ip,
                    "state_dir": str(self.state),
                    "image_root": str(self.images),
                }
            ),
            encoding="utf-8",
        )
        for argv in (
            ["--config", str(cfg_file), "status"],
            ["--config", str(cfg_file), "check"],
            ["--config", str(cfg_file), "sync", "root"],
            ["--config", str(cfg_file), "abort"],
            ["--config", str(cfg_file), "preview", "APP-1"],
        ):
            with mock.patch("sys.stderr", new=io.StringIO()):
                code = main(argv)
            self.assertEqual(code, 1, argv)

    def test_serve_daemon_sigterm_clean_shutdown_and_socket_bind_error(self):
        self.daemon.stop()

        # 1. Socket bind OSError returns exit code 2 and releases tv.lock
        with mock.patch("frame_art_mode.tv_daemon.build_tv_ipc_server", side_effect=OSError("bind failed")):
            code = serve_daemon(self.config, daemon_factory=lambda _cfg: self._make_daemon())
        self.assertEqual(code, 2)

        # 2. SIGTERM causes serve_daemon to shut down cleanly with exit code 0
        def send_sigterm_when_ready() -> None:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if self.config.tv_sock.exists():
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                time.sleep(0.01)

        sig_thread = threading.Thread(target=send_sigterm_when_ready, daemon=True)
        sig_thread.start()
        exit_code = serve_daemon(self.config, daemon_factory=lambda _cfg: self._make_daemon())
        sig_thread.join(timeout=2.0)
        self.assertEqual(exit_code, 0)
        self.assertFalse(self.config.tv_sock.exists())
        # Re-create self.daemon so tearDown cleanly succeeds
        self.daemon = self._make_daemon()

    def _seed_active_one_image_manifest(self) -> None:
        self.daemon.start_sync("root")
        deadline = time.monotonic() + 5.0
        while self.daemon.status()["operation"] is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.daemon.status()["last_result"]["state"], "completed")

    def test_wait_for_response_enforces_deadline_on_unsolicited_events(self):
        self._seed_active_one_image_manifest()
        self.daemon.stop()

        class _StubConn:
            def __init__(self) -> None:
                self.connected = True
                self.timeout = 30.0
                self.timeouts_set: list[float | None] = []

            def gettimeout(self) -> float | None:
                return self.timeout

            def settimeout(self, value: float | None) -> None:
                self.timeout = value
                self.timeouts_set.append(value)

        class _NoisyArtClient(FakeArtClient):
            def __init__(self, clock: FakeClock) -> None:
                super().__init__()
                self._clock = clock
                self.connection = _StubConn()
                self.msg_calls = 0

            def get_websocket_message(self) -> dict:
                self.msg_calls += 1
                self._clock.advance(2.5)
                return {"event": "ms.channel.ping", "request_id": "unsolicited"}

            def wait_for_response(self, wait_for_event, request_uuid=None, or_no_id=False):
                raise AssertionError("unwrapped wait_for_response must not be called")

            def select_image(self, content_id: str, category: str | None = None, show: bool = True):
                self.calls.append(("select", content_id, category))
                return self.wait_for_response(None, "req-select")

        noisy_client = _NoisyArtClient(self.clock)
        daemon = TVDaemon(
            self.config,
            tv_factory=lambda _c, _t: noisy_client,
            library_caller=self.library,
            sleeper=self.sleeps.append,
            clock=self.clock.monotonic,
            wall_clock=self.clock.utcnow,
        )
        try:
            ok = daemon.run_slideshow_tick()
            self.assertFalse(ok)
            status = daemon.status()
            self.assertIsNone(status["operation"])
            self.assertEqual(status["last_result"]["state"], "failed")
            self.assertIn("timed out", status["last_error"])
            self.assertTrue(status["cycling_paused"])
            self.assertTrue(noisy_client.closed)
            # Timeout failure must fail fast without probing get_artmode() a second time
            artmode_calls = [c for c in noisy_client.calls if c[0] == "get_artmode"]
            self.assertEqual(len(artmode_calls), 1)
            self.assertEqual(noisy_client.connection.timeout, 30.0)
        finally:
            daemon.stop()
            self.daemon = self._make_daemon()

    def test_wait_for_response_detects_empty_eof_spin_and_closed_socket(self):
        self._seed_active_one_image_manifest()
        self.daemon.stop()

        class _StubConn:
            def __init__(self, connected: bool) -> None:
                self.connected = connected
                self.timeout = 30.0

            def gettimeout(self) -> float | None:
                return self.timeout

            def settimeout(self, value: float | None) -> None:
                self.timeout = value

        class _EofArtClient(FakeArtClient):
            def __init__(self, connected: bool, *, close_on_read: bool = False) -> None:
                super().__init__()
                self.connection = _StubConn(connected=connected)
                self.close_on_read = close_on_read
                self.msg_calls = 0

            def get_websocket_message(self) -> dict:
                self.msg_calls += 1
                if self.close_on_read:
                    self.connection.connected = False
                return {}

            def wait_for_response(self, wait_for_event, request_uuid=None, or_no_id=False):
                raise AssertionError("unwrapped wait_for_response must not be called")

            def select_image(self, content_id: str, category: str | None = None, show: bool = True):
                self.calls.append(("select", content_id, category))
                return self.wait_for_response(None, "req-eof")

        cases = (
            ("already_closed", False, False, 0),
            ("closed_mid_read", True, True, 1),
            ("empty_spin", True, False, 50),
        )
        for label, connected, close_on_read, expected_calls in cases:
            with self.subTest(case=label):
                eof_client = _EofArtClient(connected=connected, close_on_read=close_on_read)
                daemon = TVDaemon(
                    self.config,
                    tv_factory=lambda _c, _t: eof_client,
                    library_caller=self.library,
                    sleeper=self.sleeps.append,
                    clock=self.clock.monotonic,
                    wall_clock=self.clock.utcnow,
                )
                try:
                    self.assertFalse(daemon.run_slideshow_tick())
                    self.assertEqual(eof_client.msg_calls, expected_calls)
                    self.assertTrue(eof_client.closed)
                    status = daemon.status()
                    self.assertIsNone(status["operation"])
                    self.assertEqual(status["last_result"]["state"], "failed")
                    self.assertIn("closed", status["last_error"])
                finally:
                    daemon.stop()
        self.daemon = self._make_daemon()

    def test_abort_wakes_blocked_select_image_and_preview_restore_still_completes(self):
        self._seed_active_one_image_manifest()
        self.daemon.stop()

        class WebSocketTimeoutException(Exception):
            pass

        class _StubConn:
            def __init__(self) -> None:
                self.connected = True
                self.timeout = 30.0
                self.timeouts_set: list[float | None] = []

            def gettimeout(self) -> float | None:
                return self.timeout

            def settimeout(self, value: float | None) -> None:
                self.timeout = value
                self.timeouts_set.append(value)

        class _BlockingSelectClient(FakeArtClient):
            def __init__(self, daemon_ref_holder: list[TVDaemon]) -> None:
                super().__init__()
                self.connection = _StubConn()
                self._daemon_ref_holder = daemon_ref_holder
                self.select_entered = threading.Event()
                self.timeouts_seen = 0

            def get_websocket_message(self) -> dict:
                self.timeouts_seen += 1
                if self.timeouts_seen == 1:
                    self.select_entered.set()
                    # Wait briefly until abort is triggered, then raise socket slice timeout
                    time.sleep(0.05)
                raise WebSocketTimeoutException("timed out slice")

            def wait_for_response(self, wait_for_event, request_uuid=None, or_no_id=False):
                raise AssertionError("unwrapped wait_for_response must not be called")

            def select_image(self, content_id: str, category: str | None = None, show: bool = True):
                self.calls.append(("select", content_id, category))
                return self.wait_for_response(None, "req-block")

        holder: list[TVDaemon] = []
        blocking_client = _BlockingSelectClient(holder)
        daemon = TVDaemon(
            self.config,
            tv_factory=lambda _c, _t: blocking_client,
            library_caller=self.library,
            clock=self.clock.monotonic,
            wall_clock=self.clock.utcnow,
        )
        holder.append(daemon)
        try:
            tick_result: list[bool] = []
            tick_done = threading.Event()

            def _run_tick() -> None:
                tick_result.append(daemon.run_slideshow_tick())
                tick_done.set()

            thread = threading.Thread(target=_run_tick, daemon=True)
            thread.start()
            self.assertTrue(blocking_client.select_entered.wait(timeout=2.0))
            abort_resp = daemon.request_abort()
            self.assertTrue(abort_resp["aborted"])
            self.assertTrue(tick_done.wait(timeout=2.0), "slideshow tick deadlocked on abort")
            self.assertEqual(tick_result, [False])
            self.assertTrue(blocking_client.closed)
            status = daemon.status()
            self.assertIsNone(status["operation"])
            self.assertEqual(status["last_result"]["state"], "aborted")
            self.assertTrue(
                any(t is not None and t <= 1.0 for t in blocking_client.connection.timeouts_set)
            )
            self.assertEqual(blocking_client.connection.timeout, 30.0)
        finally:
            daemon.stop()

        # Verify aborting a preview wait still runs restore_art.wait_for_response() to completion
        class _PreviewRestoreClient(FakeArtClient):
            def __init__(self) -> None:
                super().__init__()
                self.current = "APP-1"
                self.connection = _StubConn()
                self.msg_calls = 0

            def get_websocket_message(self) -> dict:
                self.msg_calls += 1
                return {"request_id": f"req-{self.msg_calls}", "event": "ok"}

            def wait_for_response(self, wait_for_event, request_uuid=None, or_no_id=False):
                raise AssertionError("unwrapped wait_for_response must not be called")

            def select_image(self, content_id: str, category: str | None = None, show: bool = True):
                req_id = f"req-{self.msg_calls + 1}"
                self.calls.append(("select", content_id, category))
                self.wait_for_response(None, req_id)
                self.current = content_id
                self.current_category = category
                return True

        preview_clients: list[_PreviewRestoreClient] = []

        def _preview_factory(_c: Config, _t: Path) -> _PreviewRestoreClient:
            c = _PreviewRestoreClient()
            preview_clients.append(c)
            return c

        preview_daemon = TVDaemon(self.config, tv_factory=_preview_factory)
        preview_daemon.start()
        try:
            preview_daemon.start_preview("APP-1")
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                op = preview_daemon.status()["operation"]
                if op and op["phase"] == "previewing":
                    break
                time.sleep(0.01)
            self.assertTrue(preview_daemon.request_abort()["aborted"])
            deadline = time.monotonic() + 3.0
            while preview_daemon.status()["operation"] is not None and time.monotonic() < deadline:
                time.sleep(0.01)
            st = preview_daemon.status()
            self.assertIsNone(st["operation"])
            self.assertEqual(st["last_result"]["state"], "aborted")
            self.assertIn("restored", st["last_result"]["message"])
            self.assertEqual(len(preview_clients), 2)
            self.assertTrue(all(c.closed for c in preview_clients))
        finally:
            preview_daemon.stop()
            self.daemon = self._make_daemon()


if __name__ == "__main__":
    unittest.main()
