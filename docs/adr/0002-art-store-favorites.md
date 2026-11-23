# ADR 0002: Art Store favorites are TV-owned

- **Status:** Accepted
- **Decision date:** 2026-11-23

## Context

The TV's Art Store favorites are separate from the images uploaded and tracked
by this service. Mixing them into playback must not blur the ownership boundary
or make a TV/network failure behave like an empty favorites list.

## Decision

- Store a `mix_art_store_favorites` preference per image set in the existing
  ownership manifest. It defaults to false. Saving settings persists it
  immediately; it does not require syncing the NAS set.
- Use the TV-wide `MY-C0004` category only before random picks for the active
  set when the preference is enabled. Sequential playback, the NAS gallery, and
  manual previews do not query or select favorites.
- Include only candidate IDs with the documented `SAM-` prefix. The library's
  examples do not guarantee that this prefix is universal, so warn and skip
  unknown IDs rather than assuming they are Art Store items.
- Deduplicate by content ID, retaining an existing NAS entry as the candidate
  if it also appears in the favorites response. Select favorites with the
  `MY-C0004` category.
- Favorites are read/select-only. Never upload, delete, favorite, unfavorite,
  or add them to the app-managed ownership manifest or cleanup list.
- Treat a successful empty response as no additional candidates. If a query
  fails, warn and use the NAS candidates for that pick, then query again before
  the next pick.
- When a NAS preview interrupts a favorite, restore both its content ID and
  category before resuming the normal interval.

## Consequences

Random playback can include the TV's current favorites without making them
service-managed. Non-matching IDs are intentionally excluded, and temporary
query failures can produce a NAS-only pick but do not disable later retries.
Favorites are not visible or previewable in the NAS gallery.
