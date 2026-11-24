from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

ROOT_SET = "root"
OCCASION_PREFIX = "occasion:"
ART_STORE_FAVORITES_CATEGORY = "MY-C0004"
ART_STORE_ID_PREFIX = "SAM-"
NORMALIZATION_VERSION = 1

VALID_OPERATION_RESULTS = frozenset({"completed", "aborted", "failed", "interrupted"})


class FrameArtError(Exception):
    """Base domain exception for Frame Art Mode."""


class LibraryError(FrameArtError):
    """Raised when image library discovery, caching, or lease handling fails."""


class ImageError(ValueError, FrameArtError):
    """Raised when a source image cannot be decoded or normalized safely for the TV."""


class TVServiceError(FrameArtError):
    """Raised when TV communication, manifest ownership, or TV state transitions fail."""


ServiceError = TVServiceError


class UploadError(TVServiceError):
    """Raised when uploading or verifying an image on the TV fails."""

    def __init__(self, message: str, content_id: str | None = None) -> None:
        super().__init__(message)
        self.content_id = content_id


class AbortRequested(FrameArtError):
    """Raised when an active operation stops cleanly on cooperative abort."""


class IPCError(FrameArtError):
    """Raised when Unix domain socket IPC fails or returns an error response."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        code: str = "ipc_error",
        offline: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.offline = offline


@runtime_checkable
class TVArtClient(Protocol):
    """Protocol matching the samsungframetv art API surface used by tv_daemon."""

    def get_artmode(self) -> Any: ...

    def available(self, category: str | None = None) -> list[dict[str, Any]]: ...

    def upload(self, file: Any, file_type: str = "JPEG") -> Any: ...

    def delete(self, content_id: str) -> Any: ...

    def select_image(
        self,
        content_id: str,
        category: str | None = None,
        show: bool = True,
    ) -> Any: ...

    def get_current(self) -> dict[str, Any]: ...


def close_tv_client(client: Any) -> None:
    """Safely close a TV client connection if it exposes a callable close() method."""
    if client is None:
        return
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.debug("Ignoring error while closing TV client", exc_info=True)


def utc_now_iso() -> str:
    """Return the current UTC timestamp formatted as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tv_key_for_ip(tv_ip: str) -> str:
    """Return the deterministic 16-char directory key for a TV IP address."""
    return hashlib.sha256(tv_ip.encode("utf-8")).hexdigest()[:16]


def ensure_private_dir(path: Path) -> Path:
    """Create directory (and parents if needed) with 0o700 permissions."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    fsync: bool = False,
) -> None:
    """Atomically write bytes to path via a temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
        temporary.replace(path)
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    mode: int = 0o600,
    fsync: bool = True,
) -> None:
    """Atomically serialize payload as UTF-8 JSON and write to path."""
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    atomic_write_bytes(path, data, mode=mode, fsync=fsync)


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _require_non_empty_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_optional_str(value: Any, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} must be a string or null")
    return value


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _require_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _require_optional_int(
    value: Any,
    name: str,
    *,
    minimum: int | None = None,
) -> int | None:
    if value is None:
        return None
    return _require_int(value, name, minimum=minimum)


def _require_str_tuple(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence of strings")
    items: list[str] = []
    for item in value:
        items.append(_require_non_empty_str(item, f"{name} item"))
    return tuple(items)


def _require_signature(value: Any) -> tuple[int, int, int, int]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 4:
        raise ValueError("signature must be a 4-element integer sequence")
    return (
        _require_int(value[0], "signature[0]"),
        _require_int(value[1], "signature[1]"),
        _require_int(value[2], "signature[2]", minimum=0),
        _require_int(value[3], "signature[3]", minimum=0),
    )


@dataclass(frozen=True)
class OperationState:
    kind: str
    phase: str
    message: str
    current_item: str | None = None
    completed_ops: int = 0
    total_ops: int = 0
    abort_requested: bool = False
    started_at: str = field(default_factory=utc_now_iso)
    known_uploaded: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_str(self.kind, "kind")
        _require_non_empty_str(self.phase, "phase")
        _require_str(self.message, "message")
        _require_optional_str(self.current_item, "current_item")
        _require_int(self.completed_ops, "completed_ops", minimum=0)
        _require_int(self.total_ops, "total_ops", minimum=0)
        _require_bool(self.abort_requested, "abort_requested")
        _require_non_empty_str(self.started_at, "started_at")
        object.__setattr__(
            self,
            "known_uploaded",
            _require_str_tuple(self.known_uploaded, "known_uploaded"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_public_dict()
        payload["known_uploaded"] = list(self.known_uploaded)
        return payload

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "phase": self.phase,
            "message": self.message,
            "current_item": self.current_item,
            "completed_ops": self.completed_ops,
            "total_ops": self.total_ops,
            "abort_requested": self.abort_requested,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OperationState:
        if not isinstance(data, Mapping):
            raise ValueError("operation state must be a mapping")
        return cls(
            kind=_require_non_empty_str(data.get("kind"), "kind"),
            phase=_require_non_empty_str(data.get("phase"), "phase"),
            message=_require_str(data.get("message", ""), "message"),
            current_item=_require_optional_str(data.get("current_item"), "current_item"),
            completed_ops=_require_int(data.get("completed_ops", 0), "completed_ops", minimum=0),
            total_ops=_require_int(data.get("total_ops", 0), "total_ops", minimum=0),
            abort_requested=_require_bool(data.get("abort_requested", False), "abort_requested"),
            started_at=_require_non_empty_str(data.get("started_at"), "started_at"),
            known_uploaded=_require_str_tuple(
                data.get("known_uploaded", []),
                "known_uploaded",
            ),
        )


@dataclass(frozen=True)
class OperationResult:
    kind: str
    state: str
    message: str
    timestamp: str = field(default_factory=utc_now_iso)
    recovery_pending: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_str(self.kind, "kind")
        state = _require_non_empty_str(self.state, "state")
        if state not in VALID_OPERATION_RESULTS:
            raise ValueError(f"invalid operation result state: {state!r}")
        _require_str(self.message, "message")
        _require_non_empty_str(self.timestamp, "timestamp")
        object.__setattr__(
            self,
            "recovery_pending",
            _require_str_tuple(self.recovery_pending, "recovery_pending"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_public_dict()
        payload["recovery_pending"] = list(self.recovery_pending)
        return payload

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "state": self.state,
            "message": self.message,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OperationResult:
        if not isinstance(data, Mapping):
            raise ValueError("operation result must be a mapping")
        return cls(
            kind=_require_non_empty_str(data.get("kind"), "kind"),
            state=_require_non_empty_str(data.get("state"), "state"),
            message=_require_str(data.get("message"), "message"),
            timestamp=_require_non_empty_str(data.get("timestamp"), "timestamp"),
            recovery_pending=_require_str_tuple(
                data.get("recovery_pending", []),
                "recovery_pending",
            ),
        )


@dataclass(frozen=True)
class SourceImage:
    path: Path
    set_id: str
    source_rel: str
    name_rel: str
    signature: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise ValueError("path must be a pathlib.Path")
        _require_non_empty_str(self.set_id, "set_id")
        _require_non_empty_str(self.source_rel, "source_rel")
        _require_non_empty_str(self.name_rel, "name_rel")
        object.__setattr__(self, "signature", _require_signature(self.signature))

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "set_id": self.set_id,
            "source_rel": self.source_rel,
            "name_rel": self.name_rel,
            "signature": list(self.signature),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SourceImage:
        if not isinstance(data, Mapping):
            raise ValueError("source image must be a mapping")
        raw_path = _require_non_empty_str(data.get("path"), "path")
        return cls(
            path=Path(raw_path),
            set_id=_require_non_empty_str(data.get("set_id"), "set_id"),
            source_rel=_require_non_empty_str(data.get("source_rel"), "source_rel"),
            name_rel=_require_non_empty_str(data.get("name_rel"), "name_rel"),
            signature=_require_signature(data.get("signature")),
        )


@dataclass(frozen=True)
class CachedImage:
    set_id: str
    source_rel: str
    name_rel: str
    signature: tuple[int, int, int, int]
    fingerprint: str | None = None
    tv_path: str | None = None
    thumb_path: str | None = None
    skip_reason: str | None = None
    normalization_version: int = NORMALIZATION_VERSION
    max_image_bytes: int = 5242880
    byte_size: int | None = None
    width: int | None = None
    height: int | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.set_id, "set_id")
        _require_non_empty_str(self.source_rel, "source_rel")
        _require_non_empty_str(self.name_rel, "name_rel")
        object.__setattr__(self, "signature", _require_signature(self.signature))
        _require_optional_str(self.fingerprint, "fingerprint")
        _require_optional_str(self.tv_path, "tv_path")
        _require_optional_str(self.thumb_path, "thumb_path")
        _require_optional_str(self.skip_reason, "skip_reason")
        _require_int(self.normalization_version, "normalization_version", minimum=1)
        _require_int(self.max_image_bytes, "max_image_bytes", minimum=1)
        _require_optional_int(self.byte_size, "byte_size", minimum=0)
        _require_optional_int(self.width, "width", minimum=0)
        _require_optional_int(self.height, "height", minimum=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "set_id": self.set_id,
            "source_rel": self.source_rel,
            "name_rel": self.name_rel,
            "signature": list(self.signature),
            "fingerprint": self.fingerprint,
            "tv_path": self.tv_path,
            "thumb_path": self.thumb_path,
            "skip_reason": self.skip_reason,
            "normalization_version": self.normalization_version,
            "max_image_bytes": self.max_image_bytes,
            "byte_size": self.byte_size,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CachedImage:
        if not isinstance(data, Mapping):
            raise ValueError("cached image must be a mapping")
        return cls(
            set_id=_require_non_empty_str(data.get("set_id"), "set_id"),
            source_rel=_require_non_empty_str(data.get("source_rel"), "source_rel"),
            name_rel=_require_non_empty_str(data.get("name_rel"), "name_rel"),
            signature=_require_signature(data.get("signature")),
            fingerprint=_require_optional_str(data.get("fingerprint"), "fingerprint"),
            tv_path=_require_optional_str(data.get("tv_path"), "tv_path"),
            thumb_path=_require_optional_str(data.get("thumb_path"), "thumb_path"),
            skip_reason=_require_optional_str(data.get("skip_reason"), "skip_reason"),
            normalization_version=_require_int(
                data.get("normalization_version", NORMALIZATION_VERSION),
                "normalization_version",
                minimum=1,
            ),
            max_image_bytes=_require_int(
                data.get("max_image_bytes", 5242880),
                "max_image_bytes",
                minimum=1,
            ),
            byte_size=_require_optional_int(data.get("byte_size"), "byte_size", minimum=0),
            width=_require_optional_int(data.get("width"), "width", minimum=0),
            height=_require_optional_int(data.get("height"), "height", minimum=0),
        )


@dataclass(frozen=True)
class PreparedImage:
    source_rel: str
    filename: str
    fingerprint: str
    cache_path: Path
    byte_size: int | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.source_rel, "source_rel")
        _require_non_empty_str(self.filename, "filename")
        _require_non_empty_str(self.fingerprint, "fingerprint")
        if not isinstance(self.cache_path, Path):
            raise ValueError("cache_path must be a pathlib.Path")
        _require_optional_int(self.byte_size, "byte_size", minimum=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_rel": self.source_rel,
            "filename": self.filename,
            "fingerprint": self.fingerprint,
            "cache_path": str(self.cache_path),
            "byte_size": self.byte_size,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PreparedImage:
        if not isinstance(data, Mapping):
            raise ValueError("prepared image must be a mapping")
        raw_path = _require_non_empty_str(data.get("cache_path"), "cache_path")
        return cls(
            source_rel=_require_non_empty_str(data.get("source_rel"), "source_rel"),
            filename=_require_non_empty_str(data.get("filename"), "filename"),
            fingerprint=_require_non_empty_str(data.get("fingerprint"), "fingerprint"),
            cache_path=Path(raw_path),
            byte_size=_require_optional_int(data.get("byte_size"), "byte_size", minimum=0),
        )


@dataclass(frozen=True)
class SetSettings:
    random: bool = True
    mix_art_store_favorites: bool = False
    interval_minutes: int = 15

    def __post_init__(self) -> None:
        _require_bool(self.random, "random")
        _require_bool(self.mix_art_store_favorites, "mix_art_store_favorites")
        _require_int(self.interval_minutes, "interval_minutes", minimum=1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "random": self.random,
            "mix_art_store_favorites": self.mix_art_store_favorites,
            "interval_minutes": self.interval_minutes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None = None) -> SetSettings:
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ValueError("set settings must be a mapping")
        return cls(
            random=_require_bool(data.get("random", True), "random"),
            mix_art_store_favorites=_require_bool(
                data.get("mix_art_store_favorites", False),
                "mix_art_store_favorites",
            ),
            interval_minutes=_require_int(
                data.get("interval_minutes", 15),
                "interval_minutes",
                minimum=1,
            ),
        )


@dataclass(frozen=True)
class ManifestEntry:
    source_rel: str
    filename: str
    fingerprint: str
    content_id: str

    def __post_init__(self) -> None:
        _require_str(self.source_rel, "source_rel")
        _require_str(self.filename, "filename")
        _require_str(self.fingerprint, "fingerprint")
        _require_non_empty_str(self.content_id, "content_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_rel": self.source_rel,
            "filename": self.filename,
            "fingerprint": self.fingerprint,
            "content_id": self.content_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ManifestEntry:
        if not isinstance(data, Mapping):
            raise ValueError("manifest entry must be a mapping")
        source_rel = _require_str(data.get("source_rel"), "source_rel")
        filename = data.get("filename")
        if filename is None:
            filename = Path(source_rel).name
        else:
            filename = _require_str(filename, "filename")
        return cls(
            source_rel=source_rel,
            filename=filename,
            fingerprint=_require_str(data.get("fingerprint"), "fingerprint"),
            content_id=_require_non_empty_str(data.get("content_id"), "content_id"),
        )


@dataclass(frozen=True)
class Manifest:
    schema_version: int = 1
    tv_ip: str = ""
    active_set: str | None = None
    entries: tuple[ManifestEntry, ...] = ()
    cleanup_pending: tuple[str, ...] = ()
    settings: dict[str, SetSettings] = field(default_factory=dict)
    current_content_id: str | None = None

    def __post_init__(self) -> None:
        version = _require_int(self.schema_version, "schema_version", minimum=1)
        if version != 1:
            raise ValueError(f"unsupported manifest schema_version: {version}")
        _require_str(self.tv_ip, "tv_ip")
        _require_optional_str(self.active_set, "active_set")
        _require_optional_str(self.current_content_id, "current_content_id")

        if isinstance(self.entries, (str, bytes)) or not isinstance(self.entries, Sequence):
            raise ValueError("entries must be a sequence of ManifestEntry")
        seen_ids: set[str] = set()
        normalized_entries: list[ManifestEntry] = []
        for entry in self.entries:
            if not isinstance(entry, ManifestEntry):
                raise ValueError("entries must contain ManifestEntry instances")
            if entry.content_id in seen_ids:
                raise ValueError(f"duplicate manifest content_id: {entry.content_id}")
            seen_ids.add(entry.content_id)
            normalized_entries.append(entry)
        object.__setattr__(self, "entries", tuple(normalized_entries))

        object.__setattr__(
            self,
            "cleanup_pending",
            _require_str_tuple(self.cleanup_pending, "cleanup_pending"),
        )

        if not isinstance(self.settings, Mapping):
            raise ValueError("settings must be a mapping")
        normalized_settings: dict[str, SetSettings] = {}
        for set_id, cfg in self.settings.items():
            _require_non_empty_str(set_id, "settings key")
            if not isinstance(cfg, SetSettings):
                raise ValueError("settings values must be SetSettings instances")
            normalized_settings[set_id] = cfg
        object.__setattr__(self, "settings", normalized_settings)

    def settings_for(self, set_id: str) -> SetSettings:
        return self.settings.get(set_id, SetSettings())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tv_ip": self.tv_ip,
            "active_set": self.active_set,
            "entries": [entry.to_dict() for entry in self.entries],
            "cleanup_pending": list(self.cleanup_pending),
            "settings": {set_id: cfg.to_dict() for set_id, cfg in self.settings.items()},
            "current_content_id": self.current_content_id,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        expected_tv_ip: str | None = None,
    ) -> Manifest:
        if not isinstance(data, Mapping):
            raise ValueError("manifest must be a mapping")
        schema_version = _require_int(data.get("schema_version"), "schema_version", minimum=1)
        if schema_version != 1:
            raise ValueError(f"unsupported manifest schema_version: {schema_version}")
        tv_ip = _require_non_empty_str(data.get("tv_ip"), "tv_ip")
        if expected_tv_ip is not None and tv_ip != expected_tv_ip:
            raise ValueError(f"manifest tv_ip {tv_ip!r} does not match expected {expected_tv_ip!r}")

        raw_entries = data.get("entries")
        if isinstance(raw_entries, (str, bytes)) or not isinstance(raw_entries, Sequence):
            raise ValueError("manifest entries must be a list")
        entries = tuple(ManifestEntry.from_dict(item) for item in raw_entries)

        raw_cleanup = data.get("cleanup_pending")
        cleanup_pending = _require_str_tuple(raw_cleanup, "cleanup_pending")

        raw_settings = data.get("settings", {})
        if not isinstance(raw_settings, Mapping):
            raise ValueError("manifest settings must be a mapping")
        settings: dict[str, SetSettings] = {}
        for set_id, raw_cfg in raw_settings.items():
            settings[_require_non_empty_str(set_id, "settings key")] = SetSettings.from_dict(
                raw_cfg
            )

        return cls(
            schema_version=schema_version,
            tv_ip=tv_ip,
            active_set=_require_optional_str(data.get("active_set"), "active_set"),
            entries=entries,
            cleanup_pending=cleanup_pending,
            settings=settings,
            current_content_id=_require_optional_str(
                data.get("current_content_id"),
                "current_content_id",
            ),
        )


@dataclass(frozen=True)
class CurrentDisplay:
    content_id: str | None = None
    category: str | None = None
    source_rel: str | None = None
    is_art_store: bool = False

    def __post_init__(self) -> None:
        _require_optional_str(self.content_id, "content_id")
        _require_optional_str(self.category, "category")
        _require_optional_str(self.source_rel, "source_rel")
        _require_bool(self.is_art_store, "is_art_store")

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_id": self.content_id,
            "category": self.category,
            "source_rel": self.source_rel,
            "is_art_store": self.is_art_store,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CurrentDisplay:
        if not isinstance(data, Mapping):
            raise ValueError("current display must be a mapping")
        return cls(
            content_id=_require_optional_str(data.get("content_id"), "content_id"),
            category=_require_optional_str(data.get("category"), "category"),
            source_rel=_require_optional_str(data.get("source_rel"), "source_rel"),
            is_art_store=_require_bool(data.get("is_art_store", False), "is_art_store"),
        )
