from __future__ import annotations

import hashlib
import ipaddress
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when application configuration is missing or invalid."""


_IPV4_BROADCAST = ipaddress.IPv4Address("255.255.255.255")


@dataclass(frozen=True)
class Config:
    script_dir: Path
    image_root: Path
    state_dir: Path
    tv_ip: str
    bind_ip: str
    port: int = 8080
    cache_dir: Path = field(default=None)
    preview_seconds: int = 15
    max_image_bytes: int = 5242880
    tv_op_delay_seconds: float = 3.0
    tv_batch_settle_seconds: float = 12.0
    tv_timeout_seconds: float = 30.0
    tv_verify_retries: int = 4
    tv_verify_interval_seconds: float = 3.0
    max_set_images_warning: int = 50

    def __post_init__(self) -> None:
        if self.cache_dir is None:
            object.__setattr__(self, "cache_dir", self.state_dir / "cache")

    @property
    def tv_key(self) -> str:
        return hashlib.sha256(self.tv_ip.encode("utf-8")).hexdigest()[:16]

    @property
    def tv_dir(self) -> Path:
        return self.state_dir / self.tv_key

    @property
    def library_sock(self) -> Path:
        return self.state_dir / "library.sock"

    @property
    def tv_sock(self) -> Path:
        return self.tv_dir / "tv.sock"

    @property
    def library_lock(self) -> Path:
        return self.state_dir / "library.lock"

    @property
    def tv_lock(self) -> Path:
        return self.tv_dir / "tv.lock"

    @property
    def supervisor_pid(self) -> Path:
        return self.state_dir / "supervisor.pid"


def _parse_int(
    raw: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is not None:
            raise ConfigError(f"{key} must be between {minimum} and {maximum}")
        raise ConfigError(f"{key} must be >= {minimum}")
    return value


def _parse_float(
    raw: dict[str, Any],
    key: str,
    default: float,
    *,
    minimum: float,
) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"{key} must be a finite number")
    if number < minimum:
        raise ConfigError(f"{key} must be >= {minimum}")
    return number


def _strip_jsonc_comments(text: str) -> str:
    """Remove // and /* ... */ comments outside JSON string literals, preserving newlines."""
    out: list[str] = []
    i = 0
    length = len(text)
    in_string = False

    while i < length:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < length:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif ch == "/" and i + 1 < length and text[i + 1] == "/":
            i += 2
            while i < length and text[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < length and text[i + 1] == "*":
            i += 2
            closed = False
            while i < length:
                if text[i] == "*" and i + 1 < length and text[i + 1] == "/":
                    i += 2
                    closed = True
                    break
                if text[i] == "\n":
                    out.append("\n")
                i += 1
            if not closed:
                raise ConfigError("unterminated block comment in configuration")
        else:
            out.append(ch)
            i += 1

    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """Remove trailing commas before '}' or ']' outside JSON string literals."""
    out: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    prev_non_ws = ""

    while i < length:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < length:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
                prev_non_ws = '"'
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue

        if ch == "," and prev_non_ws not in ("", "{", "[", ","):
            j = i + 1
            while j < length and text[j] in " \t\r\n":
                j += 1
            if j < length and text[j] in "}]":
                i += 1
                continue

        out.append(ch)
        if ch not in " \t\r\n":
            prev_non_ws = ch
        i += 1

    return "".join(out)


def loads_jsonc(text: str) -> Any:
    """Parse JSON with Comments (JSONC: //, /* ... */, and trailing commas)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    stripped = _strip_trailing_commas(_strip_jsonc_comments(text))
    return json.loads(stripped)


def _resolve_path(path: Path, label: str) -> Path:
    try:
        return path.resolve()
    except (RuntimeError, OSError) as exc:
        raise ConfigError(f"cannot resolve {label} {path}: {exc}") from exc


def load_config(
    config_path: Path,
    overrides: dict[str, Any] | None = None,
    script_dir: Path | None = None,
) -> Config:
    base_script_dir = script_dir or _resolve_path(Path(__file__), "module path").parent.parent
    script_dir = _resolve_path(base_script_dir, "script_dir")
    config_path = _resolve_path(config_path, "config_path")
    try:
        raw = loads_jsonc(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except ConfigError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read configuration {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration must be a JSON object")
    raw.update({key: value for key, value in (overrides or {}).items() if value is not None})

    tv_ip = raw.get("tv_ip")
    bind_ip = raw.get("bind_ip")
    if (
        not isinstance(tv_ip, str)
        or not isinstance(bind_ip, str)
        or not tv_ip.strip()
        or not bind_ip.strip()
    ):
        raise ConfigError("configuration must provide both tv_ip and bind_ip")
    try:
        tv_address = ipaddress.ip_address(tv_ip.strip())
        bind_address = ipaddress.ip_address(bind_ip.strip())
    except ValueError as exc:
        raise ConfigError("tv_ip and bind_ip must be valid IP addresses") from exc
    if tv_address.is_unspecified or tv_address.is_multicast or tv_address == _IPV4_BROADCAST:
        raise ConfigError("tv_ip must be a specific unicast TV IP address")
    if (
        bind_address.is_unspecified
        or bind_address.is_multicast
        or bind_address == _IPV4_BROADCAST
    ):
        raise ConfigError(
            "bind_ip must be a specific unicast interface address, not a wildcard or broadcast"
        )

    def configured_path(key: str, value: Any, default: Path) -> Path:
        if value is not None:
            if not isinstance(value, (str, Path)) or (
                isinstance(value, str) and not value.strip()
            ):
                raise ConfigError(f"{key} must be a non-empty path")
            path = Path(value).expanduser()
        else:
            path = default
        if not path.is_absolute():
            path = config_path.parent / path
        return _resolve_path(path, key)

    image_root = configured_path("image_root", raw.get("image_root"), script_dir / "image")
    state_dir = configured_path("state_dir", raw.get("state_dir"), script_dir / "state")
    if state_dir == image_root or state_dir.is_relative_to(image_root):
        raise ConfigError("state_dir must be outside image_root")
    cache_dir = configured_path("cache_dir", raw.get("cache_dir"), state_dir / "cache")
    if cache_dir == image_root or cache_dir.is_relative_to(image_root):
        raise ConfigError("cache_dir must be outside image_root")

    port = _parse_int(raw, "port", 8080, minimum=1, maximum=65535)
    preview_seconds = _parse_int(raw, "preview_seconds", 15, minimum=1)
    max_image_bytes = _parse_int(raw, "max_image_bytes", 5242880, minimum=262144)
    tv_op_delay_seconds = _parse_float(raw, "tv_op_delay_seconds", 3.0, minimum=0.5)
    tv_batch_settle_seconds = _parse_float(raw, "tv_batch_settle_seconds", 12.0, minimum=0.0)
    tv_timeout_seconds = _parse_float(raw, "tv_timeout_seconds", 30.0, minimum=5.0)
    tv_verify_retries = _parse_int(raw, "tv_verify_retries", 4, minimum=1)
    tv_verify_interval_seconds = _parse_float(raw, "tv_verify_interval_seconds", 3.0, minimum=0.5)
    max_set_images_warning = _parse_int(raw, "max_set_images_warning", 50, minimum=1)

    return Config(
        script_dir=script_dir,
        image_root=image_root,
        state_dir=state_dir,
        tv_ip=str(tv_address),
        bind_ip=str(bind_address),
        port=port,
        cache_dir=cache_dir,
        preview_seconds=preview_seconds,
        max_image_bytes=max_image_bytes,
        tv_op_delay_seconds=tv_op_delay_seconds,
        tv_batch_settle_seconds=tv_batch_settle_seconds,
        tv_timeout_seconds=tv_timeout_seconds,
        tv_verify_retries=tv_verify_retries,
        tv_verify_interval_seconds=tv_verify_interval_seconds,
        max_set_images_warning=max_set_images_warning,
    )
