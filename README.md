# Frame Art Mode

Frame Art Mode manages personal artwork on one Samsung The Frame TV from a
Synology NAS using
[NickWaterton/samsung-tv-ws-api](https://github.com/NickWaterton/samsung-tv-ws-api)
(published on PyPI as `samsungframetv`).

It is designed around **TV database safety** and **clean separation of
concerns** across three independent processes supervised by a single launcher:

1. **TV Communicator (`frame_art_mode.tv_daemon`)** — strictly serialized
   single-lane TV actor with anti-wedge guardrails (select-before-delete,
   fail-fast circuit breaker, conservative pacing, and clean WebSocket
   lifecycle), ownership manifest, slideshow cycling, and timed previews.
2. **Image Library & Cache (`frame_art_mode.library_daemon`)** — scans NAS
   folders on demand, maintains a persistent content-addressed cache of TV-safe
   JPEGs (capped at 5 MB by default) and pre-generated web thumbnails, and pins
   copies for Sync.
3. **Web UI Dashboard (`frame_art_mode.web_server`)** — zero-build LAN
   dashboard displaying whole-app health, live unified step-count progress with
   spinner and cooperative Abort, corner toasts, and a compact warning drawer.

Originals on the NAS are never modified, files are never uploaded from the
browser, and TV artwork not recorded in the per-TV ownership manifest is never
deleted.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — Three-process topology, Unix socket IPC contracts, anti-wedge sync state machine, and full `config.json` reference.
- [Python Coding Style & Development Guide](docs/CODING_STYLE.md) — Python 3.11+ conventions, hard import boundaries, atomic state safety, and testing rules.
- [Glossary](GLOSSARY.md) — Canonical domain vocabulary.
- [Architecture Decision Records](docs/adr/) — Historical and active design decisions ([ADR 0001](docs/adr/0001-tv-art-ownership-and-sync.md) through [ADR 0005](docs/adr/0005-persistent-tv-safe-image-cache.md)).

## Install on Synology DSM 7

1. Install Python 3.11 through DSM Package Center (or another supported
   Synology Python 3.11 package) and place this repository in a persistent
   folder. Python 3.10 or later is the minimum required by `samsungframetv`
   4.x; Python 3.8 cannot install it.
2. Create a fresh local virtual environment with Python 3.11 (do not reuse an
   existing Python 3.8 `.venv`):

   ```sh
   cd /volume1/applications/frame-art-mode
   python3.11 -m venv .venv
   .venv/bin/python --version
   .venv/bin/python -m pip install --upgrade pip
   .venv/bin/python -m pip install -r requirements.txt
   cp config.example.json config.json
   ```

3. Edit `config.json` (JSONC format—`//` and `/* ... */` comments and trailing
   commas are supported) with the TV's reserved LAN IP (`tv_ip`), a specific
   unicast LAN IP assigned to the NAS (`bind_ip`), `image_root`, and
   `state_dir`. `state_dir` (and optional `cache_dir`) must live outside
   `image_root` on persistent storage. Optional keys tune `preview_seconds`
   (default `15`), `max_image_bytes` (default `5242880` / 5 MB), TV pacing
   delays, and large-set warnings; see
   [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#8-configuration-schema-configjson)
   for the full schema.
4. In DSM **Control Panel → Task Scheduler**, create a **Triggered task → User
   defined script**, trigger it at boot, and run it as a dedicated non-admin
   account with read access to `image_root` and write access only to
   `state_dir`, `cache_dir` (if separate), and the repository directory:

   ```sh
   /bin/sh /volume1/applications/frame-art-mode/start.sh
   ```

   Do not run as `root` or an administrator, and never port-forward the Web UI
   port to the internet.
5. Open `http://<NAS bind IP>:8080/` from a trusted device on the LAN. On first
   connection, approve the pairing prompt on the TV; the token is saved with
   `0600` permissions inside `state_dir`.

`start.sh` launches the Python supervisor (`frame_art_mode.supervisor`), which
starts and monitors `library_daemon`, `tv_daemon`, and `web_server`. If a
backend daemon crashes, the supervisor restarts it with backoff while keeping
the Web UI online in degraded mode.

### Standalone CLI Usage

Each service can also be invoked directly over SSH:

```sh
.venv/bin/python -m frame_art_mode.tv_daemon status
.venv/bin/python -m frame_art_mode.tv_daemon check
.venv/bin/python -m frame_art_mode.tv_daemon sync root
.venv/bin/python -m frame_art_mode.tv_daemon abort
.venv/bin/python -m frame_art_mode.library_daemon refresh
.venv/bin/python -m frame_art_mode.library_daemon refresh --offline
```

## Organize and Use Photos

- Files directly in `image_root` form the **Default** set (`root`).
- Each immediate child folder is a separate **Occasion** set (`occasion:<name>`),
  recursively including all descendant folders.
- The dashboard lets you browse sets, inspect cached thumbnails and per-image
  sync status (`Displaying`, `On TV`, `New / Modified`, `Skipped`), refresh the
  library cache, configure playback settings (`Random order`, `Mix Art Store
  favorites`, `Minutes per image`), save settings without reloading the page,
  and press **Sync to TV** when ready to publish a set.
- **Mix Art Store favorites** is a per-set preference (default off) applied
  immediately by **Save settings**. In random mode, each pick reads TV category
  `MY-C0004` and includes `SAM-` prefixed favorites alongside NAS candidates
  (warning and skipping non-`SAM-` IDs). Favorites stay TV-owned and are never
  cached, uploaded, deleted, or recorded in the ownership manifest.
- **Preview** is available for photos in the active synced set, displays the
  photo for `preview_seconds` (15 seconds by default), and restores the
  previously displayed image and category. Clicking **Abort** during a preview
  restores the prior image immediately.
- **No continuous folder watching:** Disk changes under `image_root` are picked
  up only when you run **Refresh library** or **Sync to TV**.

### TV-Safe Normalization & Anti-Wedge Protection

- **Ignored files:** `@eaDir`, hidden files/dirs, `.DS_Store`, `._*`, `.tmp`,
  and non-image extensions. Readable symlinks are followed with loop detection.
- **TV-safe encoding:** Accepts non-animated GIF, JPEG, PNG, WebP, BMP, and
  TIFF; applies EXIF orientation; composites transparency onto `#181818`;
  converts valid ICC profiles to sRGB; strips EXIF; caps longest side at
  3840 px (rejecting `< 640px` or `> 3.5:1` aspect ratio); writes baseline
  non-progressive 4:2:0 JPEGs starting at quality 88 and stepping quality down
  if needed to stay within `max_image_bytes` (default **5 MB**).
- **Anti-wedge sync order:** Uploads and verifies new/changed images from the
  pinned cache first, commits the ownership manifest, **selects a new image on
  the TV before deleting any old managed items**, paces operations
  conservatively, and stops fail-fast on the first TV error.
- **Cooperative abort & recovery:** Clicking **Abort** stops cleanly at the
  next operation boundary without interrupting an in-flight TV write or firing
  a rollback delete storm. Partial uploads and interrupted syncs are tracked in
  `cleanup_pending` and reconciled only on the next explicit **Sync to TV**.
- **No idle TV polling:** Live dashboard updates use Server-Sent Events (with
  polling fallback) reading local daemon state only. Use the **Check TV**
  button to re-check TV reachability and Art Mode on demand.

## Development

Read [docs/CODING_STYLE.md](docs/CODING_STYLE.md) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before making changes. Run the
test suite with:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use generated images, fake TV adapters, and temporary Unix sockets; no
test ever connects to or alters a real TV.
