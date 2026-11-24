from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
import http.client
import http.server
import json
import logging
import math
import os
from pathlib import Path
import socket
import socketserver
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .models import IPCError, ensure_private_dir

logger = logging.getLogger(__name__)

HTTP_OK = 200
HTTP_ACCEPTED = 202
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_INTERNAL_ERROR = 500
HTTP_SERVICE_UNAVAILABLE = 503

MAX_IPC_BODY_BYTES = 10 * 1024 * 1024

RouteHandler = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]


def make_error_envelope(error: str, code: str) -> dict[str, str]:
    """Return the standard IPC/API JSON error envelope."""
    return {"error": error, "code": code}


def _error_code_for_status(status_code: int) -> str:
    if status_code == HTTP_INTERNAL_ERROR:
        return "internal_error"
    try:
        return HTTPStatus(status_code).name.lower()
    except ValueError:
        return f"http_{status_code}"


class _UnixHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP/1.1 JSON request handler for UnixHTTPServer."""

    protocol_version = "HTTP/1.1"
    server: UnixHTTPServer

    def address_string(self) -> str:
        return "unix"

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("IPC %s", format % args)

    def do_GET(self) -> None:
        self._handle_request("GET")

    def do_POST(self) -> None:
        self._handle_request("POST")

    def do_PUT(self) -> None:
        self._handle_request("PUT")

    def do_DELETE(self) -> None:
        self._handle_request("DELETE")

    def _handle_request(self, method: str) -> None:
        path = urlsplit(self.path).path
        body, error_response = self._read_json_body()
        if error_response is not None:
            status_code, payload = error_response
            self._send_json(status_code, payload)
            return

        handler = self.server.lookup_route(method, path)
        if handler is None:
            self._send_json(
                HTTP_NOT_FOUND,
                make_error_envelope(f"unknown endpoint: {method} {path}", "not_found"),
            )
            return

        try:
            status_code, payload = handler(body)
            encoded = self._encode_json(payload)
        except IPCError as exc:
            status_code = exc.status_code if exc.status_code is not None else HTTP_INTERNAL_ERROR
            self._send_json(status_code, make_error_envelope(str(exc), exc.code))
        except Exception as exc:
            logger.exception("Unhandled error in IPC handler %s %s", method, path)
            self._send_json(
                HTTP_INTERNAL_ERROR,
                make_error_envelope(f"internal server error: {exc}", "internal_error"),
            )
        else:
            self._send_encoded_json(status_code, encoded)

    def _read_json_body(self) -> tuple[dict[str, Any], tuple[int, dict[str, str]] | None]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return {}, None
        try:
            content_length = int(raw_length)
        except ValueError:
            return {}, (
                HTTP_BAD_REQUEST,
                make_error_envelope("invalid Content-Length header", "invalid_content_length"),
            )
        if content_length < 0 or content_length > MAX_IPC_BODY_BYTES:
            return {}, (
                HTTP_BAD_REQUEST,
                make_error_envelope("invalid Content-Length header", "invalid_content_length"),
            )
        if content_length == 0:
            return {}, None

        raw_bytes = self.rfile.read(content_length)
        if len(raw_bytes) != content_length:
            return {}, (
                HTTP_BAD_REQUEST,
                make_error_envelope("incomplete request body", "incomplete_body"),
            )
        try:
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}, (
                HTTP_BAD_REQUEST,
                make_error_envelope("invalid JSON request body", "invalid_json"),
            )
        if not isinstance(parsed, dict):
            return {}, (
                HTTP_BAD_REQUEST,
                make_error_envelope("request body must be a JSON object", "invalid_body"),
            )
        return parsed, None

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        status_code = int(code)
        try:
            shortmsg, _longmsg = self.responses[status_code]
        except KeyError:
            shortmsg = "HTTP error"
        if message is None:
            message = shortmsg
        self.log_error("code %d, message %s", status_code, message)
        if getattr(self, "request_version", None) in (None, "HTTP/0.9"):
            self.request_version = self.protocol_version
        encoded = self._encode_json(
            make_error_envelope(message, _error_code_for_status(status_code))
        )
        self.send_response(status_code, message)
        self.send_header("Connection", "close")
        include_body = status_code >= 200 and status_code not in (204, 205, 304)
        if include_body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if include_body and getattr(self, "command", None) != "HEAD":
            self.wfile.write(encoded)

    @staticmethod
    def _encode_json(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _send_encoded_json(self, status_code: int, encoded: bytes) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        self._send_encoded_json(status_code, self._encode_json(payload))


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Threaded HTTP/1.1 JSON server bound to a private Unix domain socket."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(
        self,
        socket_path: Path,
        routes: dict[tuple[str, str], RouteHandler] | None = None,
    ) -> None:
        self.socket_path = Path(socket_path)
        ensure_private_dir(self.socket_path.parent)
        self.socket_path.unlink(missing_ok=True)
        self._routes_lock = threading.Lock()
        self._routes: dict[tuple[str, str], RouteHandler] = {}
        if routes:
            for (method, path), handler in routes.items():
                self.register(method, path, handler)
        super().__init__(str(self.socket_path), _UnixHTTPRequestHandler)
        os.chmod(self.socket_path, 0o600)

    def register(self, method: str, path: str, handler: RouteHandler) -> None:
        """Register a route handler for an HTTP method and path."""
        with self._routes_lock:
            self._routes[(method.upper(), path)] = handler

    def lookup_route(self, method: str, path: str) -> RouteHandler | None:
        """Return the registered route handler for (method, path), if any."""
        with self._routes_lock:
            return self._routes.get((method.upper(), path))

    def handle_error(self, request: Any, client_address: Any) -> None:
        logger.debug("IPC connection error for %s", self.socket_path, exc_info=True)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.socket_path.unlink(missing_ok=True)


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP/1.1 client connection over a Unix domain socket."""

    def __init__(self, socket_path: Path | str, timeout: float = 5.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = str(socket_path)

    def connect(self) -> None:
        if self.timeout is not None and (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(self.timeout)
            or self.timeout < 0
        ):
            raise ValueError(f"invalid timeout: {self.timeout!r}")
        deadline = time.monotonic() + self.timeout if self.timeout is not None else None
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                if self.timeout is not None:
                    sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
            except BlockingIOError:
                sock.close()
                if deadline is not None and time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
                continue
            except Exception:
                sock.close()
                raise
            self.sock = sock
            return


def request_json(
    socket_path: Path,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, Any]]:
    """Send a JSON HTTP request over a Unix domain socket and return (status, body)."""
    resolved_path = Path(socket_path)
    normalized_method = method.upper()
    headers: dict[str, str] = {"Accept": "application/json"}
    encoded_body: bytes | None = None
    if body is not None:
        encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    conn = UnixHTTPConnection(resolved_path, timeout=timeout)
    try:
        conn.request(normalized_method, path, body=encoded_body, headers=headers)
        response = conn.getresponse()
        raw_response = response.read()
        status = response.status
    except (
        FileNotFoundError,
        ConnectionRefusedError,
        BrokenPipeError,
        TimeoutError,
        http.client.RemoteDisconnected,
        OSError,
    ) as exc:
        raise IPCError(
            f"service unavailable at {resolved_path}: {exc}",
            status_code=HTTP_SERVICE_UNAVAILABLE,
            code="service_unavailable",
            offline=True,
        ) from exc
    except http.client.HTTPException as exc:
        raise IPCError(
            f"invalid HTTP response from {resolved_path}: {exc}",
            status_code=HTTP_INTERNAL_ERROR,
            code="invalid_response",
        ) from exc
    finally:
        conn.close()

    try:
        decoded = json.loads(raw_response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IPCError(
            f"malformed JSON response from {resolved_path}: {exc}",
            status_code=status,
            code="invalid_response",
        ) from exc

    if not isinstance(decoded, dict):
        raise IPCError(
            f"expected JSON object from {resolved_path}",
            status_code=status,
            code="invalid_response",
        )
    return status, decoded


def call_json(
    socket_path: Path,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Send a JSON IPC request and raise IPCError on non-2xx responses."""
    status, data = request_json(socket_path, method, path, body=body, timeout=timeout)
    if 200 <= status < 300:
        return data
    raw_error = data.get("error")
    message = (
        raw_error
        if isinstance(raw_error, str) and raw_error
        else f"IPC request {method.upper()} {path} failed with status {status}"
    )
    raw_code = data.get("code")
    code = raw_code if isinstance(raw_code, str) and raw_code else "ipc_error"
    raise IPCError(status_code=status, code=code, message=message)
