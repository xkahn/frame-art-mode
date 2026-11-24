# Frame Art Mode — Agent Guide

Personal art manager for a Samsung The Frame TV running on Synology DSM 7 (Python 3.11+).

## Reference Pointers

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — Three-process topology, Unix socket IPC endpoints, state ownership, anti-wedge sync state machine, cache layout, and `config.json` schema. Read before modifying process boundaries, IPC contracts, TV sync, image caching, UI APIs, or configuration.
- **[docs/CODING_STYLE.md](docs/CODING_STYLE.md)** — Python 3.11+ conventions, hard import boundaries, atomic persistence, error/logging rules, zero-build frontend rules, and testing standards. Read before writing or reviewing code or tests.
- **[GLOSSARY.md](GLOSSARY.md)** — Canonical domain vocabulary. Read when naming modules, API fields, UI text, or docs.
- **[docs/adr/](docs/adr/)** — Binding architecture decisions:
  - [0001](docs/adr/0001-tv-art-ownership-and-sync.md): TV manifest ownership and anti-wedge sync rules (select-before-delete, fail-fast, drift).
  - [0002](docs/adr/0002-art-store-favorites.md): Read/select-only Art Store favorites (`MY-C0004`, `SAM-` prefix).
  - [0003](docs/adr/0003-durable-tv-operation-status.md): Durable operation status, unified step-count progress, cooperative abort, no idle TV polling.
  - [0004](docs/adr/0004-three-process-architecture-and-uds-ipc.md): Three-process split, Python supervisor, `0700` Unix domain socket IPC.
  - [0005](docs/adr/0005-persistent-tv-safe-image-cache.md): Persistent content-addressed TV-safe JPEG (5 MB cap) and thumbnail cache.

## Hard Invariants

- **Import boundaries:** Keep `web_server` free of `samsungframetv` and `PIL`; keep `tv_daemon` free of `PIL` and direct `image_root` access; keep `library_daemon` free of `samsungframetv` and `flask`; keep shared modules (`config.py`, `ipc.py`, `models.py`) stdlib-only.
- **TV safety:** Preserve manifest-only deletion, select-before-delete, fail-fast on first TV error, cooperative abort without rollback delete storms, and zero idle TV polling.
- **Verification:** Run `.venv/bin/python -m unittest discover -s tests -v`. Use fake TV adapters and temporary directories; keep tests completely isolated from real TVs.
