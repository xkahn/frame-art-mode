import ast
from concurrent.futures import ThreadPoolExecutor
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from frame_art_mode.ipc import (
    HTTP_ACCEPTED,
    HTTP_BAD_REQUEST,
    HTTP_CONFLICT,
    HTTP_INTERNAL_ERROR,
    HTTP_NOT_FOUND,
    HTTP_OK,
    HTTP_SERVICE_UNAVAILABLE,
    UnixHTTPConnection,
    UnixHTTPServer,
    call_json,
    make_error_envelope,
    request_json,
)
from frame_art_mode.models import IPCError


class IPCTests(unittest.TestCase):
    def _start_server(self, server: UnixHTTPServer) -> threading.Thread:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _cleanup() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

        self.addCleanup(_cleanup)
        return thread

    def test_get_post_round_trip_headers_and_socket_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            sock_path = Path(temporary) / "private_state" / "library.sock"
            # Pre-create stale socket file to verify startup unlink
            sock_path.parent.mkdir(parents=True)
            sock_path.write_text("stale", encoding="utf-8")

            server = UnixHTTPServer(sock_path)
            server.register(
                "GET",
                "/health",
                lambda _body: (HTTP_OK, {"ok": True, "pid": os.getpid()}),
            )
            server.register(
                "POST",
                "/refresh",
                lambda body: (HTTP_ACCEPTED, {"accepted": True, "set_id": body.get("set_id")}),
            )
            self._start_server(server)

            self.assertEqual(stat.S_IMODE(sock_path.parent.stat().st_mode), 0o700)
            self.assertTrue(sock_path.exists())

            conn = UnixHTTPConnection(sock_path, timeout=2.0)
            conn.request("GET", "/health")
            raw_resp = conn.getresponse()
            raw_body = raw_resp.read()
            conn.close()
            self.assertEqual(raw_resp.status, 200)
            self.assertEqual(raw_resp.getheader("Content-Type"), "application/json; charset=utf-8")
            self.assertEqual(raw_resp.getheader("Content-Length"), str(len(raw_body)))

            status, health = request_json(sock_path, "GET", "/health")
            self.assertEqual(status, 200)
            self.assertTrue(health["ok"])

            accepted = call_json(sock_path, "POST", "/refresh", body={"set_id": "root"})
            self.assertEqual(accepted, {"accepted": True, "set_id": "root"})

            # Query string is stripped during route dispatch
            status_qs, health_qs = request_json(sock_path, "GET", "/health?verbose=1")
            self.assertEqual(status_qs, 200)
            self.assertTrue(health_qs["ok"])

        self.assertFalse(sock_path.exists())

    def test_error_envelopes_400_404_409_500_and_call_json_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            sock_path = Path(temporary) / "tv.sock"

            def conflict_handler(_body):
                return HTTP_CONFLICT, make_error_envelope("sync already running", "conflict")

            def ipc_error_handler(_body):
                raise IPCError("bad input", status_code=HTTP_BAD_REQUEST, code="bad_request")

            def crash_handler(_body):
                raise RuntimeError("boom")

            server = UnixHTTPServer(
                sock_path,
                routes={
                    ("POST", "/sync"): conflict_handler,
                    ("POST", "/validate"): ipc_error_handler,
                    ("POST", "/crash"): crash_handler,
                    ("POST", "/echo"): lambda body: (HTTP_OK, body),
                },
            )
            self._start_server(server)

            # 404 unknown endpoint
            status, body = request_json(sock_path, "GET", "/missing")
            self.assertEqual(status, HTTP_NOT_FOUND)
            self.assertEqual(body["code"], "not_found")
            with self.assertRaises(IPCError) as not_found_ctx:
                call_json(sock_path, "GET", "/missing")
            self.assertEqual(not_found_ctx.exception.status_code, HTTP_NOT_FOUND)
            self.assertEqual(not_found_ctx.exception.code, "not_found")
            self.assertFalse(not_found_ctx.exception.offline)

            # 409 conflict
            with self.assertRaises(IPCError) as conflict_ctx:
                call_json(sock_path, "POST", "/sync", body={})
            self.assertEqual(conflict_ctx.exception.status_code, HTTP_CONFLICT)
            self.assertEqual(conflict_ctx.exception.code, "conflict")
            self.assertEqual(conflict_ctx.exception.message, "sync already running")

            # 400 raised via IPCError
            with self.assertRaises(IPCError) as bad_ctx:
                call_json(sock_path, "POST", "/validate", body={})
            self.assertEqual(bad_ctx.exception.status_code, HTTP_BAD_REQUEST)
            self.assertEqual(bad_ctx.exception.code, "bad_request")

            # 500 unhandled exception
            with self.assertLogs("frame_art_mode.ipc", level="ERROR"):
                status, crash_body = request_json(sock_path, "POST", "/crash", body={})
            self.assertEqual(status, HTTP_INTERNAL_ERROR)
            self.assertEqual(crash_body["code"], "internal_error")
            self.assertIn("boom", crash_body["error"])

            # 400 malformed JSON sent over raw HTTP connection
            conn = UnixHTTPConnection(sock_path, timeout=2.0)
            conn.request(
                "POST",
                "/echo",
                body=b"{not-json",
                headers={"Content-Type": "application/json"},
            )
            bad_json_resp = conn.getresponse()
            bad_json_payload = bad_json_resp.read().decode("utf-8")
            conn.close()
            self.assertEqual(bad_json_resp.status, HTTP_BAD_REQUEST)
            self.assertIn("invalid_json", bad_json_payload)

            # 400 JSON array instead of object
            conn = UnixHTTPConnection(sock_path, timeout=2.0)
            conn.request(
                "POST",
                "/echo",
                body=b"[1, 2, 3]",
                headers={"Content-Type": "application/json"},
            )
            non_obj_resp = conn.getresponse()
            non_obj_payload = non_obj_resp.read().decode("utf-8")
            conn.close()
            self.assertEqual(non_obj_resp.status, HTTP_BAD_REQUEST)
            self.assertIn("invalid_body", non_obj_payload)

    def test_offline_detection_for_missing_and_closed_socket(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing_sock = Path(temporary) / "nonexistent.sock"
            with self.assertRaises(IPCError) as missing_ctx:
                request_json(missing_sock, "GET", "/health")
            self.assertTrue(missing_ctx.exception.offline)
            self.assertEqual(missing_ctx.exception.status_code, HTTP_SERVICE_UNAVAILABLE)
            self.assertEqual(missing_ctx.exception.code, "service_unavailable")

            closed_sock = Path(temporary) / "closed.sock"
            server = UnixHTTPServer(
                closed_sock,
                routes={("GET", "/health"): lambda _b: (HTTP_OK, {"ok": True})},
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

            with self.assertRaises(IPCError) as closed_ctx:
                call_json(closed_sock, "GET", "/health")
            self.assertTrue(closed_ctx.exception.offline)
            self.assertEqual(closed_ctx.exception.status_code, HTTP_SERVICE_UNAVAILABLE)

    def test_malformed_server_response_raises_ipc_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            sock_path = Path(temporary) / "raw.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(sock_path))
            listener.listen(1)
            self.addCleanup(listener.close)

            def serve_bad_json() -> None:
                client, _ = listener.accept()
                with client:
                    client.recv(1024)
                    client.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: 9\r\n"
                        b"\r\n"
                        b"not-json!"
                    )

            thread = threading.Thread(target=serve_bad_json, daemon=True)
            thread.start()
            self.addCleanup(lambda: thread.join(timeout=2.0))

            with self.assertRaises(IPCError) as ctx:
                request_json(sock_path, "GET", "/health")
            self.assertFalse(ctx.exception.offline)
            self.assertEqual(ctx.exception.code, "invalid_response")

    def test_concurrent_threaded_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            sock_path = Path(temporary) / "concurrent.sock"
            server = UnixHTTPServer(
                sock_path,
                routes={
                    ("POST", "/echo"): lambda body: (HTTP_OK, {"seq": body["seq"]}),
                },
            )
            self._start_server(server)

            def send_one(index: int) -> int:
                reply = call_json(sock_path, "POST", "/echo", body={"seq": index})
                return int(reply["seq"])

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(send_one, range(24)))
            self.assertEqual(results, list(range(24)))

    def test_non_json_serializable_handler_returns_500_internal_error_not_offline(self):
        with tempfile.TemporaryDirectory() as temporary:
            sock_path = Path(temporary) / "non_json.sock"
            server = UnixHTTPServer(
                sock_path,
                routes={
                    ("GET", "/bad-payload"): lambda _b: (HTTP_OK, {"path": Path("/tmp/x")}),
                },
            )
            self._start_server(server)

            with self.assertLogs("frame_art_mode.ipc", level="ERROR"):
                status, body = request_json(sock_path, "GET", "/bad-payload")
            self.assertEqual(status, HTTP_INTERNAL_ERROR)
            self.assertEqual(body["code"], "internal_error")
            self.assertIn("internal server error", body["error"])

            with self.assertLogs("frame_art_mode.ipc", level="ERROR"):
                with self.assertRaises(IPCError) as ctx:
                    call_json(sock_path, "GET", "/bad-payload")
            self.assertFalse(ctx.exception.offline)
            self.assertEqual(ctx.exception.status_code, HTTP_INTERNAL_ERROR)
            self.assertEqual(ctx.exception.code, "internal_error")

    def test_unsupported_methods_and_malformed_http_return_json_error_envelope(self):
        with tempfile.TemporaryDirectory() as temporary:
            sock_path = Path(temporary) / "errors.sock"
            server = UnixHTTPServer(
                sock_path,
                routes={("GET", "/health"): lambda _b: (HTTP_OK, {"ok": True})},
            )
            self._start_server(server)

            status, patch_body = request_json(sock_path, "PATCH", "/health")
            self.assertEqual(status, 501)
            self.assertEqual(patch_body["code"], "not_implemented")
            self.assertIn("PATCH", patch_body["error"])

            conn = UnixHTTPConnection(sock_path, timeout=2.0)
            conn.request("HEAD", "/health")
            head_resp = conn.getresponse()
            head_raw = head_resp.read()
            conn.close()
            self.assertEqual(head_resp.status, 501)
            self.assertEqual(head_resp.getheader("Content-Type"), "application/json; charset=utf-8")
            self.assertEqual(head_raw, b"")

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw_sock:
                raw_sock.settimeout(2.0)
                raw_sock.connect(str(sock_path))
                raw_sock.sendall(b"BAD_REQUEST_LINE\r\n\r\n")
                chunks: list[bytes] = []
                while True:
                    chunk = raw_sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
            raw_response = b"".join(chunks).decode("utf-8")
            header_block, _, body_text = raw_response.partition("\r\n\r\n")
            self.assertIn("400 ", header_block)
            self.assertIn("application/json; charset=utf-8", header_block)
            self.assertNotIn("<html", body_text.lower())
            parsed = json.loads(body_text)
            self.assertEqual(parsed["code"], "bad_request")
            self.assertIn("Bad request syntax", parsed["error"])

    def test_unix_http_connection_invalid_timeout_and_settimeout_error_closes_socket(self):
        with tempfile.TemporaryDirectory() as temporary:
            sock_path = Path(temporary) / "timeout.sock"
            for bad_timeout in (-1.0, float("nan"), float("inf")):
                with self.subTest(timeout=bad_timeout):
                    conn = UnixHTTPConnection(sock_path, timeout=bad_timeout)
                    with self.assertRaises(ValueError):
                        conn.connect()

            mock_sock = mock.MagicMock()
            mock_sock.settimeout.side_effect = ValueError("bad timeout")
            with mock.patch("frame_art_mode.ipc.socket.socket", return_value=mock_sock):
                conn = UnixHTTPConnection(sock_path, timeout=1.0)
                with self.assertRaises(ValueError):
                    conn.connect()
            mock_sock.close.assert_called_once_with()

    def test_ipc_module_imports_stdlib_only(self):
        path = Path(__file__).resolve().parents[1] / "frame_art_mode" / "ipc.py"
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
