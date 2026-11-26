# ADR 0003: Durable operation status, step-count progress, and cooperative abort

- **Status:** Accepted (Amended 2026-11-23)
- **Decision date:** 2026-11-23 (amended 2026-11-23)
- **Related:** [ADR 0001](0001-tv-art-ownership-and-sync.md), [ADR 0004](0004-three-process-architecture-and-uds-ipc.md)

## Context

TV syncs and large image-library refreshes can run longer than a single browser
request, and users need to see live progress, reconnect across page reloads,
and stop a running job safely. Originally this ADR specified indeterminate
upload progress because `samsungframetv` exposes no byte-level upload callback.
However, while individual step wall-clock duration is unknown, the exact count
of discrete operations (prepare, upload, settle, commit/select, delete) **is**
known once the job plan is computed—making a determinate operation-count
progress bar far more informative than an indeterminate bar.

## Decision

- **Durable current-operation snapshot:** Run sync in `tv_daemon` and refresh
  in `library_daemon` as background jobs, persisting one current-operation
  snapshot (`operation.json` for TV; `library.json` / runtime state for
  Library) plus the most recent outcome (`completed`, `aborted`, `failed`, or
  `interrupted`) and timestamp. Do not retain an unbounded event history.
- **Unified step-count progress + active spinner:** Report a single monotonic
  `completed_ops / total_ops` counter across the entire job alongside `phase`,
  `message`, and `current_item` (superseding the earlier indeterminate-only
  progress rule). The Web UI renders a determinate progress bar together with
  an active spinner while any operation is running so users can see both overall
  completion and live activity during long individual TV calls.
- **Cooperative abort:** Support `Abort` across Sync, Library Refresh, and
  Preview. Abort sets a flag checked at every operation boundary and
  interruptible pacing sleep—never killing an in-flight TV WebSocket write:
  - Aborting Sync before manifest commit preserves the previous active set and
    records already-uploaded IDs in `cleanup_pending`.
  - Aborting Sync during old-item cleanup keeps the newly active set and leaves
    remaining old IDs in `cleanup_pending`.
  - Aborting Library Refresh stops after the current image finishes processing.
  - Aborting Preview immediately restores the previous image and category.
- **Live local status without idle TV polling:** Deliver status updates to the
  Web UI over Server-Sent Events (SSE) with automatic JSON polling fallback,
  reading local daemon state only. Perform zero automatic TV checks on startup
  or page load (`tv_ready` starts `None`); never poll the TV while idle, and
  check TV reachability only on explicit manual **Check TV**, **Sync**,
  **Preview**, or scheduled slideshow ticks.
- **Restart recovery:** On `tv_daemon` startup, convert any persisted running
  operation to `interrupted` without resuming it automatically. Preserve the
  last committed active set and move known partial upload IDs from before
  commit into `cleanup_pending` for the next explicit Sync.

## Consequences

The browser can close or reload without interrupting work and immediately
displays accurate step progress and active-spinner state on reconnect. Users
can halt runaway syncs or refreshes safely without risking half-written TV
WebSocket frames. If the TV process is killed after an upload finishes on the
TV but before the TV returns a content ID, that item remains unknown and is
intentionally not deleted.
