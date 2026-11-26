from __future__ import annotations

import argparse
from collections.abc import Iterator, Sequence
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import sys
import threading
import time
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    make_response,
    render_template,
    request,
    send_file,
    session,
)

from .config import Config, ConfigError, load_config
from .ipc import (
    HTTP_BAD_REQUEST,
    HTTP_INTERNAL_ERROR,
    HTTP_NOT_FOUND,
    HTTP_OK,
    HTTP_SERVICE_UNAVAILABLE,
    make_error_envelope,
    request_json,
)
from .models import IPCError, SetSettings

logger = logging.getLogger(__name__)

HTTP_FORBIDDEN = 403
HTTP_METHOD_NOT_ALLOWED = 405
HTTP_PAYLOAD_TOO_LARGE = 413
MAX_API_CONTENT_LENGTH = 1024 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VALID_ABORT_TARGETS = frozenset({"tv", "library", "auto"})
_ALLOWED_SETTINGS_KEYS = frozenset(
    {"set_id", "random", "mix_art_store_favorites", "interval_minutes"}
)


def _ensure_csrf_token() -> str:
    token = session.get("csrf_token")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _require_non_empty_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _parse_json_object() -> tuple[dict[str, Any] | None, tuple[Any, int] | None]:
    raw = request.get_data(cache=False)
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, (
            jsonify(make_error_envelope("invalid JSON request body", "invalid_json")),
            HTTP_BAD_REQUEST,
        )
    if not isinstance(parsed, dict):
        return None, (
            jsonify(make_error_envelope("request body must be a JSON object", "invalid_request")),
            HTTP_BAD_REQUEST,
        )
    return parsed, None


def _has_active_operation(snapshot: dict[str, Any]) -> bool:
    for key in ("tv", "library"):
        section = snapshot.get(key)
        if isinstance(section, dict) and section.get("operation") is not None:
            return True
    return False


def _encode_snapshot(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _query_daemon_status(
    socket_path: Path,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        status_code, payload = request_json(socket_path, "GET", "/status", timeout=timeout)
    except IPCError as exc:
        return {"ok": False, "error": exc.message}, None
    if status_code >= 400:
        raw_error = payload.get("error")
        message = (
            raw_error
            if isinstance(raw_error, str) and raw_error
            else f"daemon returned HTTP {status_code}"
        )
        return {"ok": False, "error": message}, None
    return {"ok": True, "error": None}, payload


def _proxy_post(
    socket_path: Path,
    path: str,
    body: dict[str, Any],
    timeout: float,
) -> tuple[Any, int]:
    try:
        status_code, payload = request_json(
            socket_path,
            "POST",
            path,
            body=body,
            timeout=timeout,
        )
    except IPCError as exc:
        if exc.offline:
            return (
                jsonify(make_error_envelope(exc.message, "service_unavailable")),
                HTTP_SERVICE_UNAVAILABLE,
            )
        status = (
            exc.status_code
            if (exc.status_code is not None and exc.status_code >= 400)
            else HTTP_INTERNAL_ERROR
        )
        return jsonify(make_error_envelope(exc.message, exc.code)), status
    return jsonify(payload), status_code


def _register_api_error_handlers(app: Flask) -> None:
    def _handle_api_error(
        status_code: int,
        code: str,
        fallback_message: str,
        exc: Any,
    ) -> Any:
        if not request.path.startswith("/api/"):
            get_response = getattr(exc, "get_response", None)
            if callable(get_response):
                return get_response()
            return fallback_message, status_code
        description = getattr(exc, "description", None)
        message = (
            description
            if isinstance(description, str) and description
            else fallback_message
        )
        return jsonify(make_error_envelope(message, code)), status_code

    @app.errorhandler(HTTP_BAD_REQUEST)
    def handle_400(exc: Any) -> Any:
        return _handle_api_error(HTTP_BAD_REQUEST, "bad_request", "bad request", exc)

    @app.errorhandler(HTTP_NOT_FOUND)
    def handle_404(exc: Any) -> Any:
        return _handle_api_error(HTTP_NOT_FOUND, "not_found", "endpoint not found", exc)

    @app.errorhandler(HTTP_METHOD_NOT_ALLOWED)
    def handle_405(exc: Any) -> Any:
        return _handle_api_error(
            HTTP_METHOD_NOT_ALLOWED,
            "method_not_allowed",
            "method not allowed",
            exc,
        )

    @app.errorhandler(HTTP_PAYLOAD_TOO_LARGE)
    def handle_413(exc: Any) -> Any:
        return _handle_api_error(
            HTTP_PAYLOAD_TOO_LARGE,
            "payload_too_large",
            "request payload too large",
            exc,
        )

    @app.errorhandler(HTTP_INTERNAL_ERROR)
    def handle_500(exc: Any) -> Any:
        logger.exception("Unhandled error during request %s %s", request.method, request.path)
        return _handle_api_error(
            HTTP_INTERNAL_ERROR,
            "internal_error",
            "internal server error",
            exc,
        )


def create_app(
    config: Config,
    *,
    ipc_timeout: float = 2.0,
    check_timeout: float | None = None,
    sse_poll_interval: float = 1.0,
    sse_active_poll_interval: float = 0.4,
    sse_heartbeat_interval: float = 15.0,
    sse_stop_event: threading.Event | None = None,
    secret_key: bytes | None = None,
) -> Flask:
    """Create the Flask web server application backed by daemon Unix sockets."""
    package_dir = Path(__file__).resolve().parent
    app = Flask(
        __name__,
        template_folder=str(package_dir / "templates"),
        static_folder=str(package_dir / "static"),
    )
    app.secret_key = secret_key or secrets.token_bytes(32)
    app.config["MAX_CONTENT_LENGTH"] = MAX_API_CONTENT_LENGTH
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    effective_check_timeout = (
        check_timeout
        if check_timeout is not None
        else max(ipc_timeout, config.tv_timeout_seconds + 5.0)
    )
    stop_event = sse_stop_event or threading.Event()
    _register_api_error_handlers(app)

    def collect_status() -> dict[str, Any]:
        tv_health, tv_data = _query_daemon_status(config.tv_sock, ipc_timeout)
        library_health, library_data = _query_daemon_status(config.library_sock, ipc_timeout)
        degraded = not (tv_health["ok"] and library_health["ok"])
        return {
            "services": {
                "web": {"ok": True},
                "tv": tv_health,
                "library": library_health,
            },
            "degraded": degraded,
            "tv": tv_data,
            "library": library_data,
            "preview_seconds": config.preview_seconds,
            "max_set_images_warning": config.max_set_images_warning,
        }

    @app.before_request
    def verify_api_csrf() -> Any:
        if request.method == "POST" and request.path.startswith("/api/"):
            expected = session.get("csrf_token")
            provided = request.headers.get("X-CSRF-Token", "")
            if (
                not isinstance(expected, str)
                or not expected
                or not isinstance(provided, str)
                or not provided
                or not secrets.compare_digest(provided, expected)
            ):
                return (
                    jsonify(
                        make_error_envelope(
                            "invalid or missing CSRF token",
                            "invalid_csrf",
                        )
                    ),
                    HTTP_FORBIDDEN,
                )
        return None

    @app.get("/")
    def index() -> Response:
        csrf_token = _ensure_csrf_token()
        initial_state = collect_status()
        response = make_response(
            render_template(
                "index.html",
                csrf_token=csrf_token,
                initial_state=initial_state,
            )
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/thumbs/<sha256>.jpg")
    def thumbnail(sha256: str) -> Response:
        if _SHA256_RE.fullmatch(sha256) is None:
            abort(HTTP_NOT_FOUND)
        try:
            thumbs_dir = (config.cache_dir / "thumbs").resolve()
        except OSError:
            abort(HTTP_NOT_FOUND)
        raw_path = thumbs_dir / f"{sha256}.jpg"
        if raw_path.is_symlink():
            abort(HTTP_NOT_FOUND)
        try:
            candidate = raw_path.resolve()
        except OSError:
            abort(HTTP_NOT_FOUND)
        if candidate.parent != thumbs_dir or not candidate.is_file():
            abort(HTTP_NOT_FOUND)
        response = send_file(candidate, mimetype="image/jpeg")
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.get("/api/status")
    def api_status() -> tuple[Response, int]:
        response = jsonify(collect_status())
        response.headers["Cache-Control"] = "no-store"
        return response, HTTP_OK

    @app.get("/api/events")
    def api_events() -> Response:
        def event_stream() -> Iterator[str]:
            try:
                snapshot = collect_status()
                last_encoded = _encode_snapshot(snapshot)
                last_sent_at = time.monotonic()
                yield f"retry: 3000\nevent: status\ndata: {last_encoded}\n\n"

                while not stop_event.is_set():
                    interval = (
                        sse_active_poll_interval
                        if _has_active_operation(snapshot)
                        else sse_poll_interval
                    )
                    if stop_event.wait(interval):
                        break
                    snapshot = collect_status()
                    encoded = _encode_snapshot(snapshot)
                    now = time.monotonic()
                    if encoded != last_encoded:
                        last_encoded = encoded
                        last_sent_at = now
                        yield f"event: status\ndata: {encoded}\n\n"
                    elif now - last_sent_at >= sse_heartbeat_interval:
                        last_sent_at = now
                        yield ": keepalive\n\n"
            except (GeneratorExit, BrokenPipeError, ConnectionResetError):
                return

        return Response(
            event_stream(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/check-tv")
    def api_check_tv() -> tuple[Any, int]:
        payload, error_resp = _parse_json_object()
        if error_resp is not None:
            return error_resp
        assert payload is not None
        return _proxy_post(config.tv_sock, "/check", payload, effective_check_timeout)

    @app.post("/api/settings")
    def api_settings() -> tuple[Any, int]:
        payload, error_resp = _parse_json_object()
        if error_resp is not None:
            return error_resp
        assert payload is not None
        try:
            unknown = set(payload.keys()) - _ALLOWED_SETTINGS_KEYS
            if unknown:
                raise ValueError(
                    f"unknown settings fields: {', '.join(sorted(unknown))}"
                )
            set_id = _require_non_empty_str(payload.get("set_id"), "set_id")
            forward_body: dict[str, Any] = {"set_id": set_id}
            if "random" in payload:
                random_val = payload["random"]
                if not isinstance(random_val, bool):
                    raise ValueError("random must be a boolean")
                forward_body["random"] = random_val
            if "mix_art_store_favorites" in payload:
                mix_val = payload["mix_art_store_favorites"]
                if not isinstance(mix_val, bool):
                    raise ValueError("mix_art_store_favorites must be a boolean")
                forward_body["mix_art_store_favorites"] = mix_val
            if "interval_minutes" in payload:
                interval_val = payload["interval_minutes"]
                if (
                    isinstance(interval_val, bool)
                    or not isinstance(interval_val, int)
                    or interval_val < 1
                ):
                    raise ValueError("interval_minutes must be an integer >= 1")
                forward_body["interval_minutes"] = interval_val
        except ValueError as exc:
            return jsonify(make_error_envelope(str(exc), "invalid_request")), HTTP_BAD_REQUEST
        return _proxy_post(
            config.tv_sock,
            "/settings",
            forward_body,
            ipc_timeout,
        )

    @app.post("/api/sync")
    def api_sync() -> tuple[Any, int]:
        payload, error_resp = _parse_json_object()
        if error_resp is not None:
            return error_resp
        assert payload is not None
        try:
            set_id = _require_non_empty_str(payload.get("set_id"), "set_id")
            raw_settings = payload.get("settings")
            forward_body: dict[str, Any] = {"set_id": set_id}
            if raw_settings is not None:
                settings = SetSettings.from_dict(raw_settings)
                forward_body["settings"] = settings.to_dict()
        except ValueError as exc:
            return jsonify(make_error_envelope(str(exc), "invalid_request")), HTTP_BAD_REQUEST
        return _proxy_post(config.tv_sock, "/sync", forward_body, ipc_timeout)

    @app.post("/api/preview")
    def api_preview() -> tuple[Any, int]:
        payload, error_resp = _parse_json_object()
        if error_resp is not None:
            return error_resp
        assert payload is not None
        try:
            content_id = _require_non_empty_str(payload.get("content_id"), "content_id")
        except ValueError as exc:
            return jsonify(make_error_envelope(str(exc), "invalid_request")), HTTP_BAD_REQUEST
        return _proxy_post(
            config.tv_sock,
            "/preview",
            {"content_id": content_id},
            ipc_timeout,
        )

    @app.post("/api/library/refresh")
    def api_library_refresh() -> tuple[Any, int]:
        payload, error_resp = _parse_json_object()
        if error_resp is not None:
            return error_resp
        assert payload is not None
        forward_body: dict[str, Any] = {}
        if "set_id" in payload and payload["set_id"] is not None:
            try:
                set_id = _require_non_empty_str(payload["set_id"], "set_id")
            except ValueError as exc:
                return (
                    jsonify(make_error_envelope(str(exc), "invalid_request")),
                    HTTP_BAD_REQUEST,
                )
            forward_body["set_id"] = set_id
        return _proxy_post(config.library_sock, "/refresh", forward_body, ipc_timeout)

    @app.post("/api/abort")
    def api_abort() -> tuple[Any, int]:
        payload, error_resp = _parse_json_object()
        if error_resp is not None:
            return error_resp
        assert payload is not None
        target = payload.get("target", "auto")
        if target is None:
            target = "auto"
        if not isinstance(target, str) or target not in _VALID_ABORT_TARGETS:
            return (
                jsonify(
                    make_error_envelope(
                        "target must be one of 'tv', 'library', or 'auto'",
                        "invalid_request",
                    )
                ),
                HTTP_BAD_REQUEST,
            )

        if target == "tv":
            return _proxy_post(config.tv_sock, "/abort", {}, ipc_timeout)
        if target == "library":
            return _proxy_post(config.library_sock, "/abort", {}, ipc_timeout)

        # target == "auto": abort only active daemon(s); stay resilient if another is offline.
        tv_health, tv_data = _query_daemon_status(config.tv_sock, ipc_timeout)
        library_health, library_data = _query_daemon_status(config.library_sock, ipc_timeout)
        if not tv_health["ok"] and not library_health["ok"]:
            return (
                jsonify(
                    make_error_envelope(
                        "TV and library services are unavailable",
                        "service_unavailable",
                    )
                ),
                HTTP_SERVICE_UNAVAILABLE,
            )

        active_targets: list[tuple[str, Path]] = []
        if isinstance(tv_data, dict) and tv_data.get("operation") is not None:
            active_targets.append(("tv", config.tv_sock))
        if isinstance(library_data, dict) and library_data.get("operation") is not None:
            active_targets.append(("library", config.library_sock))

        if active_targets:
            if len(active_targets) == 1:
                return _proxy_post(active_targets[0][1], "/abort", {}, ipc_timeout)
            results: dict[str, Any] = {}
            any_aborted = False
            offline_messages: list[str] = []
            for name, sock in active_targets:
                try:
                    status_code, resp_data = request_json(
                        sock,
                        "POST",
                        "/abort",
                        body={},
                        timeout=ipc_timeout,
                    )
                except IPCError as exc:
                    if exc.offline:
                        offline_messages.append(exc.message)
                    results[name] = make_error_envelope(exc.message, exc.code)
                    continue
                results[name] = resp_data
                if 200 <= status_code < 300 and resp_data.get("aborted") is True:
                    any_aborted = True
            if len(offline_messages) == len(active_targets):
                return (
                    jsonify(
                        make_error_envelope(
                            "; ".join(offline_messages),
                            "service_unavailable",
                        )
                    ),
                    HTTP_SERVICE_UNAVAILABLE,
                )
            return jsonify({"ok": True, "aborted": any_aborted, "targets": results}), HTTP_OK

        # Neither has an active operation and at least one daemon is online.
        reachable: list[tuple[str, Path]] = []
        if tv_health["ok"]:
            reachable.append(("tv", config.tv_sock))
        if library_health["ok"]:
            reachable.append(("library", config.library_sock))

        idle_results: dict[str, Any] = {}
        any_aborted = False
        idle_offline_messages: list[str] = []
        for name, sock in reachable:
            try:
                status_code, resp_data = request_json(
                    sock,
                    "POST",
                    "/abort",
                    body={},
                    timeout=ipc_timeout,
                )
            except IPCError as exc:
                if exc.offline:
                    idle_offline_messages.append(exc.message)
                else:
                    idle_results[name] = make_error_envelope(exc.message, exc.code)
                continue
            idle_results[name] = resp_data
            if 200 <= status_code < 300 and resp_data.get("aborted") is True:
                any_aborted = True
        if reachable and len(idle_offline_messages) == len(reachable):
            return (
                jsonify(
                    make_error_envelope(
                        "; ".join(idle_offline_messages),
                        "service_unavailable",
                    )
                ),
                HTTP_SERVICE_UNAVAILABLE,
            )
        return jsonify({"ok": True, "aborted": any_aborted, "targets": idle_results}), HTTP_OK

    return app


def _build_arg_parser(default_config_path: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frame_art_mode.web_server",
        description="Run the Frame Art Mode web UI and SSE dashboard server.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("serve",),
        default="serve",
        help="Subcommand to execute (defaults to serve).",
    )
    parser.add_argument("--config", type=Path, default=default_config_path)
    parser.add_argument("--bind-ip")
    parser.add_argument("--port", type=int)
    parser.add_argument("--tv-ip")
    parser.add_argument("--image-root", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for the Frame Art Mode web server."""
    os.umask(0o077)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    script_dir = Path(__file__).resolve().parent.parent
    parser = _build_arg_parser(script_dir / "config.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    overrides = {
        "bind_ip": args.bind_ip,
        "port": args.port,
        "tv_ip": args.tv_ip,
        "image_root": args.image_root,
        "state_dir": args.state_dir,
        "cache_dir": args.cache_dir,
    }
    try:
        config = load_config(args.config, overrides, script_dir=script_dir)
    except ConfigError as exc:
        logger.error("Invalid configuration: %s", exc)
        return 2

    sse_stop_event = threading.Event()
    app = create_app(config, sse_stop_event=sse_stop_event)

    previous_handlers: dict[int, Any] = {}

    def _handle_shutdown_signal(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s, shutting down web server", signum)
        sse_stop_event.set()
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _handle_shutdown_signal)

    try:
        logger.info("Starting web server on %s:%s", config.bind_ip, config.port)
        app.run(
            host=config.bind_ip,
            port=config.port,
            threaded=True,
            use_reloader=False,
        )
    except KeyboardInterrupt:
        logger.info("Web server stopped")
    finally:
        sse_stop_event.set()
        for sig, prev in previous_handlers.items():
            signal.signal(sig, prev)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
