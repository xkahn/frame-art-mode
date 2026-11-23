from __future__ import annotations

import hashlib
import json
import fcntl
import random
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .images import PreparedImage, discover_sets, prepare_set

ART_STORE_FAVORITES_CATEGORY = "MY-C0004"


class ServiceError(RuntimeError):
    pass


class UploadError(ServiceError):
    def __init__(self, message: str, content_id: str | None = None) -> None:
        super().__init__(message)
        self.content_id = content_id


def default_tv_factory(config: Config, token_file: Path) -> Any:
    from samsungframetv import SamsungTVWS

    return SamsungTVWS(config.tv_ip, token_file=str(token_file), timeout=30).art(timeout=30)


class FrameService:
    def __init__(
        self,
        config: Config,
        tv_factory: Callable[[Config, Path], Any] = default_tv_factory,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        chooser: Callable[[list[dict[str, Any]]], dict[str, Any]] = random.choice,
    ) -> None:
        self.config = config
        self.tv_factory = tv_factory
        self.sleeper = sleeper
        self.clock = clock
        self.chooser = chooser
        self.config.image_root.mkdir(parents=True, exist_ok=True)
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        tv_key = hashlib.sha256(config.tv_ip.encode()).hexdigest()[:16]
        self.tv_dir = config.state_dir / tv_key
        self.tv_dir.mkdir(parents=True, exist_ok=True)
        self.tv_dir.chmod(0o700)
        lock_path = self.tv_dir / "service.lock"
        self._lock_file = lock_path.open("a+")
        lock_path.chmod(0o600)
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise ServiceError(f"another Frame Art Mode instance is already managing TV {config.tv_ip}") from exc
        self.manifest_path = self.tv_dir / "manifest.json"
        self.token_file = self.tv_dir / "tv-token.txt"
        self._manifest_persisted = self.manifest_path.exists()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._syncing = False
        self._last_error: str | None = None
        self._last_warnings: list[str] = []
        self._tv_ready: bool | None = None
        self._manifest_error: str | None = None
        self._preview_restore_id: str | None = None
        self._preview_restore_category: str | None = None
        self._preview_until: float | None = None
        self._current_content_id: str | None = None
        self._next_at = self.clock()
        self._paused_remaining: float | None = None
        self._manifest = self._load_manifest()
        if self._manifest:
            self._current_content_id = self._manifest.get("current_content_id")
            if self._manifest.get("entries") and self._manifest.get("active_set"):
                minutes = self.settings_for(self._manifest["active_set"])["interval_minutes"]
                self._next_at = self.clock() + minutes * 60

    def _empty_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "tv_ip": self.config.tv_ip,
            "active_set": None,
            "entries": [],
            "cleanup_pending": [],
            "settings": {},
            "current_content_id": None,
        }

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return self._empty_manifest()
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema_version") != 1
                or manifest.get("tv_ip") != self.config.tv_ip
                or not isinstance(manifest.get("entries"), list)
                or not isinstance(manifest.get("cleanup_pending"), list)
                or not isinstance(manifest.get("settings", {}), dict)
            ):
                raise ValueError("manifest fields or TV identity are invalid")
            content_ids: set[str] = set()
            for entry in manifest["entries"]:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("content_id"), str)
                    or not entry.get("content_id")
                    or not isinstance(entry.get("source_rel"), str)
                    or not isinstance(entry.get("fingerprint"), str)
                    or entry["content_id"] in content_ids
                ):
                    raise ValueError("manifest contains an invalid managed content ID")
                content_ids.add(entry["content_id"])
            if not all(isinstance(item, str) and item for item in manifest["cleanup_pending"]):
                raise ValueError("manifest contains an invalid cleanup content ID")
            if manifest.get("active_set") is not None and not isinstance(manifest["active_set"], str):
                raise ValueError("manifest contains an invalid active set")
            if manifest.get("current_content_id") is not None and not isinstance(
                manifest["current_content_id"], str
            ):
                raise ValueError("manifest contains an invalid current content ID")
            for settings in manifest["settings"].values():
                if (
                    not isinstance(settings, dict)
                    or not isinstance(settings.get("random", True), bool)
                    or not isinstance(settings.get("mix_art_store_favorites", False), bool)
                    or not isinstance(settings.get("interval_minutes", 15), int)
                    or isinstance(settings.get("interval_minutes", 15), bool)
                    or settings.get("interval_minutes", 15) < 1
                ):
                    raise ValueError("manifest contains invalid playback settings")
            manifest.setdefault("settings", {})
            manifest.setdefault("active_set", None)
            manifest.setdefault("current_content_id", None)
            self._manifest_error = None
            return manifest
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self._manifest_error = f"ownership manifest is unreadable; no TV items will be deleted: {exc}"
            return {}

    def _assert_manifest_current(self) -> None:
        if self._manifest_persisted and not self.manifest_path.exists():
            self._manifest_error = "ownership manifest disappeared; no TV items will be deleted"
            raise ServiceError(self._manifest_error)
        if not self._manifest_persisted and self.manifest_path.exists():
            self._manifest_error = "ownership manifest appeared unexpectedly; restart after review"
            raise ServiceError(self._manifest_error)
        if self._manifest_persisted:
            try:
                disk_manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self._manifest_error = f"ownership manifest became unreadable; no TV items will be deleted: {exc}"
                raise ServiceError(self._manifest_error) from exc
            if disk_manifest != self._manifest:
                self._manifest_error = "ownership manifest changed while the service was running; restart after review"
                raise ServiceError(self._manifest_error)

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        self._assert_manifest_current()
        data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        with tempfile.NamedTemporaryFile(dir=self.tv_dir, prefix=".manifest-", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(data)
                handle.flush()
                temporary.chmod(0o600)
                temporary.replace(self.manifest_path)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        self._manifest = manifest
        self._manifest_persisted = True
        self._manifest_error = None

    def _require_manifest(self) -> dict[str, Any]:
        if self._manifest_error or not self._manifest:
            raise ServiceError(self._manifest_error or "ownership manifest is unavailable")
        self._assert_manifest_current()
        return self._manifest

    def _art(self) -> Any:
        return self.tv_factory(self.config, self.token_file)

    @staticmethod
    def _is_art_mode(value: Any) -> bool:
        return value is True or str(value).strip().casefold() in {"on", "true", "1"}

    def _check_ready(self, art: Any) -> None:
        try:
            ready = self._is_art_mode(art.get_artmode())
        except Exception as exc:
            self._tv_ready = False
            raise ServiceError(f"TV is unavailable or its Art Mode status could not be read: {exc}") from exc
        self._tv_ready = ready
        if not ready:
            raise ServiceError("TV is not in Art Mode; no sync or display change was made")

    def _clear_cycle_error(self, prefix: str) -> None:
        if self._last_error and self._last_error.startswith(prefix):
            self._last_error = None

    @staticmethod
    def _content_ids(available: Any) -> set[str]:
        if not isinstance(available, list):
            raise ServiceError("TV returned an invalid available-art list")
        return {str(item["content_id"]) for item in available if isinstance(item, dict) and item.get("content_id")}

    def list_sets(self) -> tuple[dict[str, list[Any]], list[str]]:
        return discover_sets(self.config.image_root)

    def settings_for(self, set_id: str) -> dict[str, Any]:
        settings = self._manifest.get("settings", {}).get(set_id, {})
        return {
            "random": bool(settings.get("random", True)),
            "mix_art_store_favorites": bool(settings.get("mix_art_store_favorites", False)),
            "interval_minutes": int(settings.get("interval_minutes", 15)),
        }

    def save_settings(
        self, set_id: str, randomize: bool, interval_minutes: int, mix_art_store_favorites: bool
    ) -> None:
        if interval_minutes < 1:
            raise ServiceError("interval must be at least one minute")
        with self._lock:
            manifest = self._require_manifest()
            updated = dict(manifest)
            settings = dict(updated.get("settings", {}))
            settings[set_id] = {
                "random": bool(randomize),
                "mix_art_store_favorites": bool(mix_art_store_favorites),
                "interval_minutes": int(interval_minutes),
            }
            updated["settings"] = settings
            self._write_manifest(updated)

    def _warn_favorites(self, warning: str) -> None:
        self._last_warnings = [
            item for item in self._last_warnings if not item.startswith("Art Store favorites:")
        ]
        self._last_warnings.append(f"Art Store favorites: {warning}")

    def _random_pick(
        self, art: Any, entries: list[dict[str, Any]], mix_art_store_favorites: bool
    ) -> dict[str, Any]:
        candidates = [dict(entry) for entry in entries]
        content_ids = {entry["content_id"] for entry in candidates}
        if mix_art_store_favorites:
            try:
                available = art.available(ART_STORE_FAVORITES_CATEGORY)
                if not isinstance(available, list):
                    raise ServiceError("TV returned an invalid favorites list")
            except Exception as exc:
                self._warn_favorites(f"could not read favorites; using NAS images this turn: {exc}")
            else:
                self._last_warnings = [
                    item
                    for item in self._last_warnings
                    if not item.startswith("Art Store favorites:")
                ]
                unknown_ids = []
                for item in available:
                    if not isinstance(item, dict) or not item.get("content_id"):
                        continue
                    content_id = str(item["content_id"])
                    if content_id in content_ids:
                        continue
                    if not content_id.startswith("SAM-"):
                        unknown_ids.append(content_id)
                        continue
                    candidates.append(
                        {"content_id": content_id, "category": ART_STORE_FAVORITES_CATEGORY}
                    )
                    content_ids.add(content_id)
                if unknown_ids:
                    self._warn_favorites(
                        f"skipping IDs without the documented SAM- prefix: {', '.join(unknown_ids)}"
                    )
        return self.chooser(candidates)

    @staticmethod
    def _select_image(art: Any, entry: dict[str, Any]) -> None:
        if entry.get("category"):
            art.select_image(entry["content_id"], category=entry["category"])
        else:
            art.select_image(entry["content_id"])

    def _pace(self, index: int, total: int) -> None:
        if index < total - 1:
            self.sleeper(1)

    def _upload(self, art: Any, image: PreparedImage, existing_ids: set[str]) -> str:
        content_id = art.upload(image.staged_path.read_bytes(), file_type="JPEG")
        if content_id is not None:
            content_id = str(content_id)
        try:
            for attempt in range(6):
                available = self._content_ids(art.available())
                if content_id and content_id in available:
                    return content_id
                added = available - existing_ids
                if content_id is None and len(added) == 1:
                    return next(iter(added))
                if attempt < 5:
                    self.sleeper(1)
        except Exception as exc:
            if content_id:
                raise UploadError(
                    f"could not verify uploaded item {image.source_rel}: {exc}", content_id
                ) from exc
            raise
        if content_id:
            raise UploadError(
                f"TV did not verify uploaded item {image.source_rel} ({content_id})", content_id
            )
        raise UploadError(f"TV did not confirm or expose the uploaded item {image.source_rel}")

    def _delete_batch(self, art: Any, content_ids: list[str]) -> tuple[list[str], list[str]]:
        pending: list[str] = []
        errors: list[str] = []
        for index, content_id in enumerate(content_ids):
            try:
                result = art.delete(content_id)
                if result is not True:
                    if content_id in self._content_ids(art.available()):
                        raise ServiceError("TV did not confirm deletion")
            except Exception as exc:
                pending.append(content_id)
                errors.append(f"{content_id}: {exc}")
            self._pace(index, len(content_ids))
        return pending, errors

    def sync(self, set_id: str) -> dict[str, Any]:
        with self._lock:
            self._syncing = True
            self._last_error = None
            try:
                manifest = self._require_manifest()
                sets, scan_warnings = self.list_sets()
                if set_id not in sets:
                    raise ServiceError(f"image set {set_id!r} no longer exists")
                with tempfile.TemporaryDirectory(prefix="frame-art-snapshot-", dir=self.tv_dir) as snapshot:
                    prepared, image_warnings = prepare_set(sets[set_id], Path(snapshot))
                    self._last_warnings = scan_warnings + image_warnings
                    if any(
                        "source changed while its sync snapshot was being captured" in warning
                        for warning in image_warnings
                    ):
                        raise ServiceError("an image changed while the sync snapshot was being captured; retry Sync")
                    current_sets, current_warnings = self.list_sets()
                    before_sources = {item.source_rel: item.signature for item in sets[set_id]}
                    after_sources = {item.source_rel: item.signature for item in current_sets.get(set_id, [])}
                    if before_sources != after_sources or current_warnings:
                        raise ServiceError(
                            "the image library changed while the sync snapshot was being captured; retry Sync"
                        )
                    if not prepared:
                        raise ServiceError("no valid images remain; the current TV set was left unchanged")

                    old_by_source = {
                        (entry.get("source_rel"), entry.get("fingerprint")): entry
                        for entry in manifest["entries"]
                    }

                    art = self._art()
                    self._check_ready(art)
                    before = self._content_ids(art.available())
                    desired: list[dict[str, Any]] = []
                    uploaded: list[str] = []
                    upload_errors: list[str] = []
                    upload_attempts = 0
                    for image in prepared:
                        previous = old_by_source.get((image.source_rel, image.fingerprint))
                        if previous and previous["content_id"] in before:
                            desired.append(dict(previous))
                            continue
                        upload_attempts += 1
                        try:
                            content_id = self._upload(art, image, before | set(uploaded))
                            if content_id in before or content_id in uploaded:
                                raise ServiceError(f"TV returned a duplicate content ID for {image.source_rel}")
                            uploaded.append(content_id)
                            desired.append(
                                {
                                    "source_rel": image.source_rel,
                                    "filename": image.filename,
                                    "fingerprint": image.fingerprint,
                                    "content_id": content_id,
                                }
                            )
                        except Exception as exc:
                            if isinstance(exc, UploadError) and exc.content_id:
                                uploaded.append(exc.content_id)
                            upload_errors.append(f"{image.source_rel}: {exc}")
                        self._pace(
                            upload_attempts - 1,
                            sum(
                                (item.source_rel, item.fingerprint) not in old_by_source
                                for item in prepared
                            ),
                        )

                    if upload_attempts:
                        self.sleeper(12)
                    if not upload_errors:
                        try:
                            self._check_ready(art)
                        except ServiceError as exc:
                            upload_errors.append(f"TV: {exc}")
                    if upload_errors:
                        rollback_pending, rollback_errors = self._delete_batch(art, uploaded)
                        if uploaded:
                            self.sleeper(12)
                        preserved = dict(manifest)
                        preserved["cleanup_pending"] = list(
                            dict.fromkeys(manifest.get("cleanup_pending", []) + rollback_pending)
                        )
                        if rollback_pending:
                            self._write_manifest(preserved)
                        self._last_warnings += upload_errors + rollback_errors
                        raise ServiceError("upload batch failed; the previously active TV set was preserved")

                    desired_ids = {entry["content_id"] for entry in desired}
                    old_ids = [entry["content_id"] for entry in manifest["entries"] if entry["content_id"] not in desired_ids]
                    cleanup = list(
                        dict.fromkeys(
                            [item for item in manifest.get("cleanup_pending", []) if item not in desired_ids] + old_ids
                        )
                    )
                    updated = dict(manifest)
                    updated.update(
                        active_set=set_id,
                        entries=desired,
                        cleanup_pending=cleanup,
                        current_content_id=None,
                    )
                    settings = self.settings_for(set_id)
                    updated["settings"] = dict(manifest.get("settings", {}))
                    updated["settings"][set_id] = settings
                    try:
                        self._write_manifest(updated)
                    except Exception as exc:
                        rollback_pending, rollback_errors = self._delete_batch(art, uploaded)
                        if uploaded:
                            self.sleeper(12)
                        self._last_warnings += rollback_errors
                        if rollback_pending and not self._manifest_error:
                            preserved = dict(manifest)
                            preserved["cleanup_pending"] = list(
                                dict.fromkeys(manifest.get("cleanup_pending", []) + rollback_pending)
                            )
                            try:
                                self._write_manifest(preserved)
                            except Exception as persist_error:
                                self._last_warnings.append(f"could not persist rollback IDs: {persist_error}")
                        raise ServiceError(
                            f"could not commit the ownership manifest; previous TV set was retained: {exc}"
                        ) from exc

                    pending, delete_errors = self._delete_batch(art, cleanup)
                    if cleanup:
                        self.sleeper(12)
                    updated = dict(self._manifest)
                    updated["cleanup_pending"] = pending
                    display_error = None
                    if desired:
                        initial = (
                            self._random_pick(art, desired, settings["mix_art_store_favorites"])
                            if settings["random"]
                            else desired[0]
                        )
                        try:
                            self._check_ready(art)
                            self._select_image(art, initial)
                            updated["current_content_id"] = initial["content_id"]
                            self._current_content_id = initial["content_id"]
                        except Exception as exc:
                            display_error = f"new set is synced, but display selection failed: {exc}"
                            delete_errors.append(display_error)
                    self._write_manifest(updated)
                    self._next_at = self.clock() + settings["interval_minutes"] * 60
                    self._last_warnings += delete_errors
                    if pending:
                        self._last_error = "new set is active; some old TV items remain and will be retried on next Sync"
                    if display_error:
                        self._last_error = display_error
                        raise ServiceError(display_error)
                    return self.status()
            except ServiceError as exc:
                self._last_error = str(exc)
                raise
            except Exception as exc:
                self._last_error = f"sync failed: {exc}"
                raise ServiceError(self._last_error) from exc
            finally:
                self._syncing = False

    @staticmethod
    def _current_id(response: Any) -> str | None:
        if not isinstance(response, dict):
            return None
        for key in ("content_id", "contentId"):
            if response.get(key):
                return str(response[key])
        content = response.get("content")
        if isinstance(content, dict) and content.get("content_id"):
            return str(content["content_id"])
        return None

    @staticmethod
    def _current_category(response: Any) -> str | None:
        if not isinstance(response, dict):
            return None
        for item in (response, response.get("content")):
            if isinstance(item, dict):
                for key in ("category", "category_id", "categoryId"):
                    if item.get(key):
                        return str(item[key])
        return None

    def preview(self, content_id: str) -> None:
        with self._lock:
            manifest = self._require_manifest()
            if content_id not in {entry["content_id"] for entry in manifest["entries"]}:
                raise ServiceError("preview is limited to images in the currently synced set")
            art = self._art()
            self._check_ready(art)
            try:
                current = art.get_current()
                restore_id = self._current_id(current)
            except Exception as exc:
                raise ServiceError(f"could not read the current TV image: {exc}") from exc
            if restore_id is None:
                raise ServiceError("TV did not identify its current image; preview was not started")
            try:
                art.select_image(content_id)
            except Exception as exc:
                raise ServiceError(f"TV could not display the preview: {exc}") from exc
            self._preview_restore_id = restore_id
            self._preview_restore_category = self._current_category(current)
            self._preview_until = self.clock() + 15
            self._current_content_id = content_id

    def _restore_preview(self, art: Any, now: float, active_set: str) -> None:
        self._select_image(
            art,
            {"content_id": self._preview_restore_id, "category": self._preview_restore_category},
        )
        self._current_content_id = self._preview_restore_id
        self._preview_restore_id = None
        self._preview_restore_category = None
        self._preview_until = None
        self._clear_cycle_error("preview restore paused:")
        self._next_at = now + self.settings_for(active_set)["interval_minutes"] * 60

    def _cycle(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                manifest = self._manifest
                entries = manifest.get("entries", []) if manifest else []
                if self._syncing or self._manifest_error or not entries:
                    self._stop.wait(1)
                    continue
                now = self.clock()
                if self._preview_until is not None:
                    if now < self._preview_until:
                        self._stop.wait(min(1, self._preview_until - now))
                        continue
                    try:
                        art = self._art()
                        self._check_ready(art)
                        self._restore_preview(art, now, manifest["active_set"])
                    except Exception as exc:
                        self._tv_ready = False
                        self._last_error = f"preview restore paused: {exc}"
                        self._stop.wait(5)
                        continue
                if self._paused_remaining is not None:
                    try:
                        art = self._art()
                        self._check_ready(art)
                        self._clear_cycle_error("cycling paused:")
                        self._next_at = now + self._paused_remaining
                        self._paused_remaining = None
                    except ServiceError as exc:
                        self._last_error = f"cycling paused: {exc}"
                        self._stop.wait(5)
                        continue
                if now < self._next_at:
                    self._stop.wait(min(1, self._next_at - now))
                    continue
                try:
                    art = self._art()
                    self._check_ready(art)
                    settings = self.settings_for(manifest["active_set"])
                    if settings["random"]:
                        selected = self._random_pick(art, entries, settings["mix_art_store_favorites"])
                    else:
                        index = next(
                            (i for i, entry in enumerate(entries) if entry["content_id"] == self._current_content_id),
                            -1,
                        )
                        selected = entries[(index + 1) % len(entries)]
                    self._select_image(art, selected)
                    self._current_content_id = selected["content_id"]
                    updated = dict(manifest)
                    updated["current_content_id"] = self._current_content_id
                    self._write_manifest(updated)
                    self._next_at = self.clock() + settings["interval_minutes"] * 60
                    self._clear_cycle_error("cycling paused:")
                except ServiceError as exc:
                    self._last_error = f"cycling paused: {exc}"
                    self._paused_remaining = max(0, self._next_at - self.clock())
                except Exception as exc:
                    self._last_error = f"cycling paused: {exc}"
                    self._paused_remaining = max(0, self._next_at - self.clock())
            self._stop.wait(1)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._cycle, name="frame-art-cycle", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=35)
        if (not self._thread or not self._thread.is_alive()) and not self._lock_file.closed:
            self._lock_file.close()

    def status(self) -> dict[str, Any]:
        manifest = self._manifest or self._empty_manifest()
        return {
            "active_set": manifest.get("active_set"),
            "entries": manifest.get("entries", []),
            "cleanup_pending": manifest.get("cleanup_pending", []),
            "syncing": self._syncing,
            "tv_ready": self._tv_ready,
            "last_error": self._last_error or self._manifest_error,
            "warnings": list(self._last_warnings),
        }
