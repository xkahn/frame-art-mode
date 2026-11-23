# ADR 0003: Durable TV operation status and sync recovery

- **Status:** Accepted
- **Decision date:** 2026-11-23

## Context

Sync may take long enough that a browser request should not remain open, and TV
communication can time out after partial work. Users need to see the current
phase and latest outcome after a browser reconnect or service restart without
mistaking a partial upload for an active set.

## Decision

- Run sync in a server-side background thread and persist one current-operation
  snapshot plus the most recent result/error and timestamp. Do not retain an
  event history.
- Refresh status from the browser while an operation is active; status reads do
  not contact the TV. Check reachability once on initial page load, and do not
  poll the TV while idle.
- Report phase, current image, and file counts. Use an indeterminate
  upload/waiting-for-TV-confirmation phase because the Samsung library does not
  expose byte-level upload progress.
- On service startup, convert any persisted running operation to interrupted.
  Never resume it automatically. Preserve the active set and add only known
  partial upload IDs to cleanup-pending; reconcile those IDs on the next
  explicit sync. A successful manifest commit remains authoritative if restart
  occurs during old-item cleanup. Never infer ownership from TV contents.
- A timeout ends the operation and requires an explicit retry. Existing
  transactional manifest/rollback behavior remains authoritative.

## Consequences

The browser can close while sync continues and later display the same operation
state. The operation file is written alongside the ownership manifest and
therefore shares its private state-directory requirements. If the process dies
after an upload but before the TV returns a usable content ID, that item remains
unknown and is intentionally not deleted. Interrupted operations require a
user-initiated sync before known partial uploads are cleaned up.
