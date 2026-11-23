from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def require_supported_python(version_info=None) -> None:
    version_info = sys.version_info if version_info is None else version_info
    if version_info < (3, 10):
        major = version_info[0]
        minor = version_info[1]
        raise RuntimeError(
            f"Python 3.10 or later is required (detected {major}.{minor}). "
            "Recreate .venv with Python 3.11 or newer; do not reuse a Python 3.8 venv."
        )


def main() -> None:
    try:
        require_supported_python()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    from .config import load_config
    from .service import FrameService
    from .web import create_app

    script_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Manage personal art on a Samsung The Frame TV.")
    parser.add_argument("--config", type=Path, default=script_dir / "config.json")
    parser.add_argument("--image-root", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--tv-ip")
    parser.add_argument("--bind-ip")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    os.umask(0o077)
    config = load_config(
        args.config,
        {
            "image_root": args.image_root,
            "state_dir": args.state_dir,
            "tv_ip": args.tv_ip,
            "bind_ip": args.bind_ip,
            "port": args.port,
        },
        script_dir=script_dir,
    )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = FrameService(config)
    service.start()
    try:
        create_app(service).run(host=config.bind_ip, port=config.port, threaded=True)
    finally:
        service.stop()


if __name__ == "__main__":
    main()
