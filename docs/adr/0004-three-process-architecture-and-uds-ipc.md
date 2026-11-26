# ADR 0004: Three-process architecture, Python supervisor, and Unix domain socket IPC

- **Status:** Accepted
- **Decision date:** 2026-11-23
- **Related:** [ADR 0001](0001-tv-art-ownership-and-sync.md), [ADR 0005](0005-persistent-tv-safe-image-cache.md)

## Context

The initial implementation combined TV WebSocket communication, image scanning
and conversion, and the Flask web interface into a single Python process backed
by one large `FrameService` class. That coupled three distinct operational
concerns: a hanging TV socket or heavy image-decoding pass could stall the Web
UI, boundaries between TV I/O and NAS filesystem access were easy to blur, and
individual functions could not be operated or tested independently from the CLI.

The service runs on a Synology NAS (DSM 7) under a single Task Scheduler boot
script (`start.sh`) as a dedicated non-admin user, with no container runtime or
extra system packages assumed.

## Decision

- **Three independent runnable processes:** Split the runtime into three
  isolated OS processes with standalone CLI entrypoints:
  1. `frame_art_mode.library_daemon` — reads `image_root`, builds and prunes
     the TV-safe JPEG and thumbnail cache, serves `library.sock`.
  2. `frame_art_mode.tv_daemon` — serializes all TV WebSocket calls, owns
     `manifest.json`, `operation.json`, `tv-token.txt`, slideshow cycling, and
     previews, serves `tv.sock`.
  3. `frame_art_mode.web_server` — serves the LAN dashboard, JSON API, SSE
     stream, and pre-cached thumbnails over `bind_ip:port`.
- **Hard dependency boundaries:** `web_server` never imports `samsungframetv`
  or `Pillow`; `tv_daemon` never imports `Pillow` or reads `image_root`;
  `library_daemon` never imports `samsungframetv` or `flask`.
- **Lightweight Python supervisor:** `start.sh` invokes
  `frame_art_mode.supervisor`, which spawns all three processes, restarts
  crashed children with exponential backoff (stopping on fatal exit code `2`),
  forwards `SIGTERM`/`SIGINT` cleanly, and keeps `web_server` online in
  degraded mode if a backend daemon is offline.
- **Unix domain socket IPC:** Daemons communicate using lightweight
  JSON-over-HTTP across Unix domain sockets (`state_dir/library.sock` and
  `state_dir/<tv_key>/tv.sock`) protected by `state_dir`'s `0700` permissions.

## Considered Options

- **Three Python modules in a single OS process** — simpler to wire, but offers
  no crash or GIL/memory isolation between Pillow image decoding, TV WebSocket
  hangs, and the Web UI, and does not provide clean standalone daemon
  boundaries.
- **Localhost (`127.0.0.1`) HTTP ports** — requires configuring and avoiding
  conflicts on two additional TCP ports and exposes internal control endpoints
  to every local user/process on the Synology NAS.
- **SQLite or filesystem command queue** — avoids socket servers, but makes
  synchronous request/response (such as `prepare-set`, immediate `409 Conflict`
  rejection, and instant offline liveness detection) slower and more complex.
- **Separate Synology Task Scheduler jobs per process** — pushes operational
  complexity onto the NAS user instead of preserving the single `start.sh`
  deployment model.

## Consequences

Each function is crash-isolated, independently runnable via CLI, and protected
by hard import boundaries enforced in tests. The Web UI remains responsive and
reports degraded component health even if the TV daemon or Library daemon
crashes or restarts. In exchange, the codebase maintains a small stdlib UDS
HTTP client/server module (`ipc.py`) and a supervisor loop (`supervisor.py`).
