from __future__ import annotations

import argparse
from dataclasses import replace
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import stat
import sys
import threading
from typing import Any, TextIO
import uuid

from .config import Config, ConfigError, load_config
from .images import (
    discover_sets,
    file_signature,
    is_ignored_name,
    natural_key,
    normalize_image_assets,
    read_verified_source,
    safe_filename,
)
from .ipc import (
    HTTP_ACCEPTED,
    HTTP_BAD_REQUEST,
    HTTP_CONFLICT,
    HTTP_NOT_FOUND,
    HTTP_OK,
    UnixHTTPServer,
    call_json,
)
from .models import (
    NORMALIZATION_VERSION,
    OCCASION_PREFIX,
    ROOT_SET,
    AbortRequested,
    CachedImage,
    IPCError,
    ImageError,
    LibraryError,
    OperationResult,
    OperationState,
    SourceImage,
    atomic_write_bytes,
    atomic_write_json,
    ensure_private_dir,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

LIBRARY_INDEX_SCHEMA_VERSION = 1


def _set_label(set_id: str) -> str:
    if set_id == ROOT_SET:
        return "Default"
    return set_id.removeprefix(OCCASION_PREFIX)


def _require_non_empty_str_param(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise IPCError(
            f"{name} must be a non-empty string",
            status_code=HTTP_BAD_REQUEST,
            code="bad_request",
        )
    return value


class LibraryService:
    """Owns read-only image_root scanning and the persistent TV-safe cache."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.started_at = utc_now_iso()
        ensure_private_dir(self.config.state_dir)
        self.cache_dir = self.config.cache_dir.resolve()
        self.tv_cache_dir = self.cache_dir / "tv"
        self.thumbs_cache_dir = self.cache_dir / "thumbs"
        ensure_private_dir(self.cache_dir)
        ensure_private_dir(self.tv_cache_dir)
        ensure_private_dir(self.thumbs_cache_dir)
        self.index_path = self.cache_dir / "library.json"

        self._lock_file: TextIO | None = self._acquire_lock(self.config.library_lock)
        self._state_lock = threading.Lock()
        self._persist_lock = threading.Lock()
        self._abort_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._closed = False

        self._sets: dict[str, list[CachedImage]] = {}
        self._last_refreshed_at: str | None = None
        self._scan_warnings: list[str] = []
        self._index_warnings: list[str] = []
        self._operation: OperationState | None = None
        self._last_result: OperationResult | None = None
        self._leases: dict[str, set[str]] = {}
        self._prune_pending = False
        self._mutating_active = False

        try:
            self._load_index()
        except Exception:
            self.close()
            raise

    @staticmethod
    def _acquire_lock(lock_path: Path) -> TextIO:
        ensure_private_dir(lock_path.parent)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(fd, "a+", encoding="utf-8")
        try:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise LibraryError(
                f"another library daemon instance is already running ({lock_path})"
            ) from exc
        return handle

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            if self._operation is not None:
                self._abort_event.set()
                self._operation = replace(self._operation, abort_requested=True)
            worker = self._worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=10.0)
        if self._lock_file is not None:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                logger.debug("Ignoring error unlocking %s", self.config.library_lock, exc_info=True)
            finally:
                self._lock_file.close()
                self._lock_file = None

    def _current_warnings_locked(self) -> list[str]:
        warnings = list(self._index_warnings)
        for set_images in self._sets.values():
            for item in set_images:
                if item.skip_reason:
                    warnings.append(f"{item.source_rel}: {item.skip_reason}")
        return warnings

    def _build_index_payload_locked(self) -> dict[str, Any]:
        return {
            "schema_version": LIBRARY_INDEX_SCHEMA_VERSION,
            "last_refreshed_at": self._last_refreshed_at,
            "sets": {
                set_id: [item.to_dict() for item in items]
                for set_id, items in self._sets.items()
            },
            "warnings": self._current_warnings_locked(),
            "scan_warnings": list(self._scan_warnings),
            "operation": self._operation.to_dict() if self._operation is not None else None,
            "last_result": self._last_result.to_dict() if self._last_result is not None else None,
        }

    def _persist_index_snapshot(self, payload: dict[str, Any]) -> None:
        with self._persist_lock:
            atomic_write_json(self.index_path, payload, mode=0o600, fsync=True)

    def _persist_current_index(self) -> None:
        with self._persist_lock:
            with self._state_lock:
                payload = self._build_index_payload_locked()
            atomic_write_json(self.index_path, payload, mode=0o600, fsync=True)

    def _load_index(self) -> None:
        if not self.index_path.exists():
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("library.json must contain a JSON object")
            if raw.get("schema_version") != LIBRARY_INDEX_SCHEMA_VERSION:
                raise ValueError(f"unsupported library schema_version: {raw.get('schema_version')!r}")

            last_refreshed_at = raw.get("last_refreshed_at")
            if last_refreshed_at is not None and not isinstance(last_refreshed_at, str):
                raise ValueError("last_refreshed_at must be a string or null")

            raw_sets = raw.get("sets", {})
            if not isinstance(raw_sets, dict):
                raise ValueError("sets must be a mapping")
            parsed_sets: dict[str, list[CachedImage]] = {}
            for set_id, raw_items in raw_sets.items():
                if not isinstance(set_id, str) or not set_id:
                    raise ValueError("set_id key must be a non-empty string")
                if not isinstance(raw_items, list):
                    raise ValueError(f"set {set_id!r} items must be a list")
                items = [CachedImage.from_dict(item) for item in raw_items]
                items.sort(key=lambda item: natural_key(item.name_rel))
                parsed_sets[set_id] = items

            raw_scan_warnings = raw.get("scan_warnings", [])
            if not isinstance(raw_scan_warnings, list) or not all(
                isinstance(item, str) for item in raw_scan_warnings
            ):
                raise ValueError("scan_warnings must be a list of strings")

            raw_operation = raw.get("operation")
            loaded_operation = (
                OperationState.from_dict(raw_operation) if raw_operation is not None else None
            )

            raw_last_result = raw.get("last_result")
            loaded_last_result = (
                OperationResult.from_dict(raw_last_result) if raw_last_result is not None else None
            )
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning(
                "Library index %s is unreadable; falling back to empty index: %s",
                self.index_path,
                exc,
            )
            self._sets = {}
            self._last_refreshed_at = None
            self._scan_warnings = []
            self._index_warnings = [
                f"library.json was unreadable and reset; cached media files were preserved: {exc}"
            ]
            self._operation = None
            self._last_result = None
            return

        self._sets = parsed_sets
        self._last_refreshed_at = last_refreshed_at
        self._scan_warnings = list(raw_scan_warnings)
        self._index_warnings = []
        self._operation = None
        self._last_result = loaded_last_result

        if loaded_operation is not None:
            self._last_result = OperationResult(
                kind=loaded_operation.kind,
                state="interrupted",
                message=f"{loaded_operation.kind.capitalize()} was interrupted by a daemon restart.",
                timestamp=utc_now_iso(),
            )
            self._persist_current_index()

    def _is_reusable(self, cached: CachedImage, source: SourceImage) -> bool:
        if cached.signature != source.signature:
            return False
        if cached.normalization_version != NORMALIZATION_VERSION:
            return False
        if cached.max_image_bytes != self.config.max_image_bytes:
            return False
        if cached.skip_reason is not None:
            return True
        if not cached.fingerprint:
            return False
        tv_file = self.tv_cache_dir / f"{cached.fingerprint}.jpg"
        thumb_file = self.thumbs_cache_dir / f"{cached.fingerprint}.jpg"
        try:
            return (
                tv_file.is_file()
                and tv_file.stat().st_size > 0
                and thumb_file.is_file()
                and thumb_file.stat().st_size > 0
            )
        except OSError:
            return False

    def _process_source(self, source: SourceImage, existing: CachedImage | None) -> CachedImage:
        if existing is not None and self._is_reusable(existing, source):
            return existing
        try:
            data, verified_sig = read_verified_source(
                source.path,
                expected_signature=source.signature,
            )
            assets = normalize_image_assets(
                data,
                max_image_bytes=self.config.max_image_bytes,
            )
            tv_rel = f"tv/{assets.fingerprint}.jpg"
            thumb_rel = f"thumbs/{assets.fingerprint}.jpg"
            tv_file = self.tv_cache_dir / f"{assets.fingerprint}.jpg"
            thumb_file = self.thumbs_cache_dir / f"{assets.fingerprint}.jpg"
            if not tv_file.is_file() or tv_file.stat().st_size == 0:
                atomic_write_bytes(tv_file, assets.tv_bytes, mode=0o600, fsync=False)
            if not thumb_file.is_file() or thumb_file.stat().st_size == 0:
                atomic_write_bytes(thumb_file, assets.thumb_bytes, mode=0o600, fsync=False)
            return CachedImage(
                set_id=source.set_id,
                source_rel=source.source_rel,
                name_rel=source.name_rel,
                signature=verified_sig,
                fingerprint=assets.fingerprint,
                tv_path=tv_rel,
                thumb_path=thumb_rel,
                skip_reason=None,
                normalization_version=NORMALIZATION_VERSION,
                max_image_bytes=self.config.max_image_bytes,
                byte_size=assets.byte_size,
                width=assets.width,
                height=assets.height,
            )
        except (OSError, ImageError) as exc:
            logger.warning("Skipping %s: %s", source.source_rel, exc)
            return CachedImage(
                set_id=source.set_id,
                source_rel=source.source_rel,
                name_rel=source.name_rel,
                signature=source.signature,
                fingerprint=None,
                tv_path=None,
                thumb_path=None,
                skip_reason=str(exc),
                normalization_version=NORMALIZATION_VERSION,
                max_image_bytes=self.config.max_image_bytes,
                byte_size=None,
                width=None,
                height=None,
            )

    def _is_stale(self, cached: CachedImage) -> bool:
        if (
            cached.normalization_version != NORMALIZATION_VERSION
            or cached.max_image_bytes != self.config.max_image_bytes
        ):
            return True
        if cached.skip_reason is None:
            if not cached.fingerprint:
                return True
            tv_file = self.tv_cache_dir / f"{cached.fingerprint}.jpg"
            thumb_file = self.thumbs_cache_dir / f"{cached.fingerprint}.jpg"
            try:
                if not (
                    tv_file.is_file()
                    and tv_file.stat().st_size > 0
                    and thumb_file.is_file()
                    and thumb_file.stat().st_size > 0
                ):
                    return True
            except OSError:
                return True
        source_path = self.config.image_root / cached.source_rel
        try:
            source_stat = source_path.stat()
        except OSError:
            return True
        if not stat.S_ISREG(source_stat.st_mode):
            return True
        return file_signature(source_stat) != cached.signature

    @staticmethod
    def _count_cached_jpegs(directory: Path) -> int:
        try:
            return sum(
                1
                for entry in os.scandir(directory)
                if entry.name.endswith(".jpg") and entry.is_file()
            )
        except OSError:
            return 0

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "pid": os.getpid(),
            "started_at": self.started_at,
        }

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            sets_snapshot = {set_id: list(items) for set_id, items in self._sets.items()}
            last_refreshed_at = self._last_refreshed_at
            warnings = self._current_warnings_locked()
            scan_warnings = list(self._scan_warnings)
            active_leases = sorted(self._leases.keys())
            operation = self._operation.to_public_dict() if self._operation is not None else None
            last_result = (
                self._last_result.to_public_dict() if self._last_result is not None else None
            )

        sets_payload: dict[str, dict[str, Any]] = {}
        total_valid = 0
        total_skipped = 0

        for set_id in sorted(
            sets_snapshot,
            key=lambda sid: (0 if sid == ROOT_SET else 1, natural_key(sid)),
        ):
            items = sets_snapshot[set_id]
            ordered_items = sorted(items, key=lambda item: natural_key(item.name_rel))
            used_names: set[str] = set()
            images_payload: list[dict[str, Any]] = []
            valid_count = 0
            skipped_count = 0
            for item in ordered_items:
                if item.skip_reason is None and item.fingerprint is not None:
                    filename = safe_filename(item.name_rel, used_names)
                    valid_count += 1
                else:
                    filename = None
                    skipped_count += 1
                images_payload.append(
                    {
                        "source_rel": item.source_rel,
                        "name_rel": item.name_rel,
                        "filename": filename,
                        "sha256": item.fingerprint,
                        "thumb_path": item.thumb_path,
                        "skip_reason": item.skip_reason,
                        "byte_size": item.byte_size,
                        "stale": self._is_stale(item),
                    }
                )
            total_valid += valid_count
            total_skipped += skipped_count
            sets_payload[set_id] = {
                "label": _set_label(set_id),
                "valid_count": valid_count,
                "skipped_count": skipped_count,
                "images": images_payload,
            }

        return {
            "last_refreshed_at": last_refreshed_at,
            "sets": sets_payload,
            "counts": {
                "sets": len(sets_payload),
                "valid": total_valid,
                "skipped": total_skipped,
                "cached_tv_files": self._count_cached_jpegs(self.tv_cache_dir),
                "cached_thumb_files": self._count_cached_jpegs(self.thumbs_cache_dir),
            },
            "warnings": warnings,
            "scan_warnings": scan_warnings,
            "active_leases": active_leases,
            "operation": operation,
            "last_result": last_result,
        }

    def _set_exists_for_refresh(self, set_id: str, known_sets: dict[str, list[CachedImage]]) -> bool:
        if set_id in known_sets or set_id == ROOT_SET:
            return True
        if not set_id.startswith(OCCASION_PREFIX):
            return False
        folder_name = set_id.removeprefix(OCCASION_PREFIX)
        if not folder_name or "/" in folder_name or "\\" in folder_name or folder_name in {".", ".."}:
            return False
        if is_ignored_name(folder_name):
            return False
        candidate = self.config.image_root / folder_name
        try:
            return candidate.is_dir()
        except OSError:
            return False

    def start_refresh(self, set_id: str | None = None) -> OperationState:
        with self._state_lock:
            if self._mutating_active or self._operation is not None:
                raise IPCError(
                    "another library operation is already running",
                    status_code=HTTP_CONFLICT,
                    code="conflict",
                )
            if set_id is not None and not self._set_exists_for_refresh(set_id, self._sets):
                raise IPCError(
                    f"unknown image set: {set_id}",
                    status_code=HTTP_NOT_FOUND,
                    code="not_found",
                )
            self._abort_event.clear()
            self._mutating_active = True
            target_label = "full library" if set_id is None else set_id
            op = OperationState(
                kind="refresh",
                phase="scanning",
                message=f"Scanning {target_label}",
                current_item=None,
                completed_ops=0,
                total_ops=0,
                abort_requested=False,
            )
            self._operation = op

        self._persist_current_index()

        worker = threading.Thread(
            target=self._run_refresh_job,
            args=(set_id,),
            name="library-refresh",
            daemon=True,
        )
        with self._state_lock:
            self._worker_thread = worker
        try:
            worker.start()
        except Exception as exc:
            with self._state_lock:
                self._mutating_active = False
                self._operation = None
                self._worker_thread = None
                self._last_result = OperationResult(
                    kind="refresh",
                    state="failed",
                    message=f"Could not start refresh worker: {exc}",
                    timestamp=utc_now_iso(),
                )
            self._persist_current_index()
            raise LibraryError(f"could not start refresh worker: {exc}") from exc
        return op

    def refresh_sync(self, set_id: str | None = None) -> dict[str, Any]:
        """Run a synchronous refresh (used by offline CLI mode)."""
        op = self.start_refresh(set_id=set_id)
        logger.info("Started offline refresh (%s)", op.message)
        with self._state_lock:
            worker = self._worker_thread
        if worker is not None:
            worker.join()
        return self.status()

    def _run_refresh_job(self, set_id: str | None) -> None:
        should_prune = False
        try:
            discovered, scan_warnings = discover_sets(self.config.image_root)
            if self._abort_event.is_set():
                raise AbortRequested("refresh aborted")

            target_set_ids = list(discovered.keys()) if set_id is None else [set_id]
            total_ops = sum(len(discovered.get(sid, [])) for sid in target_set_ids)

            with self._state_lock:
                if self._operation is not None:
                    self._operation = replace(
                        self._operation,
                        phase="refreshing",
                        message=(
                            f"Refreshing {total_ops} image(s)"
                            if total_ops
                            else "No images to refresh"
                        ),
                        completed_ops=0,
                        total_ops=total_ops,
                    )
            self._persist_current_index()

            completed_ops = 0
            for sid in target_set_ids:
                sources = discovered.get(sid, [])
                with self._state_lock:
                    prior_by_rel = {item.source_rel: item for item in self._sets.get(sid, [])}
                processed_for_set: dict[str, CachedImage] = {}

                for source in sources:
                    if self._abort_event.is_set():
                        raise AbortRequested("refresh aborted")
                    with self._state_lock:
                        if self._operation is not None:
                            self._operation = replace(
                                self._operation,
                                current_item=source.source_rel,
                                message=f"Processing {source.source_rel}",
                            )
                    cached = self._process_source(source, prior_by_rel.get(source.source_rel))
                    processed_for_set[source.source_rel] = cached
                    completed_ops += 1

                    merged_for_set = list(processed_for_set.values()) + [
                        item for rel, item in prior_by_rel.items() if rel not in processed_for_set
                    ]
                    merged_for_set.sort(key=lambda item: natural_key(item.name_rel))
                    with self._state_lock:
                        self._sets[sid] = merged_for_set
                        if self._operation is not None:
                            self._operation = replace(
                                self._operation,
                                completed_ops=completed_ops,
                                current_item=source.source_rel,
                                message=f"Processed {completed_ops}/{total_ops}: {source.source_rel}",
                            )
                    self._persist_current_index()

                with self._state_lock:
                    if sid in discovered:
                        final_list = list(processed_for_set.values())
                        final_list.sort(key=lambda item: natural_key(item.name_rel))
                        self._sets[sid] = final_list
                    else:
                        self._sets.pop(sid, None)
                self._persist_current_index()

            if self._abort_event.is_set():
                raise AbortRequested("refresh aborted")

            with self._state_lock:
                if set_id is None:
                    for stale_sid in [key for key in self._sets if key not in discovered]:
                        self._sets.pop(stale_sid, None)
                    self._scan_warnings = list(scan_warnings)
                    self._index_warnings = []
                else:
                    self._scan_warnings = list(scan_warnings)
                self._last_refreshed_at = utc_now_iso()

                if set_id is None:
                    if self._leases:
                        self._prune_pending = True
                    else:
                        self._prune_pending = False
                        should_prune = True

                self._operation = None
                self._last_result = OperationResult(
                    kind="refresh",
                    state="completed",
                    message=f"Refresh completed ({completed_ops} image(s) checked).",
                    timestamp=utc_now_iso(),
                )
            self._persist_current_index()

            if should_prune:
                self._prune_orphans()
        except AbortRequested:
            with self._state_lock:
                self._operation = None
                self._last_result = OperationResult(
                    kind="refresh",
                    state="aborted",
                    message="Library refresh aborted.",
                    timestamp=utc_now_iso(),
                )
            self._persist_current_index()
        except Exception as exc:
            logger.exception("Library refresh failed")
            with self._state_lock:
                self._operation = None
                self._last_result = OperationResult(
                    kind="refresh",
                    state="failed",
                    message=f"Library refresh failed: {exc}",
                    timestamp=utc_now_iso(),
                )
            self._persist_current_index()
        finally:
            while True:
                extra_prune = False
                with self._state_lock:
                    if self._prune_pending and not self._leases:
                        self._prune_pending = False
                        extra_prune = True
                    else:
                        self._mutating_active = False
                        break
                if extra_prune:
                    self._prune_orphans()

    def _prune_orphans(self) -> int:
        with self._state_lock:
            referenced: set[str] = set()
            for items in self._sets.values():
                for item in items:
                    if item.skip_reason is None and item.fingerprint:
                        referenced.add(item.fingerprint)
            for pinned in self._leases.values():
                referenced.update(pinned)

        removed = 0
        for cache_subdir in (self.tv_cache_dir, self.thumbs_cache_dir):
            try:
                entries = list(os.scandir(cache_subdir))
            except OSError:
                continue
            for entry in entries:
                if not entry.name.endswith(".jpg"):
                    continue
                stem = entry.name[:-4]
                if stem not in referenced:
                    try:
                        Path(entry.path).unlink(missing_ok=True)
                        removed += 1
                    except OSError as exc:
                        logger.warning("Could not prune orphan cache file %s: %s", entry.path, exc)
        return removed

    def prepare_set(self, set_id: str, lease_id: str | None = None) -> dict[str, Any]:
        with self._state_lock:
            if self._mutating_active or self._operation is not None:
                raise IPCError(
                    "library refresh or prepare is already running",
                    status_code=HTTP_CONFLICT,
                    code="conflict",
                )
            self._mutating_active = True

        try:
            discovered, scan_warnings = discover_sets(self.config.image_root)
            if set_id not in discovered:
                raise IPCError(
                    f"unknown image set: {set_id}",
                    status_code=HTTP_NOT_FOUND,
                    code="not_found",
                )

            resolved_lease = lease_id if lease_id is not None else uuid.uuid4().hex
            with self._state_lock:
                prior_by_rel = {item.source_rel: item for item in self._sets.get(set_id, [])}

            updated_entries: list[CachedImage] = []
            for source in discovered[set_id]:
                updated_entries.append(
                    self._process_source(source, prior_by_rel.get(source.source_rel))
                )
            updated_entries.sort(key=lambda item: natural_key(item.name_rel))

            used_names: set[str] = set()
            images: list[dict[str, Any]] = []
            skipped: list[dict[str, str]] = []
            warnings: list[str] = list(scan_warnings)
            pinned_hashes: set[str] = set()

            for entry in updated_entries:
                if entry.skip_reason is None and entry.fingerprint is not None:
                    filename = safe_filename(entry.name_rel, used_names)
                    cache_abs = str(self.tv_cache_dir / f"{entry.fingerprint}.jpg")
                    thumb_abs = str(self.thumbs_cache_dir / f"{entry.fingerprint}.jpg")
                    pinned_hashes.add(entry.fingerprint)
                    images.append(
                        {
                            "source_rel": entry.source_rel,
                            "name_rel": entry.name_rel,
                            "filename": filename,
                            "fingerprint": entry.fingerprint,
                            "cache_path": cache_abs,
                            "thumb_path": thumb_abs,
                            "byte_size": entry.byte_size,
                        }
                    )
                else:
                    reason = entry.skip_reason or "unknown image error"
                    skipped.append(
                        {
                            "source_rel": entry.source_rel,
                            "name_rel": entry.name_rel,
                            "reason": reason,
                        }
                    )
                    warnings.append(f"{entry.source_rel}: {reason}")

            with self._state_lock:
                self._sets[set_id] = updated_entries
                self._scan_warnings = list(scan_warnings)
                self._leases[resolved_lease] = pinned_hashes
            self._persist_current_index()

            return {
                "set_id": set_id,
                "lease_id": resolved_lease,
                "images": images,
                "skipped": skipped,
                "warnings": warnings,
            }
        finally:
            while True:
                extra_prune = False
                with self._state_lock:
                    if self._prune_pending and not self._leases:
                        self._prune_pending = False
                        extra_prune = True
                    else:
                        self._mutating_active = False
                        break
                if extra_prune:
                    self._prune_orphans()

    def release_lease(self, lease_id: str) -> bool:
        should_prune = False
        with self._state_lock:
            released = self._leases.pop(lease_id, None) is not None
            if (
                self._prune_pending
                and not self._leases
                and not self._mutating_active
                and self._operation is None
            ):
                self._mutating_active = True
                should_prune = True
        if should_prune:
            try:
                self._prune_orphans()
                with self._state_lock:
                    self._prune_pending = False
            finally:
                with self._state_lock:
                    self._mutating_active = False
        return released

    def request_abort(self) -> bool:
        with self._persist_lock:
            payload: dict[str, Any] | None = None
            with self._state_lock:
                if self._operation is None:
                    return False
                self._abort_event.set()
                if not self._operation.abort_requested:
                    self._operation = replace(self._operation, abort_requested=True)
                    payload = self._build_index_payload_locked()
            if payload is not None:
                atomic_write_json(self.index_path, payload, mode=0o600, fsync=True)
            return True


def create_library_ipc_server(service: LibraryService, socket_path: Path) -> UnixHTTPServer:
    """Create a UnixHTTPServer wired to LibraryService endpoints."""

    def handle_health(_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return HTTP_OK, service.health()

    def handle_status(_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return HTTP_OK, service.status()

    def handle_refresh(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        raw_set_id = body.get("set_id")
        set_id: str | None = None
        if "set_id" in body and raw_set_id is not None:
            set_id = _require_non_empty_str_param(raw_set_id, "set_id")
        op = service.start_refresh(set_id=set_id)
        return HTTP_ACCEPTED, {"ok": True, "operation": op.to_public_dict()}

    def handle_prepare_set(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        set_id = _require_non_empty_str_param(body.get("set_id"), "set_id")
        raw_lease_id = body.get("lease_id")
        lease_id: str | None = None
        if "lease_id" in body and raw_lease_id is not None:
            lease_id = _require_non_empty_str_param(raw_lease_id, "lease_id")
        result = service.prepare_set(set_id=set_id, lease_id=lease_id)
        return HTTP_OK, result

    def handle_release_lease(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        lease_id = _require_non_empty_str_param(body.get("lease_id"), "lease_id")
        released = service.release_lease(lease_id)
        return HTTP_OK, {"ok": True, "released": released}

    def handle_abort(_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        aborted = service.request_abort()
        return HTTP_OK, {"ok": True, "aborted": aborted}

    return UnixHTTPServer(
        socket_path,
        routes={
            ("GET", "/health"): handle_health,
            ("GET", "/status"): handle_status,
            ("POST", "/refresh"): handle_refresh,
            ("POST", "/prepare-set"): handle_prepare_set,
            ("POST", "/release-lease"): handle_release_lease,
            ("POST", "/abort"): handle_abort,
        },
    )


def serve_daemon(
    service: LibraryService,
    socket_path: Path,
    *,
    stop_event: threading.Event | None = None,
) -> int:
    """Serve library IPC requests until stop_event or SIGINT/SIGTERM."""
    event = stop_event if stop_event is not None else threading.Event()
    server: UnixHTTPServer | None = None
    server_thread: threading.Thread | None = None
    previous_handlers: dict[int, Any] = {}
    in_main_thread = threading.current_thread() is threading.main_thread()

    try:
        try:
            server = create_library_ipc_server(service, socket_path)
        except OSError as exc:
            logger.error("Failed to start library IPC server on %s: %s", socket_path, exc)
            return 2

        server_thread = threading.Thread(
            target=server.serve_forever,
            name="library-ipc-server",
            daemon=True,
        )

        if in_main_thread:
            def _handle_signal(_signum: int, _frame: Any) -> None:
                event.set()

            for sig in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, _handle_signal)

        server_thread.start()
        logger.info("Library daemon listening on %s", socket_path)
        event.wait()
        return 0
    finally:
        if in_main_thread:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
        service.request_abort()
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5.0)
        service.close()


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--config", type=Path, help="Path to config.json")
    common.add_argument("--image-root", type=Path, help="Override configured image_root")
    common.add_argument("--state-dir", type=Path, help="Override configured state_dir")
    common.add_argument("--cache-dir", type=Path, help="Override configured cache_dir")
    common.add_argument("--tv-ip", help="Override configured tv_ip")
    common.add_argument("--bind-ip", help="Override configured bind_ip")
    common.add_argument("--port", type=int, help="Override configured port")

    parser = argparse.ArgumentParser(
        prog="python -m frame_art_mode.library_daemon",
        description="Manage Frame Art Mode image library scanning and persistent cache.",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", parents=[common], help="Run the library IPC daemon")
    subparsers.add_parser(
        "status", parents=[common], help="Fetch status from running library daemon"
    )

    refresh_parser = subparsers.add_parser(
        "refresh", parents=[common], help="Refresh the library cache"
    )
    refresh_parser.add_argument("--set", dest="set_id", default=None, help="Scope refresh to one set_id")
    refresh_parser.add_argument(
        "--offline",
        action="store_true",
        help="Run refresh directly against cache_dir using library.lock instead of library.sock",
    )

    subparsers.add_parser(
        "abort", parents=[common], help="Request cooperative abort of running refresh"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

    script_dir = Path(__file__).resolve().parent.parent
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"

    config_path = getattr(args, "config", script_dir / "config.json")
    overrides: dict[str, Any] = {
        "image_root": getattr(args, "image_root", None),
        "state_dir": getattr(args, "state_dir", None),
        "cache_dir": getattr(args, "cache_dir", None),
        "tv_ip": getattr(args, "tv_ip", None),
        "bind_ip": getattr(args, "bind_ip", None),
        "port": getattr(args, "port", None),
    }

    try:
        config = load_config(config_path, overrides=overrides, script_dir=script_dir)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if command == "serve":
        try:
            service = LibraryService(config)
        except LibraryError as exc:
            print(f"Library startup error: {exc}", file=sys.stderr)
            return 2
        return serve_daemon(service, config.library_sock)

    if command == "refresh" and args.offline:
        try:
            service = LibraryService(config)
        except LibraryError as exc:
            print(f"Library lock error: {exc}", file=sys.stderr)
            return 2
        try:
            result = service.refresh_sync(set_id=args.set_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except IPCError as exc:
            print(f"Refresh error: {exc}", file=sys.stderr)
            return 1
        finally:
            service.close()

    try:
        if command == "status":
            payload = call_json(config.library_sock, "GET", "/status")
        elif command == "refresh":
            body: dict[str, Any] = {}
            if args.set_id is not None:
                body["set_id"] = args.set_id
            payload = call_json(config.library_sock, "POST", "/refresh", body=body)
        elif command == "abort":
            payload = call_json(config.library_sock, "POST", "/abort", body={})
        else:
            parser.error(f"unknown command: {command}")
            return 2
    except IPCError as exc:
        print(f"Library IPC error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
