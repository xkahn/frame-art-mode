from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Config:
    script_dir: Path
    image_root: Path
    state_dir: Path
    tv_ip: str
    bind_ip: str
    port: int = 8080


def load_config(
    config_path: Path,
    overrides: dict[str, Any] | None = None,
    script_dir: Path | None = None,
) -> Config:
    script_dir = (script_dir or Path(__file__).resolve().parent.parent).resolve()
    config_path = config_path.resolve()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read configuration {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("configuration must be a JSON object")
    raw.update({key: value for key, value in (overrides or {}).items() if value is not None})

    tv_ip = raw.get("tv_ip")
    bind_ip = raw.get("bind_ip")
    if not tv_ip or not bind_ip:
        raise ValueError("configuration must provide both tv_ip and bind_ip")
    try:
        ipaddress.ip_address(tv_ip)
        bind_address = ipaddress.ip_address(bind_ip)
    except ValueError as exc:
        raise ValueError("tv_ip and bind_ip must be valid IP addresses") from exc
    if bind_address.is_unspecified or bind_address.is_multicast:
        raise ValueError("bind_ip must be a specific unicast interface address, not a wildcard")

    def configured_path(value: Any, default: Path) -> Path:
        path = Path(value).expanduser() if value is not None else default
        if not path.is_absolute():
            path = config_path.parent / path
        return path.resolve()

    image_root = configured_path(raw.get("image_root"), script_dir / "image")
    state_dir = configured_path(raw.get("state_dir"), script_dir / "state")
    if state_dir == image_root or state_dir.is_relative_to(image_root):
        raise ValueError("state_dir must be outside image_root")
    port = int(raw.get("port", 8080))
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return Config(script_dir, image_root, state_dir, str(tv_ip), str(bind_ip), port)
