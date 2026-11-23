# Glossary

- **Image root**: Configured NAS directory from which source photos are read.
- **Default set**: Image files directly inside the image root.
- **Occasion set**: All images recursively below one immediate child directory
  of the image root.
- **Active set**: The last set successfully synced and currently selected for
  automatic cycling.
- **Pending selection**: The set and playback options currently chosen in the
  web form; they do not affect the TV until an explicit sync.
- **Mix Art Store favorites**: Per-set preference to include recognized
  TV-owned favorites in random playback; disabled by default and unused when
  sequential playback is selected.
- **Art Store favorite**: TV-owned content returned from category `MY-C0004`.
  This service only reads and selects it. It is not part of the NAS gallery or
  ownership manifest; IDs without the documented `SAM-` prefix are skipped.
- **Snapshot**: Immutable, sanitized JPEG copies staged outside the image root
  during one explicit sync.
- **Managed TV item**: A TV content ID returned/verified during this service's
  upload and recorded in its per-TV manifest.
- **Cleanup-pending item**: A previously managed TV item whose deletion failed;
  it remains recorded for retry on the next explicit sync.
- **Ownership manifest**: Per-TV durable record of active and cleanup-pending
  content IDs, source fingerprints, selected set, and per-set playback settings.
- **Preview**: A temporary selection of an image already in the active set,
  followed by restoration of the image that had been on screen.
- **TV communication status**: The durable current-operation snapshot and most
  recent outcome, including its timestamp; it is not an event history.
- **Interrupted sync**: A sync that was running when the service restarted.
  The last committed active set remains selected, and only known partial uploads are
  queued as cleanup-pending for the next explicit sync.
