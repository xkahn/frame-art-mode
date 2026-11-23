# Frame Art Mode

Frame Art Mode is a small web service for managing personal art on one Samsung
The Frame TV from a Synology NAS. It uses
[NickWaterton/samsung-tv-ws-api](https://github.com/NickWaterton/samsung-tv-ws-api),
pinned here to upstream release `4.1.4` (also published on PyPI as
`samsungframetv`).

The service reads photos from NAS folders, creates safe JPEG copies, and uploads
the selected set only when you press **Sync to TV**. It never edits originals,
does not upload files from the browser, and never deletes TV items that are not
recorded in its per-TV ownership manifest.

## Install on Synology DSM 7

1. Install Python 3.11 through DSM Package Center (or another supported
   Synology Python 3.11 package) and place this repository in a persistent folder.
   Python 3.10 or later is the minimum, but Python 3.11 is required by these startup
   instructions. Samsung TV library releases 4.0 through 4.1.4 require Python
   3.10 or later; Python 3.8 cannot resolve or install them.
2. Create a fresh local virtual environment with Python 3.11. Do not reuse an
   existing `.venv` created by Python 3.8. If `.venv` already exists, rename or
   remove it, then recreate it:

   ```sh
   cd /volume1/applications/frame-art-mode
   python3.11 -m venv .venv
   .venv/bin/python --version
   .venv/bin/python -m pip install --upgrade pip
   .venv/bin/python -m pip install -r requirements.txt
   cp config.example.json config.json
   ```

   The TV library is pinned to upstream release tag `4.1.4` through Git instead
   of relying on a Synology, corporate, or other private package mirror to carry
   the package. The NAS and its Python environment must allow HTTPS GitHub
   access during installation. Verify the source interpreter first with
   `.venv/bin/python --version`; it must report 3.10 or newer. If GitHub access
   is unavailable, configure your private mirror to include the public
   `samsungframetv==4.1.4` package or use a separately vendored wheel.

3. Edit `config.json` with the TV's reserved LAN IP, a specific LAN IP assigned
   to the NAS, and the image and state directories. The `state_dir` must be
   outside `image_root`; it holds the TV token, ownership manifest, and temporary
   sync snapshots. Keep it on persistent storage.
4. In DSM **Control Panel → Task Scheduler**, create a **Triggered task → User
   defined script**, trigger it at boot, and run it as a dedicated non-admin
   account. Give that account read access to the image library and write access
   only to the application state directory and repository/virtual environment.
   Use this as the task command:

   ```sh
   /bin/sh /volume1/applications/frame-art-mode/start.sh
   ```

   Do not run the task as `root` or an administrator. The service binds only to
   `bind_ip`; do not expose port 8080 (or your selected port) to the internet.
5. Open `http://<NAS bind IP>:8080/` from a trusted device on the LAN. On first
   connection, the TV may ask you to allow the service. Approve it on the TV;
   the library saves a TV-specific token in the protected state directory.

`start.sh` invokes the repository's `.venv` and `config.json`. Arguments passed
to the launcher are forwarded to the service. The equivalent command-line
options (`--tv-ip`, `--bind-ip`, `--image-root`, `--state-dir`, `--port`) override
JSON values; `--config` selects another JSON file. Both `tv_ip` and `bind_ip`
are required. The default image root, when omitted, is `image/` beside the
service code; the UI port defaults to 8080.

## Organize and use photos

- Files directly in `image_root` form the **Default** set.
- Each immediate child folder is a separate occasion set. All its descendant
  folders are included. For example, put party pictures under `image/Party/`.
- The web page lets you choose a set, choose random or natural relative-path
  order, set a per-image interval (15 minutes by default), and save those
  settings. A selection does not affect the TV until you press **Sync to TV**.
- The UI shows image thumbnails and warnings. **Preview 15 seconds** is offered
  only for a photo in the currently synced set; the service restores the image
  that was on screen and restarts its normal interval.
- Add/remove/replace NAS files as needed, then sync explicitly to publish the
  new snapshot. The service does not watch folders or queue changes.

The service ignores `@eaDir`, hidden files/directories, `.DS_Store`, `._*`,
`.tmp`, and non-image extensions. It follows readable symlinks but detects
directory loops. It accepts non-animated GIF and common JPEG, PNG, WebP, BMP,
TIFF inputs, applies EXIF orientation, composites transparency onto `#181818`,
converts valid embedded profiles to sRGB, strips photo metadata from TV copies,
and writes quality-88 non-progressive 4:2:0 JPEGs. Inputs smaller than 640 px on
either side or wider/taller than 3.5:1 are skipped; output is capped at 3840 px
on its longest side. Originals are never modified.

The TV token and manifest are private application state. If the manifest is
unreadable, the service refuses to delete TV content. If the manifest is absent
(for example, on first launch), the service treats the owned set as empty and
preserves all existing TV art. On upload failure it rolls back only newly
uploaded IDs and preserves the previous active set. If cleanup after a successful
upload is incomplete, the new set remains active and known old IDs are retried
on the next explicit sync. Untracked/pre-existing Samsung art is never deleted;
accordingly, the service cannot guarantee that outside art is absent from the
TV's own rotation.

Sync requires the TV to be reachable and already in Art Mode. The service never
turns the TV on or changes Art Mode. Automatic cycling pauses when the TV is
unavailable or leaves Art Mode and resumes when it returns. One running service
instance manages one TV. The local web interface has no login; restrict access
to a trusted LAN and do not port-forward it.

## Development

Run the focused test suite with:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use generated images and a fake TV adapter; no tests connect to or alter a
real TV.
