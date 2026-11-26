# ADR 0001: TV art ownership and anti-wedge sync behavior

- **Status:** Accepted (Amended 2026-11-23)
- **Decision date:** 2026-11-22 (amended 2026-11-23)
- **Related:** [ADR 0004](0004-three-process-architecture-and-uds-ipc.md), [ADR 0005](0005-persistent-tv-safe-image-cache.md)

## Context

The service manages user-selected art on a shared Samsung The Frame TV that may
already contain Samsung Store art or images uploaded by other tools. Replacing
the complete TV library or deleting IDs discovered at runtime could destroy art
the service does not own.

In addition, real-world experience swapping a full image set on The Frame
showed that aggressive delete/upload sequences can wedge the TV's internal art
database, making it impossible to display or upload photos until the TV is
factory-reset. Contributing hazards include deleting the currently displayed
artwork, continuing to send writes to a struggling TV after an error, rapid
back-to-back mutations, and reusing stale WebSocket connections.

## Decision

- **Manifest-only ownership:** Keep a persistent ownership manifest per TV and
  delete only IDs previously recorded by this service. A missing manifest starts
  with no owned IDs; an unreadable or externally modified manifest blocks sync
  rather than guessing ownership. Never infer ownership from a TV content list.
- **Strictly serialized TV lane:** All TV WebSocket traffic (reachability
  checks, sync, preview, preview restore, slideshow cycling) runs sequentially
  through one serialized TV actor using a fresh WebSocket per operation/job,
  closed and discarded immediately on completion, error, or timeout.
- **Pinned TV-safe inputs:** Consume verified, content-addressed TV-safe JPEGs
  pinned by the library service (see [ADR 0005](0005-persistent-tv-safe-image-cache.md))
  rather than rebuilding ephemeral snapshots or reading raw NAS files directly.
- **Upload and verify before mutating active state:** Upload and verify all
  new/changed images before committing the manifest, pacing consecutive TV
  writes with conservative configurable delays (`tv_op_delay_seconds=3.0`,
  `tv_batch_settle_seconds=12.0`, `tv_verify_retries=4`,
  `tv_verify_interval_seconds=3.0`).
- **Fail-fast circuit breaker:** Stop immediately on the **first** upload error,
  verification failure, delete error, timeout, or lost Art Mode state rather
  than continuing the batch against a potentially wedged TV. If failure occurs
  before manifest commit, leave the prior active set unchanged and record any
  already-uploaded partial IDs in `cleanup_pending` instead of firing an
  immediate rollback delete storm.
- **Select-before-delete:** Commit the new manifest and **select a new image on
  the TV first**, then delete old managed IDs (explicitly excluding the
  currently displayed content ID). If cleanup stops early due to an error or
  abort, keep the new set active and retain remaining old IDs in
  `cleanup_pending` for retry on the next explicit sync.
- **Drift tolerance:** If a manifest-tracked item disappears from the TV
  externally, surface a persistent drift warning, skip that ID during slideshow
  cycling, and re-upload it cleanly on the next explicit sync without mutating
  ownership rules.

## Considered Options

- **Immediate rollback deletes on upload failure or abort** — rejected because
  firing a burst of `delete()` calls immediately after a TV error or user Abort
  increases the chance of wedging The Frame's art database. Recording known
  partial IDs in `cleanup_pending` keeps the prior active set safe with zero
  extra TV traffic until the user explicitly syncs again.
- **Delete old set before uploading/selecting new set** — rejected because
  deleting the currently displayed item or emptying the TV art set mid-swap is
  a primary trigger for Frame TV database wedges.

## Consequences

Existing or untracked art can remain in the TV's own rotation; the application
does not claim exclusive control of the TV library. If the ownership manifest
is lost, old uploaded files become untracked and are intentionally not deleted.
Failed, aborted, or partially cleaned syncs can temporarily leave extra
app-managed images on the TV until the next explicit Sync reconciles
`cleanup_pending`. Full set swaps take slightly longer due to conservative
pacing, trading raw speed for TV database stability.
