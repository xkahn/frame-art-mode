(() => {
  "use strict";

  const SHA256_RE = /^[0-9a-f]{64}$/;
  const THUMB_PATH_RE = /(?:^|\/)thumbs\/([0-9a-f]{64})\.jpg$/;
  const DEFAULT_SETTINGS = {
    random: true,
    mix_art_store_favorites: false,
    interval_minutes: 15,
  };

  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = csrfMeta ? csrfMeta.getAttribute("content") || "" : "";

  const initialStateEl = document.getElementById("initial-state");
  let state = initialStateEl ? JSON.parse(initialStateEl.textContent || "{}") : {};

  let viewedSetId = chooseInitialSetId(state);
  let isDirty = false;
  let lastGallerySignature = "";
  let lastSeenTvResultKey = resultKey(state.tv && state.tv.last_result);
  let lastSeenLibraryResultKey = resultKey(state.library && state.library.last_result);
  let eventSource = null;
  let pollTimer = null;
  let reconnectTimer = null;
  let failedNowPlayingSrc = null;

  const busy = {
    check: false,
    settings: false,
    sync: false,
    refresh: false,
    abort: false,
    previewId: null,
  };

  const dom = {
    liveConnection: document.getElementById("live-connection"),
    warningsToggleBtn: document.getElementById("warnings-toggle-btn"),
    warningsCount: document.getElementById("warnings-count"),
    warningsDrawer: document.getElementById("warnings-drawer"),
    warningsCloseBtn: document.getElementById("warnings-close-btn"),
    warningsList: document.getElementById("warnings-list"),
    dotWeb: document.getElementById("dot-web"),
    statusWeb: document.getElementById("status-web"),
    dotTv: document.getElementById("dot-tv"),
    statusTv: document.getElementById("status-tv"),
    errorTv: document.getElementById("error-tv"),
    dotLibrary: document.getElementById("dot-library"),
    statusLibrary: document.getElementById("status-library"),
    errorLibrary: document.getElementById("error-library"),
    tvReadyBadge: document.getElementById("tv-ready-badge"),
    tvStatusDetail: document.getElementById("tv-status-detail"),
    checkTvBtn: document.getElementById("check-tv-btn"),
    nowPlayingImg: document.getElementById("now-playing-img"),
    nowPlayingPlaceholder: document.getElementById("now-playing-placeholder"),
    activeSetLabel: document.getElementById("active-set-label"),
    nowPlayingTitle: document.getElementById("now-playing-title"),
    nowPlayingCountdown: document.getElementById("now-playing-countdown"),
    libraryCounts: document.getElementById("library-counts"),
    libraryRefreshed: document.getElementById("library-refreshed"),
    viewedSyncState: document.getElementById("viewed-sync-state"),
    operationBar: document.getElementById("operation-bar"),
    operationLabel: document.getElementById("operation-label"),
    operationCounts: document.getElementById("operation-counts"),
    operationItem: document.getElementById("operation-item"),
    operationSecondary: document.getElementById("operation-secondary"),
    operationProgressFill: document.getElementById("operation-progress-fill"),
    progressTrack: document.querySelector(".progress-track"),
    abortBtn: document.getElementById("abort-btn"),
    setSelect: document.getElementById("set-select"),
    settingRandom: document.getElementById("setting-random"),
    settingFavorites: document.getElementById("setting-favorites"),
    settingInterval: document.getElementById("setting-interval"),
    saveSettingsBtn: document.getElementById("save-settings-btn"),
    syncBtn: document.getElementById("sync-btn"),
    refreshBtn: document.getElementById("refresh-btn"),
    galleryHeading: document.getElementById("gallery-heading"),
    gallerySubtitle: document.getElementById("gallery-subtitle"),
    galleryEmpty: document.getElementById("gallery-empty"),
    galleryGrid: document.getElementById("gallery-grid"),
    cardTemplate: document.getElementById("card-template"),
    toastContainer: document.getElementById("toast-container"),
    toastTemplate: document.getElementById("toast-template"),
  };

  function resultKey(result) {
    if (!result || typeof result !== "object") {
      return "";
    }
    return [
      result.timestamp || "",
      result.kind || "",
      result.state || "",
      result.message || "",
    ].join("|");
  }

  function getSetItems(library, setId) {
    if (!library || !library.sets || typeof library.sets !== "object") {
      return [];
    }
    const entry = library.sets[setId];
    if (Array.isArray(entry)) {
      return entry;
    }
    if (entry && typeof entry === "object" && Array.isArray(entry.images)) {
      return entry.images;
    }
    return [];
  }

  function formatSetName(setId) {
    if (!setId) {
      return "None";
    }
    if (setId === "root") {
      return "Default (root-level images)";
    }
    if (setId.startsWith("occasion:")) {
      return setId.slice("occasion:".length);
    }
    return setId;
  }

  function extractSha256(item) {
    if (!item || typeof item !== "object") {
      return null;
    }
    for (const candidate of [item.sha256, item.fingerprint]) {
      if (typeof candidate === "string" && SHA256_RE.test(candidate)) {
        return candidate;
      }
    }
    if (typeof item.thumb_path === "string") {
      const match = THUMB_PATH_RE.exec(item.thumb_path);
      if (match) {
        return match[1];
      }
    }
    return null;
  }

  function availableSetIds(snapshot) {
    const sets = snapshot.library && snapshot.library.sets;
    if (sets && typeof sets === "object") {
      const keys = Object.keys(sets);
      if (keys.length > 0) {
        return keys;
      }
    }
    if (snapshot.tv && snapshot.tv.active_set) {
      return [snapshot.tv.active_set];
    }
    return ["root"];
  }

  function chooseInitialSetId(snapshot) {
    const ids = availableSetIds(snapshot);
    const active = snapshot.tv && snapshot.tv.active_set;
    if (active && ids.includes(active)) {
      return active;
    }
    if (ids.includes("root")) {
      return "root";
    }
    return ids[0] || "root";
  }

  function settingsForSet(snapshot, setId) {
    const map = snapshot.tv && snapshot.tv.settings;
    if (map && typeof map === "object" && Object.prototype.hasOwnProperty.call(map, setId)) {
      const cfg = map[setId];
      if (cfg && typeof cfg === "object") {
        return {
          random: typeof cfg.random === "boolean" ? cfg.random : DEFAULT_SETTINGS.random,
          mix_art_store_favorites:
            typeof cfg.mix_art_store_favorites === "boolean"
              ? cfg.mix_art_store_favorites
              : DEFAULT_SETTINGS.mix_art_store_favorites,
          interval_minutes:
            Number.isInteger(cfg.interval_minutes) && cfg.interval_minutes >= 1
              ? cfg.interval_minutes
              : DEFAULT_SETTINGS.interval_minutes,
        };
      }
    }
    return { ...DEFAULT_SETTINGS };
  }

  function setDirty(dirty) {
    isDirty = Boolean(dirty);
    dom.saveSettingsBtn.classList.toggle("is-dirty", isDirty);
  }

  function applyFormSettingsFromState() {
    const cfg = settingsForSet(state, viewedSetId);
    dom.settingRandom.checked = cfg.random;
    dom.settingFavorites.checked = cfg.mix_art_store_favorites;
    dom.settingInterval.value = String(cfg.interval_minutes);
  }

  function readFormSettings() {
    const parsedInterval = Number.parseInt(dom.settingInterval.value, 10);
    if (!Number.isInteger(parsedInterval) || parsedInterval < 1) {
      return { error: "Minutes per image must be a whole number >= 1." };
    }
    return {
      value: {
        random: Boolean(dom.settingRandom.checked),
        mix_art_store_favorites: Boolean(dom.settingFavorites.checked),
        interval_minutes: parsedInterval,
      },
    };
  }

  function formatTimestamp(isoString) {
    if (!isoString || typeof isoString !== "string") {
      return "Never refreshed";
    }
    const ms = Date.parse(isoString);
    if (Number.isNaN(ms)) {
      return isoString;
    }
    return new Date(ms).toLocaleString();
  }

  function formatDurationSeconds(totalSeconds) {
    const clamped = Math.max(0, Math.floor(totalSeconds));
    const mins = Math.floor(clamped / 60);
    const secs = clamped % 60;
    if (mins >= 60) {
      const hrs = Math.floor(mins / 60);
      const remMins = mins % 60;
      return `${hrs}h ${String(remMins).padStart(2, "0")}m`;
    }
    return `${mins}:${String(secs).padStart(2, "0")}`;
  }

  function showToast(message, kind = "info", options = {}) {
    const fragment = dom.toastTemplate.content.cloneNode(true);
    const toastEl = fragment.querySelector(".toast");
    const msgEl = fragment.querySelector(".toast-message");
    const reloadBtn = fragment.querySelector(".toast-reload");
    const dismissBtn = fragment.querySelector(".toast-dismiss");

    toastEl.classList.add(`is-${kind}`);
    msgEl.textContent = message;

    if (options.showReload) {
      reloadBtn.hidden = false;
      reloadBtn.addEventListener("click", () => {
        window.location.reload();
      });
    }

    const removeToast = () => {
      if (toastEl.parentNode) {
        toastEl.parentNode.removeChild(toastEl);
      }
    };

    dismissBtn.addEventListener("click", removeToast);
    dom.toastContainer.appendChild(toastEl);

    if (kind !== "error") {
      window.setTimeout(removeToast, 5000);
    }
  }

  function setConnectionStatus(mode, label) {
    dom.liveConnection.className = "connection-pill";
    if (mode) {
      dom.liveConnection.classList.add(mode);
    }
    dom.liveConnection.textContent = label;
  }

  function renderHealth() {
    const services = state.services || {};
    const webOk = !services.web || services.web.ok !== false;
    dom.dotWeb.className = `status-dot ${webOk ? "is-ok" : "is-offline"}`;
    dom.statusWeb.textContent = webOk ? "online" : "offline";

    const tvSvc = services.tv || { ok: false, error: "unknown" };
    dom.dotTv.className = `status-dot ${tvSvc.ok ? "is-ok" : "is-offline"}`;
    dom.statusTv.textContent = tvSvc.ok ? "online" : "offline";
    if (!tvSvc.ok && tvSvc.error) {
      dom.errorTv.hidden = false;
      dom.errorTv.textContent = tvSvc.error;
    } else {
      dom.errorTv.hidden = true;
      dom.errorTv.textContent = "";
    }

    const libSvc = services.library || { ok: false, error: "unknown" };
    dom.dotLibrary.className = `status-dot ${libSvc.ok ? "is-ok" : "is-offline"}`;
    dom.statusLibrary.textContent = libSvc.ok ? "online" : "offline";
    if (!libSvc.ok && libSvc.error) {
      dom.errorLibrary.hidden = false;
      dom.errorLibrary.textContent = libSvc.error;
    } else {
      dom.errorLibrary.hidden = true;
      dom.errorLibrary.textContent = "";
    }
  }

  function renderTvStatus() {
    const tvOk = Boolean(state.services && state.services.tv && state.services.tv.ok);
    const tv = state.tv;

    dom.tvReadyBadge.className = "status-pill";
    if (!tvOk || !tv) {
      dom.tvReadyBadge.classList.add("is-bad");
      dom.tvReadyBadge.textContent = "TV Daemon offline";
      dom.tvStatusDetail.textContent = "Start TV daemon to check Art Mode or manage playback.";
    } else if (tv.tv_ready === true) {
      dom.tvReadyBadge.classList.add("is-ok");
      dom.tvReadyBadge.textContent = "In Art Mode";
      dom.tvStatusDetail.textContent = tv.tv_checked_at
        ? `Ready (checked ${formatTimestamp(tv.tv_checked_at)}).`
        : "TV ready for manual sync or preview.";
    } else if (tv.tv_ready === false) {
      dom.tvReadyBadge.classList.add("is-bad");
      dom.tvReadyBadge.textContent = "Unavailable / Not in Art Mode";
      dom.tvStatusDetail.textContent = tv.tv_checked_at
        ? `Unavailable as of ${formatTimestamp(tv.tv_checked_at)}.`
        : "TV unreachable or powered off / not in Art Mode.";
    } else {
      dom.tvReadyBadge.classList.add("is-warn");
      dom.tvReadyBadge.textContent = "Not checked yet";
      dom.tvStatusDetail.textContent = "No idle polling; click Check TV to query Art Mode.";
    }
  }

  function resolveNowPlayingSha(display, tv, library) {
    if (!display || display.is_art_store) {
      return null;
    }
    if (tv && Array.isArray(tv.entries) && display.content_id) {
      for (const entry of tv.entries) {
        if (entry && entry.content_id === display.content_id) {
          const sha = extractSha256(entry);
          if (sha) {
            return sha;
          }
        }
      }
    }
    if (library && library.sets && typeof library.sets === "object" && display.source_rel) {
      for (const setId of Object.keys(library.sets)) {
        const setItems = getSetItems(library, setId);
        for (const item of setItems) {
          if (item && item.source_rel === display.source_rel) {
            const sha = extractSha256(item);
            if (sha) {
              return sha;
            }
          }
        }
      }
    }
    return null;
  }

  function showNowPlayingPlaceholder(label) {
    dom.nowPlayingImg.hidden = true;
    dom.nowPlayingImg.removeAttribute("src");
    dom.nowPlayingPlaceholder.hidden = false;
    dom.nowPlayingPlaceholder.textContent = label;
  }

  function renderNowPlaying() {
    const tv = state.tv;
    const library = state.library;

    if (!tv) {
      failedNowPlayingSrc = null;
      dom.activeSetLabel.textContent = "Unavailable";
      dom.nowPlayingTitle.textContent = "TV status unavailable";
      showNowPlayingPlaceholder("Offline");
      updateNowPlayingCountdown();
      return;
    }

    dom.activeSetLabel.textContent = formatSetName(tv.active_set);
    const display = tv.current_display;
    if (!display || !display.content_id) {
      failedNowPlayingSrc = null;
      dom.nowPlayingTitle.textContent = "Nothing displaying";
      showNowPlayingPlaceholder("None");
      updateNowPlayingCountdown();
      return;
    }

    if (display.is_art_store) {
      failedNowPlayingSrc = null;
      dom.nowPlayingTitle.textContent = `Art Store (${display.content_id})`;
      showNowPlayingPlaceholder("Art Store");
      updateNowPlayingCountdown();
      return;
    }

    dom.nowPlayingTitle.textContent = display.source_rel || display.content_id;
    const sha = resolveNowPlayingSha(display, tv, library);
    if (sha) {
      const targetSrc = `/thumbs/${sha}.jpg`;
      if (failedNowPlayingSrc !== null && failedNowPlayingSrc !== targetSrc) {
        failedNowPlayingSrc = null;
      }
      if (failedNowPlayingSrc === targetSrc) {
        showNowPlayingPlaceholder("NAS");
      } else {
        if (dom.nowPlayingImg.getAttribute("src") !== targetSrc) {
          dom.nowPlayingImg.src = targetSrc;
        }
        dom.nowPlayingImg.hidden = false;
        dom.nowPlayingPlaceholder.hidden = true;
      }
    } else {
      failedNowPlayingSrc = null;
      showNowPlayingPlaceholder("NAS");
    }
    updateNowPlayingCountdown();
  }

  function updateNowPlayingCountdown() {
    const tv = state.tv;
    if (!tv) {
      dom.nowPlayingCountdown.textContent = "TV status unavailable";
      return;
    }

    if (
      tv.preview &&
      typeof tv.preview === "object" &&
      tv.preview.active !== false &&
      tv.preview.expires_at
    ) {
      const expiresMs = Date.parse(tv.preview.expires_at);
      if (!Number.isNaN(expiresMs)) {
        const remainingMs = Math.max(0, expiresMs - Date.now());
        const secs = Math.ceil(remainingMs / 1000);
        dom.nowPlayingCountdown.textContent = `Preview: ${secs}s remaining`;
        return;
      }
    }
    if (tv.operation && tv.operation.kind === "preview") {
      dom.nowPlayingCountdown.textContent = tv.operation.message || "Preview active";
      return;
    }
    if (tv.cycling_paused) {
      dom.nowPlayingCountdown.textContent = "Cycling paused";
      return;
    }
    if (tv.next_rotation_at) {
      const parsed = Date.parse(tv.next_rotation_at);
      if (!Number.isNaN(parsed)) {
        const remainingSec = Math.max(0, (parsed - Date.now()) / 1000);
        dom.nowPlayingCountdown.textContent =
          remainingSec > 0
            ? `Next rotation in ${formatDurationSeconds(remainingSec)}`
            : "Rotation due now";
        return;
      }
    }
    dom.nowPlayingCountdown.textContent = "No active rotation";
  }

  function computeLibraryStats(library) {
    if (!library || typeof library !== "object") {
      return { valid: 0, skipped: 0, refreshedAt: null };
    }
    let valid = 0;
    let skipped = 0;
    const counts = library.counts;
    const stats = library.cache_stats || library.stats || library;
    if (
      counts &&
      typeof counts === "object" &&
      typeof counts.valid === "number" &&
      typeof counts.skipped === "number"
    ) {
      valid = counts.valid;
      skipped = counts.skipped;
    } else if (
      typeof stats.valid_count === "number" &&
      typeof stats.skipped_count === "number"
    ) {
      valid = stats.valid_count;
      skipped = stats.skipped_count;
    } else if (library.sets && typeof library.sets === "object") {
      for (const setId of Object.keys(library.sets)) {
        for (const item of getSetItems(library, setId)) {
          if (item && item.skip_reason) {
            skipped += 1;
          } else if (item) {
            valid += 1;
          }
        }
      }
    }
    const refreshedAt =
      library.last_refreshed_at ||
      library.last_refresh_at ||
      library.refreshed_at ||
      (library.last_result && library.last_result.timestamp) ||
      null;
    return { valid, skipped, refreshedAt };
  }

  function computeViewedSetSyncSummary() {
    const tv = state.tv;
    const library = state.library;
    if (!tv) {
      return "TV status unavailable";
    }
    if (!library || !library.sets || typeof library.sets !== "object") {
      return "Library cache unavailable";
    }
    const items = getSetItems(library, viewedSetId);
    const hasStale = items.some((item) => item && item.stale === true);
    const validItems = items.filter((item) => item && !item.skip_reason);
    if (tv.active_set !== viewedSetId) {
      if (hasStale) {
        return "Viewed set has uncached or stale changes (not active on TV)";
      }
      return validItems.length > 0
        ? "Viewed set has unsynced changes (not active on TV)"
        : "Not active on TV";
    }
    if (hasStale) {
      return "Viewed set has stale NAS changes";
    }
    const tvEntries = Array.isArray(tv.entries) ? tv.entries : [];
    const tvBySource = new Map();
    for (const entry of tvEntries) {
      if (entry && entry.source_rel) {
        tvBySource.set(entry.source_rel, entry);
      }
    }
    if (tvBySource.size !== validItems.length) {
      return "Viewed set has unsynced changes";
    }
    for (const item of validItems) {
      const entry = tvBySource.get(item.source_rel);
      if (!entry) {
        return "Viewed set has unsynced changes";
      }
      const itemSha = extractSha256(item);
      if (itemSha && entry.fingerprint && itemSha !== entry.fingerprint) {
        return "Viewed set has modified photos to sync";
      }
    }
    return "Viewed set is in sync with TV";
  }

  function renderLibrarySummary() {
    const library = state.library;
    if (!library) {
      dom.libraryCounts.textContent = "Library Cache offline";
      dom.libraryRefreshed.textContent = "Unavailable";
      dom.viewedSyncState.textContent = computeViewedSetSyncSummary();
      return;
    }
    const stats = computeLibraryStats(library);
    dom.libraryCounts.textContent = `${stats.valid} valid, ${stats.skipped} skipped`;
    dom.libraryRefreshed.textContent = `Last refresh: ${formatTimestamp(stats.refreshedAt)}`;
    dom.viewedSyncState.textContent = computeViewedSetSyncSummary();
  }

  function collectWarnings() {
    const items = [];
    const seen = new Set();
    const pushWarning = (raw) => {
      if (!raw) {
        return;
      }
      const text = String(raw);
      if (!seen.has(text)) {
        seen.add(text);
        items.push(text);
      }
    };

    const tv = state.tv;
    const library = state.library;

    const isAlreadyShownTvError = (text) =>
      Boolean(
        tv &&
          ((tv.manifest_error && text === String(tv.manifest_error)) ||
            (tv.last_error && text === String(tv.last_error)))
      );

    if (tv && Array.isArray(tv.cleanup_pending) && tv.cleanup_pending.length > 0) {
      pushWarning(
        `Cleanup pending for ${tv.cleanup_pending.length} app-managed TV item(s); retry with Sync to TV.`
      );
    }
    if (tv && tv.manifest_error) {
      pushWarning(`TV manifest error: ${tv.manifest_error}`);
    }
    if (tv && tv.last_error && tv.last_error !== tv.manifest_error) {
      pushWarning(`TV error: ${tv.last_error}`);
    }
    if (tv && Array.isArray(tv.warnings)) {
      for (const w of tv.warnings) {
        if (w) {
          const text = String(w);
          if (!isAlreadyShownTvError(text)) {
            pushWarning(text);
          }
        }
      }
    }
    if (tv && Array.isArray(tv.drift_warnings)) {
      for (const w of tv.drift_warnings) {
        if (w) {
          const text = String(w);
          if (!isAlreadyShownTvError(text)) {
            pushWarning(text);
          }
        }
      }
    }
    const libWarnings =
      library && Array.isArray(library.warnings)
        ? library.warnings.filter(Boolean).map(String)
        : [];
    for (const w of libWarnings) {
      pushWarning(w);
    }
    if (library && Array.isArray(library.scan_warnings)) {
      for (const w of library.scan_warnings) {
        pushWarning(w);
      }
    }

    const skippedSources = [];
    if (library && library.sets && typeof library.sets === "object") {
      for (const setId of Object.keys(library.sets)) {
        for (const item of getSetItems(library, setId)) {
          if (item && item.skip_reason) {
            skippedSources.push({
              sourceRel: item.source_rel || "",
              reason: String(item.skip_reason),
            });
          }
        }
      }
    }
    const hasSkippedWarning =
      libWarnings.some((w) => /\bskip(?:ped)?\b/i.test(w)) ||
      skippedSources.some((s) =>
        libWarnings.some(
          (w) =>
            (s.sourceRel && w.includes(s.sourceRel)) ||
            (s.reason && w.includes(s.reason))
        )
      );

    const stats = computeLibraryStats(library);
    if (stats.skipped > 0 && (libWarnings.length === 0 || !hasSkippedWarning)) {
      pushWarning(`${stats.skipped} image(s) skipped during library scan.`);
    }

    const maxWarn = state.max_set_images_warning || 50;
    const viewedItems = getSetItems(library, viewedSetId);
    const viewedValidCount = viewedItems.filter((item) => item && !item.skip_reason).length;
    if (viewedValidCount > maxWarn) {
      pushWarning(
        `Viewed set has ${viewedValidCount} valid images (warning threshold: ${maxWarn}).`
      );
    }
    return items;
  }

  function renderWarnings() {
    const warnings = collectWarnings();
    if (warnings.length === 0) {
      dom.warningsToggleBtn.hidden = true;
      dom.warningsDrawer.hidden = true;
      dom.warningsToggleBtn.setAttribute("aria-expanded", "false");
      dom.warningsList.replaceChildren();
      return;
    }

    dom.warningsToggleBtn.hidden = false;
    dom.warningsCount.textContent = String(warnings.length);
    const listNodes = warnings.map((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      return li;
    });
    dom.warningsList.replaceChildren(...listNodes);
  }

  function renderOperationBar() {
    const tvOp = state.tv && state.tv.operation ? state.tv.operation : null;
    const libOp = state.library && state.library.operation ? state.library.operation : null;
    const primary = tvOp || libOp;
    const secondary = tvOp && libOp ? libOp : null;

    if (!primary) {
      dom.operationBar.hidden = true;
      return;
    }

    dom.operationBar.hidden = false;
    const phaseLabel = primary.phase ? `${primary.kind} — ${primary.phase}` : primary.kind;
    dom.operationLabel.textContent = primary.message
      ? `${phaseLabel}: ${primary.message}`
      : phaseLabel;

    const completed = Number(primary.completed_ops || 0);
    const total = Number(primary.total_ops || 0);
    if (total > 0) {
      const pct = Math.min(100, Math.max(0, Math.round((completed / total) * 100)));
      dom.operationCounts.textContent = `${completed} / ${total} (${pct}%)`;
      dom.operationProgressFill.classList.remove("is-indeterminate");
      dom.operationProgressFill.style.width = `${pct}%`;
      dom.progressTrack.setAttribute("aria-valuenow", String(pct));
    } else {
      dom.operationCounts.textContent = "Preparing…";
      dom.operationProgressFill.style.width = "";
      dom.operationProgressFill.classList.add("is-indeterminate");
      dom.progressTrack.removeAttribute("aria-valuenow");
    }

    if (primary.current_item) {
      dom.operationItem.hidden = false;
      dom.operationItem.textContent = `Item: ${primary.current_item}`;
    } else {
      dom.operationItem.hidden = true;
      dom.operationItem.textContent = "";
    }

    if (secondary) {
      dom.operationSecondary.hidden = false;
      dom.operationSecondary.textContent = `Library: ${secondary.phase} — ${secondary.message}`;
    } else {
      dom.operationSecondary.hidden = true;
      dom.operationSecondary.textContent = "";
    }

    const abortRequested = Boolean(primary.abort_requested || (secondary && secondary.abort_requested));
    dom.abortBtn.disabled = abortRequested || busy.abort;
    dom.abortBtn.textContent = abortRequested || busy.abort ? "Aborting…" : "Abort";
  }

  function reconcileViewedSetId() {
    const ids = availableSetIds(state);
    if (!ids.includes(viewedSetId)) {
      viewedSetId = chooseInitialSetId(state);
      setDirty(false);
    }
    return ids;
  }

  function renderSetSelectorAndControls() {
    const ids = reconcileViewedSetId();

    const currentOptions = Array.from(dom.setSelect.options).map((opt) => opt.value);
    const needsRebuild =
      currentOptions.length !== ids.length ||
      currentOptions.some((val, index) => val !== ids[index]);
    if (needsRebuild) {
      const fragment = document.createDocumentFragment();
      for (const id of ids) {
        const option = document.createElement("option");
        option.value = id;
        const hasSet =
          state.library &&
          state.library.sets &&
          Object.prototype.hasOwnProperty.call(state.library.sets, id);
        const count = hasSet ? getSetItems(state.library, id).length : null;
        option.textContent =
          count !== null ? `${formatSetName(id)} (${count})` : formatSetName(id);
        fragment.appendChild(option);
      }
      dom.setSelect.replaceChildren(fragment);
    } else {
      for (const option of Array.from(dom.setSelect.options)) {
        const id = option.value;
        const hasSet =
          state.library &&
          state.library.sets &&
          Object.prototype.hasOwnProperty.call(state.library.sets, id);
        const count = hasSet ? getSetItems(state.library, id).length : null;
        option.textContent =
          count !== null ? `${formatSetName(id)} (${count})` : formatSetName(id);
      }
    }
    dom.setSelect.value = viewedSetId;

    if (!isDirty) {
      applyFormSettingsFromState();
    }

    const tvOk = Boolean(state.services && state.services.tv && state.services.tv.ok);
    const libOk = Boolean(state.services && state.services.library && state.services.library.ok);
    const tvOp = state.tv && state.tv.operation ? state.tv.operation : null;
    const libOp = state.library && state.library.operation ? state.library.operation : null;

    dom.checkTvBtn.disabled = !tvOk || Boolean(tvOp) || busy.check;
    dom.saveSettingsBtn.disabled =
      !tvOk || Boolean(tvOp && tvOp.kind === "sync") || busy.settings;
    dom.syncBtn.disabled = !tvOk || !libOk || Boolean(tvOp) || busy.sync;
    dom.refreshBtn.disabled = !libOk || Boolean(libOp) || busy.refresh;
  }

  function makePill(text, className) {
    const span = document.createElement("span");
    span.className = `pill ${className}`;
    span.textContent = text;
    return span;
  }

  function updateCardState(cardEl, item, entry, ctx) {
    const pillsEl = cardEl.querySelector(".card-pills");
    const skipEl = cardEl.querySelector(".card-skip-reason");
    const previewBtn = cardEl.querySelector(".btn-preview");
    const sha = extractSha256(item);
    const isSkipped = Boolean(item && item.skip_reason);

    let nextPillKey = "";
    let nextPills = [];
    let nextSkipText = "";
    let nextContentId = "";

    if (isSkipped) {
      nextPillKey = "skipped";
      nextPills = [makePill("Skipped", "pill-skipped")];
      nextSkipText = String(item.skip_reason);
    } else if (entry) {
      const modifiedOnNas =
        Boolean(item.stale) ||
        Boolean(sha && entry.fingerprint && sha !== entry.fingerprint);
      if (modifiedOnNas) {
        nextPillKey = "modified";
        nextPills = [makePill("New / Modified", "pill-modified")];
      } else {
        const isDisplaying = Boolean(
          ctx.currentDisplay &&
            !ctx.currentDisplay.is_art_store &&
            ctx.currentDisplay.content_id === entry.content_id
        );
        if (isDisplaying) {
          nextPillKey = "displaying";
          nextPills = [
            makePill("Displaying", "pill-displaying"),
            makePill("On TV", "pill-synced"),
          ];
        } else {
          nextPillKey = "synced";
          nextPills = [makePill("On TV", "pill-synced")];
        }
        if (entry.content_id) {
          nextContentId = entry.content_id;
        }
      }
    } else {
      nextPillKey = "modified";
      nextPills = [makePill("New / Modified", "pill-modified")];
    }

    if (pillsEl.dataset.pillKey !== nextPillKey) {
      pillsEl.dataset.pillKey = nextPillKey;
      pillsEl.replaceChildren(...nextPills);
    }

    if (nextSkipText) {
      skipEl.hidden = false;
      if (skipEl.textContent !== nextSkipText) {
        skipEl.textContent = nextSkipText;
      }
    } else {
      skipEl.hidden = true;
      if (skipEl.textContent !== "") {
        skipEl.textContent = "";
      }
    }

    if (nextContentId) {
      previewBtn.hidden = false;
      previewBtn.dataset.contentId = nextContentId;
      previewBtn.disabled =
        !ctx.tvOk || ctx.tvOpActive || busy.previewId !== null;
      previewBtn.textContent =
        busy.previewId === nextContentId
          ? "Starting…"
          : `Preview (${ctx.previewSeconds}s)`;
    } else {
      previewBtn.hidden = true;
      delete previewBtn.dataset.contentId;
    }
  }

  function renderGallery() {
    const tv = state.tv;
    const library = state.library;
    const tvOk = Boolean(state.services && state.services.tv && state.services.tv.ok);
    const libOk = Boolean(state.services && state.services.library && state.services.library.ok);
    const tvOpActive = Boolean(tv && tv.operation);
    const previewSeconds = state.preview_seconds || 15;

    dom.galleryHeading.textContent = `${formatSetName(viewedSetId)} images`;

    if (!libOk || !library || !library.sets || typeof library.sets !== "object") {
      lastGallerySignature = "";
      dom.gallerySubtitle.textContent = "Library Cache is offline.";
      dom.galleryEmpty.hidden = false;
      dom.galleryEmpty.textContent =
        "Library Cache service is offline. Start library_daemon to browse images.";
      dom.galleryGrid.replaceChildren();
      return;
    }

    const items = getSetItems(library, viewedSetId);
    dom.gallerySubtitle.textContent = `${items.length} image(s) in cached library`;

    if (items.length === 0) {
      lastGallerySignature = "";
      dom.galleryEmpty.hidden = false;
      dom.galleryEmpty.textContent = "No images found in this set. Click Refresh library after adding photos.";
      dom.galleryGrid.replaceChildren();
      return;
    }
    dom.galleryEmpty.hidden = true;

    const tvEntries =
      tv && tv.active_set === viewedSetId && Array.isArray(tv.entries) ? tv.entries : [];
    const currentDisplay = tv && tv.current_display ? tv.current_display : null;

    const entryBySource = new Map();
    for (const entry of tvEntries) {
      if (entry && entry.source_rel) {
        entryBySource.set(entry.source_rel, entry);
      }
    }

    const structureSignature = JSON.stringify({
      viewedSetId,
      items: items.map((item) => ({
        source_rel: item && item.source_rel ? item.source_rel : "",
        name_rel: item && item.name_rel ? item.name_rel : "",
        sha: extractSha256(item),
      })),
    });

    if (structureSignature !== lastGallerySignature) {
      lastGallerySignature = structureSignature;

      const fragment = document.createDocumentFragment();
      for (const item of items) {
        const cardFrag = dom.cardTemplate.content.cloneNode(true);
        const cardEl = cardFrag.querySelector(".card");
        const imgEl = cardFrag.querySelector(".card-img");
        const placeholderEl = cardFrag.querySelector(".thumb-placeholder");
        const filenameEl = cardFrag.querySelector(".filename");
        const previewBtn = cardFrag.querySelector(".btn-preview");

        const displayName = item.name_rel || item.source_rel || "Unknown image";
        filenameEl.textContent = displayName;
        filenameEl.title = displayName;

        const sha = extractSha256(item);
        if (sha) {
          imgEl.src = `/thumbs/${sha}.jpg`;
          imgEl.hidden = false;
          placeholderEl.hidden = true;
          imgEl.addEventListener("error", () => {
            imgEl.hidden = true;
            placeholderEl.hidden = false;
          });
        } else {
          imgEl.hidden = true;
          placeholderEl.hidden = false;
        }

        previewBtn.addEventListener("click", () => {
          const cid = previewBtn.dataset.contentId;
          if (cid) {
            requestPreview(cid);
          }
        });

        fragment.appendChild(cardEl);
      }

      dom.galleryGrid.replaceChildren(fragment);
    }

    const cardEls = dom.galleryGrid.children;
    const cardCtx = {
      currentDisplay,
      tvOk,
      tvOpActive,
      previewSeconds,
    };
    for (let i = 0; i < items.length && i < cardEls.length; i += 1) {
      const item = items[i];
      const entry = entryBySource.get(item.source_rel);
      updateCardState(cardEls[i], item, entry, cardCtx);
    }
  }

  function checkResultTransitions(nextState) {
    const tvKey = resultKey(nextState.tv && nextState.tv.last_result);
    if (tvKey && tvKey !== lastSeenTvResultKey) {
      lastSeenTvResultKey = tvKey;
      const res = nextState.tv.last_result;
      const toastKind =
        res.state === "completed"
          ? "success"
          : res.state === "aborted"
          ? "info"
          : "error";
      showToast(`TV ${res.kind} ${res.state}: ${res.message}`, toastKind);
    }

    const libKey = resultKey(nextState.library && nextState.library.last_result);
    if (libKey && libKey !== lastSeenLibraryResultKey) {
      lastSeenLibraryResultKey = libKey;
      const res = nextState.library.last_result;
      const toastKind =
        res.state === "completed"
          ? "success"
          : res.state === "aborted"
          ? "info"
          : "error";
      showToast(`Library ${res.kind} ${res.state}: ${res.message}`, toastKind);
    }
  }

  function applyState(nextState, { checkTransitions = true } = {}) {
    if (!nextState || typeof nextState !== "object") {
      return;
    }
    if (checkTransitions) {
      checkResultTransitions(nextState);
    }
    state = nextState;
    reconcileViewedSetId();
    renderHealth();
    renderTvStatus();
    renderNowPlaying();
    renderLibrarySummary();
    renderWarnings();
    renderOperationBar();
    renderSetSelectorAndControls();
    renderGallery();
  }

  async function apiPost(path, payload) {
    const response = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify(payload || {}),
    });

    let data = {};
    try {
      data = await response.json();
    } catch (_) {
      data = {};
    }

    if (!response.ok) {
      if (
        (response.status === 403 || response.status === 400) &&
        data &&
        data.code === "invalid_csrf"
      ) {
        showToast(
          "Session expired or server restarted (CSRF token invalid). Please reload the page.",
          "error",
          { showReload: true }
        );
      } else {
        const message =
          (data && data.error) || `Request failed with status ${response.status}`;
        showToast(message, "error");
      }
      return { ok: false, status: response.status, data };
    }

    return { ok: true, status: response.status, data };
  }

  async function refreshStatusOnce() {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      if (!response.ok) {
        return;
      }
      const nextState = await response.json();
      applyState(nextState);
    } catch (_) {
      // Keep retrying on fallback timer.
    }
  }

  async function requestCheckTv() {
    busy.check = true;
    renderSetSelectorAndControls();
    try {
      const res = await apiPost("/api/check-tv", {});
      if (res.ok) {
        if (res.data && res.data.tv_ready === false) {
          showToast(
            res.data.error || "TV is unavailable or not in Art Mode.",
            "error"
          );
        } else {
          showToast("TV is reachable and in Art Mode.", "success");
        }
        await refreshStatusOnce();
      }
    } finally {
      busy.check = false;
      renderSetSelectorAndControls();
    }
  }

  async function requestSaveSettings() {
    const parsed = readFormSettings();
    if (parsed.error) {
      showToast(parsed.error, "error");
      return;
    }
    busy.settings = true;
    renderSetSelectorAndControls();
    try {
      const res = await apiPost("/api/settings", {
        set_id: viewedSetId,
        ...parsed.value,
      });
      if (res.ok) {
        if (state.tv && state.tv.settings) {
          state.tv.settings[viewedSetId] = parsed.value;
        }
        setDirty(false);
        showToast(`Settings saved for ${formatSetName(viewedSetId)}.`, "success");
        await refreshStatusOnce();
      }
    } finally {
      busy.settings = false;
      renderSetSelectorAndControls();
    }
  }

  async function requestSync() {
    const parsed = readFormSettings();
    if (parsed.error) {
      showToast(parsed.error, "error");
      return;
    }
    busy.sync = true;
    renderSetSelectorAndControls();
    try {
      const res = await apiPost("/api/sync", {
        set_id: viewedSetId,
        settings: parsed.value,
      });
      if (res.ok) {
        if (state.tv && state.tv.settings) {
          state.tv.settings[viewedSetId] = parsed.value;
        }
        setDirty(false);
        showToast(`Sync started for ${formatSetName(viewedSetId)}.`, "info");
        await refreshStatusOnce();
      }
    } finally {
      busy.sync = false;
      renderSetSelectorAndControls();
    }
  }

  async function requestRefreshLibrary() {
    busy.refresh = true;
    renderSetSelectorAndControls();
    try {
      const res = await apiPost("/api/library/refresh", {});
      if (res.ok) {
        showToast("Library refresh started.", "info");
        await refreshStatusOnce();
      }
    } finally {
      busy.refresh = false;
      renderSetSelectorAndControls();
    }
  }

  async function requestAbort() {
    busy.abort = true;
    renderOperationBar();
    try {
      const res = await apiPost("/api/abort", { target: "auto" });
      if (res.ok) {
        showToast("Abort requested.", "info");
        await refreshStatusOnce();
      }
    } finally {
      busy.abort = false;
      renderOperationBar();
    }
  }

  async function requestPreview(contentId) {
    busy.previewId = contentId;
    renderGallery();
    try {
      const res = await apiPost("/api/preview", { content_id: contentId });
      if (res.ok) {
        showToast(`Preview started (${state.preview_seconds || 15}s).`, "info");
        await refreshStatusOnce();
      }
    } finally {
      busy.previewId = null;
      renderGallery();
    }
  }

  function startPollingFallback() {
    if (pollTimer !== null) {
      return;
    }
    setConnectionStatus("is-fallback", "Polling fallback");
    pollTimer = window.setInterval(refreshStatusOnce, 2000);
  }

  function stopPollingFallback() {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function connectEvents() {
    if (typeof window.EventSource !== "function") {
      startPollingFallback();
      return;
    }
    if (eventSource) {
      eventSource.close();
    }
    eventSource = new EventSource("/api/events");

    eventSource.addEventListener("open", () => {
      stopPollingFallback();
      setConnectionStatus("is-live", "Live");
    });

    eventSource.addEventListener("status", (event) => {
      stopPollingFallback();
      setConnectionStatus("is-live", "Live");
      try {
        const nextState = JSON.parse(event.data);
        applyState(nextState);
      } catch (_) {
        // Ignore malformed event payload.
      }
    });

    eventSource.addEventListener("error", () => {
      startPollingFallback();
      if (eventSource && eventSource.readyState === EventSource.CLOSED) {
        setConnectionStatus("is-offline", "Reconnecting…");
        if (reconnectTimer === null) {
          reconnectTimer = window.setTimeout(() => {
            reconnectTimer = null;
            connectEvents();
          }, 3000);
        }
      }
    });
  }

  dom.nowPlayingImg.addEventListener("error", () => {
    failedNowPlayingSrc = dom.nowPlayingImg.getAttribute("src") || null;
    showNowPlayingPlaceholder("NAS");
  });

  dom.warningsToggleBtn.addEventListener("click", () => {
    const nextHidden = !dom.warningsDrawer.hidden;
    dom.warningsDrawer.hidden = nextHidden;
    dom.warningsToggleBtn.setAttribute("aria-expanded", String(!nextHidden));
  });

  dom.warningsCloseBtn.addEventListener("click", () => {
    dom.warningsDrawer.hidden = true;
    dom.warningsToggleBtn.setAttribute("aria-expanded", "false");
  });

  dom.setSelect.addEventListener("change", () => {
    viewedSetId = dom.setSelect.value || "root";
    setDirty(false);
    applyFormSettingsFromState();
    renderLibrarySummary();
    renderWarnings();
    renderSetSelectorAndControls();
    renderGallery();
  });

  for (const inputEl of [dom.settingRandom, dom.settingFavorites, dom.settingInterval]) {
    inputEl.addEventListener("input", () => setDirty(true));
    inputEl.addEventListener("change", () => setDirty(true));
  }

  dom.checkTvBtn.addEventListener("click", requestCheckTv);
  dom.saveSettingsBtn.addEventListener("click", requestSaveSettings);
  dom.syncBtn.addEventListener("click", requestSync);
  dom.refreshBtn.addEventListener("click", requestRefreshLibrary);
  dom.abortBtn.addEventListener("click", requestAbort);

  applyState(state, { checkTransitions: false });
  connectEvents();
  window.setInterval(updateNowPlayingCountdown, 1000);
})();
