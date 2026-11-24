# Architecture

Frame Art Mode manages personal artwork on a single Samsung The Frame TV from a Synology NAS (DSM 7). Its primary design goal is **TV safety**: Samsung The Frame's Tizen Art Mode database can wedge—requiring a factory reset—if flooded with rapid uploads/deletions, oversized/malformed JPEGs, or deletion of the currently displayed image.

To isolate responsibilities and contain failures, the application is split into **three independent OS processes** coordinated by a lightweight **Python supervisor**.

---

## 1. System Topology

```text
Synology Task Scheduler
        │
        ▼
    start.sh
        │
        ▼
  supervisor (python -m frame_art_mode.supervisor)
   ├── library_daemon  (owns image_root reads, TV-safe JPEG & thumbnail cache, library.sock)
   ├── tv_daemon       (owns TV WebSocket traffic, manifest, operation status, slideshow, tv.sock)
   └── web_server      (owns LAN HTTP dashboard, SSE stream, static assets, read-only cache thumbnails)
```

### Component Responsibilities and Hard Boundaries

| Component | Entrypoint | Owns (Read/Write) | Reads Only | Forbidden Dependencies |
| :--- | :--- | :--- | :--- | :--- |
| **Library Daemon** | `frame_art_mode.library_daemon` | `cache_dir/`, `library.json`, `library.sock`, `library.lock` | `image_root/` | Never imports `samsungframetv` or opens TV network sockets. Never writes inside `image_root/`. |
| **TV Daemon** | `frame_art_mode.tv_daemon` | `tv_dir/manifest.json`, `tv_dir/operation.json`, `tv_dir/tv-token.txt`, `tv.sock`, `tv.lock` | Cached TV-safe JPEGs pinned by `library_daemon` | Never reads `image_root/` directly. Never imports `Pillow` or converts images. |
| **Web Server** | `frame_art_mode.web_server` | HTTP sessions / CSRF tokens (in-memory) | Pre-generated cached thumbnails in `cache_dir/thumbs/` | Never imports `samsungframetv` or `Pillow`. Never touches `image_root/` or TV WebSockets directly. |
| **Supervisor** | `frame_art_mode.supervisor` | Child process lifecycle, stdout/stderr log forwarding | `config.json` | Never accesses TV sockets, `image_root/`, or state files directly. |

Shared code lives in narrow, dependency-free internal modules (`config.py`, `ipc.py`, `models.py`) that import only the Python standard library.

---

## 2. Filesystem & State Ownership

All mutable runtime state lives inside `state_dir` (created with `0700` permissions; must live outside `image_root`). Sensitive files (`tv-token.txt`, `manifest.json`, `operation.json`) are written atomically via temporary files + `os.replace` with `0600` permissions.

```text
<state_dir>/                          # chmod 0700 (owned by dedicated non-admin NAS user)
├── library.lock                      # fcntl exclusive lock held by library_daemon
├── library.sock                      # Unix domain socket for library_daemon
├── cache/                            # Default cache_dir (overridable via config.cache_dir)
│   ├── library.json                  # Durable set index, fingerprints, normalization version, warnings
│   ├── tv/                           # Content-addressed TV-safe JPEGs: <sha256>.jpg
│   └── thumbs/                       # Content-addressed web thumbnails: <sha256>.jpg (360x240)
└── <tv_key>/                         # sha256(tv_ip)[:16], chmod 0700
    ├── tv.lock                       # fcntl exclusive lock held by tv_daemon
    ├── tv.sock                       # Unix domain socket for tv_daemon
    ├── tv-token.txt                  # samsungframetv pairing token (0600)
    ├── manifest.json                 # Ownership manifest + per-set playback settings (0600)
    └── operation.json                # Current TV operation + last result snapshot (0600)
```

Each daemon holds an exclusive `fcntl.flock` lock on its lockfile at startup so two instances can never corrupt the same TV or cache directory.

---

## 3. Inter-Process Communication (IPC)

Services communicate exclusively over **Unix domain sockets** (`tv.sock` and `library.sock`) using lightweight **JSON-over-HTTP/1.1** (`http.server` / `socketserver.UnixStreamServer` on the server side; stdlib `http.client.HTTPConnection` subclass over `AF_UNIX` on the client side).

Why Unix domain sockets instead of `127.0.0.1` TCP ports:
- Zero extra TCP ports to configure or risk colliding with other Synology packages.
- Directory permissions (`0700` on `state_dir`) restrict socket access to the dedicated service user—other local NAS users cannot issue TV commands.
- Connection failure (`FileNotFoundError` / `ConnectionRefusedError`) gives the Web UI immediate, unambiguous offline detection for each backend daemon.

### 3.1 `library.sock` Endpoints

| Method & Path | Purpose | Notes |
| :--- | :--- | :--- |
| `GET /health` | Liveness + basic metadata | Returns `{"ok": true, "pid": ..., "started_at": ...}`. |
| `GET /status` | Full library & cache state | Returns `last_refreshed_at`, `sets`, per-image metadata (cached hash, thumb path, skip reason), `counts` (`sets`, `valid`, `skipped`, `cached_tv_files`, `cached_thumb_files`), `warnings`, `scan_warnings`, `active_leases`, active refresh progress (`operation`), and `last_result`. |
| `POST /refresh` | Start explicit library scan & cache build | Optional body `{"set_id": "..."}` to scope to one set; omits to scan full library. Rejects (`409`) if a refresh is already running. |
| `POST /prepare-set` | Incremental catch-up + hash pin for TV Sync | Body `{"set_id": "..."}` with optional `"lease_id": "..."` (auto-generated if omitted). Incrementally updates any changed/missing files in `set_id`, pins those `<sha256>` files against orphan pruning under `lease_id`, and returns `set_id`, `lease_id`, ordered verified `images`, `skipped` entries, and `warnings`. |
| `POST /release-lease` | Release Sync hash pin | Body `{"lease_id": "..."}`. Released when Sync finishes, fails, or aborts. |
| `POST /abort` | Cooperative abort of running refresh | Stops after the current image finishes processing. |

### 3.2 `tv.sock` Endpoints

| Method & Path | Purpose | Notes |
| :--- | :--- | :--- |
| `GET /health` | Liveness + basic metadata | Returns `{"ok": true, "pid": ..., "started_at": ..., "tv_key": ...}`. |
| `GET /status` | Full TV daemon snapshot (no TV I/O) | Returns `tv_ready`, `tv_checked_at`, `active_set`, `entries`, `current_display` (content ID, category, source_rel if NAS, Art Store flag), `next_rotation_at`, `cycling_paused`, `preview` (`active`, `content_id`, `expires_at`), `cleanup_pending`, `settings`, `operation`, `last_result`, `warnings`, `drift_warnings`, `manifest_error`, and `last_error`. |
| `POST /check` | Explicit one-shot TV reachability / Art Mode check | Runs in TV actor lane. Rejects (`409`) if Sync or Preview is running. |
| `POST /settings` | Persist per-set playback settings immediately | Body `{"set_id": "...", "random": bool, "mix_art_store_favorites": bool, "interval_minutes": int}`. Rejects (`409`) during Sync. |
| `POST /sync` | Start background TV Sync | Body `{"set_id": "...", "settings": {...}}` (optional settings saved atomically first). Calls `library.sock /prepare-set` to obtain pinned TV-safe copies, computes exact `total_ops`, and runs the anti-wedge sync pipeline. |
| `POST /preview` | Start timed preview of a synced image | Body `{"content_id": "..."}`. Restricted to currently active `manifest.entries`. Duration comes from `config.preview_seconds`. |
| `POST /abort` | Cooperative abort of running TV operation | Stops Sync at next op boundary; immediately restores previous image if Preview is active. |

### 3.3 Common Progress Payload (`operation`)

Both daemons report running operations with the same unified step-count schema:

```json
{
  "kind": "sync",
  "phase": "uploading",
  "message": "Uploading sunset.jpg",
  "current_item": "Party/sunset.jpg",
  "completed_ops": 4,
  "total_ops": 14,
  "abort_requested": false,
  "started_at": "2026-11-23T23:00:00+00:00"
}
```

- `completed_ops` / `total_ops` advances monotonically from `0` to `total_ops` across the entire job (no backwards phase resets).
- During the brief initial planning step before `total_ops` is known (e.g., querying `art.available()`), `total_ops` is `0` and the UI displays an active spinner with the current phase label.
- `last_result` holds `{kind, state, message, timestamp}` where `state` is one of `completed`, `aborted`, `failed`, or `interrupted`.

---

## 4. TV Communicator & Anti-Wedge Sync Pipeline (Function 1)

`tv_daemon` is a **strictly single-lane serialized actor**. All TV WebSocket calls—reachability checks, syncs, previews, preview restores, and slideshow ticks—execute sequentially through one TV lock/lane so two WebSocket calls never overlap.

### 4.1 Four Anti-Wedge Guardrails

1. **Select-before-delete:** Never delete old managed artwork while one of those items may still be displayed on screen. New images are uploaded and verified, the manifest is committed, and a new image is selected on the TV **before** any old managed items are deleted. The currently displayed content ID is never included in a delete batch.
2. **Fail-fast circuit breaker:** Stop the current batch immediately on the **first** upload error, verification failure, delete error, timeout, or lost Art Mode state. Never continue hammering a struggling TV with remaining queued operations.
3. **Conservative configurable pacing:** Respect explicit delays between consecutive TV writes (`tv_op_delay_seconds`, default `3.0s`), settle cooldowns after upload and delete batches (`tv_batch_settle_seconds`, default `12.0s`), and gentler verification polling (`tv_verify_retries=4`, `tv_verify_interval_seconds=3.0s`).
4. **Clean WebSocket lifecycle:** Open a fresh `SamsungTVWS(...).art(timeout=tv_timeout_seconds)` connection per operation/job and explicitly close/discard it on completion, error, or timeout—never reuse a wedged socket.

### 4.2 Sync State Machine

```text
[1. Check TV ready & Art Mode]
        │
        ▼
[2. Call library.sock /prepare-set (pin content-addressed TV-safe JPEGs)]
        │  (emit soft warning if image count > max_set_images_warning)
        ▼
[3. Query art.available() → compute diff: uploads (U) + deletes (D) + select (1)]
        │  set total_ops = 1 (check) + U + 1 (settle if U>0) + 1 (commit+select) + D + 1 (settle if D>0)
        ▼
[4. Upload Loop (U items, reusing unchanged content_ids already on TV)]
        │  • check abort flag before each item
        │  • upload bytes from pinned cache path → record content_id in operation.json immediately
        │  • verify presence in art.available() with conservative retry interval
        │  • pace by tv_op_delay_seconds
        │  • on FIRST error → fail-fast: keep previous active set, queue partial uploaded IDs to cleanup_pending
        ▼
[5. Post-Upload Settle (tv_batch_settle_seconds, abortable sleep)]
        │  • verify TV still in Art Mode
        ▼
[6. Commit Manifest (active_set, new entries, old_ids + prior cleanup_pending)]
        │  • manifest commit is the atomic point of no return
        ▼
[7. Select Initial Image on TV (from new set, or Art Store favorite if random+mix enabled)]
        │  • guarantees TV is displaying an item outside the delete batch
        ▼
[8. Delete Loop (old managed IDs + prior cleanup_pending, excluding current_content_id)]
        │  • check abort flag before each item
        │  • delete item → pace by tv_op_delay_seconds
        │  • on FIRST error or abort → stop loop, keep remaining IDs in manifest.cleanup_pending
        ▼
[9. Post-Delete Settle (if any deletions ran) → Update manifest.cleanup_pending → Release library lease]
```

### 4.3 Cooperative Abort Semantics

Abort never forcibly kills an in-flight WebSocket write (which can wedge the TV or lose a returned content ID). Instead, `POST /abort` sets an abort flag checked at every operation boundary and interruptible sleep:
- **Aborted before Step 6 (manifest commit):** Stop immediately. Keep the previous active set untouched. Move any IDs already uploaded in this attempt into `manifest.cleanup_pending` (no immediate rollback delete storm right when the user asked to stop).
- **Aborted during Step 8 (old-item cleanup):** Stop deleting immediately. The new set remains active and displayed; remaining undeleted old IDs stay in `manifest.cleanup_pending` for the next explicit Sync.
- **Aborted during Preview:** Immediately restore the previous image and category and resume the normal slideshow interval.

### 4.4 Ownership, Drift, Favorites, and Crash Recovery

- **Ownership (ADR 0001):** Only IDs recorded in `manifest.json` are ever deleted. Unreadable or tampered manifests block Sync and delete nothing. Missing manifests start with zero owned IDs.
- **Drift Detection:** If a tracked content ID disappears from the TV externally:
  - *During Sync:* `art.available()` detects it is absent and re-uploads it from cache.
  - *During Slideshow / Preview:* A failed `select_image` records a persistent drift warning suggesting Sync and skips to the next managed candidate during cycling without mutating ownership.
- **Art Store Favorites (ADR 0002):** Per-set `mix_art_store_favorites` (default `false`). Used only during random picks by reading `MY-C0004` and including `SAM-` prefixed IDs (warning and skipping non-`SAM-` IDs). Read/select-only; never cached, uploaded, deleted, or added to the ownership manifest.
- **Restart Recovery (ADR 0003):** On `tv_daemon` startup, any persisted in-progress `operation.json` is converted to `interrupted` (never auto-resumed). Known partial uploads from before commit are moved to `manifest.cleanup_pending` for the next explicit Sync.
- **Slideshow & Preview:** Cycling pauses automatically when the TV is unreachable or exits Art Mode, and resumes when it returns (tracking remaining interval and exposing `next_rotation_at`). Preview is limited to currently synced entries, lasts `config.preview_seconds` (default `15`), and restores both prior `content_id` and `category`.

---

## 5. Image Library & Persistent TV-Safe Cache (Function 2)

`library_daemon` owns all read access to `image_root` and maintains a persistent, content-addressed cache under `cache_dir` (default `<state_dir>/cache`).

### 5.1 Discovery & Set Rules

- Files directly in `image_root` form the **Default set** (`root`).
- Each immediate child directory forms an **Occasion set** (`occasion:<dirname>`), recursively including all descendant directories (following readable symlinks with `(st_dev, st_ino)` loop detection).
- Ignored entries: `@eaDir`, `.DS_Store`, dot-prefixed hidden files/dirs, `._*`, `*.tmp`, and non-image extensions.
- Accepted extensions: `.jpg`, `.jpeg`, `.png`, `.webp`, `.bmp`, `.tif`, `.tiff`, `.gif` (non-animated only).
- Natural sort order (`natural_key`) by relative path within each set.

### 5.2 TV-Safe Normalization Pipeline

For every new or modified source file (detected via `(st_dev, st_ino, st_size, st_mtime_ns)` change or `normalization_version` / `max_image_bytes` mismatch):
1. Verify source signature did not change mid-read.
2. Reject animated multi-frame images (including animated GIFs and WebPs), corrupt images, images `< 640px` on either side, or aspect ratios `> 3.5:1` (recorded as per-image skip warnings—never aborting the whole set).
3. Apply EXIF orientation (`ImageOps.exif_transpose`).
4. Convert embedded ICC profiles to sRGB via `ImageCms` (rejecting mismatched/unconvertible profiles); composite transparency onto `#181818`.
5. Downscale with `LANCZOS` if longest side `> 3840px`.
6. Encode baseline (non-progressive) 4:2:0 JPEG with stripped EXIF and embedded sRGB ICC profile, starting at quality `88`.
7. **Size cap step-down:** If encoded bytes exceed `config.max_image_bytes` (default **5 MB** / `5242880`), step quality down cleanly (`82 → 76 → 70 → 64`) until `<= max_image_bytes`; if still over cap, skip the image with a clear size warning.
8. Generate a `360×240` JPEG thumbnail (quality `78`, sRGB, EXIF stripped).
9. Compute `sha256` of the normalized TV-safe JPEG and write atomically to `cache/tv/<sha256>.jpg` and `cache/thumbs/<sha256>.jpg`.

### 5.3 Refresh Policy, Pinning, and Pruning

- **No continuous watching or background polling:** Refresh runs only on explicit user/CLI request (`POST /refresh`) or incrementally during Sync preparation (`POST /prepare-set`).
- **Progress & Abort:** Full or set refreshes compute `total_ops` from the count of files needing scan/normalization and report unified step-count progress; `POST /abort` stops cleanly after the current image.
- **Orphan Pruning & Sync Leases:** After a completed full refresh, cached files in `cache/tv/` and `cache/thumbs/` not referenced by `library.json` **or** any active Sync `lease_id` are pruned. While any Sync lease is active, orphan pruning is deferred so no file can be deleted mid-upload.

---

## 6. Web UI Dashboard (Function 3)

`web_server` runs a lightweight Flask application bound strictly to `config.bind_ip:config.port` (unicast LAN address only; no wildcard `0.0.0.0`, no internet exposure).

### 6.1 Architecture & Security

- **Zero-build frontend:** Separate Jinja template (`templates/index.html`) and static files (`static/app.css`, `static/app.js`). No Node.js, npm, or build step required on Synology.
- **No full-page reloads:** All mutations (`POST /api/settings`, `POST /api/sync`, `POST /api/preview`, `POST /api/abort`, `POST /api/check-tv`, `POST /api/library/refresh`) use `fetch()` with JSON responses and an `X-CSRF-Token` header validated against the Flask session token.
- **Thumbnails:** Served directly from `cache_dir/thumbs/<sha256>.jpg` with immutable cache headers (`Cache-Control: public, max-age=31536000, immutable` since filenames are content-addressed hashes).
- **Degraded Mode:** If `tv.sock` or `library.sock` is unreachable, the Web UI stays online, marks the affected service badge offline in the status header, and disables only the controls depending on that service.

### 6.2 Dashboard Layout & UX

1. **Compact 4-Part Status Header:**
   - **Service Health:** Live status dots for `TV Daemon`, `Library Cache`, and `Web UI`.
   - **TV Status:** Reachability / Art Mode badge + manual **`Check TV`** button (still zero idle TV polling).
   - **Now Playing:** Active synced set name, currently displayed image (cached thumbnail for NAS items or Art Store badge for favorites), and next rotation countdown (or paused indicator).
   - **Library Summary:** Last refresh timestamp, valid/skipped counts, and whether the viewed set has uncached or unsynced changes.
2. **Active Operation Bar (shown whenever TV or Library has an active `operation`):**
   - Running **spinner**, determinate **unified progress bar** (`completed_ops / total_ops` + percentage), current phase/item label, and **`Abort`** button.
3. **Notifications (Two Tiers):**
   - **Corner Toasts:** Stacked, dismissible notifications for action results and live errors (success/info auto-dismiss after ~5s; errors stay sticky until dismissed).
   - **Persistent Warning Badge & Expandable Drawer:** Compact count pill in the header for durable warnings (`cleanup_pending`, skipped NAS images, TV drift, manifest errors, Art Store favorite warnings)—never pushing page content down with stacked inline banners.
4. **Controls & Gallery:**
   - Set selector (switches viewed set via client state/API without touching the TV), `Random order`, `Mix Art Store favorites`, `Minutes per image`, explicit **`Save settings`** button (highlighted when dirty), **`Sync to TV`**, and **`Refresh library`**.
   - Responsive thumbnail grid where each card shows a sync-state pill (`Displaying`, `On TV`, `New / Modified`, or `Skipped` with reason) and a **`Preview (<N>s)`** button on synced cards.
5. **Live Updates:**
   - `GET /api/events` streams **Server-Sent Events (SSE)** aggregated from local `tv.sock` and `library.sock` status snapshots (never polling the TV while idle), with automatic fallback to `GET /api/status` polling if the SSE stream drops.

---

## 7. Process Supervision & CLI Surfaces

### 7.1 Supervisor (`start.sh` → `frame_art_mode.supervisor`)

`start.sh` validates the Python 3.11+ `.venv` and `exec`s `frame_art_mode.supervisor --config config.json`. The supervisor:
1. Validates `config.json` once up front (failing fast on invalid config).
2. Spawns `library_daemon`, `tv_daemon`, and `web_server` as child processes using the same interpreter and config arguments.
3. Restarts crashed children with exponential backoff (`1s → 2s → 4s → ... → 30s`), resetting backoff after stable uptime, and halting restarts for exit code `2` (fatal config/lock error).
4. Forwards `SIGTERM` and `SIGINT` cleanly to all children and waits for graceful shutdown before exiting.

### 7.2 Standalone CLI Entrypoints

Every component can be run or inspected directly over SSH:
- `python -m frame_art_mode.supervisor [--config PATH]`
- `python -m frame_art_mode.tv_daemon [serve | status | check | sync <set_id> | abort | preview <content_id>]`
- `python -m frame_art_mode.library_daemon [serve | status | refresh [--set <set_id>] [--offline] | abort]`
- `python -m frame_art_mode.web_server [serve]`

---

## 8. Configuration Schema (`config.json`)

Configuration files are parsed as **JSONC** (`//` line comments, `/* ... */` block comments, and trailing commas are supported). All new keys are optional and backwards-compatible with existing `config.json` files.

| Key | Type | Default | Constraints |
| :--- | :--- | :--- | :--- |
| `tv_ip` | string | *(required)* | Valid IP address of The Frame TV. |
| `bind_ip` | string | *(required)* | Specific unicast NAS LAN IP (no wildcards/multicast). |
| `port` | int | `8080` | `1..65535`. |
| `image_root` | path | `<repo>/image` | Read-only source photo root. |
| `state_dir` | path | `<repo>/state` | Must be outside `image_root`. |
| `cache_dir` | path | `<state_dir>/cache` | Optional override; must be outside `image_root`. |
| `preview_seconds` | int | `15` | `>= 1`. Timed preview duration before auto-restore. |
| `max_image_bytes` | int | `5242880` | `>= 262144` (256 KB). Default 5 MB TV-safe JPEG cap. |
| `tv_op_delay_seconds` | float | `3.0` | `>= 0.5`. Pause between TV upload/delete operations. |
| `tv_batch_settle_seconds` | float | `12.0` | `>= 0.0`. Cooldown after upload/delete batches. |
| `tv_timeout_seconds` | float | `30.0` | `>= 5.0`. Per-call TV WebSocket timeout. |
| `tv_verify_retries` | int | `4` | `>= 1`. Max `available()` checks after an upload. |
| `tv_verify_interval_seconds` | float | `3.0` | `>= 0.5`. Pause between upload verification checks. |
| `max_set_images_warning` | int | `50` | `>= 1`. Non-blocking warning threshold for large sets. |
