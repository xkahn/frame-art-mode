# ADR 0005: Persistent TV-safe image and thumbnail cache

- **Status:** Accepted
- **Decision date:** 2026-11-23
- **Related:** [ADR 0001](0001-tv-art-ownership-and-sync.md), [ADR 0004](0004-three-process-architecture-and-uds-ipc.md)

## Context

Previously, `images.py` re-decoded and re-normalized every photo in a set into
a temporary directory on every Sync, then deleted the directory when Sync
ended. Meanwhile, `web.py` opened full-resolution source photos from the NAS on
every `/thumbnail/` request with `max_age=0`. That caused unnecessary NAS disk
and CPU churn, slow gallery loads, and repeated conversion work, while also
leaving the TV vulnerable to oversized JPEGs that could still exceed safe Frame
TV upload sizes at quality 88.

## Decision

- **Persistent content-addressed cache:** `library_daemon` maintains a durable
  cache in `cache_dir` (default `<state_dir>/cache`, optionally overridden in
  `config.json`, always outside `image_root`) containing:
  - `library.json` (discovered sets, source signatures, content hashes, skip
    reasons, and normalization version/settings key),
  - `tv/<sha256>.jpg` (TV-safe baseline 4:2:0 sRGB JPEGs with EXIF stripped),
  - `thumbs/<sha256>.jpg` (pre-generated `360×240` web thumbnails served with
    immutable browser cache headers).
- **Incremental validation & 5 MB size guard:** Unchanged source files matching
  `(st_dev, st_ino, st_size, st_mtime_ns)` and the current normalization
  version are reused without re-encoding. If a quality-88 normalized JPEG
  exceeds `config.max_image_bytes` (default **5 MB** / `5242880`), the encoder
  steps quality down (`82 → 76 → 70 → 64`) until under the cap, skipping the
  file with a clear warning if it still exceeds the limit.
- **Explicit refresh + pre-sync catch-up:** The library daemon does **not**
  continuously watch or poll NAS folders. Cache refreshes run only on explicit
  user/CLI request (`Refresh library`) or incrementally for the target set at
  the start of a Sync (`POST /prepare-set`).
- **Sync hash pinning & orphan pruning:** Full refreshes automatically prune
  unreferenced files in `tv/` and `thumbs/`, except while `tv_daemon` holds an
  active Sync lease (`lease_id`) pinning the hashes it is uploading.

## Considered Options

- **Keep ephemeral per-sync snapshots** — wastes CPU/disk on every Sync and
  forces `web_server` to decode raw NAS originals on every page view.
- **Live filesystem watching (`inotify`/`watchdog`) or periodic polling** —
  unreliable across Synology SMB/NFS mounts and prevents NAS drives from
  idling; conflicts with the explicit-refresh mental model.
- **Higher (10 MB) or uncapped JPEG file size** — noisy high-resolution scans
  at quality 88 can produce oversized uploads that slow down or wedge Samsung
  The Frame's WebSocket upload handler.

## Consequences

Gallery browsing is fast and decoupled from `image_root`, and Sync uploads
pre-validated TV-safe files immediately unless source files actually changed.
Disk usage grows proportionally to the cached library inside `cache_dir`
(bounded automatically by orphan pruning on refresh). Changes made directly on
the NAS appear in the UI only after an explicit **Refresh library** or
**Sync to TV**.
