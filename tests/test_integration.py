from __future__ import annotations

import fcntl
import http.client
import io
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any

from PIL import Image

from frame_art_mode import ipc
from frame_art_mode.config import Config
from frame_art_mode.ipc import call_json
from frame_art_mode.library_daemon import LibraryService, create_library_ipc_server
from frame_art_mode.models import TVArtClient, ensure_private_dir
from frame_art_mode.supervisor import (
    ChildSpec,
    Supervisor,
    build_child_specs,
    main as supervisor_main,
    stop_supervisor,
)
from frame_art_mode.tv_daemon import TVDaemon, build_tv_ipc_server
from frame_art_mode.web_server import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]

_FAKE_SAMSUNGFRAMETV_SOURCE = '''
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path


def _state_path() -> Path:
    return Path(os.environ["FAKE_TV_STATE_FILE"])


def _with_state(fn):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
            else:
                data = {"next_id": 1, "items": {}, "current_id": None, "artmode": "on"}
            result, updated = fn(data)
            if updated is not None:
                path.write_text(json.dumps(updated), encoding="utf-8")
            return result
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


class _FakeArt:
    def get_artmode(self):
        return _with_state(lambda d: (d.get("artmode", "on"), None))

    def available(self, category=None):
        if category == "MY-C0004":
            return []
        return _with_state(
            lambda d: ([{"content_id": cid} for cid in d["items"]], None)
        )

    def upload(self, file, file_type="JPEG"):
        raw = Path(file).read_bytes() if isinstance(file, (str, os.PathLike)) else bytes(file)
        def _do(d):
            cid = f"MY_F{d['next_id']:04d}"
            d["next_id"] += 1
            d["items"][cid] = len(raw)
            return cid, d
        return _with_state(_do)

    def delete(self, content_id):
        def _do(d):
            d["items"].pop(content_id, None)
            if d.get("current_id") == content_id:
                d["current_id"] = None
            return True, d
        return _with_state(_do)

    def select_image(self, content_id, category=None, show=True):
        def _do(d):
            if content_id not in d["items"] and category != "MY-C0004":
                raise RuntimeError(f"missing {content_id}")
            d["current_id"] = content_id
            return True, d
        return _with_state(_do)

    def get_current(self):
        return _with_state(lambda d: ({"content_id": d.get("current_id")}, None))

    def close(self):
        return None


class SamsungTVWS:
    def __init__(self, host, token_file=None, timeout=30.0):
        self.host = host
        self.token_file = token_file
        self.timeout = timeout

    def art(self, timeout=None):
        return _FakeArt()
'''


def _make_jpeg_bytes(size: tuple[int, int] = (800, 640), color: tuple[int, int, int] = (200, 80, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _wait_until(predicate: Any, *, timeout: float = 10.0, interval: float = 0.02, desc: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:
            last_exc = exc
        time.sleep(interval)
    if last_exc is not None:
        raise AssertionError(f"Timed out waiting for {desc}: {last_exc}") from last_exc
    raise AssertionError(f"Timed out waiting for {desc}")


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
    if not match:
        raise AssertionError("CSRF meta tag not found in index HTML")
    return match.group(1)


class _InProcessFakeTV(TVArtClient):
    def __init__(self, shared_state: dict[str, Any]) -> None:
        self.state = shared_state
        self.closed = False

    def get_artmode(self) -> str:
        return "on"

    def available(self, category: str | None = None) -> list[dict[str, Any]]:
        if category == "MY-C0004":
            return []
        return [{"content_id": cid} for cid in self.state["items"]]

    def upload(self, file: Any, file_type: str = "JPEG") -> str:
        hook = self.state.get("on_upload")
        if callable(hook):
            hook()
        raw = Path(file).read_bytes() if isinstance(file, (str, os.PathLike)) else bytes(file)
        cid = f"MY_F{self.state['next_id']:04d}"
        self.state["next_id"] += 1
        self.state["items"][cid] = len(raw)
        self.state["calls"].append(("upload", cid))
        return cid

    def delete(self, content_id: str) -> bool:
        self.state["calls"].append(("delete", content_id))
        self.state["items"].pop(content_id, None)
        return True

    def select_image(
        self,
        content_id: str,
        category: str | None = None,
        show: bool = True,
    ) -> bool:
        self.state["calls"].append(("select", content_id))
        self.state["current_id"] = content_id
        return True

    def get_current(self) -> dict[str, Any]:
        return {"content_id": self.state["current_id"]}

    def close(self) -> None:
        self.closed = True


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


class _FakeChildProcess:
    def __init__(
        self,
        *,
        clock: _FakeClock,
        exit_code: int | None = None,
        crash_after: float | None = None,
        ignore_terminate: bool = False,
        output_lines: list[str] | None = None,
    ) -> None:
        self._clock = clock
        self._started_at = clock()
        self._exit_code = exit_code
        self._crash_after = crash_after
        self._ignore_terminate = ignore_terminate
        self._terminated_code: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.stdout = io.StringIO("".join(f"{line}\n" for line in (output_lines or [])))

    def poll(self) -> int | None:
        if self._terminated_code is not None:
            return self._terminated_code
        if self._exit_code is not None:
            if self._crash_after is None or (self._clock() - self._started_at) >= self._crash_after:
                return self._exit_code
        return None

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self._ignore_terminate and self._terminated_code is None:
            self._terminated_code = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self._terminated_code = -9

    def wait(self, timeout: float | None = None) -> int:
        code = self.poll()
        return 0 if code is None else code


class SupervisorUnitTests(unittest.TestCase):
    def test_build_child_specs_omits_image_root_from_tv_daemon(self):
        cfg = Path("/tmp/test-config.json")
        specs = build_child_specs(
            cfg,
            {
                "tv_ip": "192.168.1.50",
                "bind_ip": "127.0.0.1",
                "port": 8080,
                "image_root": Path("/tmp/images"),
                "state_dir": Path("/tmp/state"),
                "cache_dir": Path("/tmp/cache"),
            },
            python_executable="/usr/bin/python3.11",
        )
        by_name = {spec.name: spec.command for spec in specs}
        self.assertEqual(set(by_name.keys()), {"library_daemon", "tv_daemon", "web_server"})
        self.assertIn("--image-root", by_name["library_daemon"])
        self.assertIn("--image-root", by_name["web_server"])
        self.assertNotIn("--image-root", by_name["tv_daemon"])
        for name, cmd in by_name.items():
            self.assertEqual(cmd[:4], ("/usr/bin/python3.11", "-m", f"frame_art_mode.{name}", "serve"))
            self.assertIn("--config", cmd)
            self.assertIn("--state-dir", cmd)
            self.assertIn("--cache-dir", cmd)

    def test_exponential_backoff_cap_and_stable_uptime_reset_with_fast_clock(self):
        clock = _FakeClock(start=100.0)
        spawn_times: list[float] = []
        # Sequence of behaviors per spawn:
        # Spawns 1..7 crash immediately (exit 1) -> delays 1, 2, 4, 8, 16, 30, 30
        # Spawn 8 runs for 60s before crashing -> resets failure_count, delay = 1.0
        # Spawn 9 runs then stops supervisor
        behaviors: list[dict[str, Any]] = [
            {"exit_code": 1, "crash_after": 0.0},
            {"exit_code": 1, "crash_after": 0.0},
            {"exit_code": 1, "crash_after": 0.0},
            {"exit_code": 1, "crash_after": 0.0},
            {"exit_code": 1, "crash_after": 0.0},
            {"exit_code": 1, "crash_after": 0.0},
            {"exit_code": 1, "crash_after": 0.0},
            {"exit_code": 1, "crash_after": 60.0},
            {"exit_code": None},
        ]
        stream = io.StringIO()
        sup: Supervisor | None = None

        def factory(_cmd):
            idx = len(spawn_times)
            spawn_times.append(clock())
            spec = behaviors[idx]
            proc = _FakeChildProcess(
                clock=clock,
                exit_code=spec.get("exit_code"),
                crash_after=spec.get("crash_after"),
                output_lines=[f"started #{idx + 1}"],
            )
            if idx == len(behaviors) - 1:
                assert sup is not None
                sup.request_stop()
            return proc

        sup = Supervisor(
            [ChildSpec("tv_daemon", ("python", "-m", "frame_art_mode.tv_daemon", "serve"))],
            process_factory=factory,
            clock=clock,
            sleeper=clock.sleep,
            backoff_initial=1.0,
            backoff_max=30.0,
            stable_uptime_seconds=60.0,
            poll_interval=0.25,
            output_stream=stream,
        )
        rc = sup.run()
        self.assertEqual(rc, 0)
        deltas = [round(b - a, 6) for a, b in zip(spawn_times, spawn_times[1:])]
        # After spawn 8 (index 7) runs 60.0s, it crashes and waits 1.0s => delta is 61.0s
        self.assertEqual(deltas, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 61.0])
        self.assertIn("[tv_daemon] started #1", stream.getvalue())

    def test_all_children_exit_2_returns_2(self):
        clock = _FakeClock()
        specs = [
            ChildSpec("library_daemon", ("py", "lib")),
            ChildSpec("tv_daemon", ("py", "tv")),
            ChildSpec("web_server", ("py", "web")),
        ]
        spawn_counts = {"lib": 0, "tv": 0, "web": 0}

        def factory(cmd):
            spawn_counts[cmd[1]] += 1
            return _FakeChildProcess(clock=clock, exit_code=2, crash_after=0.0)

        sup = Supervisor(
            specs,
            process_factory=factory,
            clock=clock,
            sleeper=clock.sleep,
            output_stream=io.StringIO(),
        )
        rc = sup.run()
        self.assertEqual(rc, 2)
        self.assertEqual(spawn_counts, {"lib": 1, "tv": 1, "web": 1})

    def test_sigkill_escalation_after_shutdown_timeout(self):
        clock = _FakeClock()
        processes: list[_FakeChildProcess] = []
        sup: Supervisor | None = None

        def factory(_cmd):
            proc = _FakeChildProcess(clock=clock, ignore_terminate=True)
            processes.append(proc)
            assert sup is not None
            sup.request_stop()
            return proc

        sup = Supervisor(
            [ChildSpec("tv_daemon", ("py", "tv"))],
            process_factory=factory,
            clock=clock,
            sleeper=clock.sleep,
            shutdown_timeout=3.0,
            poll_interval=0.5,
            output_stream=io.StringIO(),
        )
        rc = sup.run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].terminate_calls, 1)
        self.assertEqual(processes[0].kill_calls, 1)
        self.assertGreaterEqual(clock(), 3.0)


class InProcessMultiDaemonIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="fam-int-")
        self.base = Path(self._tmp.name)
        self.image_root = self.base / "img"
        self.state_dir = self.base / "st"
        self.image_root.mkdir()
        (self.image_root / "sunset.jpg").write_bytes(_make_jpeg_bytes(color=(220, 70, 30)))
        party = self.image_root / "Party"
        party.mkdir()
        (party / "01.jpg").write_bytes(_make_jpeg_bytes(color=(30, 120, 220)))

        self.config = Config(
            script_dir=self.base,
            image_root=self.image_root,
            state_dir=self.state_dir,
            tv_ip="192.168.1.50",
            bind_ip="127.0.0.1",
            preview_seconds=5,
            tv_op_delay_seconds=0.5,
            tv_batch_settle_seconds=0.0,
            tv_verify_retries=2,
            tv_verify_interval_seconds=0.5,
        )
        self.tv_state: dict[str, Any] = {
            "next_id": 1,
            "items": {},
            "current_id": None,
            "calls": [],
            "on_upload": None,
        }

        self.lib_service: LibraryService | None = None
        self.lib_server: Any = None
        self.lib_thread: threading.Thread | None = None
        self.tv_daemon: TVDaemon | None = None
        self.tv_server: Any = None
        self.tv_thread: threading.Thread | None = None

    def tearDown(self) -> None:
        self._stop_tv()
        self._stop_library()
        self._tmp.cleanup()

    def _start_library(self) -> None:
        self.lib_service = LibraryService(self.config)
        self.lib_server = create_library_ipc_server(self.lib_service, self.config.library_sock)
        self.lib_thread = threading.Thread(target=self.lib_server.serve_forever, daemon=True)
        self.lib_thread.start()

    def _stop_library(self) -> None:
        if self.lib_server is not None:
            self.lib_server.shutdown()
            self.lib_server.server_close()
            self.lib_server = None
        if self.lib_thread is not None:
            self.lib_thread.join(timeout=3.0)
            self.lib_thread = None
        if self.lib_service is not None:
            self.lib_service.close()
            self.lib_service = None

    def _start_tv(self) -> None:
        self.tv_daemon = TVDaemon(
            self.config,
            tv_factory=lambda _cfg, _tok: _InProcessFakeTV(self.tv_state),
            library_caller=ipc.call_json,
        )
        self.tv_server = build_tv_ipc_server(self.tv_daemon, self.config.tv_sock)
        self.tv_thread = threading.Thread(target=self.tv_server.serve_forever, daemon=True)
        self.tv_thread.start()
        self.tv_daemon.start()

    def _stop_tv(self) -> None:
        if self.tv_server is not None:
            self.tv_server.shutdown()
            self.tv_server.server_close()
            self.tv_server = None
        if self.tv_thread is not None:
            self.tv_thread.join(timeout=3.0)
            self.tv_thread = None
        if self.tv_daemon is not None:
            self.tv_daemon.stop()
            self.tv_daemon = None

    def test_end_to_end_uds_pipeline_leases_sse_preview_abort_and_degraded_recovery(self):
        self._start_library()
        self._start_tv()

        sse_stop = threading.Event()
        self.addCleanup(sse_stop.set)
        app = create_app(
            self.config,
            ipc_timeout=2.0,
            sse_poll_interval=0.1,
            sse_active_poll_interval=0.05,
            sse_stop_event=sse_stop,
        )
        client = app.test_client()

        # 1. Index + CSRF token
        index_resp = client.get("/")
        self.assertEqual(index_resp.status_code, 200)
        csrf = _extract_csrf_token(index_resp.get_data(as_text=True))
        headers = {"X-CSRF-Token": csrf}

        # 2. Library refresh
        refresh_resp = client.post("/api/library/refresh", json={}, headers=headers)
        self.assertEqual(refresh_resp.status_code, 202)
        status = _wait_until(
            lambda: (
                s := client.get("/api/status").get_json()
            )
            and s["library"]["operation"] is None
            and s["library"]["counts"]["valid"] == 2
            and s,
            desc="library refresh completion",
        )
        root_img = status["library"]["sets"]["root"]["images"][0]
        sha = root_img["sha256"]

        # 3. Cached thumbnail serving
        with client.get(f"/thumbs/{sha}.jpg") as thumb_resp:
            self.assertEqual(thumb_resp.status_code, 200)
            self.assertTrue(thumb_resp.data.startswith(b"\xff\xd8"))
            self.assertIn("immutable", thumb_resp.headers.get("Cache-Control", ""))

        # 4. Check TV
        check_resp = client.post("/api/check-tv", json={}, headers=headers)
        self.assertEqual(check_resp.status_code, 200)
        self.assertTrue(check_resp.get_json()["tv_ready"])

        # 5. Sync root set while observing active library Sync lease pin
        upload_entered = threading.Event()
        upload_release = threading.Event()
        observed_leases: list[list[str]] = []

        def pause_first_upload() -> None:
            upload_entered.set()
            upload_release.wait(timeout=5.0)

        self.tv_state["on_upload"] = pause_first_upload
        sync_resp = client.post("/api/sync", json={"set_id": "root"}, headers=headers)
        self.assertEqual(sync_resp.status_code, 202)
        self.assertTrue(upload_entered.wait(timeout=5.0))
        lib_status_during_sync = call_json(self.config.library_sock, "GET", "/status")
        observed_leases.append(lib_status_during_sync["active_leases"])
        self.tv_state["on_upload"] = None
        upload_release.set()

        self.assertEqual(len(observed_leases[0]), 1)

        after_root_sync = _wait_until(
            lambda: (
                s := client.get("/api/status").get_json()
            )
            and s["tv"]["operation"] is None
            and s["tv"]["active_set"] == "root"
            and s,
            desc="root sync completion",
        )
        self.assertEqual(len(after_root_sync["tv"]["entries"]), 1)
        self.assertEqual(after_root_sync["library"]["active_leases"], [])
        first_cid = after_root_sync["tv"]["entries"][0]["content_id"]

        # 6. Sync occasion:Party to verify select-before-delete ordering
        self.tv_state["calls"].clear()
        sync_party = client.post("/api/sync", json={"set_id": "occasion:Party"}, headers=headers)
        self.assertEqual(sync_party.status_code, 202)
        after_party_sync = _wait_until(
            lambda: (
                s := client.get("/api/status").get_json()
            )
            and s["tv"]["operation"] is None
            and s["tv"]["active_set"] == "occasion:Party"
            and s,
            desc="occasion:Party sync completion",
        )
        calls = self.tv_state["calls"]
        select_idx = next(i for i, c in enumerate(calls) if c[0] == "select")
        delete_idx = next(i for i, c in enumerate(calls) if c == ("delete", first_cid))
        self.assertLess(select_idx, delete_idx)

        # 7. Preview + cooperative abort
        preview_cid = after_party_sync["tv"]["entries"][0]["content_id"]
        preview_resp = client.post("/api/preview", json={"content_id": preview_cid}, headers=headers)
        self.assertEqual(preview_resp.status_code, 202)
        _wait_until(
            lambda: client.get("/api/status").get_json()["tv"]["preview"]["active"] is True,
            desc="preview active",
        )
        abort_resp = client.post("/api/abort", json={"target": "tv"}, headers=headers)
        self.assertEqual(abort_resp.status_code, 200)
        after_abort = _wait_until(
            lambda: (
                s := client.get("/api/status").get_json()
            )
            and s["tv"]["operation"] is None
            and s["tv"]["preview"]["active"] is False
            and s,
            desc="preview abort completion",
        )
        self.assertEqual(after_abort["tv"]["last_result"]["state"], "aborted")

        # 8. SSE /api/events emits initial snapshot
        sse_resp = client.get("/api/events", buffered=False)
        self.assertEqual(sse_resp.status_code, 200)
        chunk = next(sse_resp.response)
        sse_stop.set()
        sse_resp.close()
        text_chunk = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        self.assertIn("event: status", text_chunk)
        data_line = next(line for line in text_chunk.splitlines() if line.startswith("data: "))
        sse_payload = json.loads(data_line.removeprefix("data: "))
        self.assertFalse(sse_payload["degraded"])

        # 9. Degraded mode when tv.sock stops -> 503 -> recovery when restarted
        self._stop_tv()
        degraded_tv = client.get("/api/status").get_json()
        self.assertTrue(degraded_tv["degraded"])
        self.assertFalse(degraded_tv["services"]["tv"]["ok"])
        self.assertTrue(degraded_tv["services"]["library"]["ok"])
        self.assertEqual(
            client.post("/api/sync", json={"set_id": "root"}, headers=headers).status_code,
            503,
        )

        sse_stop.clear()
        self._start_tv()
        recovered_tv = client.get("/api/status").get_json()
        self.assertFalse(recovered_tv["degraded"])

        # 10. Degraded mode when library.sock stops -> 503 -> recovery when restarted
        self._stop_library()
        degraded_lib = client.get("/api/status").get_json()
        self.assertTrue(degraded_lib["degraded"])
        self.assertFalse(degraded_lib["services"]["library"]["ok"])
        self.assertEqual(
            client.post("/api/library/refresh", json={}, headers=headers).status_code,
            503,
        )
        self._start_library()
        self.assertFalse(client.get("/api/status").get_json()["degraded"])


class SupervisorSubprocessIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="fam-sup-")
        self.base = Path(self._tmp.name)
        self.fake_pkg_dir = self.base / "fake_deps"
        self.fake_pkg_dir.mkdir()
        (self.fake_pkg_dir / "samsungframetv.py").write_text(
            _FAKE_SAMSUNGFRAMETV_SOURCE,
            encoding="utf-8",
        )
        self.tv_state_file = self.base / "fake_tv_state.json"

        self.image_root = self.base / "img"
        self.state_dir = self.base / "st"
        self.image_root.mkdir()
        (self.image_root / "photo.jpg").write_bytes(_make_jpeg_bytes(color=(100, 180, 90)))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _sub_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{self.fake_pkg_dir}{os.pathsep}{REPO_ROOT}"
        env["FAKE_TV_STATE_FILE"] = str(self.tv_state_file)
        return env

    def _write_config(self, port: int) -> tuple[Path, Config]:
        cfg_path = self.base / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "tv_ip": "192.168.1.50",
                    "bind_ip": "127.0.0.1",
                    "port": port,
                    "image_root": str(self.image_root),
                    "state_dir": str(self.state_dir),
                    "preview_seconds": 5,
                    "tv_op_delay_seconds": 0.5,
                    "tv_batch_settle_seconds": 0.0,
                    "tv_verify_retries": 2,
                    "tv_verify_interval_seconds": 0.5,
                }
            ),
            encoding="utf-8",
        )
        cfg = Config(
            script_dir=self.base,
            image_root=self.image_root,
            state_dir=self.state_dir,
            tv_ip="192.168.1.50",
            bind_ip="127.0.0.1",
            port=port,
        )
        return cfg_path, cfg

    def _http_json(
        self,
        port: int,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any, http.client.HTTPResponse]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        try:
            req_headers = dict(headers or {})
            raw_body: bytes | None = None
            if body is not None:
                raw_body = json.dumps(body).encode("utf-8")
                req_headers["Content-Type"] = "application/json"
            conn.request(method, path, body=raw_body, headers=req_headers)
            resp = conn.getresponse()
            data = resp.read()
            parsed = json.loads(data.decode("utf-8")) if resp.getheader("Content-Type", "").startswith("application/json") else data
            return resp.status, parsed, resp
        finally:
            conn.close()

    def test_upfront_config_error_exits_2_without_creating_state_files(self):
        bad_cfg = self.base / "bad.json"
        bad_cfg.write_text('{"tv_ip": "0.0.0.0", "bind_ip": "127.0.0.1"}', encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "frame_art_mode.supervisor",
                "--config",
                str(bad_cfg),
                "--state-dir",
                str(self.state_dir),
            ],
            cwd=REPO_ROOT,
            env=self._sub_env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Invalid configuration", proc.stderr)
        self.assertFalse(self.state_dir.exists())

        # Also exercise supervisor_main directly
        rc = supervisor_main(["--config", str(bad_cfg), "--state-dir", str(self.state_dir)])
        self.assertEqual(rc, 2)

    def test_live_supervisor_http_flow_sigkill_restart_and_clean_sigterm(self):
        port = _free_local_port()
        cfg_path, cfg = self._write_config(port)

        sup_proc = subprocess.Popen(
            [sys.executable, "-m", "frame_art_mode.supervisor", "--config", str(cfg_path)],
            cwd=REPO_ROOT,
            env=self._sub_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def _ensure_stopped() -> None:
            if sup_proc.poll() is None:
                sup_proc.kill()
                sup_proc.wait(timeout=5.0)
            if sup_proc.stdout is not None:
                sup_proc.stdout.close()

        self.addCleanup(_ensure_stopped)

        # Wait for all 3 processes to come online
        _wait_until(
            lambda: (
                (res := self._http_json(port, "GET", "/api/status"))[0] == 200
                and res[1]["degraded"] is False
            ),
            timeout=15.0,
            interval=0.1,
            desc="supervisor services ready",
        )

        # GET / for CSRF token and session cookie
        status_code, html_bytes, index_resp = self._http_json(port, "GET", "/")
        self.assertEqual(status_code, 200)
        csrf = _extract_csrf_token(html_bytes.decode("utf-8"))
        cookie = index_resp.getheader("Set-Cookie", "").split(";", 1)[0]
        api_headers = {"X-CSRF-Token": csrf, "Cookie": cookie}

        # Check TV -> Refresh -> Thumbnail -> Sync
        code, check_payload, _ = self._http_json(port, "POST", "/api/check-tv", body={}, headers=api_headers)
        self.assertEqual(code, 200)
        self.assertTrue(check_payload["tv_ready"])

        code, _, _ = self._http_json(port, "POST", "/api/library/refresh", body={}, headers=api_headers)
        self.assertEqual(code, 202)
        status_after_refresh = _wait_until(
            lambda: (
                (res := self._http_json(port, "GET", "/api/status"))[1]["library"]["operation"] is None
                and res[1]["library"]["counts"]["valid"] == 1
                and res[1]
            ),
            timeout=10.0,
            interval=0.05,
            desc="live refresh completion",
        )
        sha = status_after_refresh["library"]["sets"]["root"]["images"][0]["sha256"]
        thumb_code, thumb_bytes, _ = self._http_json(port, "GET", f"/thumbs/{sha}.jpg")
        self.assertEqual(thumb_code, 200)
        self.assertTrue(thumb_bytes.startswith(b"\xff\xd8"))

        code, _, _ = self._http_json(port, "POST", "/api/sync", body={"set_id": "root"}, headers=api_headers)
        self.assertEqual(code, 202)
        synced = _wait_until(
            lambda: (
                (res := self._http_json(port, "GET", "/api/status"))[1]["tv"]["operation"] is None
                and res[1]["tv"]["active_set"] == "root"
                and res[1]
            ),
            timeout=10.0,
            interval=0.05,
            desc="live sync completion",
        )
        self.assertEqual(len(synced["tv"]["entries"]), 1)

        # Kill tv_daemon with SIGKILL -> degraded mode -> supervisor restarts tv_daemon
        tv_pid = call_json(cfg.tv_sock, "GET", "/health")["pid"]
        lib_pid = call_json(cfg.library_sock, "GET", "/health")["pid"]
        os.kill(tv_pid, signal.SIGKILL)

        degraded = _wait_until(
            lambda: (
                (res := self._http_json(port, "GET", "/api/status"))[0] == 200
                and res[1]["degraded"] is True
                and res[1]["services"]["tv"]["ok"] is False
                and res[1]["services"]["library"]["ok"] is True
                and res[1]
            ),
            timeout=5.0,
            interval=0.05,
            desc="degraded mode after tv_daemon SIGKILL",
        )
        self.assertTrue(degraded["degraded"])

        _wait_until(
            lambda: (
                (res := self._http_json(port, "GET", "/api/status"))[0] == 200
                and res[1]["degraded"] is False
                and res[1]["services"]["tv"]["ok"] is True
            ),
            timeout=10.0,
            interval=0.1,
            desc="tv_daemon recovery after restart",
        )
        new_tv_pid = call_json(cfg.tv_sock, "GET", "/health")["pid"]
        self.assertNotEqual(new_tv_pid, tv_pid)

        # SIGTERM to supervisor -> clean exit 0, children gone, sockets unlinked
        sup_proc.terminate()
        rc = sup_proc.wait(timeout=15.0)
        sup_output = sup_proc.stdout.read() if sup_proc.stdout is not None else ""
        self.assertEqual(rc, 0)
        self.assertIn("[library_daemon]", sup_output)
        self.assertIn("[tv_daemon]", sup_output)
        self.assertIn("[web_server]", sup_output)
        self.assertFalse(cfg.tv_sock.exists())
        self.assertFalse(cfg.library_sock.exists())
        for child_pid in (new_tv_pid, lib_pid):
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_fatal_child_exit_2_stays_stopped_while_web_stays_degraded(self):
        port = _free_local_port()
        cfg_path, cfg = self._write_config(port)

        ensure_private_dir(cfg.state_dir)
        ensure_private_dir(cfg.tv_dir)
        lock_handle = cfg.tv_lock.open("a+", encoding="utf-8")
        os.chmod(cfg.tv_lock, 0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(lock_handle.close)
        self.addCleanup(lambda: fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN))

        sup_proc = subprocess.Popen(
            [sys.executable, "-m", "frame_art_mode.supervisor", "--config", str(cfg_path)],
            cwd=REPO_ROOT,
            env=self._sub_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def _ensure_stopped() -> None:
            if sup_proc.poll() is None:
                sup_proc.kill()
                sup_proc.wait(timeout=5.0)
            if sup_proc.stdout is not None:
                sup_proc.stdout.close()

        self.addCleanup(_ensure_stopped)

        status = _wait_until(
            lambda: (
                (res := self._http_json(port, "GET", "/api/status"))[0] == 200
                and res[1]["services"]["library"]["ok"] is True
                and res[1]["services"]["tv"]["ok"] is False
                and res[1]["degraded"] is True
                and res[1]
            ),
            timeout=15.0,
            interval=0.1,
            desc="degraded web + library while tv_daemon blocked by lock",
        )
        self.assertTrue(status["degraded"])
        self.assertFalse(cfg.tv_sock.exists())

        # Supervisor must stay online and NOT keep restarting tv_daemon
        time.sleep(1.5)
        self.assertIsNone(sup_proc.poll())
        self.assertFalse(cfg.tv_sock.exists())

        sup_proc.terminate()
        rc = sup_proc.wait(timeout=15.0)
        output = sup_proc.stdout.read() if sup_proc.stdout is not None else ""
        self.assertEqual(rc, 0)
        self.assertFalse(cfg.library_sock.exists())
        self.assertIn("tv_daemon exited with fatal status 2", output)
        self.assertEqual(output.count("Starting tv_daemon"), 1)

    def test_supervisor_pid_file_and_stop_sh_lifecycle(self):
        port = _free_local_port()
        cfg_path, cfg = self._write_config(port)

        # 1. Missing PID file -> exits 0 cleanly
        self.assertFalse(cfg.supervisor_pid.exists())
        self.assertEqual(stop_supervisor(cfg.supervisor_pid), 0)

        # 2. Stale dead PID file -> unlinks and exits 0
        ensure_private_dir(cfg.state_dir)
        cfg.supervisor_pid.write_text("999999\n", encoding="utf-8")
        self.assertEqual(stop_supervisor(cfg.supervisor_pid), 0)
        self.assertFalse(cfg.supervisor_pid.exists())

        # 3. Unrelated live PID -> refuses to signal it, unlinks stale PID file
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=REPO_ROOT,
        )
        try:
            cfg.supervisor_pid.write_text(f"{unrelated.pid}\n", encoding="utf-8")
            self.assertEqual(stop_supervisor(cfg.supervisor_pid), 0)
            self.assertFalse(cfg.supervisor_pid.exists())
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5.0)

        # 4. Live supervisor writes 0600 supervisor.pid and stop.sh stops it cleanly
        sup_proc = subprocess.Popen(
            [sys.executable, "-m", "frame_art_mode.supervisor", "--config", str(cfg_path)],
            cwd=REPO_ROOT,
            env=self._sub_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def _ensure_stopped() -> None:
            if sup_proc.poll() is None:
                sup_proc.kill()
                sup_proc.wait(timeout=5.0)
            if sup_proc.stdout is not None:
                sup_proc.stdout.close()

        self.addCleanup(_ensure_stopped)

        _wait_until(
            lambda: (
                (res := self._http_json(port, "GET", "/api/status"))[0] == 200
                and res[1]["degraded"] is False
                and cfg.supervisor_pid.is_file()
            ),
            timeout=15.0,
            interval=0.1,
            desc="supervisor healthy with PID file",
        )
        self.assertEqual(stat.S_IMODE(cfg.supervisor_pid.stat().st_mode), 0o600)
        self.assertEqual(int(cfg.supervisor_pid.read_text(encoding="utf-8").strip()), sup_proc.pid)

        tv_pid = call_json(cfg.tv_sock, "GET", "/health")["pid"]
        lib_pid = call_json(cfg.library_sock, "GET", "/health")["pid"]

        stop_proc = subprocess.run(
            [str(REPO_ROOT / "stop.sh"), "--config", str(cfg_path)],
            cwd=REPO_ROOT,
            env=self._sub_env(),
            capture_output=True,
            text=True,
            timeout=20.0,
        )
        self.assertEqual(stop_proc.returncode, 0, stop_proc.stderr)
        self.assertEqual(sup_proc.wait(timeout=10.0), 0)
        self.assertFalse(cfg.supervisor_pid.exists())
        self.assertFalse(cfg.tv_sock.exists())
        self.assertFalse(cfg.library_sock.exists())
        for child_pid in (tv_pid, lib_pid):
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)


if __name__ == "__main__":
    unittest.main()
