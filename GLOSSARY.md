# Frame Art Mode Glossary

Domain language for managing personal NAS artwork on a Samsung The Frame TV. Keep definitions free of implementation details.

## Library & Sets

**Image root**:
The configured NAS directory from which source photos are read without modification.
_Avoid_: Source folder, photo share

**Default set**:
The image set formed by photo files placed directly inside the image root.
_Avoid_: Root folder, main gallery

**Occasion set**:
An image set formed by all photos recursively below one immediate child directory of the image root.
_Avoid_: Album, subfolder set

**TV-safe copy**:
A normalized, size-bounded JPEG copy derived from a source photo so the TV can upload and display it reliably without corrupting its art library.
_Avoid_: Snapshot, transcoded file, export

**Library cache**:
The persistent store of TV-safe copies, web thumbnails, and per-image validation results maintained outside the image root.
_Avoid_: Staging directory, temp snapshot

**Sync lease**:
A temporary hold that prevents library cache pruning from removing TV-safe copies required by an in-flight sync.
_Avoid_: Cache lock

**Library refresh**:
An explicit scan of the image root that updates the library cache for added, changed, or removed photos.
_Avoid_: Folder watch, background reindex

## TV Ownership & Playback

**Active set**:
The last image set committed to the TV and used for automatic slideshow cycling.
_Avoid_: Current folder, live gallery

**Viewed set**:
The image set currently selected for browsing and configuring playback preferences in the web interface; changing it does not affect the TV until an explicit sync.
_Avoid_: Pending selection, active set

**Ownership manifest**:
The per-TV durable record of managed TV items, cleanup-pending items, active set, and per-set playback settings.
_Avoid_: TV index, database

**Managed TV item**:
An artwork item uploaded and verified by this application and recorded in the ownership manifest.
_Avoid_: App photo, tracked art

**Cleanup-pending item**:
A previously managed TV item whose deletion failed or was deferred by an abort or restart, queued for retry on the next explicit sync.
_Avoid_: Orphan, stale upload

**TV drift**:
The condition where a managed TV item recorded in the ownership manifest is no longer present on the TV.
_Avoid_: Desync, missing cache

**Mix Art Store favorites**:
A per-set preference to include recognized TV-owned favorites alongside NAS photos during random playback; unused in sequential playback.
_Avoid_: Favorites sync

**Art Store favorite**:
TV-owned artwork returned from the TV's favorites category (`MY-C0004`) that this application may select for display but never owns, caches, uploads, or deletes.
_Avoid_: Store item, managed favorite

**Preview**:
A temporary display of a photo already in the active set for a configured duration, followed by restoring the artwork (and category) that was previously on screen.
_Avoid_: Temporary upload, spotlight

## Operations & Status

**Operation status**:
The durable snapshot of the currently running TV or library job—including unified step count—and the most recent completed, aborted, failed, or interrupted outcome.
_Avoid_: Event log, job history

**Cooperative abort**:
A user-requested stop that halts a running sync, library refresh, or preview cleanly at the next safe operation boundary without interrupting an in-flight TV write.
_Avoid_: Force kill, cancel storm

**Interrupted sync**:
A sync that was running when the TV service restarted; its last committed active set remains active and known partial uploads are moved to cleanup-pending rather than resumed automatically.
_Avoid_: Crashed sync
