from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any, TextIO

from .config import ConfigError, load_config

logger = logging.getLogger(__name__)

DEFAULT_BACKOFF_INITIAL = 1.0
DEFAULT_BACKOFF_MAX = 30.0
DEFAULT_STABLE_UPTIME_SECONDS = 60.0
DEFAULT_SHUTDOWN_TIMEOUT = 10.0
DEFAULT_POLL_INTERVAL = 0.2

_COMMON_OVERRIDE_FLAGS: tuple[tuple[str, str], ...] = (
    ("tv_ip", "--tv-ip"),
    ("bind_ip", "--bind-ip"),
    ("port", "--port"),
    ("state_dir", "--state-dir"),
    ("cache_dir", "--cache-dir"),
)


def require_supported_python(version_info: tuple[int, ...] | None = None) -> None:
    """Ensure the interpreter meets the minimum supported Python 3.10+ version."""
    info = sys.version_info if version_info is None else version_info
    if info < (3, 10):
        major = info[0]
        minor = info[1]
        raise RuntimeError(
            f"Python 3.10 or later is required (detected {major}.{minor}). "
            "Recreate .venv with Python 3.11 or newer; do not reuse a Python 3.8 venv."
        )


@dataclass(frozen=True)
class ChildSpec:
    """Specification for a supervised child daemon process."""

    name: str
    command: tuple[str, ...]


@dataclass
class _ManagedChild:
    spec: ChildSpec
    proc: Any = None
    reader_thread: threading.Thread | None = None
    started_at: float | None = None
    failure_count: int = 0
    restart_at: float | None = None
    permanently_stopped: bool = False


def build_child_specs(
    config_path: Path,
    overrides: Mapping[str, Any] | None = None,
    *,
    python_executable: str | None = None,
) -> tuple[ChildSpec, ...]:
    """Build child process commands for library_daemon, tv_daemon, and web_server."""
    exe = python_executable or sys.executable
    override_map = dict(overrides or {})

    common_args: list[str] = ["--config", str(config_path)]
    for key, flag in _COMMON_OVERRIDE_FLAGS:
        value = override_map.get(key)
        if value is not None:
            common_args.extend([flag, str(value)])

    image_root = override_map.get("image_root")
    image_args = ["--image-root", str(image_root)] if image_root is not None else []

    return (
        ChildSpec(
            name="library_daemon",
            command=(
                exe,
                "-m",
                "frame_art_mode.library_daemon",
                "serve",
                *common_args,
                *image_args,
            ),
        ),
        ChildSpec(
            name="tv_daemon",
            command=(
                exe,
                "-m",
                "frame_art_mode.tv_daemon",
                "serve",
                *common_args,
            ),
        ),
        ChildSpec(
            name="web_server",
            command=(
                exe,
                "-m",
                "frame_art_mode.web_server",
                "serve",
                *common_args,
                *image_args,
            ),
        ),
    )


def _default_process_factory(command: Sequence[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


class Supervisor:
    """Lightweight supervisor managing library_daemon, tv_daemon, and web_server."""

    def __init__(
        self,
        specs: Sequence[ChildSpec],
        *,
        process_factory: Callable[[Sequence[str]], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] | None = None,
        backoff_initial: float = DEFAULT_BACKOFF_INITIAL,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        stable_uptime_seconds: float = DEFAULT_STABLE_UPTIME_SECONDS,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        output_stream: TextIO | None = None,
    ) -> None:
        if not specs:
            raise ValueError("specs must not be empty")
        if backoff_initial <= 0:
            raise ValueError("backoff_initial must be > 0")
        if backoff_max < backoff_initial:
            raise ValueError("backoff_max must be >= backoff_initial")
        if stable_uptime_seconds < 0:
            raise ValueError("stable_uptime_seconds must be >= 0")
        if shutdown_timeout < 0:
            raise ValueError("shutdown_timeout must be >= 0")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be > 0")

        self.specs = tuple(specs)
        self._process_factory = process_factory or _default_process_factory
        self._clock = clock
        self._sleeper = sleeper
        self.backoff_initial = float(backoff_initial)
        self.backoff_max = float(backoff_max)
        self.stable_uptime_seconds = float(stable_uptime_seconds)
        self.shutdown_timeout = float(shutdown_timeout)
        self.poll_interval = float(poll_interval)
        self._output_stream = output_stream

        self._stop_event = threading.Event()
        self._output_lock = threading.Lock()
        self._children: list[_ManagedChild] = [_ManagedChild(spec=spec) for spec in self.specs]

    def request_stop(self) -> None:
        """Signal the supervisor loop to terminate all children and exit cleanly."""
        self._stop_event.set()

    def _write_child_line(self, name: str, line: str) -> None:
        stream = self._output_stream if self._output_stream is not None else sys.stderr
        with self._output_lock:
            stream.write(f"[{name}] {line}\n")
            stream.flush()

    def _forward_output(self, name: str, pipe: Any) -> None:
        try:
            for raw_line in pipe:
                self._write_child_line(name, raw_line.rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            close_fn = getattr(pipe, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except (OSError, ValueError):
                    pass

    def _spawn_child(self, child: _ManagedChild, now: float) -> None:
        logger.info("Starting %s", child.spec.name)
        try:
            proc = self._process_factory(child.spec.command)
        except OSError as exc:
            logger.error("Failed to start %s: %s", child.spec.name, exc)
            child.failure_count += 1
            delay = min(
                self.backoff_max,
                self.backoff_initial * (2 ** (child.failure_count - 1)),
            )
            child.restart_at = now + delay
            child.proc = None
            child.started_at = None
            return

        child.proc = proc
        child.started_at = now
        child.restart_at = None

        pipe = getattr(proc, "stdout", None)
        if pipe is not None:
            thread = threading.Thread(
                target=self._forward_output,
                args=(child.spec.name, pipe),
                name=f"supervisor-log-{child.spec.name}",
                daemon=True,
            )
            child.reader_thread = thread
            thread.start()
        else:
            child.reader_thread = None

    def _cleanup_reader(self, child: _ManagedChild, *, timeout: float = 0.5) -> None:
        thread = child.reader_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        child.reader_thread = None

    def _sleep_step(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._sleeper is not None:
            self._sleeper(seconds)
        else:
            time.sleep(seconds)

    def _wait_step(self, seconds: float) -> bool:
        if self._stop_event.is_set():
            return True
        if self._sleeper is not None:
            if seconds > 0:
                self._sleeper(seconds)
            return self._stop_event.is_set()
        return self._stop_event.wait(timeout=max(0.0, seconds))

    def _shutdown_children(self) -> None:
        running = [
            child
            for child in self._children
            if child.proc is not None and child.proc.poll() is None
        ]
        if running:
            logger.info("Shutting down %d child process(es)", len(running))
            for child in running:
                try:
                    child.proc.terminate()
                except OSError:
                    logger.debug(
                        "Failed to send terminate to %s",
                        child.spec.name,
                        exc_info=True,
                    )

            deadline = self._clock() + self.shutdown_timeout
            while True:
                stragglers = [
                    child
                    for child in running
                    if child.proc is not None and child.proc.poll() is None
                ]
                if not stragglers:
                    break
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._sleep_step(min(self.poll_interval, remaining))

            for child in running:
                if child.proc is not None and child.proc.poll() is None:
                    logger.warning(
                        "%s did not exit within %.1fs; sending SIGKILL",
                        child.spec.name,
                        self.shutdown_timeout,
                    )
                    try:
                        child.proc.kill()
                    except OSError:
                        logger.debug(
                            "Failed to kill %s",
                            child.spec.name,
                            exc_info=True,
                        )
                    wait_fn = getattr(child.proc, "wait", None)
                    if callable(wait_fn):
                        try:
                            wait_fn(timeout=2.0)
                        except (subprocess.TimeoutExpired, OSError):
                            pass

        for child in self._children:
            self._cleanup_reader(child, timeout=1.0)
            child.proc = None

    def run(self) -> int:
        """Run the supervisor loop until stopped or all children permanently stop."""
        previous_handlers: dict[int, Any] = {}
        in_main_thread = threading.current_thread() is threading.main_thread()
        if in_main_thread:
            def _handle_signal(_signum: int, _frame: Any) -> None:
                self.request_stop()

            for sig in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, _handle_signal)

        try:
            while not self._stop_event.is_set():
                now = self._clock()

                for child in self._children:
                    if child.permanently_stopped or child.proc is not None:
                        continue
                    if child.restart_at is None or now >= child.restart_at:
                        self._spawn_child(child, now)

                for child in self._children:
                    if child.proc is None:
                        continue
                    returncode = child.proc.poll()
                    if returncode is None:
                        if (
                            child.failure_count > 0
                            and child.started_at is not None
                            and (now - child.started_at) >= self.stable_uptime_seconds
                        ):
                            child.failure_count = 0
                        continue

                    self._cleanup_reader(child, timeout=0.5)
                    uptime = (now - child.started_at) if child.started_at is not None else 0.0
                    child.proc = None
                    child.started_at = None

                    if returncode == 2:
                        child.permanently_stopped = True
                        child.restart_at = None
                        logger.error(
                            "%s exited with fatal status 2; leaving stopped "
                            "(remaining services stay online in degraded mode)",
                            child.spec.name,
                        )
                        continue

                    if uptime >= self.stable_uptime_seconds:
                        child.failure_count = 0
                    child.failure_count += 1
                    delay = min(
                        self.backoff_max,
                        self.backoff_initial * (2 ** (child.failure_count - 1)),
                    )
                    child.restart_at = now + delay
                    logger.warning(
                        "%s exited with code %s after %.1fs; restarting in %.1fs (attempt %d)",
                        child.spec.name,
                        returncode,
                        uptime,
                        delay,
                        child.failure_count,
                    )

                if all(child.permanently_stopped for child in self._children):
                    logger.error("All child services exited with fatal status 2; exiting")
                    return 2

                pending_restarts = [
                    child.restart_at
                    for child in self._children
                    if (
                        not child.permanently_stopped
                        and child.proc is None
                        and child.restart_at is not None
                    )
                ]
                any_running = any(child.proc is not None for child in self._children)
                if pending_restarts:
                    next_restart = min(pending_restarts)
                    until_restart = max(0.0, next_restart - now)
                    wait_seconds = (
                        min(self.poll_interval, until_restart)
                        if any_running
                        else until_restart
                    )
                else:
                    wait_seconds = self.poll_interval

                if self._wait_step(wait_seconds):
                    break

            logger.info("Supervisor stopping cleanly")
            self._shutdown_children()
            return 0
        finally:
            if in_main_thread:
                for sig, handler in previous_handlers.items():
                    signal.signal(sig, handler)


def _build_arg_parser(default_config_path: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frame_art_mode.supervisor",
        description="Supervise Frame Art Mode library, TV, and web server processes.",
    )
    parser.add_argument("--config", type=Path, default=default_config_path)
    parser.add_argument("--tv-ip")
    parser.add_argument("--bind-ip")
    parser.add_argument("--port", type=int)
    parser.add_argument("--image-root", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for the Frame Art Mode process supervisor."""
    os.umask(0o077)
    try:
        require_supported_python()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

    script_dir = Path(__file__).resolve().parent.parent
    parser = _build_arg_parser(script_dir / "config.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    overrides: dict[str, Any] = {
        "tv_ip": args.tv_ip,
        "bind_ip": args.bind_ip,
        "port": args.port,
        "image_root": args.image_root,
        "state_dir": args.state_dir,
        "cache_dir": args.cache_dir,
    }
    try:
        load_config(args.config, overrides, script_dir=script_dir)
    except ConfigError as exc:
        logger.error("Invalid configuration: %s", exc)
        return 2

    specs = build_child_specs(args.config.resolve(), overrides)
    supervisor = Supervisor(specs)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
