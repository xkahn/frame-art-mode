from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import queue
import random
import signal
import stat
import sys
import threading
import time
from typing import Any
import uuid

from . import ipc
from .config import Config, ConfigError, load_config
from .ipc import (
    HTTP_ACCEPTED,
    HTTP_BAD_REQUEST,
    HTTP_CONFLICT,
    HTTP_NOT_FOUND,
    HTTP_OK,
    UnixHTTPServer,
    make_error_envelope,
)
from .models import (
    ART_STORE_FAVORITES_CATEGORY,
    ART_STORE_ID_PREFIX,
    AbortRequested,
    CurrentDisplay,
    IPCError,
    Manifest,
    ManifestEntry,
    OperationResult,
    OperationState,
    PreparedImage,
    SetSettings,
    TVArtClient,
    TVServiceError,
    UploadError,
    atomic_write_json,
    close_tv_client,
    ensure_private_dir,
)

logger = logging.getLogger(__name__)

_LIBRARY_PREPARE_TIMEOUT_SECONDS = 120.0
_LIBRARY_RELEASE_TIMEOUT_SECONDS = 10.0
_DEFAULT_SYNC_POLL_SLICE_SECONDS = 60.0
_UNSET: Any = object()

LibraryCaller = Callable[[Path, str, str, dict[str, Any] | None, float], dict[str, Any]]


@dataclass(frozen=True)
class _DisplayCandidate:
    content_id: str
    category: str | None = None
    source_rel: str | None = None
    filename: str | None = None
    is_art_store: bool = False

    @property
    def label(self) -> str:
        return self.filename or self.source_rel or self.content_id


@dataclass
class _ActorTask:
    kind: str
    fn: Callable[[], Any]
    done_event: threading.Event
    result: Any = None
    error: BaseException | None = None


class _ConflictError(TVServiceError):
    """Raised when an IPC request conflicts with an active operation or manifest lock."""

    def __init__(self, message: str, code: str = "conflict") -> None:
        super().__init__(message)
        self.code = code


class _InvalidRequestError(TVServiceError):
    """Raised when an IPC request body is missing required fields or malformed."""

    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.code = code


class _NotFoundError(TVServiceError):
    """Raised when a requested resource (such as preview content_id) is not active."""

    def __init__(self, message: str, code: str = "not_found") -> None:
        super().__init__(message)
        self.code = code


def _is_timeout_exception(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or type(exc).__name__ in {
        "TimeoutError",
        "WebSocketTimeoutException",
    }


def _is_timeout_or_connection_error(exc: BaseException) -> bool:
    return isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {
        "TimeoutError",
        "WebSocketTimeoutException",
        "WebSocketConnectionClosedException",
        "ConnectionFailure",
    }


def _extract_art_error_request(data: dict[str, Any]) -> str:
    raw = data.get("request_data")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict) and parsed.get("request"):
            return str(parsed["request"])
    elif isinstance(raw, dict) and raw.get("request"):
        return str(raw["request"])
    return "art"


def _install_wait_deadline_guard(
    art: Any,
    *,
    timeout_seconds: float,
    should_abort: Callable[[], bool],
    clock: Callable[[], float],
) -> None:
    """Wrap SamsungTVArt.wait_for_response with a hard deadline and abort/EOF checks."""
    if getattr(art, "_wait_deadline_guarded", False):
        return
    original_wait = getattr(art, "wait_for_response", None)
    get_msg = getattr(art, "get_websocket_message", None)
    if not callable(original_wait) or not callable(get_msg):
        return

    def guarded_wait_for_response(
        wait_for_event: str | None,
        request_uuid: str | None = None,
        or_no_id: bool = False,
    ) -> dict[str, Any] | None:
        deadline = clock() + float(timeout_seconds)
        conn = getattr(art, "connection", None)
        has_original_timeout = False
        original_timeout: float | None = None
        if conn is not None and hasattr(conn, "gettimeout"):
            try:
                original_timeout = conn.gettimeout()
                has_original_timeout = True
            except Exception:
                has_original_timeout = False

        empty_count = 0
        try:
            while True:
                if should_abort() and wait_for_event != "image_added":
                    raise AbortRequested("operation aborted by user")
                remaining = deadline - clock()
                if remaining <= 0:
                    raise TimeoutError(f"TV response timed out after {timeout_seconds:.1f}s")

                conn = getattr(art, "connection", None)
                if conn is not None and getattr(conn, "connected", True) is False:
                    raise ConnectionError("TV WebSocket connection closed")
                if conn is not None and hasattr(conn, "settimeout"):
                    try:
                        conn.settimeout(min(remaining, 1.0))
                    except Exception as exc:
                        raise ConnectionError(f"TV WebSocket connection unavailable: {exc}") from exc

                try:
                    data = get_msg()
                except Exception as exc:
                    if _is_timeout_exception(exc):
                        if should_abort() and wait_for_event != "image_added":
                            raise AbortRequested("operation aborted by user") from exc
                        if clock() >= deadline:
                            raise TimeoutError(
                                f"TV response timed out after {timeout_seconds:.1f}s"
                            ) from exc
                        continue
                    raise

                if not isinstance(data, dict) or not data:
                    empty_count += 1
                    conn = getattr(art, "connection", None)
                    if (
                        conn is not None and getattr(conn, "connected", True) is False
                    ) or empty_count >= 50:
                        raise ConnectionError("TV WebSocket connection closed")
                    continue

                empty_count = 0
                message_uuid = data.get("request_id", data.get("id"))
                if message_uuid == request_uuid or (or_no_id and message_uuid is None):
                    sub_event = data.get("event", "*")
                    if sub_event == "error":
                        request_name = _extract_art_error_request(data)
                        raise TVServiceError(
                            f"{request_name} request failed with error number "
                            f"{data.get('error_code')}"
                        )
                    if not wait_for_event or sub_event == wait_for_event:
                        return data
        finally:
            if has_original_timeout and conn is not None and hasattr(conn, "settimeout"):
                try:
                    conn.settimeout(original_timeout)
                except Exception:
                    pass

    art.wait_for_response = guarded_wait_for_response
    art._wait_deadline_guarded = True


def default_tv_factory(config: Config, token_file: Path) -> TVArtClient:
    """Create a fresh Samsung Frame TV art client for a single TV operation."""
    from samsungframetv import SamsungTVWS

    return SamsungTVWS(
        config.tv_ip,
        token_file=str(token_file),
        timeout=config.tv_timeout_seconds,
    ).art(timeout=config.tv_timeout_seconds)


def _default_library_call(
    socket_path: Path,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    return ipc.call_json(socket_path, method, path, body=body, timeout=timeout)


class TVDaemon:
    """Single-lane serialized TV actor owning manifest, operation status, sync, and playback."""

    def __init__(
        self,
        config: Config,
        tv_factory: Callable[[Config, Path], TVArtClient] = default_tv_factory,
        library_caller: Callable[..., dict[str, Any]] = _default_library_call,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        chooser: Callable[[Sequence[Any]], Any] = random.choice,
    ) -> None:
        self.config = config
        self._tv_factory = tv_factory
        self._library_caller = library_caller
        self._sleeper = sleeper
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._chooser = chooser

        ensure_private_dir(self.config.state_dir)
        ensure_private_dir(self.config.tv_dir)

        self.lock_path = self.config.tv_lock
        self._lock_file = self.lock_path.open("a+", encoding="utf-8")
        lock_acquired = False
        try:
            os.chmod(self.lock_path, 0o600)
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TVServiceError(
                    f"another tv_daemon instance is already managing TV {self.config.tv_ip}"
                ) from exc
            lock_acquired = True

            self.manifest_path = self.config.tv_dir / "manifest.json"
            self.operation_path = self.config.tv_dir / "operation.json"
            self.token_file = self.config.tv_dir / "tv-token.txt"
            self._ensure_token_file_permissions()

            self._state_lock = threading.RLock()
            self._started_at = self._wall_clock_iso()

            self._stopping = False
            self._stop_event = threading.Event()
            self._abort_event = threading.Event()
            self._sleep_wake_event = threading.Event()
            self._actor_wake_event = threading.Event()
            self._task_queue: queue.Queue[_ActorTask] = queue.Queue()
            self._actor_thread: threading.Thread | None = None
            self._actor_busy = False

            self._tv_ready: bool | None = None
            self._tv_checked_at: str | None = None
            self._last_error: str | None = None
            self._warnings: list[str] = []
            self._drift_warnings: list[str] = []
            self._manifest_error: str | None = None
            self._manifest_persisted = self.manifest_path.exists()
            self._manifest = self._load_manifest()

            self._current_op: OperationState | None = None
            self._last_result: OperationResult | None = None
            self._load_operation()
            self._recover_interrupted_operation()

            self._current_display = self._initial_current_display()
            self._preview_active = False
            self._restoring_preview = False
            self._preview_content_id: str | None = None
            self._preview_expires_at: str | None = None

            self._cycling_paused = False
            self._paused_remaining: float | None = None
            self._next_at_mono: float | None = None
            self._next_rotation_at: str | None = None
            if (
                self._manifest_error is None
                and self._manifest.active_set is not None
                and self._manifest.entries
            ):
                interval_seconds = self._manifest.settings_for(
                    self._manifest.active_set
                ).interval_minutes * 60.0
                self._schedule_next_rotation(interval_seconds)
        except Exception:
            if not self._lock_file.closed:
                if lock_acquired:
                    try:
                        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                self._lock_file.close()
            raise

    def _wall_clock_iso(self, offset_seconds: float = 0.0) -> str:
        now = self._wall_clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if offset_seconds:
            now = now + timedelta(seconds=offset_seconds)
        return now.astimezone(timezone.utc).isoformat(timespec="seconds")

    def _schedule_next_rotation(self, delay_seconds: float) -> None:
        delay = max(0.0, float(delay_seconds))
        self._next_at_mono = self._clock() + delay
        self._next_rotation_at = self._wall_clock_iso(offset_seconds=delay)

    def _initial_current_display(self) -> CurrentDisplay:
        cid = self._manifest.current_content_id if self._manifest_error is None else None
        if not cid:
            return CurrentDisplay()
        for entry in self._manifest.entries:
            if entry.content_id == cid:
                return CurrentDisplay(
                    content_id=cid,
                    category=None,
                    source_rel=entry.source_rel,
                    is_art_store=False,
                )
        is_store = cid.startswith(ART_STORE_ID_PREFIX)
        return CurrentDisplay(
            content_id=cid,
            category=ART_STORE_FAVORITES_CATEGORY if is_store else None,
            source_rel=None,
            is_art_store=is_store,
        )

    def start(self) -> None:
        """Start the single-lane TV actor thread if not already running."""
        with self._state_lock:
            if self._stopping:
                raise TVServiceError("TV daemon is stopping")
            if self._actor_thread is not None and self._actor_thread.is_alive():
                return
            self._stop_event.clear()
            self._actor_wake_event.clear()
            self._sleep_wake_event.clear()
            self._actor_thread = threading.Thread(
                target=self._actor_loop,
                name="frame-art-tv-actor",
                daemon=True,
            )
            self._actor_thread.start()

    def stop(self) -> None:
        """Stop the TV actor cleanly, wake any interruptible wait, and release tv.lock."""
        with self._state_lock:
            self._stopping = True
            self._stop_event.set()
            self._sleep_wake_event.set()
            self._actor_wake_event.set()
            thread = self._actor_thread
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=35.0)
        if not self._lock_file.closed:
            if thread is None or not thread.is_alive():
                try:
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                self._lock_file.close()

    def _ensure_token_file_permissions(self) -> None:
        """Ensure tv-token.txt uses 0o600 permissions whenever it exists."""
        try:
            if self.token_file.exists() and not self.token_file.is_symlink():
                if stat.S_IMODE(self.token_file.stat().st_mode) != 0o600:
                    os.chmod(self.token_file, 0o600)
        except OSError as exc:
            logger.debug("Could not enforce permissions on %s: %s", self.token_file, exc)

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """Wait until the TV actor is idle and no operation is active (test helper)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._state_lock:
                if (
                    self._current_op is None
                    and not self._actor_busy
                    and self._task_queue.empty()
                ):
                    return True
            time.sleep(0.01)
        return False

    def health(self) -> dict[str, Any]:
        """Return lightweight daemon liveness metadata without touching the TV."""
        return {
            "ok": True,
            "pid": os.getpid(),
            "started_at": self._started_at,
            "tv_key": self.config.tv_key,
        }

    def status(self) -> dict[str, Any]:
        """Return a snapshot of TV daemon state without performing any TV I/O."""
        with self._state_lock:
            manifest = self._manifest
            return {
                "tv_ready": self._tv_ready,
                "tv_checked_at": self._tv_checked_at,
                "active_set": None if self._manifest_error else manifest.active_set,
                "entries": (
                    []
                    if self._manifest_error
                    else [entry.to_dict() for entry in manifest.entries]
                ),
                "current_display": self._current_display.to_dict(),
                "next_rotation_at": None if self._cycling_paused else self._next_rotation_at,
                "cycling_paused": self._cycling_paused,
                "preview": {
                    "active": self._preview_active,
                    "content_id": self._preview_content_id,
                    "expires_at": self._preview_expires_at,
                },
                "cleanup_pending": (
                    [] if self._manifest_error else list(manifest.cleanup_pending)
                ),
                "settings": (
                    {}
                    if self._manifest_error
                    else {set_id: cfg.to_dict() for set_id, cfg in manifest.settings.items()}
                ),
                "operation": (
                    self._current_op.to_public_dict()
                    if self._current_op is not None
                    else None
                ),
                "last_result": (
                    self._last_result.to_public_dict()
                    if self._last_result is not None
                    else None
                ),
                "warnings": list(self._warnings),
                "drift_warnings": list(self._drift_warnings),
                "manifest_error": self._manifest_error,
                "last_error": self._last_error or self._manifest_error,
            }

    # ------------------------------------------------------------------
    # Manifest & Operation Persistence
    # ------------------------------------------------------------------

    def _load_manifest(self) -> Manifest:
        if not self.manifest_path.exists():
            self._manifest_error = None
            return Manifest(tv_ip=self.config.tv_ip)
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest = Manifest.from_dict(raw, expected_tv_ip=self.config.tv_ip)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self._manifest_error = (
                f"ownership manifest is unreadable; no TV items will be deleted: {exc}"
            )
            logger.error("Failed to load manifest %s: %s", self.manifest_path, exc)
            return Manifest(tv_ip=self.config.tv_ip)
        self._manifest_error = None
        return manifest

    def _assert_manifest_current(self) -> None:
        if self._manifest_error:
            raise TVServiceError(self._manifest_error)
        if self._manifest_persisted and not self.manifest_path.exists():
            self._manifest_error = "ownership manifest disappeared; no TV items will be deleted"
            raise TVServiceError(self._manifest_error)
        if not self._manifest_persisted and self.manifest_path.exists():
            self._manifest_error = (
                "ownership manifest appeared unexpectedly; restart after review"
            )
            raise TVServiceError(self._manifest_error)
        if self._manifest_persisted:
            try:
                raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                disk_manifest = Manifest.from_dict(raw, expected_tv_ip=self.config.tv_ip)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                self._manifest_error = (
                    "ownership manifest became unreadable; no TV items will be deleted: "
                    f"{exc}"
                )
                raise TVServiceError(self._manifest_error) from exc
            if disk_manifest != self._manifest:
                self._manifest_error = (
                    "ownership manifest changed while the service was running; "
                    "restart after review"
                )
                raise TVServiceError(self._manifest_error)

    def _write_manifest(self, manifest: Manifest) -> None:
        self._assert_manifest_current()
        atomic_write_json(self.manifest_path, manifest.to_dict(), mode=0o600, fsync=True)
        self._manifest = manifest
        self._manifest_persisted = True
        self._manifest_error = None

    def _load_operation(self) -> None:
        if not self.operation_path.exists():
            self._current_op = None
            self._last_result = None
            return
        try:
            raw = json.loads(self.operation_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("operation file must be a JSON object")
            raw_current = raw.get("current")
            raw_last = raw.get("last_result")
            current = OperationState.from_dict(raw_current) if raw_current is not None else None
            last_result = OperationResult.from_dict(raw_last) if raw_last is not None else None
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise TVServiceError(f"TV operation status is unreadable: {exc}") from exc
        self._current_op = current
        self._last_result = last_result

    def _persist_operation(self) -> None:
        payload = {
            "current": self._current_op.to_dict() if self._current_op is not None else None,
            "last_result": (
                self._last_result.to_dict() if self._last_result is not None else None
            ),
        }
        atomic_write_json(self.operation_path, payload, mode=0o600, fsync=True)

    def _recover_interrupted_operation(self) -> None:
        current = self._current_op
        last_result = self._last_result
        if current is None and not (last_result and last_result.recovery_pending):
            return

        kind = current.kind if current is not None else last_result.kind  # type: ignore[union-attr]
        prior_recovery = list(last_result.recovery_pending) if last_result is not None else []
        current_uploads = list(current.known_uploaded) if (current and kind == "sync") else []
        known_uploads = list(dict.fromkeys(prior_recovery + current_uploads))

        recovery_pending: tuple[str, ...] = ()
        if known_uploads and self._manifest_error is None:
            active_ids = {entry.content_id for entry in self._manifest.entries}
            new_cleanup = tuple(
                dict.fromkeys(
                    list(self._manifest.cleanup_pending)
                    + [cid for cid in known_uploads if cid not in active_ids]
                )
            )
            if new_cleanup != self._manifest.cleanup_pending:
                updated_manifest = replace(self._manifest, cleanup_pending=new_cleanup)
                self._write_manifest(updated_manifest)
            if current is None and last_result is not None and last_result.recovery_pending:
                self._last_result = OperationResult(
                    kind=last_result.kind,
                    state=last_result.state,
                    message=last_result.message,
                    timestamp=last_result.timestamp,
                    recovery_pending=(),
                )
                self._persist_operation()
        elif known_uploads:
            recovery_pending = tuple(known_uploads)

        if current is not None:
            message = f"{kind.capitalize()} was interrupted by a service restart."
            if kind == "sync":
                if self._manifest_error and recovery_pending:
                    message += (
                        " The ownership manifest is unreadable, so known partial uploads "
                        "were retained for recovery; no TV items will be deleted."
                    )
                else:
                    message += (
                        " The last committed active set was left unchanged by recovery; "
                        "known partial uploads are cleanup-pending and will be reconciled "
                        "on the next explicit Sync."
                    )
            self._current_op = None
            self._last_result = OperationResult(
                kind=kind,
                state="interrupted",
                message=message,
                timestamp=self._wall_clock_iso(),
                recovery_pending=recovery_pending,
            )
            self._persist_operation()

    def _begin_operation(
        self,
        kind: str,
        phase: str,
        message: str,
        *,
        current_item: str | None = None,
        total_ops: int = 0,
    ) -> OperationState:
        with self._state_lock:
            if self._current_op is not None:
                raise _ConflictError(
                    f"another TV operation ({self._current_op.kind}) is already in progress"
                )
            self._abort_event.clear()
            self._sleep_wake_event.clear()
            op = OperationState(
                kind=kind,
                phase=phase,
                message=message,
                current_item=current_item,
                completed_ops=0,
                total_ops=total_ops,
                abort_requested=False,
                started_at=self._wall_clock_iso(),
                known_uploaded=(),
            )
            self._current_op = op
            self._persist_operation()
            return op

    def _update_operation(
        self,
        *,
        phase: str | None = None,
        message: str | None = None,
        current_item: str | None = ...,  # type: ignore[assignment]
        completed_ops: int | None = None,
        total_ops: int | None = None,
    ) -> None:
        with self._state_lock:
            if self._current_op is None:
                return
            op = self._current_op
            new_op = OperationState(
                kind=op.kind,
                phase=phase if phase is not None else op.phase,
                message=message if message is not None else op.message,
                current_item=op.current_item if current_item is ... else current_item,
                completed_ops=(
                    completed_ops if completed_ops is not None else op.completed_ops
                ),
                total_ops=total_ops if total_ops is not None else op.total_ops,
                abort_requested=op.abort_requested or self._abort_event.is_set(),
                started_at=op.started_at,
                known_uploaded=op.known_uploaded,
            )
            self._current_op = new_op
            self._persist_operation()

    def _remember_uploaded(self, content_id: str) -> None:
        with self._state_lock:
            op = self._current_op
            if op is None or op.kind != "sync":
                return
            if content_id in op.known_uploaded:
                return
            updated = replace(op, known_uploaded=op.known_uploaded + (content_id,))
            self._current_op = updated
            try:
                self._persist_operation()
            except Exception as exc:
                self._current_op = op
                raise UploadError(
                    f"could not persist the ID of uploaded item {content_id}: {exc}",
                    content_id,
                ) from exc

    def _finish_operation(
        self,
        state: str,
        message: str,
        *,
        kind: str | None = None,
        preserve_previous_result: bool = False,
        recovery_pending: tuple[str, ...] = (),
    ) -> None:
        with self._state_lock:
            op = self._current_op
            if op is None or (kind is not None and op.kind != kind):
                return
            result = self._last_result
            if not preserve_previous_result or result is None:
                result = OperationResult(
                    kind=op.kind,
                    state=state,
                    message=message,
                    timestamp=self._wall_clock_iso(),
                    recovery_pending=recovery_pending,
                )
            self._current_op = None
            self._last_result = result
            self._persist_operation()

    # ------------------------------------------------------------------
    # Interruptible Waits & Abort Checks
    # ------------------------------------------------------------------

    def _is_abort_requested(self) -> bool:
        if self._stop_event.is_set() or self._abort_event.is_set():
            return True
        with self._state_lock:
            return self._current_op is not None and self._current_op.abort_requested

    def _check_abort(self) -> None:
        if self._stop_event.is_set():
            raise AbortRequested("TV daemon is shutting down")
        if self._is_abort_requested():
            raise AbortRequested("operation aborted by user")

    def _interruptible_wait(self, seconds: float) -> None:
        self._check_abort()
        if seconds <= 0:
            return
        if self._sleeper is not None:
            self._sleeper(seconds)
            self._check_abort()
            return
        deadline = self._clock() + seconds
        while True:
            self._check_abort()
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            self._sleep_wake_event.wait(timeout=remaining)

    # ------------------------------------------------------------------
    # TV Helpers
    # ------------------------------------------------------------------

    def _should_abort_tv_wait(self) -> bool:
        if self._stop_event.is_set():
            return True
        if self._restoring_preview:
            return False
        return self._abort_event.is_set()

    def _open_tv(self) -> TVArtClient:
        art = self._tv_factory(self.config, self.token_file)
        _install_wait_deadline_guard(
            art,
            timeout_seconds=self.config.tv_timeout_seconds,
            should_abort=self._should_abort_tv_wait,
            clock=self._clock,
        )
        return art

    @staticmethod
    def _is_art_mode(value: Any) -> bool:
        return value is True or str(value).strip().casefold() in {"on", "true", "1"}

    def _check_ready(self, art: TVArtClient) -> None:
        try:
            ready = self._is_art_mode(art.get_artmode())
        except AbortRequested:
            raise
        except Exception as exc:
            with self._state_lock:
                self._tv_ready = False
                self._tv_checked_at = self._wall_clock_iso()
            raise TVServiceError(
                f"TV is unavailable or its Art Mode status could not be read: {exc}"
            ) from exc
        with self._state_lock:
            self._tv_ready = ready
            self._tv_checked_at = self._wall_clock_iso()
        if not ready:
            raise TVServiceError("TV is not in Art Mode; no sync or display change was made")

    @staticmethod
    def _content_ids(available: Any) -> set[str]:
        if not isinstance(available, list):
            raise TVServiceError("TV returned an invalid available-art list")
        return {
            str(item["content_id"])
            for item in available
            if isinstance(item, dict) and item.get("content_id")
        }

    @staticmethod
    def _extract_current_id(response: Any) -> str | None:
        if not isinstance(response, dict):
            return None
        for key in ("content_id", "contentId"):
            if response.get(key):
                return str(response[key])
        content = response.get("content")
        if isinstance(content, dict):
            for key in ("content_id", "contentId"):
                if content.get(key):
                    return str(content[key])
        return None

    @staticmethod
    def _extract_current_category(response: Any) -> str | None:
        if not isinstance(response, dict):
            return None
        for container in (response, response.get("content")):
            if isinstance(container, dict):
                for key in ("category", "category_id", "categoryId"):
                    if container.get(key):
                        return str(container[key])
        return None

    def _display_from_tv_current(self, response: Any) -> CurrentDisplay | None:
        cid = self._extract_current_id(response)
        if cid is None:
            return None
        cat = self._extract_current_category(response)
        source_rel: str | None = None
        with self._state_lock:
            for entry in self._manifest.entries:
                if entry.content_id == cid:
                    source_rel = entry.source_rel
                    break
        is_art_store = source_rel is None and (
            cat == ART_STORE_FAVORITES_CATEGORY or cid.startswith(ART_STORE_ID_PREFIX)
        )
        return CurrentDisplay(
            content_id=cid,
            category=cat,
            source_rel=source_rel,
            is_art_store=is_art_store,
        )

    def _select_candidate(self, art: TVArtClient, candidate: _DisplayCandidate) -> None:
        if candidate.category:
            art.select_image(candidate.content_id, category=candidate.category)
        else:
            art.select_image(candidate.content_id)

    def _select_raw(
        self,
        art: TVArtClient,
        content_id: str,
        category: str | None,
    ) -> None:
        if category:
            art.select_image(content_id, category=category)
        else:
            art.select_image(content_id)

    def _set_favorites_warning(self, warning: str) -> None:
        with self._state_lock:
            self._warnings = [
                item for item in self._warnings if not item.startswith("Art Store favorites:")
            ]
            self._warnings.append(f"Art Store favorites: {warning}")

    def _clear_favorites_warnings(self) -> None:
        with self._state_lock:
            self._warnings = [
                item for item in self._warnings if not item.startswith("Art Store favorites:")
            ]

    def _add_drift_warning(self, warning: str) -> None:
        with self._state_lock:
            if warning not in self._drift_warnings:
                self._drift_warnings.append(warning)

    def _resume_cycling_if_paused(self) -> None:
        with self._state_lock:
            if self._last_error and (
                self._last_error.startswith("cycling paused:")
                or self._last_error.startswith("TV is unavailable")
                or self._last_error.startswith("TV is not in Art Mode")
                or self._last_error.startswith("preview restore failed:")
                or self._last_error.startswith("preview ")
            ):
                self._last_error = None
            if not self._cycling_paused:
                return
            self._cycling_paused = False
            if (
                self._manifest_error is None
                and self._manifest.active_set is not None
                and self._manifest.entries
            ):
                delay = self._paused_remaining
                if delay is None:
                    delay = (
                        self._manifest.settings_for(
                            self._manifest.active_set
                        ).interval_minutes
                        * 60.0
                    )
                self._paused_remaining = None
                self._schedule_next_rotation(delay)
            else:
                self._paused_remaining = None

    def _pause_cycling(self, reason: str) -> None:
        with self._state_lock:
            if self._manifest.active_set and self._manifest.entries:
                if self._next_at_mono is not None and self._paused_remaining is None:
                    self._paused_remaining = max(0.0, self._next_at_mono - self._clock())
                self._cycling_paused = True
            self._last_error = reason

    def _candidates_for_random_pick(
        self,
        art: TVArtClient,
        entries: Sequence[ManifestEntry],
        mix_art_store_favorites: bool,
    ) -> list[_DisplayCandidate]:
        candidates = [
            _DisplayCandidate(
                content_id=entry.content_id,
                category=None,
                source_rel=entry.source_rel,
                filename=entry.filename,
                is_art_store=False,
            )
            for entry in entries
        ]
        seen_ids = {entry.content_id for entry in entries}
        if not mix_art_store_favorites:
            return candidates

        self._update_operation(
            phase="querying-favorites",
            message="Querying Art Store favorites",
        )
        try:
            available = art.available(ART_STORE_FAVORITES_CATEGORY)
            if not isinstance(available, list):
                raise TVServiceError("TV returned an invalid favorites list")
        except AbortRequested:
            raise
        except Exception as exc:
            self._set_favorites_warning(
                f"could not read favorites; using NAS images this turn: {exc}"
            )
            return candidates

        self._clear_favorites_warnings()
        unknown_ids: list[str] = []
        for item in available:
            if not isinstance(item, dict) or not item.get("content_id"):
                continue
            cid = str(item["content_id"])
            if cid in seen_ids:
                continue
            if not cid.startswith(ART_STORE_ID_PREFIX):
                unknown_ids.append(cid)
                continue
            candidates.append(
                _DisplayCandidate(
                    content_id=cid,
                    category=ART_STORE_FAVORITES_CATEGORY,
                    source_rel=None,
                    filename=None,
                    is_art_store=True,
                )
            )
            seen_ids.add(cid)
        if unknown_ids:
            self._set_favorites_warning(
                "skipping IDs without the documented SAM- prefix: "
                + ", ".join(unknown_ids)
            )
        return candidates

    def _random_pick(
        self,
        art: TVArtClient,
        entries: Sequence[ManifestEntry],
        mix_art_store_favorites: bool,
    ) -> _DisplayCandidate:
        candidates = self._candidates_for_random_pick(
            art,
            entries,
            mix_art_store_favorites,
        )
        return self._chooser(candidates)

    # ------------------------------------------------------------------
    # Single-Lane Actor Scheduling
    # ------------------------------------------------------------------

    def _ensure_actor_started(self) -> None:
        with self._state_lock:
            if self._stopping or self._stop_event.is_set():
                raise TVServiceError("TV daemon is stopping")
            if self._actor_thread is None or not self._actor_thread.is_alive():
                self.start()

    def _submit_actor_task(
        self,
        kind: str,
        fn: Callable[[], Any],
        *,
        wait: bool,
    ) -> _ActorTask:
        with self._state_lock:
            if self._stopping or self._stop_event.is_set():
                raise TVServiceError("TV daemon is stopping")
            self._ensure_actor_started()
            task = _ActorTask(kind=kind, fn=fn, done_event=threading.Event())
            self._task_queue.put(task)
            self._actor_wake_event.set()
        if wait:
            task.done_event.wait()
            if task.error is not None:
                raise task.error
        return task

    def _fail_queued_tasks_on_stop(self) -> None:
        with self._state_lock:
            while True:
                try:
                    task = self._task_queue.get_nowait()
                except queue.Empty:
                    break
                task.error = TVServiceError("TV daemon is stopping")
                task.done_event.set()

    def _next_idle_wait_seconds(self) -> float | None:
        with self._state_lock:
            if (
                self._cycling_paused
                or self._manifest_error is not None
                or self._manifest.active_set is None
                or not self._manifest.entries
                or self._next_at_mono is None
            ):
                return None
            remaining = self._next_at_mono - self._clock()
            if remaining <= 0:
                return 0.0
            return min(remaining, _DEFAULT_SYNC_POLL_SLICE_SECONDS)

    def _actor_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    task = self._task_queue.get_nowait()
                except queue.Empty:
                    task = None

                if task is not None:
                    with self._state_lock:
                        self._actor_busy = True
                    try:
                        task.result = task.fn()
                    except BaseException as exc:  # noqa: BLE001 - propagated to waiter
                        task.error = exc
                    finally:
                        with self._state_lock:
                            self._actor_busy = False
                        task.done_event.set()
                    continue

                wait_seconds = self._next_idle_wait_seconds()
                if wait_seconds is None:
                    self._actor_wake_event.wait()
                    self._actor_wake_event.clear()
                    continue
                if wait_seconds <= 0:
                    with self._state_lock:
                        self._actor_busy = True
                    try:
                        self.run_slideshow_tick()
                    except Exception:
                        logger.exception("Unhandled error during slideshow tick")
                    finally:
                        with self._state_lock:
                            self._actor_busy = False
                    continue

                self._actor_wake_event.wait(timeout=wait_seconds)
                self._actor_wake_event.clear()
        finally:
            self._fail_queued_tasks_on_stop()

    # ------------------------------------------------------------------
    # Public Operations (IPC + Domain API)
    # ------------------------------------------------------------------

    def check_tv(self) -> dict[str, Any]:
        """Run an explicit one-shot TV reachability and Art Mode check on the actor lane."""
        with self._state_lock:
            if self._stopping or self._stop_event.is_set():
                raise TVServiceError("TV daemon is stopping")
            if self._current_op is not None or self._actor_busy:
                raise _ConflictError("another TV operation is already in progress")
            self._begin_operation(
                "reachability",
                "checking-tv",
                "Checking TV reachability",
                total_ops=1,
            )

        def _run() -> dict[str, Any]:
            return self._execute_check()

        try:
            return self._submit_actor_task("reachability", _run, wait=True).result
        except Exception as exc:
            self._finish_operation(
                "interrupted",
                str(exc),
                kind="reachability",
                preserve_previous_result=True,
            )
            raise

    def _execute_check(self) -> dict[str, Any]:
        art: TVArtClient | None = None
        error_msg: str | None = None
        try:
            art = self._open_tv()
            self._check_ready(art)
            try:
                current_raw = art.get_current()
            except AbortRequested:
                raise
            except Exception as exc:
                logger.debug("Ignoring get_current() failure during check: %s", exc)
            else:
                updated_display = self._display_from_tv_current(current_raw)
                if updated_display is not None:
                    with self._state_lock:
                        self._current_display = updated_display
            self._resume_cycling_if_paused()
            self._update_operation(
                phase="checking-tv",
                message="TV is reachable and in Art Mode",
                completed_ops=1,
                total_ops=1,
            )
            self._finish_operation(
                "completed",
                "TV is reachable and in Art Mode",
                kind="reachability",
                preserve_previous_result=True,
            )
        except AbortRequested as exc:
            error_msg = str(exc) or "Check TV aborted"
            self._finish_operation(
                "aborted",
                error_msg,
                kind="reachability",
                preserve_previous_result=True,
            )
        except Exception as exc:
            error_msg = str(exc)
            with self._state_lock:
                self._tv_ready = False
                self._tv_checked_at = self._wall_clock_iso()
                self._last_error = error_msg
            self._finish_operation(
                "failed",
                error_msg,
                kind="reachability",
                preserve_previous_result=True,
            )
        finally:
            close_tv_client(art)
            self._ensure_token_file_permissions()

        with self._state_lock:
            return {
                "ok": True,
                "tv_ready": bool(self._tv_ready),
                "current_display": self._current_display.to_dict(),
                "error": error_msg,
            }

    def save_settings(
        self,
        set_id: str,
        *,
        random: Any = _UNSET,
        mix_art_store_favorites: Any = _UNSET,
        interval_minutes: Any = _UNSET,
    ) -> dict[str, Any]:
        """Validate and atomically persist per-set playback settings."""
        if not isinstance(set_id, str) or not set_id.strip():
            raise _InvalidRequestError("set_id must be a non-empty string")
        if (
            random is _UNSET
            and mix_art_store_favorites is _UNSET
            and interval_minutes is _UNSET
        ):
            raise _InvalidRequestError("at least one setting field must be provided")

        with self._state_lock:
            if self._current_op is not None and self._current_op.kind == "sync":
                raise _ConflictError(
                    "settings cannot be changed while Sync to TV is running"
                )
            if self._manifest_error:
                raise _ConflictError(self._manifest_error, code="manifest_error")
            try:
                self._assert_manifest_current()
            except TVServiceError as exc:
                raise _ConflictError(str(exc), code="manifest_error") from exc
            existing = self._manifest.settings_for(set_id)
            try:
                new_settings = SetSettings(
                    random=existing.random if random is _UNSET else random,
                    mix_art_store_favorites=(
                        existing.mix_art_store_favorites
                        if mix_art_store_favorites is _UNSET
                        else mix_art_store_favorites
                    ),
                    interval_minutes=(
                        existing.interval_minutes
                        if interval_minutes is _UNSET
                        else interval_minutes
                    ),
                )
            except ValueError as exc:
                raise _InvalidRequestError(str(exc)) from exc

            settings_map = dict(self._manifest.settings)
            settings_map[set_id] = new_settings
            updated_manifest = replace(self._manifest, settings=settings_map)
            self._write_manifest(updated_manifest)
            if (
                set_id == updated_manifest.active_set
                and updated_manifest.entries
                and not self._cycling_paused
            ):
                self._schedule_next_rotation(new_settings.interval_minutes * 60.0)
                self._actor_wake_event.set()
            return {
                "ok": True,
                "set_id": set_id,
                "settings": new_settings.to_dict(),
            }

    def start_sync(
        self,
        set_id: str,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate inputs, begin durable sync state, and enqueue background sync."""
        if not isinstance(set_id, str) or not set_id.strip():
            raise _InvalidRequestError("set_id must be a non-empty string")
        if settings is not None:
            if not isinstance(settings, Mapping):
                raise _InvalidRequestError("settings must be a mapping")
            allowed_setting_keys = {"random", "mix_art_store_favorites", "interval_minutes"}
            unknown = set(settings.keys()) - allowed_setting_keys
            if unknown:
                raise _InvalidRequestError(
                    f"unknown settings fields: {', '.join(sorted(unknown))}"
                )
            try:
                SetSettings.from_dict(settings)
            except ValueError as exc:
                raise _InvalidRequestError(str(exc)) from exc

        with self._state_lock:
            if self._stopping or self._stop_event.is_set():
                raise TVServiceError("TV daemon is stopping")
            if self._current_op is not None or self._actor_busy:
                raise _ConflictError("another TV operation is already in progress")
            if self._manifest_error:
                raise _ConflictError(self._manifest_error, code="manifest_error")
            try:
                self._assert_manifest_current()
            except TVServiceError as exc:
                raise _ConflictError(str(exc), code="manifest_error") from exc
            if settings is not None:
                existing = self._manifest.settings_for(set_id)
                try:
                    merged_settings = SetSettings(
                        random=settings["random"] if "random" in settings else existing.random,
                        mix_art_store_favorites=(
                            settings["mix_art_store_favorites"]
                            if "mix_art_store_favorites" in settings
                            else existing.mix_art_store_favorites
                        ),
                        interval_minutes=(
                            settings["interval_minutes"]
                            if "interval_minutes" in settings
                            else existing.interval_minutes
                        ),
                    )
                except ValueError as exc:
                    raise _InvalidRequestError(str(exc)) from exc
                settings_map = dict(self._manifest.settings)
                settings_map[set_id] = merged_settings
                self._write_manifest(replace(self._manifest, settings=settings_map))

            op = self._begin_operation(
                "sync",
                "starting",
                f"Starting sync for {set_id}",
                total_ops=0,
            )

        try:
            self._submit_actor_task("sync", lambda: self._execute_sync(set_id), wait=False)
        except Exception as exc:
            self._finish_operation("interrupted", str(exc), kind="sync")
            raise
        return {"ok": True, "operation": op.to_public_dict()}

    def start_preview(self, content_id: str) -> dict[str, Any]:
        """Validate active-set ownership and enqueue a timed preview on the actor lane."""
        if not isinstance(content_id, str) or not content_id.strip():
            raise _InvalidRequestError("content_id must be a non-empty string")

        with self._state_lock:
            if self._stopping or self._stop_event.is_set():
                raise TVServiceError("TV daemon is stopping")
            if self._manifest_error:
                raise _ConflictError(self._manifest_error, code="manifest_error")
            try:
                self._assert_manifest_current()
            except TVServiceError as exc:
                raise _ConflictError(str(exc), code="manifest_error") from exc

            entry = next(
                (item for item in self._manifest.entries if item.content_id == content_id),
                None,
            )
            if entry is None:
                raise _NotFoundError(
                    "preview is limited to images in the currently synced set"
                )
            if self._current_op is not None or self._actor_busy:
                raise _ConflictError("another TV operation is already in progress")

            self._begin_operation(
                "preview",
                "checking-tv",
                "Checking TV before preview",
                current_item=content_id,
                total_ops=3,
            )
            self._preview_active = True
            self._preview_content_id = content_id
            self._preview_expires_at = None
            preview_payload = {
                "active": self._preview_active,
                "content_id": self._preview_content_id,
                "expires_at": self._preview_expires_at,
            }

        try:
            self._submit_actor_task(
                "preview",
                lambda: self._execute_preview(entry),
                wait=False,
            )
        except Exception as exc:
            with self._state_lock:
                self._preview_active = False
                self._preview_content_id = None
                self._preview_expires_at = None
            self._finish_operation("interrupted", str(exc), kind="preview")
            raise
        return {"ok": True, "preview": preview_payload}

    def request_abort(self) -> dict[str, Any]:
        """Request cooperative abort of the active TV operation without blocking on TV I/O."""
        with self._state_lock:
            op = self._current_op
            if op is None:
                return {"ok": True, "aborted": False}
            if not op.abort_requested:
                self._current_op = replace(op, abort_requested=True)
                self._persist_operation()
            self._abort_event.set()
            self._sleep_wake_event.set()
            return {"ok": True, "aborted": True}

    # ------------------------------------------------------------------
    # Anti-Wedge Sync State Machine (ADR 0001 + ADR 0003)
    # ------------------------------------------------------------------

    def _prepare_library_set(
        self,
        set_id: str,
        lease_id: str,
    ) -> tuple[list[PreparedImage], list[str]]:
        try:
            response = self._library_caller(
                self.config.library_sock,
                "POST",
                "/prepare-set",
                {"set_id": set_id, "lease_id": lease_id},
                timeout=_LIBRARY_PREPARE_TIMEOUT_SECONDS,
            )
        except IPCError as exc:
            raise TVServiceError(
                f"library service failed to prepare set {set_id!r}: {exc}"
            ) from exc
        except Exception as exc:
            raise TVServiceError(
                f"library IPC failed while preparing set {set_id!r}: {exc}"
            ) from exc

        if not isinstance(response, dict):
            raise TVServiceError("library service returned an invalid prepare-set response")
        raw_images = response.get("images")
        if not isinstance(raw_images, list):
            raise TVServiceError("library service response is missing an images list")
        prepared: list[PreparedImage] = []
        for item in raw_images:
            try:
                prepared.append(PreparedImage.from_dict(item))
            except ValueError as exc:
                raise TVServiceError(
                    f"library service returned an invalid prepared image: {exc}"
                ) from exc

        raw_warnings = response.get("warnings", [])
        warnings: list[str] = []
        if isinstance(raw_warnings, list):
            warnings.extend(str(item) for item in raw_warnings if item)

        if len(prepared) > self.config.max_set_images_warning:
            warnings.append(
                f"Set {set_id!r} has {len(prepared)} images (exceeds warning threshold "
                f"{self.config.max_set_images_warning}); TV sync may take longer."
            )
        return prepared, warnings

    def _release_library_lease(self, lease_id: str) -> None:
        try:
            self._library_caller(
                self.config.library_sock,
                "POST",
                "/release-lease",
                {"lease_id": lease_id},
                timeout=_LIBRARY_RELEASE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("Failed to release library lease %s: %s", lease_id, exc)

    def _upload_one(
        self,
        art: TVArtClient,
        image: PreparedImage,
        known_before: set[str],
    ) -> str:
        try:
            payload = image.cache_path.read_bytes()
        except OSError as exc:
            raise UploadError(
                f"could not read cached TV-safe JPEG for {image.source_rel}: {exc}"
            ) from exc

        returned = art.upload(payload, file_type="JPEG")
        content_id: str | None = None
        if returned is not None:
            content_id = str(returned)
            if not content_id:
                raise UploadError(f"TV returned an empty content ID for {image.source_rel}")
            if content_id in known_before:
                raise UploadError(
                    f"TV returned a duplicate content ID ({content_id}) for {image.source_rel}"
                )
            self._remember_uploaded(content_id)

        self._update_operation(
            phase="verifying",
            message=f"Waiting for TV to confirm {image.filename}",
            current_item=image.source_rel,
        )
        try:
            for attempt in range(self.config.tv_verify_retries):
                available = self._content_ids(art.available())
                if content_id is not None and content_id in available:
                    return content_id
                if content_id is None:
                    added = available - known_before
                    if len(added) == 1:
                        content_id = next(iter(added))
                        self._remember_uploaded(content_id)
                        return content_id
                    if len(added) > 1:
                        raise UploadError(
                            f"TV returned multiple new IDs while verifying {image.source_rel}"
                        )
                if attempt < self.config.tv_verify_retries - 1:
                    self._interruptible_wait(self.config.tv_verify_interval_seconds)
        except AbortRequested:
            raise
        except UploadError:
            raise
        except Exception as exc:
            raise UploadError(
                f"could not verify uploaded item {image.source_rel}: {exc}",
                content_id,
            ) from exc

        if content_id:
            raise UploadError(
                f"TV did not verify uploaded item {image.source_rel} ({content_id})",
                content_id,
            )
        raise UploadError(
            f"TV did not confirm or expose the uploaded item {image.source_rel}"
        )

    def _record_pre_commit_cleanup(self) -> tuple[str, ...]:
        """Move pre-commit known_uploaded IDs to manifest.cleanup_pending without deleting."""
        with self._state_lock:
            op = self._current_op
            known = list(op.known_uploaded) if op is not None else []
            if not known:
                return ()
            try:
                self._assert_manifest_current()
            except TVServiceError as exc:
                logger.warning(
                    "Could not merge partial uploads into manifest cleanup_pending: %s",
                    exc,
                )
                return tuple(dict.fromkeys(known))
            active_ids = {entry.content_id for entry in self._manifest.entries}
            merged = tuple(
                dict.fromkeys(
                    list(self._manifest.cleanup_pending)
                    + [cid for cid in known if cid not in active_ids]
                )
            )
            if merged != self._manifest.cleanup_pending:
                try:
                    self._write_manifest(replace(self._manifest, cleanup_pending=merged))
                except TVServiceError as exc:
                    logger.warning(
                        "Failed to persist pre-commit cleanup_pending: %s",
                        exc,
                    )
                    return tuple(dict.fromkeys(known))
            return ()

    def _execute_sync(self, set_id: str) -> None:
        art: TVArtClient | None = None
        lease_id = f"sync-{uuid.uuid4().hex}"
        prepare_called = False
        committed = False

        try:
            # Step 1: Check TV Art Mode & manifest
            self._update_operation(
                phase="checking-tv",
                message="Checking TV reachability",
                current_item=None,
                completed_ops=0,
                total_ops=0,
            )
            self._check_abort()
            with self._state_lock:
                self._assert_manifest_current()
                prior_manifest = self._manifest

            art = self._open_tv()
            self._check_ready(art)

            # Step 2: Prepare pinned TV-safe copies via library.sock
            self._update_operation(
                phase="preparing",
                message=f"Preparing images for {set_id}",
                current_item=None,
            )
            self._check_abort()
            prepare_called = True
            prepared, prepare_warnings = self._prepare_library_set(set_id, lease_id)
            with self._state_lock:
                self._warnings = list(prepare_warnings)
            if not prepared:
                raise TVServiceError(
                    "no valid images remain; the current TV set was left unchanged"
                )
            self._check_abort()

            # Step 3: Query available TV art and compute exact plan
            self._update_operation(
                phase="planning",
                message="Checking existing artwork on TV",
                current_item=None,
            )
            with self._state_lock:
                self._assert_manifest_current()
                prior_manifest = self._manifest

            existing_ids = self._content_ids(art.available())
            old_by_source = {
                (entry.source_rel, entry.fingerprint): entry
                for entry in prior_manifest.entries
            }

            reused_by_index: dict[int, ManifestEntry] = {}
            to_upload: list[tuple[int, PreparedImage]] = []
            for idx, image in enumerate(prepared):
                prev = old_by_source.get((image.source_rel, image.fingerprint))
                if prev is not None and prev.content_id in existing_ids:
                    reused_by_index[idx] = ManifestEntry(
                        source_rel=image.source_rel,
                        filename=image.filename,
                        fingerprint=image.fingerprint,
                        content_id=prev.content_id,
                    )
                else:
                    to_upload.append((idx, image))

            reused_ids = {entry.content_id for entry in reused_by_index.values()}
            old_ids_est = [
                entry.content_id
                for entry in prior_manifest.entries
                if entry.content_id not in reused_ids
            ]
            cleanup_est = list(
                dict.fromkeys(
                    [cid for cid in prior_manifest.cleanup_pending if cid not in reused_ids]
                    + old_ids_est
                )
            )

            upload_count = len(to_upload)
            delete_count = len(cleanup_est)
            total_ops = (
                1
                + upload_count
                + (1 if upload_count > 0 else 0)
                + 1
                + delete_count
                + (1 if delete_count > 0 else 0)
            )
            completed_ops = 1
            self._update_operation(
                phase="planning",
                message="Sync plan ready",
                current_item=None,
                completed_ops=completed_ops,
                total_ops=total_ops,
            )

            # Step 4: Upload loop (fail-fast, NO rollback delete storm)
            known_before = set(existing_ids)
            uploaded_by_index: dict[int, ManifestEntry] = {}
            for upload_idx, (orig_idx, image) in enumerate(to_upload):
                self._check_abort()
                self._update_operation(
                    phase="uploading",
                    message=f"Uploading {image.filename}",
                    current_item=image.source_rel,
                    completed_ops=completed_ops,
                    total_ops=total_ops,
                )
                content_id = self._upload_one(art, image, known_before)
                known_before.add(content_id)
                uploaded_by_index[orig_idx] = ManifestEntry(
                    source_rel=image.source_rel,
                    filename=image.filename,
                    fingerprint=image.fingerprint,
                    content_id=content_id,
                )
                completed_ops += 1
                self._update_operation(
                    phase="uploading",
                    message=f"Uploaded {image.filename}",
                    current_item=image.source_rel,
                    completed_ops=completed_ops,
                    total_ops=total_ops,
                )
                if upload_idx < upload_count - 1:
                    self._interruptible_wait(self.config.tv_op_delay_seconds)

            # Step 5: Post-upload settle & Art Mode re-check
            if upload_count > 0:
                self._check_abort()
                self._update_operation(
                    phase="settling-uploads",
                    message="Waiting for TV to settle after uploads",
                    current_item=None,
                    completed_ops=completed_ops,
                    total_ops=total_ops,
                )
                self._interruptible_wait(self.config.tv_batch_settle_seconds)
                self._check_ready(art)
                completed_ops += 1
                self._update_operation(
                    phase="settling-uploads",
                    message="Upload batch settled",
                    current_item=None,
                    completed_ops=completed_ops,
                    total_ops=total_ops,
                )

            # Step 6: Commit Manifest (atomic point of no return)
            self._check_abort()
            desired = tuple(
                reused_by_index[idx] if idx in reused_by_index else uploaded_by_index[idx]
                for idx in range(len(prepared))
            )
            desired_ids = {entry.content_id for entry in desired}
            with self._state_lock:
                self._assert_manifest_current()
                prior_manifest = self._manifest
                old_ids = [
                    entry.content_id
                    for entry in prior_manifest.entries
                    if entry.content_id not in desired_ids
                ]
                cleanup = tuple(
                    dict.fromkeys(
                        [
                            cid
                            for cid in prior_manifest.cleanup_pending
                            if cid not in desired_ids
                        ]
                        + old_ids
                    )
                )
                active_settings = prior_manifest.settings_for(set_id)
                settings_map = dict(prior_manifest.settings)
                settings_map[set_id] = active_settings

                self._update_operation(
                    phase="committing",
                    message="Activating new image set",
                    current_item=None,
                    completed_ops=completed_ops,
                    total_ops=total_ops,
                )
                committed_manifest = Manifest(
                    schema_version=1,
                    tv_ip=self.config.tv_ip,
                    active_set=set_id,
                    entries=desired,
                    cleanup_pending=cleanup,
                    settings=settings_map,
                    current_content_id=prior_manifest.current_content_id,
                )
                self._write_manifest(committed_manifest)
                committed = True

            # Step 7: Select initial image BEFORE any old-item deletions
            self._update_operation(
                phase="selecting",
                message="Selecting initial image on TV",
                current_item=None,
                completed_ops=completed_ops,
                total_ops=total_ops,
            )
            if active_settings.random:
                selected = self._random_pick(
                    art,
                    desired,
                    active_settings.mix_art_store_favorites,
                )
            else:
                first = desired[0]
                selected = _DisplayCandidate(
                    content_id=first.content_id,
                    category=None,
                    source_rel=first.source_rel,
                    filename=first.filename,
                    is_art_store=False,
                )

            self._update_operation(
                phase="selecting",
                message=f"Selecting {selected.label}",
                current_item=selected.source_rel or selected.content_id,
                completed_ops=completed_ops,
                total_ops=total_ops,
            )
            self._check_ready(art)
            try:
                self._select_candidate(art, selected)
            except AbortRequested:
                raise
            except Exception as exc:
                try:
                    still_ready = self._is_art_mode(art.get_artmode())
                except Exception:
                    still_ready = False
                if still_ready:
                    self._add_drift_warning(
                        f"Selected artwork {selected.label} ({selected.content_id}) could not "
                        f"be displayed on TV (possible TV drift; run Sync to reconcile): {exc}"
                    )
                raise TVServiceError(
                    f"new set is synced, but display selection failed: {exc}"
                ) from exc

            with self._state_lock:
                self._current_display = CurrentDisplay(
                    content_id=selected.content_id,
                    category=selected.category,
                    source_rel=selected.source_rel,
                    is_art_store=selected.is_art_store,
                )
                self._drift_warnings = []
                updated_manifest = replace(
                    self._manifest,
                    current_content_id=selected.content_id,
                )
                self._write_manifest(updated_manifest)
                self._resume_cycling_if_paused()
                self._schedule_next_rotation(active_settings.interval_minutes * 60.0)

            completed_ops += 1
            self._update_operation(
                phase="selecting",
                message=f"Selected {selected.label}",
                current_item=selected.source_rel or selected.content_id,
                completed_ops=completed_ops,
                total_ops=total_ops,
            )

            # Step 8: Delete loop (excluding current_content_id, fail-fast)
            delete_targets = [cid for cid in cleanup if cid != selected.content_id]
            remaining_cleanup = list(cleanup)
            deleted_count = 0
            delete_error: str | None = None
            delete_aborted = False

            for idx, cid in enumerate(delete_targets):
                if self._is_abort_requested():
                    delete_aborted = True
                    break
                self._update_operation(
                    phase="deleting",
                    message=f"Deleting old TV item {idx + 1}/{len(delete_targets)}",
                    current_item=cid,
                    completed_ops=completed_ops,
                    total_ops=total_ops,
                )
                try:
                    result = art.delete(cid)
                    if result is not True and cid in self._content_ids(art.available()):
                        raise TVServiceError("TV did not confirm deletion")
                except AbortRequested:
                    delete_aborted = True
                    break
                except Exception as exc:
                    delete_error = (
                        f"new set is active; old-item cleanup stopped on {cid}: {exc}"
                    )
                    break

                if cid in remaining_cleanup:
                    remaining_cleanup.remove(cid)
                deleted_count += 1
                completed_ops += 1
                self._update_operation(
                    phase="deleting",
                    message=f"Deleted old TV item {idx + 1}/{len(delete_targets)}",
                    current_item=cid,
                    completed_ops=completed_ops,
                    total_ops=total_ops,
                )
                if idx < len(delete_targets) - 1:
                    try:
                        self._interruptible_wait(self.config.tv_op_delay_seconds)
                    except AbortRequested:
                        delete_aborted = True
                        break

            # Step 9: Post-delete settle & final cleanup_pending persistence
            if deleted_count >= 1 and not delete_aborted and delete_error is None:
                if self._is_abort_requested():
                    delete_aborted = True
                else:
                    self._update_operation(
                        phase="settling-deletes",
                        message="Waiting for TV to settle after deletions",
                        current_item=None,
                        completed_ops=completed_ops,
                        total_ops=total_ops,
                    )
                    try:
                        self._interruptible_wait(self.config.tv_batch_settle_seconds)
                    except AbortRequested:
                        delete_aborted = True
                    else:
                        completed_ops += 1
                        self._update_operation(
                            phase="settling-deletes",
                            message="Delete batch settled",
                            current_item=None,
                            completed_ops=completed_ops,
                            total_ops=total_ops,
                        )

            with self._state_lock:
                final_manifest = replace(
                    self._manifest,
                    cleanup_pending=tuple(remaining_cleanup),
                )
                self._write_manifest(final_manifest)
                self._schedule_next_rotation(active_settings.interval_minutes * 60.0)
                if delete_error is not None:
                    self._last_error = delete_error
                    self._warnings.append(delete_error)
                else:
                    self._last_error = None

            if delete_aborted:
                self._finish_operation(
                    "aborted",
                    "Sync aborted during cleanup; the new set remains active and "
                    "remaining old items stay in cleanup_pending.",
                    kind="sync",
                )
            elif delete_error is not None:
                self._finish_operation("failed", delete_error, kind="sync")
            else:
                self._finish_operation("completed", "Sync completed", kind="sync")

        except AbortRequested as exc:
            if not committed:
                recovery_pending = self._record_pre_commit_cleanup()
                msg = (
                    "Sync aborted before activation; the previous active set was preserved "
                    "and any partial uploads were queued in cleanup_pending."
                )
                with self._state_lock:
                    self._last_error = None
                self._finish_operation(
                    "aborted",
                    msg if not str(exc) else f"{msg} ({exc})",
                    kind="sync",
                    recovery_pending=recovery_pending,
                )
            else:
                self._finish_operation(
                    "aborted",
                    f"Sync aborted: {exc}",
                    kind="sync",
                )
        except Exception as exc:
            error_msg = str(exc)
            recovery_pending = ()
            if not committed:
                recovery_pending = self._record_pre_commit_cleanup()
                if not error_msg.startswith("no valid images") and "Art Mode" not in error_msg:
                    error_msg = (
                        f"{error_msg}; the previously active TV set was preserved"
                    )
            with self._state_lock:
                self._last_error = error_msg
            self._finish_operation(
                "failed",
                error_msg,
                kind="sync",
                recovery_pending=recovery_pending,
            )
        finally:
            if prepare_called:
                self._release_library_lease(lease_id)
            close_tv_client(art)
            self._ensure_token_file_permissions()

    # ------------------------------------------------------------------
    # Preview Execution & Cooperative Abort
    # ------------------------------------------------------------------

    def _execute_preview(self, entry: ManifestEntry) -> None:
        restore_id: str | None = None
        restore_category: str | None = None
        aborted = False

        # Phase 1: Check TV, capture current artwork, select preview item
        art: TVArtClient | None = None
        try:
            art = self._open_tv()
            self._check_ready(art)
            self._check_abort()
            try:
                current_raw = art.get_current()
                restore_id = self._extract_current_id(current_raw)
                restore_category = self._extract_current_category(current_raw)
            except AbortRequested:
                raise
            except Exception as exc:
                raise TVServiceError(f"could not read the current TV image: {exc}") from exc
            if restore_id is None:
                raise TVServiceError(
                    "TV did not identify its current image; preview was not started"
                )
            self._check_abort()

            self._update_operation(
                phase="selecting-preview",
                message=f"Selecting preview image {entry.filename}",
                current_item=entry.content_id,
                completed_ops=0,
                total_ops=3,
            )
            try:
                art.select_image(entry.content_id)
            except AbortRequested:
                raise
            except Exception as exc:
                try:
                    still_ready = self._is_art_mode(art.get_artmode())
                except Exception:
                    still_ready = False
                if still_ready:
                    self._add_drift_warning(
                        f"Preview image {entry.source_rel} ({entry.content_id}) could not be "
                        f"selected on TV (possible TV drift; run Sync to reconcile): {exc}"
                    )
                raise TVServiceError(
                    f"TV could not display the preview: {exc}"
                ) from exc

            expires_at = self._wall_clock_iso(offset_seconds=float(self.config.preview_seconds))
            with self._state_lock:
                self._current_display = CurrentDisplay(
                    content_id=entry.content_id,
                    category=None,
                    source_rel=entry.source_rel,
                    is_art_store=False,
                )
                self._preview_active = True
                self._preview_content_id = entry.content_id
                self._preview_expires_at = expires_at
                self._resume_cycling_if_paused()
            self._update_operation(
                phase="previewing",
                message=f"Previewing {entry.filename} for {self.config.preview_seconds}s",
                current_item=entry.content_id,
                completed_ops=1,
                total_ops=3,
            )
        except AbortRequested as exc:
            with self._state_lock:
                self._preview_active = False
                self._preview_content_id = None
                self._preview_expires_at = None
            self._finish_operation("aborted", f"Preview aborted: {exc}", kind="preview")
            return
        except Exception as exc:
            with self._state_lock:
                self._preview_active = False
                self._preview_content_id = None
                self._preview_expires_at = None
                self._last_error = str(exc)
            self._finish_operation("failed", str(exc), kind="preview")
            return
        finally:
            close_tv_client(art)
            self._ensure_token_file_permissions()

        # Phase 2: Interruptible wait for preview_seconds
        try:
            self._interruptible_wait(float(self.config.preview_seconds))
        except AbortRequested:
            if self._stop_event.is_set():
                with self._state_lock:
                    self._preview_active = False
                    self._preview_content_id = None
                    self._preview_expires_at = None
                self._finish_operation(
                    "interrupted",
                    "Preview was interrupted by service shutdown.",
                    kind="preview",
                )
                return
            aborted = True

        # Phase 3: Restore previous image and category (fresh WebSocket)
        self._update_operation(
            phase="restoring-preview",
            message="Restoring the previous TV image",
            current_item=restore_id,
            completed_ops=2,
            total_ops=3,
        )
        restore_art: TVArtClient | None = None
        self._restoring_preview = True
        try:
            restore_art = self._open_tv()
            self._check_ready(restore_art)
            self._select_raw(restore_art, restore_id, restore_category)
            with self._state_lock:
                restore_source: str | None = None
                for item in self._manifest.entries:
                    if item.content_id == restore_id:
                        restore_source = item.source_rel
                        break
                is_store = restore_source is None and (
                    restore_category == ART_STORE_FAVORITES_CATEGORY
                    or restore_id.startswith(ART_STORE_ID_PREFIX)
                )
                self._current_display = CurrentDisplay(
                    content_id=restore_id,
                    category=restore_category,
                    source_rel=restore_source,
                    is_art_store=is_store,
                )
                self._preview_active = False
                self._preview_content_id = None
                self._preview_expires_at = None
                if self._last_error and self._last_error.startswith("preview "):
                    self._last_error = None
                if self._manifest.active_set is not None:
                    interval_seconds = (
                        self._manifest.settings_for(
                            self._manifest.active_set
                        ).interval_minutes
                        * 60.0
                    )
                    self._schedule_next_rotation(interval_seconds)

            self._update_operation(
                phase="restoring-preview",
                message="Previous TV image restored",
                current_item=restore_id,
                completed_ops=3,
                total_ops=3,
            )
            if aborted:
                self._finish_operation(
                    "aborted",
                    "Preview aborted and previous image was restored",
                    kind="preview",
                )
            else:
                self._finish_operation(
                    "completed",
                    "Preview ended and previous image was restored",
                    kind="preview",
                )
        except Exception as exc:
            error_msg = f"preview restore failed: {exc}"
            with self._state_lock:
                self._preview_active = False
                self._preview_content_id = None
                self._preview_expires_at = None
                self._tv_ready = False
                self._pause_cycling(error_msg)
            self._finish_operation("failed", error_msg, kind="preview")
        finally:
            self._restoring_preview = False
            close_tv_client(restore_art)
            self._ensure_token_file_permissions()

    # ------------------------------------------------------------------
    # Slideshow Cycling & Drift Tolerance
    # ------------------------------------------------------------------

    def run_slideshow_tick(self) -> bool:
        """Execute one slideshow rotation tick if an active set has entries."""
        with self._state_lock:
            if (
                self._cycling_paused
                or self._manifest_error is not None
                or self._manifest.active_set is None
                or not self._manifest.entries
            ):
                return False
            active_set = self._manifest.active_set
            entries = self._manifest.entries
            settings = self._manifest.settings_for(active_set)
            current_id = self._current_display.content_id or self._manifest.current_content_id
            if self._current_op is not None:
                return False
            self._begin_operation(
                "slideshow-selection",
                "checking-tv",
                "Checking TV before slideshow selection",
                total_ops=2,
            )

        art: TVArtClient | None = None
        try:
            art = self._open_tv()
            self._check_ready(art)
            self._check_abort()
            self._update_operation(
                phase="checking-tv",
                message="TV is ready for slideshow selection",
                completed_ops=1,
                total_ops=2,
            )

            ordered_candidates = self._build_slideshow_candidates(
                art,
                entries,
                settings,
                current_id,
            )
            selected: _DisplayCandidate | None = None
            last_select_error: Exception | None = None
            for candidate in ordered_candidates:
                self._check_abort()
                self._update_operation(
                    phase="selecting",
                    message=f"Selecting {candidate.label}",
                    current_item=candidate.source_rel or candidate.content_id,
                    completed_ops=1,
                    total_ops=2,
                )
                try:
                    self._select_candidate(art, candidate)
                    selected = candidate
                    break
                except AbortRequested:
                    raise
                except Exception as exc:
                    last_select_error = exc
                    if _is_timeout_or_connection_error(exc):
                        raise
                    try:
                        still_ready = self._is_art_mode(art.get_artmode())
                    except Exception as mode_exc:
                        with self._state_lock:
                            self._tv_ready = False
                            self._tv_checked_at = self._wall_clock_iso()
                        raise TVServiceError(
                            f"TV is unavailable or its Art Mode status could not be read: "
                            f"{mode_exc}"
                        ) from mode_exc
                    if not still_ready:
                        with self._state_lock:
                            self._tv_ready = False
                            self._tv_checked_at = self._wall_clock_iso()
                        raise TVServiceError(
                            "TV is not in Art Mode; slideshow cycling paused"
                        ) from exc
                    self._add_drift_warning(
                        f"Artwork {candidate.label} ({candidate.content_id}) could not be "
                        f"selected on TV (possible TV drift; run Sync to reconcile): {exc}"
                    )

            if selected is None:
                raise TVServiceError(
                    f"all managed artwork candidates failed to select: {last_select_error}"
                )

            with self._state_lock:
                self._current_display = CurrentDisplay(
                    content_id=selected.content_id,
                    category=selected.category,
                    source_rel=selected.source_rel,
                    is_art_store=selected.is_art_store,
                )
                if self._manifest_error is None:
                    self._write_manifest(
                        replace(self._manifest, current_content_id=selected.content_id)
                    )
                self._cycling_paused = False
                self._paused_remaining = None
                self._schedule_next_rotation(settings.interval_minutes * 60.0)
                if self._last_error and self._last_error.startswith("cycling paused:"):
                    self._last_error = None

            self._update_operation(
                phase="selecting",
                message=f"Selected {selected.label}",
                current_item=selected.source_rel or selected.content_id,
                completed_ops=2,
                total_ops=2,
            )
            self._finish_operation(
                "completed",
                "Slideshow image selected",
                kind="slideshow-selection",
            )
            return True

        except AbortRequested as exc:
            with self._state_lock:
                if self._manifest.active_set is not None and self._manifest.entries:
                    interval_seconds = (
                        self._manifest.settings_for(
                            self._manifest.active_set
                        ).interval_minutes
                        * 60.0
                    )
                    self._schedule_next_rotation(interval_seconds)
            self._finish_operation(
                "aborted",
                f"Slideshow selection aborted: {exc}",
                kind="slideshow-selection",
            )
            return False
        except Exception as exc:
            reason = f"cycling paused: {exc}"
            self._pause_cycling(reason)
            self._finish_operation(
                "failed",
                str(exc),
                kind="slideshow-selection",
            )
            return False
        finally:
            close_tv_client(art)
            self._ensure_token_file_permissions()

    def _build_slideshow_candidates(
        self,
        art: TVArtClient,
        entries: Sequence[ManifestEntry],
        settings: SetSettings,
        current_id: str | None,
    ) -> list[_DisplayCandidate]:
        if settings.random:
            pool = self._candidates_for_random_pick(
                art,
                entries,
                settings.mix_art_store_favorites,
            )
            first = self._chooser(pool)
            ordered = [first] + [item for item in pool if item != first]
            return ordered

        current_index = next(
            (idx for idx, item in enumerate(entries) if item.content_id == current_id),
            -1,
        )
        ordered_seq: list[_DisplayCandidate] = []
        count = len(entries)
        for offset in range(1, count + 1):
            item = entries[(current_index + offset) % count]
            ordered_seq.append(
                _DisplayCandidate(
                    content_id=item.content_id,
                    category=None,
                    source_rel=item.source_rel,
                    filename=item.filename,
                    is_art_store=False,
                )
            )
        return ordered_seq


# ----------------------------------------------------------------------
# Unix Domain Socket IPC Server Wiring
# ----------------------------------------------------------------------


def _ipc_error_response(exc: TVServiceError) -> tuple[int, dict[str, Any]]:
    if isinstance(exc, _InvalidRequestError):
        return HTTP_BAD_REQUEST, make_error_envelope(str(exc), exc.code)
    if isinstance(exc, _NotFoundError):
        return HTTP_NOT_FOUND, make_error_envelope(str(exc), exc.code)
    if isinstance(exc, _ConflictError):
        return HTTP_CONFLICT, make_error_envelope(str(exc), exc.code)
    return HTTP_CONFLICT, make_error_envelope(str(exc), "tv_error")


def build_tv_ipc_server(
    daemon: TVDaemon,
    socket_path: Path | None = None,
) -> UnixHTTPServer:
    """Create and configure the Unix domain socket server for tv_daemon."""
    target_socket = socket_path or daemon.config.tv_sock

    def handle_health(_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return HTTP_OK, daemon.health()

    def handle_status(_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return HTTP_OK, daemon.status()

    def handle_check(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if body:
            return HTTP_BAD_REQUEST, make_error_envelope(
                f"unknown check fields: {', '.join(sorted(body.keys()))}",
                "bad_request",
            )
        try:
            return HTTP_OK, daemon.check_tv()
        except TVServiceError as exc:
            return _ipc_error_response(exc)

    def handle_settings(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        allowed_keys = {"set_id", "random", "mix_art_store_favorites", "interval_minutes"}
        unknown = set(body.keys()) - allowed_keys
        if unknown:
            return HTTP_BAD_REQUEST, make_error_envelope(
                f"unknown settings fields: {', '.join(sorted(unknown))}",
                "bad_request",
            )
        kwargs: dict[str, Any] = {}
        for key in ("random", "mix_art_store_favorites", "interval_minutes"):
            if key in body:
                kwargs[key] = body[key]
        try:
            result = daemon.save_settings(
                body.get("set_id"),  # type: ignore[arg-type]
                **kwargs,
            )
        except TVServiceError as exc:
            return _ipc_error_response(exc)
        return HTTP_OK, result

    def handle_sync(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        allowed_keys = {"set_id", "settings"}
        unknown = set(body.keys()) - allowed_keys
        if unknown:
            return HTTP_BAD_REQUEST, make_error_envelope(
                f"unknown sync fields: {', '.join(sorted(unknown))}",
                "bad_request",
            )
        try:
            result = daemon.start_sync(
                body.get("set_id"),  # type: ignore[arg-type]
                settings=body.get("settings"),
            )
        except TVServiceError as exc:
            return _ipc_error_response(exc)
        return HTTP_ACCEPTED, result

    def handle_preview(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        allowed_keys = {"content_id"}
        unknown = set(body.keys()) - allowed_keys
        if unknown:
            return HTTP_BAD_REQUEST, make_error_envelope(
                f"unknown preview fields: {', '.join(sorted(unknown))}",
                "bad_request",
            )
        try:
            result = daemon.start_preview(body.get("content_id"))  # type: ignore[arg-type]
        except TVServiceError as exc:
            return _ipc_error_response(exc)
        return HTTP_ACCEPTED, result

    def handle_abort(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if body:
            return HTTP_BAD_REQUEST, make_error_envelope(
                f"unknown abort fields: {', '.join(sorted(body.keys()))}",
                "bad_request",
            )
        return HTTP_OK, daemon.request_abort()

    return UnixHTTPServer(
        target_socket,
        routes={
            ("GET", "/health"): handle_health,
            ("GET", "/status"): handle_status,
            ("POST", "/check"): handle_check,
            ("POST", "/settings"): handle_settings,
            ("POST", "/sync"): handle_sync,
            ("POST", "/preview"): handle_preview,
            ("POST", "/abort"): handle_abort,
        },
    )


# ----------------------------------------------------------------------
# CLI & Process Entrypoint
# ----------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--config", type=Path)
    common.add_argument("--state-dir", type=Path)
    common.add_argument("--cache-dir", type=Path)
    common.add_argument("--tv-ip")
    common.add_argument("--bind-ip")
    common.add_argument("--port", type=int)

    parser = argparse.ArgumentParser(
        prog="frame_art_mode.tv_daemon",
        description="TV WebSocket actor and slideshow daemon for Frame Art Mode.",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", parents=[common], help="Run the TV daemon server.")
    subparsers.add_parser("status", parents=[common], help="Print cached TV daemon status.")
    subparsers.add_parser("check", parents=[common], help="Check TV reachability and Art Mode.")

    sync_parser = subparsers.add_parser(
        "sync", parents=[common], help="Start TV sync for a set."
    )
    sync_parser.add_argument("set_id", help="Image set ID (e.g. 'root' or 'occasion:Party').")

    subparsers.add_parser("abort", parents=[common], help="Abort active TV operation.")

    preview_parser = subparsers.add_parser(
        "preview", parents=[common], help="Preview an active TV artwork item."
    )
    preview_parser.add_argument("content_id", help="Managed TV content_id to preview.")

    return parser


def serve_daemon(
    config: Config,
    *,
    daemon_factory: Callable[[Config], TVDaemon] = TVDaemon,
) -> int:
    """Start TVDaemon and serve tv.sock until SIGTERM/SIGINT."""
    os.umask(0o077)
    try:
        daemon = daemon_factory(config)
    except TVServiceError as exc:
        logger.error("Fatal tv_daemon startup error: %s", exc)
        return 2

    server: UnixHTTPServer | None = None
    previous_sigterm = None
    previous_sigint = None

    def _handle_signal(_signum: int, _frame: Any) -> None:
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        server = build_tv_ipc_server(daemon)
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.signal(signal.SIGTERM, _handle_signal)
            previous_sigint = signal.signal(signal.SIGINT, _handle_signal)
        daemon.start()
        logger.info("tv_daemon listening on %s", config.tv_sock)
        server.serve_forever()
    except OSError as exc:
        logger.error("Failed to start tv_daemon IPC server on %s: %s", config.tv_sock, exc)
        return 2
    finally:
        if threading.current_thread() is threading.main_thread():
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)
        if server is not None:
            server.server_close()
        daemon.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for frame_art_mode.tv_daemon."""
    os.umask(0o077)
    script_dir = Path(__file__).resolve().parent.parent
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    command = args.command or "serve"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config_path = getattr(args, "config", script_dir / "config.json")
    overrides: dict[str, Any] = {
        "state_dir": getattr(args, "state_dir", None),
        "cache_dir": getattr(args, "cache_dir", None),
        "tv_ip": getattr(args, "tv_ip", None),
        "bind_ip": getattr(args, "bind_ip", None),
        "port": getattr(args, "port", None),
    }
    try:
        config = load_config(config_path, overrides=overrides, script_dir=script_dir)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if command == "serve":
        return serve_daemon(config)

    try:
        if command == "status":
            payload = ipc.call_json(config.tv_sock, "GET", "/status", timeout=5.0)
        elif command == "check":
            payload = ipc.call_json(
                config.tv_sock,
                "POST",
                "/check",
                body={},
                timeout=max(10.0, config.tv_timeout_seconds + 5.0),
            )
        elif command == "sync":
            payload = ipc.call_json(
                config.tv_sock,
                "POST",
                "/sync",
                body={"set_id": args.set_id},
                timeout=10.0,
            )
        elif command == "abort":
            payload = ipc.call_json(
                config.tv_sock,
                "POST",
                "/abort",
                body={},
                timeout=5.0,
            )
        elif command == "preview":
            payload = ipc.call_json(
                config.tv_sock,
                "POST",
                "/preview",
                body={"content_id": args.content_id},
                timeout=10.0,
            )
        else:
            print(f"ERROR: unknown command {command!r}", file=sys.stderr)
            return 2
    except IPCError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
