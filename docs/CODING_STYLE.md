# Python Coding Style & Development Guide

This guide defines the engineering conventions for `frame-art-mode`. Its goals are **predictable runtime safety on a Synology NAS**, **strict separation between the three processes**, and **code that is easy to read, test, and maintain without heavy dependencies**.

---

## 1. Runtime & Dependency Rules

- **Target Python:** Python **3.11+** (minimum supported 3.10 per `samsungframetv >= 4.0`; Synology DSM 7 instructions target Python 3.11).
- **Minimal dependencies:** Prefer the Python standard library (`http.server`, `http.client`, `socketserver`, `dataclasses`, `pathlib`, `fcntl`, `json`, `threading`, `logging`, `unittest`) over adding third-party packages.
- **No build toolchain:** Keep the project pure Python plus static HTML/CSS/JS—no Node.js, bundlers, or compiled extensions outside `Pillow`.
- **Hard import boundaries (enforced by tests):**
  - `web_server` and web modules **must never** import `samsungframetv`, `PIL`, or `tv_daemon` internals.
  - `tv_daemon` and TV modules **must never** import `PIL`, `flask`, or read `image_root`.
  - `library_daemon` and image modules **must never** import `samsungframetv` or `flask`.
  - Shared modules (`config.py`, `ipc.py`, `models.py`) **must only** import the Python standard library.

---

## 2. Python Style & Formatting

Follow **PEP 8** with these repository-specific conventions:

- **Future annotations:** Start every module with `from __future__ import annotations`.
- **Line length:** Soft limit **100 characters** (wrap cleanly at logical boundaries; avoid ragged manual alignment).
- **Quotes & indentation:** 4 spaces, double quotes for strings, trailing commas on multi-line collections/calls/signatures.
- **Imports:** Group in three blocks separated by a blank line: (1) standard library, (2) third-party, (3) local relative imports (`.config`, `.ipc`). Keep imports explicit—no wildcard imports (`from x import *`).
- **Naming:**
  - `snake_case` for modules, functions, methods, variables, and JSON keys.
  - `PascalCase` for classes, dataclasses, and custom exceptions.
  - `UPPER_SNAKE_CASE` for module-level constants.
  - Leading underscore (`_helper`) for internal functions and private attributes.
- **Type annotations:**
  - Annotate all public function parameters and return types.
  - Use modern built-in generics (`list[str]`, `dict[str, Any]`, `tuple[int, int]`) and union syntax (`str | None`) instead of `typing.List`/`Optional`.
  - Use `@dataclass(frozen=True)` for immutable value objects (`Config`, `SourceImage`, `CachedImage`, `OperationState`) and `Protocol` for test seams (such as `TVArtClient`).
- **Paths:** Use `pathlib.Path` everywhere; convert to `str` only at third-party API boundaries (`samsungframetv`, `os.scandir`, JSON serialization).

---

## 3. Module Design & State Safety

- **Keep modules small and cohesive:** Each process entrypoint should separate CLI argument parsing (`main()`), IPC transport wiring, and core domain logic so domain logic is directly unit-testable without spawning processes or sockets.
- **Atomic file writes:** Every durable JSON file (`manifest.json`, `operation.json`, `library.json`) and cached image must be written to a temporary file in the same directory, flushed (`flush()` + `os.fsync()` for manifests/operations), `chmod`ed (`0o600` for private state), and committed with `Path.replace()`. Never truncate and write durable state in place.
- **Process locks:** Any daemon owning mutable state acquires a non-blocking exclusive `fcntl.flock` lock at startup and exits with code `2` if already held.
- **Umask & permissions:** Daemons set `os.umask(0o077)` at startup; `state_dir` and `<tv_key>/` are `0o700`.
- **Single-lane TV actor:** All TV WebSocket I/O runs sequentially inside one serialized lane. Never spawn ad-hoc threads that call the TV concurrently with sync, preview, or slideshow cycling.
- **Interruptible sleeps:** Long pacing/settle waits (`tv_op_delay_seconds`, `tv_batch_settle_seconds`) must wait on an abort/stop `threading.Event` (or injected sleeper in tests), never an uninterruptible `time.sleep()` loop that ignores `Abort` or `SIGTERM`.

---

## 4. Error Handling & Logging

- **Explicit domain exceptions:** Raise narrow, meaningful exceptions (`ConfigError`, `LibraryError`, `ImageError`, `TVServiceError`, `UploadError`, `AbortRequested`) rather than raw `RuntimeError` or silent `None` returns.
- **No silent catches:** Never write bare `except:` or swallow exceptions without logging or surfacing a user-visible warning/error. Catch `Exception` only at process/job/IPC boundaries where the error is recorded into `operation.json`, returned as a JSON error response, or converted to a skip warning.
- **Chain exceptions:** Always use `raise NewError("...") from exc` when translating errors.
- **Fail fast on TV errors:** Inside a TV write batch (upload or delete), treat the first TV/network/timeout error as a stop signal—never swallow it and continue sending subsequent TV mutations.
- **Structured, concise logging:**
  - Use module-level loggers: `logger = logging.getLogger(__name__)`.
  - Use lazy `%`-formatting in log calls (`logger.info("Uploaded %s as %s", filename, content_id)`), not f-strings.
  - Never log secrets or TV auth tokens.
  - Log format includes timestamp, component/logger name, level, and message so interleaved supervisor logs clearly identify which process emitted each line.

---

## 5. IPC & Web API Conventions

- **JSON envelopes:** All Unix socket and Web `/api/*` endpoints return `application/json` with consistent HTTP status codes:
  - `200 OK` / `202 Accepted` for successful reads or background job starts.
  - `400 Bad Request` for invalid JSON or invalid input parameters.
  - `403 Forbidden` for missing or invalid CSRF token.
  - `404 Not Found` for unknown sets, images, or endpoints.
  - `405 Method Not Allowed` for unsupported HTTP methods on `/api/*`.
  - `409 Conflict` when a mutually exclusive operation is already running.
  - `413 Payload Too Large` when a `/api/*` request body exceeds the 1 MB limit.
  - `500 Internal Server Error` for unexpected unhandled server errors.
  - `503 Service Unavailable` when the Web UI cannot reach a required backend daemon socket.
- **Error body format:** Return `{"error": "<human-readable message>", "code": "<machine_code>"}` on all 4xx/5xx responses.
- **CSRF protection:** Every mutating Web endpoint (`POST`) requires a matching `X-CSRF-Token` header tied to the Flask session.
- **Zero idle TV polling:** Status endpoints (`GET /status`, `GET /api/status`, `GET /api/events`) must **only** read in-memory or local state—never initiate TV network calls.

---

## 6. Frontend Conventions (Zero-Build)

- Keep HTML in `frame_art_mode/templates/` and CSS/JS in `frame_art_mode/static/`—never inline large HTML/JS blobs inside Python modules.
- Use semantic HTML, vanilla ES2020+ JavaScript, and CSS custom properties (`:root` variables); no build steps or external CDN links (the NAS LAN may be offline).
- Render untrusted strings (filenames, warnings, error messages) with `textContent` / DOM builders—never concatenate them into `innerHTML`.
- Keep all mutating UI actions `fetch()`-based with explicit busy/disabled states, corner toasts for outcomes/errors, and the unified progress bar + spinner + Abort control for active operations.

---

## 7. Testing Standards

- **Never touch a real TV in tests.** Inject fake TV adapters (`TVArtClient` fakes), fake clocks, and fake sleepers so tests are deterministic and run in milliseconds.
- **Test runner:** Standard library `unittest`:
  ```sh
  .venv/bin/python -m unittest discover -s tests -v
  ```
- **What every change should test:**
  - **Unit tests** for config validation, image normalization (including 5 MB step-down and skip rules), cache invalidation/pruning, manifest integrity, and TV sync state transitions.
  - **Anti-wedge tests** proving select-before-delete ordering, fail-fast stop on first upload/delete error, cooperative abort before and after commit, drift skipping, and fresh socket disposal on error.
  - **IPC integration tests** exercising real Unix domain sockets (`tv.sock`, `library.sock`) in a temporary directory and Web UI degraded-mode behavior when a backend socket is offline.
  - **Boundary tests** verifying forbidden cross-component imports (`web_server` does not import `samsungframetv`/`PIL`; `tv_daemon` does not import `PIL`).
- **No hidden test coupling:** Use `tempfile.TemporaryDirectory()` for every filesystem/socket test and ensure all daemon threads/servers shut down cleanly in `tearDown` / `addCleanup`.

---

## 8. Documentation & ADR Workflow

- Keep [`GLOSSARY.md`](../GLOSSARY.md) strictly for domain terminology (no implementation specs).
- Keep [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) aligned with actual IPC endpoints, file layouts, and state machines whenever a boundary changes.
- Add a numbered ADR under `docs/adr/` when a decision is hard to reverse, surprising without context, and reflects a genuine trade-off. Never silently rewrite historical ADR decisions—mark superseded ADRs explicitly and link to the replacement.
