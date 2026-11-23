# ADR 0001: TV art ownership and sync behavior

- **Status:** Accepted
- **Decision date:** 2026-11-22

## Context

The service manages user-selected art on a shared TV that may already contain
Samsung Store art or images uploaded by other tools. Replacing the complete TV
library or deleting IDs discovered at runtime could destroy art the service
does not own. A network failure can also occur after some images have uploaded
or after the new set has become usable.

## Decision

- Keep a persistent ownership manifest per TV and delete only IDs previously
  recorded by this service. A missing manifest starts with no owned IDs; an
  unreadable or invalid manifest blocks sync rather than guessing ownership.
- Build and verify all valid new/changed images before deleting old managed IDs.
  If any upload fails, delete only IDs uploaded by that attempt and leave the
  prior active set unchanged.
- Commit the newly verified set before old-item cleanup. If cleanup is partial,
  make the new set active, retain failed old IDs as cleanup-pending, and retry
  them during the next explicit sync. This favors keeping the new selection
  usable over rolling it back when the TV has already accepted it.
- Never infer ownership from a TV content list. A TV item not in the manifest
  is preserved, even if it appears to be one uploaded by this service.
- Stage a fixed sanitized snapshot outside the source tree for each sync.
  Source changes during an operation are therefore deferred to the next
  explicit sync.

## Consequences

Existing or untracked art can remain in the TV's own rotation; the application
does not claim exclusive control of the TV library. If the ownership manifest
is lost, old uploaded files become untracked and are intentionally not deleted.
Partial cleanup can leave duplicate/old app-managed images on the TV until a
later retry. Those trade-offs avoid destructive guesses and preserve a working
active selection during failures.
