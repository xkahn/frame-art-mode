import ast
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from PIL import Image

from frame_art_mode import library_daemon
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
from frame_art_mode.library_daemon import (
    LibraryService,
    create_library_ipc_server,
    main as library_main,
    serve_daemon,
)
from frame_art_mode.models import IPCError, LibraryError, NORMALIZATION_VERSION, PreparedImage


def make_jpeg(size=(800, 640), color=(180, 50, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def make_png(size=(800, 640), color=(30, 150, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("timed out waiting for condition")


class LibraryDaemonTests(unittest.TestCase):
    def _make_config(self, root: Path, **overrides) -> Config:
        image_root = root / "images"
        state_dir = root / "state"
        image_root.mkdir(exist_ok=True)
        return Config(
            script_dir=root,
            image_root=image_root,
            state_dir=state_dir,
            tv_ip="192.168.1.50",
            bind_ip="192.168.1.20",
            **overrides,
        )

    def _start_service(self, config: Config) -> LibraryService:
        service = LibraryService(config)
        self.addCleanup(service.close)
        return service

    def _start_ipc_server(self, service: LibraryService, sock_path: Path) -> None:
        server = create_library_ipc_server(service, sock_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _cleanup() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

        self.addCleanup(_cleanup)

    def test_permissions_lock_exclusivity_and_health(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._make_config(root)
            service = self._start_service(config)
            self._start_ipc_server(service, config.library_sock)

            for private_dir in (
                config.state_dir,
                config.cache_dir,
                config.cache_dir / "tv",
                config.cache_dir / "thumbs",
            ):
                self.assertEqual(stat.S_IMODE(private_dir.stat().st_mode), 0o700)

            self.assertEqual(stat.S_IMODE(config.library_lock.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config.library_sock.stat().st_mode), 0o600)

            with self.assertRaisesRegex(LibraryError, "already running"):
                LibraryService(config)

            health = call_json(config.library_sock, "GET", "/health")
            self.assertTrue(health["ok"])
            self.assertEqual(health["pid"], os.getpid())
            self.assertTrue(health["started_at"])

    def test_full_refresh_incremental_reuse_skips_and_status_stale_detection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._make_config(root)
            (config.image_root / "sunset.jpg").write_bytes(make_jpeg(color=(200, 40, 40)))
            (config.image_root / "tiny.jpg").write_bytes(make_jpeg(size=(200, 200)))
            party = config.image_root / "Party"
            party.mkdir()
            (party / "01.png").write_bytes(make_png(color=(20, 90, 220)))
            (party / "bad.jpg").write_bytes(b"corrupt data")

            service = self._start_service(config)
            self._start_ipc_server(service, config.library_sock)

            initial_status = call_json(config.library_sock, "GET", "/status")
            self.assertIsNone(initial_status["last_refreshed_at"])
            self.assertEqual(initial_status["counts"]["sets"], 0)
            self.assertEqual(initial_status["active_leases"], [])

            status_code, accepted = request_json(config.library_sock, "POST", "/refresh", body={})
            self.assertEqual(status_code, HTTP_ACCEPTED)
            self.assertTrue(accepted["ok"])
            self.assertEqual(accepted["operation"]["kind"], "refresh")

            wait_until(lambda: call_json(config.library_sock, "GET", "/status")["operation"] is None)
            status = call_json(config.library_sock, "GET", "/status")
            self.assertEqual(
                set(status.keys()),
                {
                    "last_refreshed_at",
                    "sets",
                    "counts",
                    "warnings",
                    "scan_warnings",
                    "active_leases",
                    "operation",
                    "last_result",
                },
            )
            self.assertIsNotNone(status["last_refreshed_at"])
            self.assertEqual(status["last_result"]["kind"], "refresh")
            self.assertEqual(status["last_result"]["state"], "completed")
            self.assertEqual(status["counts"], {
                "sets": 2,
                "valid": 2,
                "skipped": 2,
                "cached_tv_files": 2,
                "cached_thumb_files": 2,
            })

            root_set = status["sets"]["root"]
            self.assertEqual(
                set(root_set.keys()),
                {"label", "valid_count", "skipped_count", "images"},
            )
            self.assertEqual(root_set["label"], "Default")
            self.assertEqual(root_set["valid_count"], 1)
            self.assertEqual(root_set["skipped_count"], 1)
            by_rel = {img["source_rel"]: img for img in root_set["images"]}
            self.assertEqual(
                set(by_rel["sunset.jpg"].keys()),
                {
                    "source_rel",
                    "name_rel",
                    "filename",
                    "sha256",
                    "thumb_path",
                    "skip_reason",
                    "byte_size",
                    "stale",
                },
            )
            self.assertEqual(by_rel["sunset.jpg"]["filename"], "sunset.jpg")
            self.assertTrue(by_rel["sunset.jpg"]["sha256"])
            self.assertEqual(
                by_rel["sunset.jpg"]["thumb_path"],
                f"thumbs/{by_rel['sunset.jpg']['sha256']}.jpg",
            )
            self.assertIsInstance(by_rel["sunset.jpg"]["byte_size"], int)
            self.assertGreater(by_rel["sunset.jpg"]["byte_size"], 0)
            self.assertIsNone(by_rel["sunset.jpg"]["skip_reason"])
            self.assertFalse(by_rel["sunset.jpg"]["stale"])
            self.assertIsNone(by_rel["tiny.jpg"]["filename"])
            self.assertIsNone(by_rel["tiny.jpg"]["sha256"])
            self.assertIsNone(by_rel["tiny.jpg"]["thumb_path"])
            self.assertIsNone(by_rel["tiny.jpg"]["byte_size"])
            self.assertIn("640px", by_rel["tiny.jpg"]["skip_reason"])
            self.assertFalse(by_rel["tiny.jpg"]["stale"])
            self.assertIn("scan_warnings", status)

            party_set = status["sets"]["occasion:Party"]
            self.assertEqual(party_set["label"], "Party")
            self.assertEqual(party_set["valid_count"], 1)
            self.assertEqual(party_set["skipped_count"], 1)
            self.assertEqual(len(status["warnings"]), 2)

            index_file = config.cache_dir / "library.json"
            self.assertEqual(stat.S_IMODE(index_file.stat().st_mode), 0o600)

            # Second refresh reuses both valid and skipped entries without re-decoding
            with mock.patch(
                "frame_art_mode.library_daemon.normalize_image_assets",
                side_effect=AssertionError("should not re-normalize unchanged images"),
            ):
                service.refresh_sync()

            # Mutate sunset.jpg on disk and delete Party/01.png's cached thumb to verify GET /status stale check
            (config.image_root / "sunset.jpg").write_bytes(make_jpeg(color=(10, 200, 10)) + b"\x00")
            party_hash = next(
                img["sha256"] for img in party_set["images"] if img["source_rel"] == "Party/01.png"
            )
            thumb_file = config.cache_dir / "thumbs" / f"{party_hash}.jpg"
            thumb_file.unlink()

            # GET /status must detect staleness without modifying cache or normalizing
            with mock.patch(
                "frame_art_mode.library_daemon.normalize_image_assets",
                side_effect=AssertionError("GET /status must never normalize"),
            ):
                stale_status = call_json(config.library_sock, "GET", "/status")

            stale_root = {
                img["source_rel"]: img for img in stale_status["sets"]["root"]["images"]
            }
            stale_party = {
                img["source_rel"]: img for img in stale_status["sets"]["occasion:Party"]["images"]
            }
            self.assertTrue(stale_root["sunset.jpg"]["stale"])
            self.assertFalse(stale_root["tiny.jpg"]["stale"])
            self.assertTrue(stale_party["Party/01.png"]["stale"])
            self.assertFalse(thumb_file.exists())

            # Re-refresh rebuilds missing thumb and re-encodes modified sunset.jpg
            rebuilt = service.refresh_sync()
            self.assertTrue(thumb_file.is_file())
            self.assertEqual(rebuilt["counts"]["cached_tv_files"], 2)
            self.assertEqual(rebuilt["counts"]["cached_thumb_files"], 2)

            # Changing max_image_bytes or normalization_version marks cached entries stale and triggers re-normalization
            service.close()
            tighter_cfg = self._make_config(root, max_image_bytes=262144)
            reopened_tighter = self._start_service(tighter_cfg)
            tighter_status = reopened_tighter.status()
            self.assertTrue(
                all(
                    img["stale"]
                    for s in tighter_status["sets"].values()
                    for img in s["images"]
                )
            )
            reopened_tighter.refresh_sync()
            post_tighter = reopened_tighter.status()
            self.assertFalse(
                any(
                    img["stale"]
                    for s in post_tighter["sets"].values()
                    for img in s["images"]
                )
            )
            reopened_tighter.close()

            # Simulate normalization_version mismatch in library.json
            raw_index = json.loads(index_file.read_text(encoding="utf-8"))
            raw_index["sets"]["root"][0]["normalization_version"] = NORMALIZATION_VERSION + 1
            index_file.write_text(json.dumps(raw_index), encoding="utf-8")
            reopened_ver = self._start_service(tighter_cfg)
            ver_imgs = {img["source_rel"]: img for img in reopened_ver.status()["sets"]["root"]["images"]}
            self.assertTrue(any(img["stale"] for img in ver_imgs.values()))

    def test_oversized_image_at_quality_64_is_skipped_without_aborting_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._make_config(root, max_image_bytes=262144)
            (config.image_root / "good.jpg").write_bytes(make_jpeg(size=(800, 640)))
            raw_noise = os.urandom(2400 * 1800 * 3)
            noisy = Image.frombytes("RGB", (2400, 1800), raw_noise)
            noisy.save(config.image_root / "huge_noise.png", format="PNG")

            service = self._start_service(config)
            status = service.refresh_sync()
            self.assertEqual(status["last_result"]["state"], "completed")
            self.assertEqual(status["counts"]["valid"], 1)
            self.assertEqual(status["counts"]["skipped"], 1)
            imgs = {img["source_rel"]: img for img in status["sets"]["root"]["images"]}
            self.assertIsNone(imgs["good.jpg"]["skip_reason"])
            self.assertIn("max_image_bytes", imgs["huge_noise.png"]["skip_reason"])
            self.assertIsNone(imgs["huge_noise.png"]["filename"])
            self.assertTrue(any("huge_noise.png" in w for w in status["warnings"]))

    def test_refresh_validation_single_set_and_deleted_set_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._make_config(root)
            (config.image_root / "a.jpg").write_bytes(make_jpeg())
            party = config.image_root / "Party"
            party.mkdir()
            (party / "b.jpg").write_bytes(make_jpeg(color=(10, 20, 30)))

            service = self._start_service(config)
            self._start_ipc_server(service, config.library_sock)

            for bad_body in ({"set_id": ""}, {"set_id": "   "}, {"set_id": 123}):
                code, err = request_json(config.library_sock, "POST", "/refresh", body=bad_body)
                self.assertEqual(code, HTTP_BAD_REQUEST)
                self.assertEqual(err["code"], "bad_request")

            code, not_found = request_json(
                config.library_sock, "POST", "/refresh", body={"set_id": "occasion:Missing"}
            )
            self.assertEqual(code, HTTP_NOT_FOUND)
            self.assertEqual(not_found["code"], "not_found")

            # Single-set refresh on occasion:Party leaves root out of index initially
            code, _ = request_json(
                config.library_sock, "POST", "/refresh", body={"set_id": "occasion:Party"}
            )
            self.assertEqual(code, HTTP_ACCEPTED)
            wait_until(lambda: call_json(config.library_sock, "GET", "/status")["operation"] is None)
            status = call_json(config.library_sock, "GET", "/status")
            self.assertEqual(list(status["sets"].keys()), ["occasion:Party"])

            # Delete Party from disk: refreshing occasion:Party is still allowed (present in index) and clears it
            (party / "b.jpg").unlink()
            party.rmdir()
            code, _ = request_json(
                config.library_sock, "POST", "/refresh", body={"set_id": "occasion:Party"}
            )
            self.assertEqual(code, HTTP_ACCEPTED)
            wait_until(lambda: call_json(config.library_sock, "GET", "/status")["operation"] is None)
            status_after = call_json(config.library_sock, "GET", "/status")
            self.assertEqual(status_after["sets"], {})

    def test_prepare_set_lease_pinning_deferred_pruning_and_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._make_config(root)
            (config.image_root / "01.jpg").write_bytes(make_jpeg(color=(210, 10, 10)))
            (config.image_root / "02.jpg").write_bytes(make_jpeg(color=(10, 210, 10)))
            (config.image_root / "bad.jpg").write_bytes(b"broken")
            party = config.image_root / "Party"
            party.mkdir()
            (party / "p1.jpg").write_bytes(make_jpeg(color=(10, 10, 210)))

            service = self._start_service(config)
            self._start_ipc_server(service, config.library_sock)

            # Validation errors and 404 without creating a lease
            for bad_body in ({}, {"set_id": ""}, {"set_id": 5}, {"set_id": "root", "lease_id": ""}):
                code, _ = request_json(config.library_sock, "POST", "/prepare-set", body=bad_body)
                self.assertEqual(code, HTTP_BAD_REQUEST)

            code, _ = request_json(
                config.library_sock,
                "POST",
                "/prepare-set",
                body={"set_id": "occasion:NoSuch", "lease_id": "bad-lease"},
            )
            self.assertEqual(code, HTTP_NOT_FOUND)
            self.assertEqual(call_json(config.library_sock, "GET", "/status")["active_leases"], [])

            # Prepare root without explicit lease_id -> auto-generated lease_id
            prepared = call_json(config.library_sock, "POST", "/prepare-set", body={"set_id": "root"})
            lease_id = prepared["lease_id"]
            self.assertTrue(lease_id)
            self.assertEqual(prepared["set_id"], "root")
            self.assertEqual(len(prepared["images"]), 2)
            self.assertEqual(len(prepared["skipped"]), 1)
            self.assertEqual(
                set(prepared["skipped"][0].keys()),
                {"source_rel", "name_rel", "reason"},
            )
            self.assertEqual(prepared["skipped"][0]["source_rel"], "bad.jpg")
            self.assertTrue(any("bad.jpg" in w for w in prepared["warnings"]))

            pinned_hashes = set()
            for item in prepared["images"]:
                self.assertEqual(
                    set(item.keys()),
                    {
                        "source_rel",
                        "name_rel",
                        "filename",
                        "fingerprint",
                        "cache_path",
                        "thumb_path",
                        "byte_size",
                    },
                )
                parsed_item = PreparedImage.from_dict(item)
                self.assertEqual(parsed_item.cache_path, Path(item["cache_path"]))
                self.assertTrue(Path(item["cache_path"]).is_absolute())
                self.assertTrue(Path(item["thumb_path"]).is_absolute())
                self.assertTrue(Path(item["cache_path"]).is_file())
                self.assertTrue(Path(item["thumb_path"]).is_file())
                self.assertIsInstance(item["byte_size"], int)
                self.assertGreater(item["byte_size"], 0)
                pinned_hashes.add(item["fingerprint"])

            # Delete 01.jpg from disk while lease is active, then run full refresh
            (config.image_root / "01.jpg").unlink()
            service.refresh_sync()

            # Pinned orphan hash for 01.jpg must STILL exist on disk because lease is held!
            for fp in pinned_hashes:
                self.assertTrue((config.cache_dir / "tv" / f"{fp}.jpg").is_file())
                self.assertTrue((config.cache_dir / "thumbs" / f"{fp}.jpg").is_file())

            # Release lease -> triggers deferred orphan pruning; 01.jpg cache files are removed, 02.jpg remains
            code, _ = request_json(config.library_sock, "POST", "/release-lease", body={"lease_id": ""})
            self.assertEqual(code, HTTP_BAD_REQUEST)

            rel1 = call_json(
                config.library_sock, "POST", "/release-lease", body={"lease_id": lease_id}
            )
            self.assertEqual(rel1, {"ok": True, "released": True})
            # Idempotent second release
            rel2 = call_json(
                config.library_sock, "POST", "/release-lease", body={"lease_id": lease_id}
            )
            self.assertEqual(rel2, {"ok": True, "released": False})

            removed_fp = prepared["images"][0]["fingerprint"]
            kept_fp = prepared["images"][1]["fingerprint"]
            self.assertFalse((config.cache_dir / "tv" / f"{removed_fp}.jpg").exists())
            self.assertFalse((config.cache_dir / "thumbs" / f"{removed_fp}.jpg").exists())
            self.assertTrue((config.cache_dir / "tv" / f"{kept_fp}.jpg").is_file())
            self.assertTrue((config.cache_dir / "thumbs" / f"{kept_fp}.jpg").is_file())

            # Deleting 02.jpg and running full refresh with no leases prunes immediately
            (config.image_root / "02.jpg").unlink()
            service.refresh_sync()
            self.assertFalse((config.cache_dir / "tv" / f"{kept_fp}.jpg").exists())
            self.assertFalse((config.cache_dir / "thumbs" / f"{kept_fp}.jpg").exists())

    def test_multi_lease_overlap_defers_pruning_until_final_lease_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._make_config(root)
            (config.image_root / "a.jpg").write_bytes(make_jpeg(color=(210, 10, 10)))
            party = config.image_root / "Party"
            party.mkdir()
            (party / "b.jpg").write_bytes(make_jpeg(color=(10, 210, 10)))

            service = self._start_service(config)
            self._start_ipc_server(service, config.library_sock)

            prep1 = call_json(
                config.library_sock,
                "POST",
                "/prepare-set",
                body={"set_id": "root", "lease_id": "L1"},
            )
            prep2 = call_json(
                config.library_sock,
                "POST",
                "/prepare-set",
                body={"set_id": "occasion:Party", "lease_id": "L2"},
            )
            fp_a = prep1["images"][0]["fingerprint"]
            fp_b = prep2["images"][0]["fingerprint"]
            self.assertEqual(
                call_json(config.library_sock, "GET", "/status")["active_leases"],
                ["L1", "L2"],
            )

            # Remove a.jpg and run full refresh while L1 and L2 are both held
            (config.image_root / "a.jpg").unlink()
            service.refresh_sync()
            self.assertTrue((config.cache_dir / "tv" / f"{fp_a}.jpg").is_file())
            self.assertTrue((config.cache_dir / "thumbs" / f"{fp_a}.jpg").is_file())

            # Releasing L1 leaves L2 active -> orphan pruning MUST still be deferred
            self.assertEqual(
                call_json(config.library_sock, "POST", "/release-lease", body={"lease_id": "L1"}),
                {"ok": True, "released": True},
            )
            self.assertEqual(
                call_json(config.library_sock, "GET", "/status")["active_leases"],
                ["L2"],
            )
            self.assertTrue((config.cache_dir / "tv" / f"{fp_a}.jpg").is_file())
            self.assertTrue((config.cache_dir / "thumbs" / f"{fp_a}.jpg").is_file())

            # Releasing final lease L2 triggers deferred pruning of fp_a while keeping fp_b
            self.assertEqual(
                call_json(config.library_sock, "POST", "/release-lease", body={"lease_id": "L2"}),
                {"ok": True, "released": True},
            )
            self.assertEqual(call_json(config.library_sock, "GET", "/status")["active_leases"], [])
            self.assertFalse((config.cache_dir / "tv" / f"{fp_a}.jpg").exists())
            self.assertFalse((config.cache_dir / "thumbs" / f"{fp_a}.jpg").exists())
            self.assertTrue((config.cache_dir / "tv" / f"{fp_b}.jpg").is_file())
            self.assertTrue((config.cache_dir / "thumbs" / f"{fp_b}.jpg").is_file())

    def test_cooperative_abort_preserves_entries_skips_pruning_and_conflict_409(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._make_config(root)
            (config.image_root / "1.jpg").write_bytes(make_jpeg(color=(100, 10, 10)))
            (config.image_root / "2.jpg").write_bytes(make_jpeg(color=(10, 100, 10)))
            party = config.image_root / "Party"
            party.mkdir()
            (party / "3.jpg").write_bytes(make_jpeg(color=(10, 10, 100)))

            service = self._start_service(config)
            self._start_ipc_server(service, config.library_sock)

            # Idle abort returns aborted=False
            self.assertEqual(
                call_json(config.library_sock, "POST", "/abort", body={}),
                {"ok": True, "aborted": False},
            )

            # Initial full refresh populates all 3 images
            service.refresh_sync()
            # Create an orphan file in tv/ to prove abort skips pruning
            orphan = config.cache_dir / "tv" / ("f" * 64 + ".jpg")
            orphan.write_bytes(b"orphan")

            # Modify 1.jpg and 2.jpg so second refresh processes them
            (config.image_root / "1.jpg").write_bytes(make_jpeg(color=(200, 20, 20)) + b"\x00")
            (config.image_root / "2.jpg").write_bytes(make_jpeg(color=(20, 200, 20)) + b"\x00")

            entered_first = threading.Event()
            allow_first_finish = threading.Event()
            original_normalize = library_daemon.normalize_image_assets
            call_count = 0

            def gated_normalize(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    entered_first.set()
                    allow_first_finish.wait(timeout=5.0)
                return original_normalize(*args, **kwargs)

            with mock.patch("frame_art_mode.library_daemon.normalize_image_assets", side_effect=gated_normalize):
                code, _ = request_json(config.library_sock, "POST", "/refresh", body={})
                self.assertEqual(code, HTTP_ACCEPTED)
                self.assertTrue(entered_first.wait(timeout=5.0))

                # Concurrent refresh and prepare-set both return 409 Conflict
                c1, _ = request_json(config.library_sock, "POST", "/refresh", body={})
                self.assertEqual(c1, HTTP_CONFLICT)
                c2, _ = request_json(
                    config.library_sock, "POST", "/prepare-set", body={"set_id": "root"}
                )
                self.assertEqual(c2, HTTP_CONFLICT)

                abort_resp = call_json(config.library_sock, "POST", "/abort", body={})
                self.assertEqual(abort_resp, {"ok": True, "aborted": True})

                # abort_requested=True must be visible in GET /status AND persisted in library.json immediately
                live_status = call_json(config.library_sock, "GET", "/status")
                self.assertTrue(live_status["operation"]["abort_requested"])
                persisted = json.loads((config.cache_dir / "library.json").read_text(encoding="utf-8"))
                self.assertTrue(persisted["operation"]["abort_requested"])

                allow_first_finish.set()

                wait_until(lambda: call_json(config.library_sock, "GET", "/status")["operation"] is None)

            status = call_json(config.library_sock, "GET", "/status")
            self.assertEqual(status["last_result"]["kind"], "refresh")
            self.assertEqual(status["last_result"]["state"], "aborted")
            # 1.jpg completed, 2.jpg (prior entry) preserved, Party/3.jpg preserved!
            self.assertEqual(status["counts"]["valid"], 3)
            self.assertEqual(call_count, 1)
            # Orphan pruning skipped on abort
            self.assertTrue(orphan.is_file())

    def test_startup_recovers_interrupted_operation_and_handles_corrupt_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._make_config(root)
            (config.image_root / "photo.jpg").write_bytes(make_jpeg())

            service = LibraryService(config)
            service.refresh_sync()
            service.close()

            index_path = config.cache_dir / "library.json"
            raw = json.loads(index_path.read_text(encoding="utf-8"))
            cached_hash = raw["sets"]["root"][0]["fingerprint"]
            raw["operation"] = {
                "kind": "refresh",
                "phase": "refreshing",
                "message": "Refreshing 1 image(s)",
                "current_item": "photo.jpg",
                "completed_ops": 0,
                "total_ops": 1,
                "abort_requested": False,
                "started_at": "2026-11-24T00:00:00+00:00",
                "known_uploaded": [],
            }
            index_path.write_text(json.dumps(raw), encoding="utf-8")

            reopened = self._start_service(config)
            status = reopened.status()
            self.assertIsNone(status["operation"])
            self.assertEqual(status["last_result"]["kind"], "refresh")
            self.assertEqual(status["last_result"]["state"], "interrupted")
            reopened.close()

            # Corrupt library.json falls back cleanly without deleting tv/ or thumbs/
            index_path.write_text("{corrupt-json", encoding="utf-8")
            with self.assertLogs("frame_art_mode.library_daemon", level="WARNING"):
                recovered = self._start_service(config)
            rec_status = recovered.status()
            self.assertEqual(rec_status["counts"]["sets"], 0)
            self.assertTrue(any("library.json" in w for w in rec_status["warnings"]))
            self.assertTrue((config.cache_dir / "tv" / f"{cached_hash}.jpg").is_file())
            self.assertTrue((config.cache_dir / "thumbs" / f"{cached_hash}.jpg").is_file())

    def test_serve_daemon_clean_shutdown_and_cli_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg_path = root / "config.json"
            image_root = root / "images"
            state_dir = root / "state"
            image_root.mkdir()
            (image_root / "one.jpg").write_bytes(make_jpeg())
            cfg_path.write_text(
                json.dumps(
                    {
                        "tv_ip": "192.168.1.50",
                        "bind_ip": "192.168.1.20",
                        "image_root": str(image_root),
                        "state_dir": str(state_dir),
                    }
                ),
                encoding="utf-8",
            )

            # Invalid config exits with code 2
            bad_cfg = root / "bad_config.json"
            bad_cfg.write_text('{"bind_ip": "0.0.0.0"}', encoding="utf-8")
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                self.assertEqual(library_main(["--config", str(bad_cfg), "serve"]), 2)
            self.assertIn("Configuration error", err_buf.getvalue())

            # Online status fails with non-zero exit code when daemon is offline
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                self.assertEqual(library_main(["--config", str(cfg_path), "status"]), 1)
            self.assertIn("IPC error", err_buf.getvalue())

            # Offline refresh runs with library.lock
            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                self.assertEqual(library_main(["--config", str(cfg_path), "refresh", "--offline"]), 0)
            self.assertEqual(json.loads(out_buf.getvalue())["counts"]["valid"], 1)

            # Start serve_daemon in thread and test online CLI + lock contention + clean shutdown
            from frame_art_mode.config import load_config

            loaded_cfg = load_config(cfg_path)
            svc = LibraryService(loaded_cfg)
            stop_event = threading.Event()
            daemon_thread = threading.Thread(
                target=serve_daemon,
                args=(svc, loaded_cfg.library_sock),
                kwargs={"stop_event": stop_event},
                daemon=True,
            )
            daemon_thread.start()
            self.addCleanup(lambda: (stop_event.set(), daemon_thread.join(timeout=5.0)))

            wait_until(lambda: loaded_cfg.library_sock.exists())

            # Second serve while locked exits with code 2
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                self.assertEqual(library_main(["--config", str(cfg_path), "serve"]), 2)
                self.assertEqual(library_main(["--config", str(cfg_path), "refresh", "--offline"]), 2)
            self.assertIn("already running", err_buf.getvalue())

            # Online CLI commands succeed
            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                self.assertEqual(library_main(["--config", str(cfg_path), "status"]), 0)
            self.assertEqual(json.loads(out_buf.getvalue())["counts"]["valid"], 1)

            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                self.assertEqual(library_main(["--config", str(cfg_path), "refresh"]), 0)
            self.assertTrue(json.loads(out_buf.getvalue())["ok"])
            wait_until(lambda: call_json(loaded_cfg.library_sock, "GET", "/status")["operation"] is None)

            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                self.assertEqual(library_main(["--config", str(cfg_path), "abort"]), 0)
            self.assertEqual(json.loads(out_buf.getvalue()), {"ok": True, "aborted": False})

            stop_event.set()
            daemon_thread.join(timeout=5.0)
            self.assertFalse(daemon_thread.is_alive())
            self.assertFalse(loaded_cfg.library_sock.exists())

            # After shutdown, lock is released so offline refresh succeeds again
            proc = subprocess.run(
                [sys.executable, "-m", "frame_art_mode.library_daemon", "--config", str(cfg_path), "refresh", "--offline"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            parsed = json.loads(proc.stdout)
            self.assertEqual(parsed["counts"]["valid"], 1)

            # Subprocess serve handles SIGTERM cleanly (unlinks socket and releases lock)
            daemon_proc = subprocess.Popen(
                [sys.executable, "-m", "frame_art_mode.library_daemon", "--config", str(cfg_path), "serve"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(lambda: daemon_proc.kill() if daemon_proc.poll() is None else None)
            wait_until(lambda: loaded_cfg.library_sock.exists())

            # Multi-process lock contention: second subprocess serve & offline refresh exit with code 2
            contend_serve = subprocess.run(
                [sys.executable, "-m", "frame_art_mode.library_daemon", "--config", str(cfg_path), "serve"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(contend_serve.returncode, 2)
            self.assertIn("already running", contend_serve.stderr)

            contend_offline = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "frame_art_mode.library_daemon",
                    "--config",
                    str(cfg_path),
                    "refresh",
                    "--offline",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(contend_offline.returncode, 2)
            self.assertIn("already running", contend_offline.stderr)

            daemon_proc.terminate()
            stdout, stderr = daemon_proc.communicate(timeout=10)
            self.assertEqual(daemon_proc.returncode, 0, f"stdout={stdout!r} stderr={stderr!r}")
            self.assertFalse(loaded_cfg.library_sock.exists())

    def test_offline_refresh_set_and_cli_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg_path = root / "config.json"
            base_images = root / "base_images"
            override_images = root / "override_images"
            override_state = root / "override_state"
            override_cache = root / "override_cache"
            base_images.mkdir()
            override_images.mkdir()
            (override_images / "top.jpg").write_bytes(make_jpeg(color=(200, 30, 30)))
            party = override_images / "Party"
            party.mkdir()
            (party / "p1.jpg").write_bytes(make_jpeg(color=(30, 200, 30)))
            cfg_path.write_text(
                json.dumps(
                    {
                        "tv_ip": "192.168.1.50",
                        "bind_ip": "192.168.1.20",
                        "image_root": str(base_images),
                        "state_dir": str(root / "base_state"),
                    }
                ),
                encoding="utf-8",
            )

            # refresh --offline --set <set_id> with CLI overrides succeeds and scopes to Party
            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                rc = library_main(
                    [
                        "refresh",
                        "--config",
                        str(cfg_path),
                        "--image-root",
                        str(override_images),
                        "--state-dir",
                        str(override_state),
                        "--cache-dir",
                        str(override_cache),
                        "--tv-ip",
                        "192.168.1.51",
                        "--bind-ip",
                        "192.168.1.21",
                        "--port",
                        "8081",
                        "--offline",
                        "--set",
                        "occasion:Party",
                    ]
                )
            self.assertEqual(rc, 0)
            parsed = json.loads(out_buf.getvalue())
            self.assertEqual(list(parsed["sets"].keys()), ["occasion:Party"])
            self.assertEqual(parsed["counts"]["valid"], 1)
            self.assertTrue((override_cache / "library.json").is_file())
            self.assertTrue((override_state / "library.lock").is_file())

            # Unknown set returns exit code 1
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                rc_missing = library_main(
                    [
                        "--config",
                        str(cfg_path),
                        "--image-root",
                        str(override_images),
                        "--state-dir",
                        str(override_state),
                        "--cache-dir",
                        str(override_cache),
                        "refresh",
                        "--offline",
                        "--set",
                        "occasion:Missing",
                    ]
                )
            self.assertEqual(rc_missing, 1)
            self.assertIn("unknown image set", err_buf.getvalue())

    def test_serve_daemon_oserror_returns_exit_2_and_closes_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._make_config(root)
            service = LibraryService(config)

            with self.assertLogs("frame_art_mode.library_daemon", level="ERROR"):
                with mock.patch(
                    "frame_art_mode.library_daemon.create_library_ipc_server",
                    side_effect=OSError("AF_UNIX path too long"),
                ):
                    rc = serve_daemon(service, config.library_sock)
            self.assertEqual(rc, 2)

            # Lock must be released by service.close() in finally
            reopened = self._start_service(config)
            self.assertTrue(reopened.health()["ok"])

    def test_library_daemon_module_respects_import_boundaries(self):
        path = Path(__file__).resolve().parents[1] / "frame_art_mode" / "library_daemon.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden = {"samsungframetv", "flask", "tv_daemon", "web_server"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    self.assertNotIn(top, forbidden)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    self.assertNotIn(top, forbidden)
                for alias in node.names:
                    self.assertNotIn(alias.name, forbidden)


if __name__ == "__main__":
    unittest.main()
