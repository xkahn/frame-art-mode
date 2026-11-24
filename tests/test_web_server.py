import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from frame_art_mode.config import Config
from frame_art_mode.ipc import (
    HTTP_CONFLICT,
    HTTP_INTERNAL_ERROR,
    HTTP_NOT_FOUND,
    HTTP_OK,
    UnixHTTPServer,
    make_error_envelope,
)
from frame_art_mode.models import ensure_private_dir
from frame_art_mode.web_server import create_app, main


class FakeDaemonState:
    def __init__(self, status_payload: dict) -> None:
        self.lock = threading.Lock()
        self.status_payload = status_payload
        self.requests: list[tuple[str, str, dict]] = []
        self.next_post_responses: dict[str, tuple[int, dict]] = {}
        self.status_http_code = HTTP_OK

    def get_status(self, body: dict) -> tuple[int, dict]:
        with self.lock:
            self.requests.append(("GET", "/status", body))
            if self.status_http_code != HTTP_OK:
                return self.status_http_code, make_error_envelope("daemon error", "daemon_error")
            return HTTP_OK, json.loads(json.dumps(self.status_payload))

    def handle_post(self, path: str, body: dict, default_response: tuple[int, dict]) -> tuple[int, dict]:
        with self.lock:
            self.requests.append(("POST", path, body))
            if path in self.next_post_responses:
                return self.next_post_responses.pop(path)
            return default_response


class WebServerBoundaryAndStaticTests(unittest.TestCase):
    def test_web_server_imports_respect_hard_boundaries(self):
        path = Path(__file__).resolve().parents[1] / "frame_art_mode" / "web_server.py"
        source = path.read_text(encoding="utf-8")
        self.assertTrue(source.startswith("from __future__ import annotations"))

        tree = ast.parse(source, filename=str(path))
        forbidden_modules = {
            "samsungframetv",
            "PIL",
            "Pillow",
            "tv_daemon",
            "library_daemon",
            "images",
            "service",
        }
        allowed_relative = {"config", "models", "ipc"}

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    self.assertNotIn(top, forbidden_modules)
                    self.assertTrue(
                        top in sys.stdlib_module_names or top == "flask",
                        f"Unexpected import {alias.name}",
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    mod = node.module or ""
                    self.assertIn(mod, allowed_relative, f"Forbidden relative import .{mod}")
                elif node.module:
                    top = node.module.split(".")[0]
                    self.assertNotIn(top, forbidden_modules)
                    if top == "frame_art_mode":
                        parts = node.module.split(".")
                        self.assertGreaterEqual(len(parts), 2)
                        self.assertIn(parts[1], allowed_relative)
                    else:
                        self.assertTrue(
                            top in sys.stdlib_module_names or top == "flask",
                            f"Unexpected import from {node.module}",
                        )

    def test_static_and_template_assets_meet_security_and_compatibility_markers(self):
        pkg = Path(__file__).resolve().parents[1] / "frame_art_mode"
        html = (pkg / "templates" / "index.html").read_text(encoding="utf-8")
        css = (pkg / "static" / "app.css").read_text(encoding="utf-8")
        js = (pkg / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("Frame Art Mode", html)
        self.assertIn('<meta name="csrf-token" content="{{ csrf_token }}">', html)
        self.assertIn(
            '<script id="initial-state" type="application/json">{{ initial_state | tojson }}</script>',
            html,
        )
        for forbidden_url in ("http://", "https://", "//cdn"):
            self.assertNotIn(forbidden_url, html)
            self.assertNotIn(forbidden_url, css)
            self.assertNotIn(forbidden_url, js)

        for dangerous_sink in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "new Function(",
        ):
            self.assertNotIn(dangerous_sink, html)
            self.assertNotIn(dangerous_sink, js)

        # Reject inline on* event handlers in index.html
        self.assertIsNone(re.search(r"\bon[a-z]+\s*=", html, re.IGNORECASE))
        # Ensure only the two expected <script> tags exist in index.html (JSON data + external defer)
        script_tags = re.findall(r"<script\b[^>]*>", html, re.IGNORECASE)
        self.assertEqual(len(script_tags), 2)
        self.assertIn('type="application/json"', script_tags[0])
        self.assertIn("defer", script_tags[1])

        self.assertIn("X-CSRF-Token", js)
        self.assertIn("403", js)
        self.assertIn("textContent", js)
        self.assertIn("EventSource", js)
        self.assertIn("updateCardState", js)
        self.assertIn("hasSkippedWarning", js)

        self.assertIn(".card { min-width: 0; }", css)
        self.assertIn(".filename { overflow-wrap: anywhere; }", css)


class WebServerFrontendBehaviorTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend behavior tests")
    def test_app_js_reconciles_viewed_set_and_deduplicates_warnings(self):
        node_bin = shutil.which("node")
        assert node_bin is not None
        app_js_path = (
            Path(__file__).resolve().parents[1] / "frame_art_mode" / "static" / "app.js"
        )
        app_js_source = app_js_path.read_text(encoding="utf-8")

        harness = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const appCode = fs.readFileSync(process.env.APP_JS_PATH, "utf8");

function makeClassList(el) {
  const tokens = new Set();
  return {
    add(...names) {
      for (const n of names) {
        if (n) tokens.add(n);
      }
      el._syncClassName(tokens);
    },
    remove(...names) {
      for (const n of names) tokens.delete(n);
      el._syncClassName(tokens);
    },
    toggle(name, force) {
      if (force === undefined) {
        if (tokens.has(name)) tokens.delete(name);
        else tokens.add(name);
      } else if (force) {
        tokens.add(name);
      } else {
        tokens.delete(name);
      }
      el._syncClassName(tokens);
      return tokens.has(name);
    },
    contains(name) {
      return tokens.has(name);
    },
    _replaceFromString(str) {
      tokens.clear();
      for (const part of String(str || "").split(/\s+/)) {
        if (part) tokens.add(part);
      }
    },
  };
}

function makeElement(tagName, id = "") {
  const el = {
    tagName: tagName.toUpperCase(),
    id,
    _className: "",
    children: [],
    parentNode: null,
    attributes: {},
    dataset: {},
    style: {},
    listeners: {},
    hidden: false,
    disabled: false,
    value: "",
    checked: false,
    textContent: "",
    title: "",
    src: "",
    _syncClassName(tokens) {
      this._className = Array.from(tokens).join(" ");
    },
    get className() {
      return this._className;
    },
    set className(val) {
      this._className = String(val || "");
      this.classList._replaceFromString(this._className);
    },
    get options() {
      return this.children.filter((c) => c.tagName === "OPTION");
    },
    setAttribute(k, v) {
      this.attributes[k] = String(v);
    },
    getAttribute(k) {
      if (k === "src") return this.src || null;
      return Object.prototype.hasOwnProperty.call(this.attributes, k)
        ? this.attributes[k]
        : null;
    },
    removeAttribute(k) {
      if (k === "src") this.src = "";
      delete this.attributes[k];
    },
    addEventListener(type, fn) {
      if (!this.listeners[type]) this.listeners[type] = [];
      this.listeners[type].push(fn);
    },
    dispatchEvent(type, evt = {}) {
      for (const fn of this.listeners[type] || []) {
        fn(evt);
      }
    },
    appendChild(child) {
      if (child.tagName === "#FRAGMENT") {
        for (const grand of child.children.slice()) {
          this.appendChild(grand);
        }
        child.children = [];
        return child;
      }
      child.parentNode = this;
      this.children.push(child);
      return child;
    },
    removeChild(child) {
      this.children = this.children.filter((c) => c !== child);
      child.parentNode = null;
      return child;
    },
    replaceChildren(...nodes) {
      this.children = [];
      for (const node of nodes) {
        this.appendChild(node);
      }
    },
    querySelector(selector) {
      if (selector.startsWith(".")) {
        const cls = selector.slice(1);
        const queue = [...this.children];
        while (queue.length > 0) {
          const cur = queue.shift();
          if (cur.classList.contains(cls)) return cur;
          queue.push(...cur.children);
        }
      }
      return null;
    },
  };
  el.classList = makeClassList(el);
  return el;
}

function makeFragment() {
  return {
    tagName: "#FRAGMENT",
    children: [],
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    querySelector(selector) {
      const cls = selector.startsWith(".") ? selector.slice(1) : "";
      const queue = [...this.children];
      while (queue.length > 0) {
        const cur = queue.shift();
        if (cls && cur.classList.contains(cls)) return cur;
        queue.push(...cur.children);
      }
      return null;
    },
  };
}

function buildCardTemplate() {
  return {
    content: {
      cloneNode() {
        const frag = makeFragment();
        const card = makeElement("article");
        card.className = "card";
        const img = makeElement("img");
        img.className = "card-img";
        const ph = makeElement("div");
        ph.className = "thumb-placeholder";
        const pills = makeElement("div");
        pills.className = "card-pills";
        const filename = makeElement("div");
        filename.className = "filename";
        const skip = makeElement("div");
        skip.className = "card-skip-reason";
        const btn = makeElement("button");
        btn.className = "btn-preview";
        card.appendChild(img);
        card.appendChild(ph);
        card.appendChild(pills);
        card.appendChild(filename);
        card.appendChild(skip);
        card.appendChild(btn);
        frag.appendChild(card);
        return frag;
      },
    },
  };
}

function buildToastTemplate() {
  return {
    content: {
      cloneNode() {
        const frag = makeFragment();
        const toast = makeElement("div");
        toast.className = "toast";
        const msg = makeElement("span");
        msg.className = "toast-message";
        const reload = makeElement("button");
        reload.className = "toast-reload";
        const dismiss = makeElement("button");
        dismiss.className = "toast-dismiss";
        toast.appendChild(msg);
        toast.appendChild(reload);
        toast.appendChild(dismiss);
        frag.appendChild(toast);
        return frag;
      },
    },
  };
}

function createRuntime(initialState) {
  const byId = new Map();
  const ids = [
    "live-connection", "warnings-toggle-btn", "warnings-count", "warnings-drawer",
    "warnings-close-btn", "warnings-list", "dot-web", "status-web", "dot-tv",
    "status-tv", "error-tv", "dot-library", "status-library", "error-library",
    "tv-ready-badge", "tv-status-detail", "check-tv-btn", "now-playing-img",
    "now-playing-placeholder", "active-set-label", "now-playing-title",
    "now-playing-countdown", "library-counts", "library-refreshed",
    "viewed-sync-state", "operation-bar", "operation-label", "operation-counts",
    "operation-item", "operation-secondary", "operation-progress-fill",
    "abort-btn", "set-select", "setting-random", "setting-favorites",
    "setting-interval", "save-settings-btn", "sync-btn", "refresh-btn",
    "gallery-heading", "gallery-subtitle", "gallery-empty", "gallery-grid",
    "toast-container",
  ];
  for (const id of ids) {
    byId.set(id, makeElement("div", id));
  }
  byId.set("set-select", makeElement("select", "set-select"));
  byId.set("card-template", buildCardTemplate());
  byId.set("toast-template", buildToastTemplate());

  const initialEl = makeElement("script", "initial-state");
  initialEl.textContent = JSON.stringify(initialState);
  byId.set("initial-state", initialEl);

  const csrfMeta = makeElement("meta");
  csrfMeta.setAttribute("content", "test-csrf");
  const progressTrack = makeElement("div");

  let latestEventSource = null;
  class MockEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.listeners = {};
      latestEventSource = this;
    }
    addEventListener(type, fn) {
      if (!this.listeners[type]) this.listeners[type] = [];
      this.listeners[type].push(fn);
    }
    emit(type, evt) {
      for (const fn of this.listeners[type] || []) fn(evt);
    }
    close() {
      this.readyState = 2;
    }
  }
  MockEventSource.CLOSED = 2;

  const sandbox = {
    document: {
      getElementById: (id) => byId.get(id) || null,
      querySelector: (sel) => {
        if (sel === 'meta[name="csrf-token"]') return csrfMeta;
        if (sel === ".progress-track") return progressTrack;
        return null;
      },
      createElement: (tag) => makeElement(tag),
      createDocumentFragment: () => makeFragment(),
    },
    window: {
      EventSource: MockEventSource,
      setInterval: () => 1,
      clearInterval: () => {},
      setTimeout: () => 1,
      location: { reload: () => {} },
    },
    EventSource: MockEventSource,
    fetch: async () => ({ ok: false, status: 503, json: async () => ({}) }),
    Number,
    String,
    Boolean,
    Array,
    Object,
    Map,
    Set,
    JSON,
    Math,
    Date,
    RegExp,
  };
  vm.runInNewContext(appCode, sandbox);
  return {
    byId,
    pushStatus(nextState) {
      latestEventSource.emit("status", { data: JSON.stringify(nextState) });
    },
    warningTexts() {
      return byId.get("warnings-list").children.map((li) => li.textContent);
    },
  };
}

function makeImages(prefix, count) {
  const out = [];
  for (let i = 0; i < count; i += 1) {
    const sha = String(i).padStart(64, "a");
    out.push({
      source_rel: `${prefix}/img_${i}.jpg`,
      name_rel: `img_${i}.jpg`,
      filename: `img_${i}.jpg`,
      sha256: sha,
      skip_reason: null,
      stale: false,
    });
  }
  return out;
}

// 1. Degraded startup ("root") -> SSE brings online only occasion:Winter (55 images)
const rt = createRuntime({
  services: { web: { ok: true }, tv: { ok: false, error: "offline" }, library: { ok: false, error: "offline" } },
  degraded: true,
  tv: null,
  library: null,
  preview_seconds: 15,
  max_set_images_warning: 50,
});
assert.equal(rt.byId.get("set-select").value, "root");
assert.equal(rt.byId.get("viewed-sync-state").textContent, "TV status unavailable");

const winterImages = makeImages("Winter", 55);
rt.pushStatus({
  services: { web: { ok: true }, tv: { ok: true, error: null }, library: { ok: true, error: null } },
  degraded: false,
  tv: {
    tv_ready: true,
    active_set: "occasion:Winter",
    entries: winterImages.slice(0, 54).map((im, idx) => ({
      source_rel: im.source_rel,
      filename: im.filename,
      fingerprint: im.sha256,
      content_id: `MY_F${idx}`,
    })),
    current_display: { content_id: "MY_F0", source_rel: winterImages[0].source_rel, is_art_store: false },
    settings: { "occasion:Winter": { random: false, mix_art_store_favorites: true, interval_minutes: 30 } },
    cleanup_pending: [],
    warnings: [],
    drift_warnings: [],
  },
  library: {
    last_refreshed_at: "2026-11-24T05:00:00+00:00",
    sets: {
      "occasion:Winter": { label: "Winter", valid_count: 55, skipped_count: 0, images: winterImages },
    },
    counts: { sets: 1, valid: 55, skipped: 0 },
    warnings: [],
    scan_warnings: [],
  },
  preview_seconds: 15,
  max_set_images_warning: 50,
});

assert.equal(rt.byId.get("set-select").value, "occasion:Winter");
assert.equal(rt.byId.get("viewed-sync-state").textContent, "Viewed set has unsynced changes");
assert.deepEqual(rt.warningTexts(), [
  "Viewed set has 55 valid images (warning threshold: 50).",
]);
assert.equal(rt.byId.get("setting-interval").value, "30");

// Mark dirty, then remove occasion:Winter in favor of occasion:Spring (not active on TV)
rt.byId.get("setting-interval").value = "99";
rt.byId.get("setting-interval").dispatchEvent("input");
assert.ok(rt.byId.get("save-settings-btn").classList.contains("is-dirty"));

const springImages = makeImages("Spring", 2);
rt.pushStatus({
  services: { web: { ok: true }, tv: { ok: true, error: null }, library: { ok: true, error: null } },
  degraded: false,
  tv: {
    tv_ready: true,
    active_set: "occasion:Winter",
    entries: [],
    current_display: null,
    settings: { "occasion:Spring": { random: true, mix_art_store_favorites: false, interval_minutes: 12 } },
    cleanup_pending: [],
    warnings: [],
    drift_warnings: [],
  },
  library: {
    last_refreshed_at: "2026-11-24T05:05:00+00:00",
    sets: {
      "occasion:Spring": { label: "Spring", valid_count: 2, skipped_count: 0, images: springImages },
    },
    counts: { sets: 1, valid: 2, skipped: 0 },
    warnings: [],
    scan_warnings: [],
  },
  preview_seconds: 15,
  max_set_images_warning: 50,
});

assert.equal(rt.byId.get("set-select").value, "occasion:Spring");
assert.equal(
  rt.byId.get("viewed-sync-state").textContent,
  "Viewed set has unsynced changes (not active on TV)"
);
assert.deepEqual(rt.warningTexts(), []);
assert.equal(rt.byId.get("save-settings-btn").classList.contains("is-dirty"), false);
assert.equal(rt.byId.get("setting-interval").value, "12");

// Mark dirty while occasion:Spring stays present -> dirty edits must be preserved on SSE
rt.byId.get("setting-interval").value = "42";
rt.byId.get("setting-interval").dispatchEvent("input");
rt.pushStatus({
  services: { web: { ok: true }, tv: { ok: true, error: null }, library: { ok: true, error: null } },
  degraded: false,
  tv: {
    tv_ready: true,
    active_set: "occasion:Spring",
    entries: springImages.map((im, idx) => ({
      source_rel: im.source_rel,
      filename: im.filename,
      fingerprint: im.sha256,
      content_id: `MY_S${idx}`,
    })),
    current_display: null,
    settings: { "occasion:Spring": { random: true, mix_art_store_favorites: false, interval_minutes: 12 } },
    cleanup_pending: [],
    warnings: [],
    drift_warnings: [],
  },
  library: {
    last_refreshed_at: "2026-11-24T05:06:00+00:00",
    sets: {
      "occasion:Spring": { label: "Spring", valid_count: 2, skipped_count: 0, images: springImages },
    },
    counts: { sets: 1, valid: 2, skipped: 0 },
    warnings: [],
    scan_warnings: [],
  },
  preview_seconds: 15,
  max_set_images_warning: 50,
});
assert.equal(rt.byId.get("viewed-sync-state").textContent, "Viewed set is in sync with TV");
assert.equal(rt.byId.get("save-settings-btn").classList.contains("is-dirty"), true);
assert.equal(rt.byId.get("setting-interval").value, "42");

// 2. Deduplicate warnings across tv/library while preserving order, errors, and skipped fallback
rt.pushStatus({
  services: { web: { ok: true }, tv: { ok: true, error: null }, library: { ok: true, error: null } },
  degraded: false,
  tv: {
    tv_ready: true,
    active_set: "occasion:Spring",
    entries: [],
    manifest_error: "manifest bad",
    last_error: "delete failed",
    cleanup_pending: [],
    warnings: ["delete failed", "Prepare warning A", "Shared warning", "Prepare warning A"],
    drift_warnings: ["Shared warning", "Drift warning B"],
  },
  library: {
    last_refreshed_at: "2026-11-24T05:07:00+00:00",
    sets: {
      "occasion:Spring": {
        label: "Spring",
        valid_count: 1,
        skipped_count: 1,
        images: [
          springImages[0],
          { source_rel: "bad.jpg", name_rel: "bad.jpg", sha256: null, skip_reason: "corrupt image" },
        ],
      },
    },
    counts: { sets: 1, valid: 1, skipped: 1 },
    warnings: ["Shared warning", "bad.jpg: corrupt image", "Prepare warning A"],
    scan_warnings: ["bad.jpg: corrupt image", "Scan note C"],
  },
  preview_seconds: 15,
  max_set_images_warning: 50,
});
assert.deepEqual(rt.warningTexts(), [
  "TV manifest error: manifest bad",
  "TV error: delete failed",
  "Prepare warning A",
  "Shared warning",
  "Drift warning B",
  "bad.jpg: corrupt image",
  "Scan note C",
]);

// Fallback skipped-image warning appears when library.warnings has no skipped entry
rt.pushStatus({
  services: { web: { ok: true }, tv: { ok: true, error: null }, library: { ok: true, error: null } },
  degraded: false,
  tv: { tv_ready: true, active_set: "occasion:Spring", entries: [], cleanup_pending: [], warnings: [], drift_warnings: [] },
  library: {
    last_refreshed_at: "2026-11-24T05:08:00+00:00",
    sets: {
      "occasion:Spring": {
        label: "Spring",
        valid_count: 1,
        skipped_count: 1,
        images: [
          springImages[0],
          { source_rel: "bad.jpg", name_rel: "bad.jpg", sha256: null, skip_reason: "corrupt image" },
        ],
      },
    },
    counts: { sets: 1, valid: 1, skipped: 1 },
    warnings: [],
    scan_warnings: [],
  },
  preview_seconds: 15,
  max_set_images_warning: 50,
});
assert.deepEqual(rt.warningTexts(), ["1 image(s) skipped during library scan."]);
"""
        proc = subprocess.run(
            [node_bin, "-"],
            input=harness,
            text=True,
            capture_output=True,
            env={**os.environ, "APP_JS_PATH": str(app_js_path)},
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node frontend behavior harness failed:\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        # Keep app_js_source referenced so static sanity is explicit
        self.assertIn("reconcileViewedSetId", app_js_source)


class WebServerRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.image_root = base / "images"
        self.image_root.mkdir()
        self.state_dir = base / "state"
        self.config = Config(
            script_dir=base,
            image_root=self.image_root,
            state_dir=self.state_dir,
            tv_ip="192.168.1.50",
            bind_ip="192.168.1.20",
            port=8080,
            preview_seconds=15,
            max_set_images_warning=50,
        )
        self.sample_sha = hashlib.sha256(b"sample-jpeg").hexdigest()
        self.tv_state = FakeDaemonState(
            {
                "tv_ready": None,
                "tv_checked_at": None,
                "active_set": "root",
                "entries": [
                    {
                        "source_rel": "sunset.jpg",
                        "filename": "sunset.jpg",
                        "fingerprint": self.sample_sha,
                        "content_id": "MY_F0001",
                    }
                ],
                "current_display": {
                    "content_id": "MY_F0001",
                    "category": None,
                    "source_rel": "sunset.jpg",
                    "is_art_store": False,
                },
                "next_rotation_at": "2026-11-24T06:00:00+00:00",
                "cycling_paused": False,
                "preview": {
                    "active": False,
                    "content_id": None,
                    "expires_at": None,
                },
                "cleanup_pending": [],
                "settings": {
                    "root": {
                        "random": True,
                        "mix_art_store_favorites": False,
                        "interval_minutes": 15,
                    }
                },
                "operation": None,
                "last_result": None,
                "warnings": [],
                "drift_warnings": [],
                "manifest_error": None,
                "last_error": None,
            }
        )
        self.library_state = FakeDaemonState(
            {
                "last_refreshed_at": "2026-11-24T05:00:00+00:00",
                "sets": {
                    "root": {
                        "label": "Default",
                        "valid_count": 1,
                        "skipped_count": 0,
                        "images": [
                            {
                                "source_rel": "sunset.jpg",
                                "name_rel": "sunset.jpg",
                                "filename": "sunset.jpg",
                                "sha256": self.sample_sha,
                                "skip_reason": None,
                                "byte_size": 102400,
                                "stale": False,
                            }
                        ],
                    }
                },
                "counts": {
                    "sets": 1,
                    "valid": 1,
                    "skipped": 0,
                    "cached_tv_files": 1,
                    "cached_thumb_files": 1,
                },
                "warnings": [],
                "scan_warnings": [],
                "active_leases": [],
                "operation": None,
                "last_result": None,
            }
        )
        self._servers: list[tuple[UnixHTTPServer, threading.Thread]] = []

    def tearDown(self):
        self._stop_servers()
        self.temp.cleanup()

    def _start_tv_server(self) -> UnixHTTPServer:
        server = UnixHTTPServer(
            self.config.tv_sock,
            routes={
                ("GET", "/status"): self.tv_state.get_status,
                ("POST", "/check"): lambda body: self.tv_state.handle_post(
                    "/check",
                    body,
                    (
                        HTTP_OK,
                        {
                            "ok": True,
                            "tv_ready": True,
                            "current_display": self.tv_state.status_payload["current_display"],
                            "error": None,
                        },
                    ),
                ),
                ("POST", "/settings"): lambda body: self.tv_state.handle_post(
                    "/settings",
                    body,
                    (HTTP_OK, {"ok": True, "set_id": body.get("set_id"), "settings": body}),
                ),
                ("POST", "/sync"): lambda body: self.tv_state.handle_post(
                    "/sync", body, (202, {"ok": True, "accepted": True, "set_id": body.get("set_id")})
                ),
                ("POST", "/preview"): lambda body: self.tv_state.handle_post(
                    "/preview",
                    body,
                    (
                        202,
                        {
                            "ok": True,
                            "preview": {
                                "active": True,
                                "content_id": body.get("content_id"),
                                "expires_at": None,
                            },
                        },
                    ),
                ),
                ("POST", "/abort"): lambda body: self.tv_state.handle_post(
                    "/abort", body, (HTTP_OK, {"ok": True, "aborted": True})
                ),
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._servers.append((server, thread))
        return server

    def _start_library_server(self) -> UnixHTTPServer:
        server = UnixHTTPServer(
            self.config.library_sock,
            routes={
                ("GET", "/status"): self.library_state.get_status,
                ("POST", "/refresh"): lambda body: self.library_state.handle_post(
                    "/refresh", body, (202, {"ok": True, "accepted": True})
                ),
                ("POST", "/abort"): lambda body: self.library_state.handle_post(
                    "/abort", body, (HTTP_OK, {"ok": True, "aborted": True})
                ),
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._servers.append((server, thread))
        return server

    def _stop_servers(self) -> None:
        while self._servers:
            server, thread = self._servers.pop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def _get_csrf_client(self, **app_kwargs):
        app = create_app(self.config, **app_kwargs)
        app.testing = True
        client = app.test_client()
        with client.get("/") as resp:
            self.assertEqual(resp.status_code, 200)
            body = resp.data
            cache_control = resp.headers.get("Cache-Control")
        with client.session_transaction() as sess:
            csrf = sess["csrf_token"]
        return app, client, csrf, body, cache_control

    def test_index_renders_initial_state_cookie_flags_and_zero_idle_tv_polling(self):
        self._start_tv_server()
        self._start_library_server()

        app, client, csrf, body, cache_control = self._get_csrf_client()
        self.assertIn(b"Frame Art Mode", body)
        self.assertIn(csrf.encode("utf-8"), body)
        self.assertEqual(cache_control, "no-store")
        self.assertEqual(app.config["MAX_CONTENT_LENGTH"], 1024 * 1024)
        self.assertFalse(app.config.get("SESSION_COOKIE_SECURE", False))
        self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")

        with client.get("/api/status") as status_resp:
            self.assertEqual(status_resp.status_code, 200)
            self.assertEqual(status_resp.headers.get("Cache-Control"), "no-store")
            self.assertFalse(status_resp.get_json()["degraded"])

        # Zero POST /check calls during GET / or GET /api/status
        self.assertFalse(any(req[:2] == ("POST", "/check") for req in self.tv_state.requests))

    def test_index_renders_cleanly_in_degraded_mode(self):
        app = create_app(self.config, ipc_timeout=0.5)
        app.testing = True
        client = app.test_client()

        with client.get("/") as both_down:
            self.assertEqual(both_down.status_code, 200)
            self.assertEqual(both_down.headers.get("Cache-Control"), "no-store")
            self.assertIn(b'"degraded":true', both_down.data.replace(b" ", b""))

        self._start_library_server()
        with client.get("/") as tv_down:
            self.assertEqual(tv_down.status_code, 200)
            self.assertIn(b'"degraded":true', tv_down.data.replace(b" ", b""))

    def test_csrf_enforcement_and_api_json_error_envelopes(self):
        self._start_tv_server()
        self._start_library_server()

        # Fresh client before GET / has no CSRF session token -> 403 invalid_csrf
        fresh_app = create_app(self.config)
        fresh_app.testing = True
        fresh_client = fresh_app.test_client()
        for endpoint in (
            "/api/check-tv",
            "/api/settings",
            "/api/sync",
            "/api/preview",
            "/api/library/refresh",
            "/api/abort",
        ):
            with fresh_client.post(endpoint, json={}) as resp:
                self.assertEqual(resp.status_code, 403, endpoint)
                self.assertEqual(resp.get_json()["code"], "invalid_csrf")

        _, client, csrf, _, _ = self._get_csrf_client()

        for endpoint in (
            "/api/check-tv",
            "/api/settings",
            "/api/sync",
            "/api/preview",
            "/api/library/refresh",
            "/api/abort",
        ):
            with client.post(endpoint, json={}) as missing:
                self.assertEqual(missing.status_code, 403, endpoint)
                self.assertEqual(missing.get_json()["code"], "invalid_csrf")

            with client.post(
                endpoint,
                json={},
                headers={"X-CSRF-Token": "wrong-token"},
            ) as wrong:
                self.assertEqual(wrong.status_code, 403, endpoint)
                self.assertEqual(wrong.get_json()["code"], "invalid_csrf")

        # Invalid JSON and non-dict JSON
        with client.post(
            "/api/check-tv",
            data=b"{broken",
            content_type="application/json",
            headers={"X-CSRF-Token": csrf},
        ) as bad_json:
            self.assertEqual(bad_json.status_code, 400)
            self.assertEqual(bad_json.get_json()["code"], "invalid_json")

        with client.post(
            "/api/check-tv",
            data=b"[1, 2]",
            content_type="application/json",
            headers={"X-CSRF-Token": csrf},
        ) as non_dict:
            self.assertEqual(non_dict.status_code, 400)
            self.assertEqual(non_dict.get_json()["code"], "invalid_request")

        # Payload > MAX_CONTENT_LENGTH (1 MB) -> 413 JSON envelope
        oversized = b'{"x":"' + (b"a" * (1024 * 1024 + 64)) + b'"}'
        with client.post(
            "/api/check-tv",
            data=oversized,
            content_type="application/json",
            headers={"X-CSRF-Token": csrf},
        ) as too_large:
            self.assertEqual(too_large.status_code, 413)
            self.assertEqual(too_large.get_json()["code"], "payload_too_large")

        # Settings validation (including unknown keys, whitespace set_id, invalid types)
        for bad_settings in (
            {},
            {"set_id": ""},
            {"set_id": "   "},
            {"set_id": "root", "unknown_field": True},
            {"set_id": "root", "interval_minutes": 0},
            {"set_id": "root", "interval_minutes": True},
            {"set_id": "root", "random": "yes"},
            {"set_id": "root", "random": None},
            {"set_id": "root", "mix_art_store_favorites": "false"},
        ):
            with client.post(
                "/api/settings",
                json=bad_settings,
                headers={"X-CSRF-Token": csrf},
            ) as resp:
                self.assertEqual(resp.status_code, 400, bad_settings)
                self.assertEqual(resp.get_json()["code"], "invalid_request")

        # Sync validation
        for bad_sync in (
            {"set_id": "   "},
            {"set_id": "root", "settings": {"interval_minutes": 0}},
            {"set_id": "root", "settings": "bad"},
        ):
            with client.post(
                "/api/sync",
                json=bad_sync,
                headers={"X-CSRF-Token": csrf},
            ) as resp:
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()["code"], "invalid_request")

        # Preview validation
        with client.post(
            "/api/preview",
            json={"content_id": ""},
            headers={"X-CSRF-Token": csrf},
        ) as preview_bad:
            self.assertEqual(preview_bad.status_code, 400)

        # Refresh validation
        with client.post(
            "/api/library/refresh",
            json={"set_id": ""},
            headers={"X-CSRF-Token": csrf},
        ) as refresh_bad:
            self.assertEqual(refresh_bad.status_code, 400)

        # Abort validation
        with client.post(
            "/api/abort",
            json={"target": "invalid"},
            headers={"X-CSRF-Token": csrf},
        ) as abort_bad:
            self.assertEqual(abort_bad.status_code, 400)

        # JSON error envelopes for 404 / 405 on /api/*
        with client.get("/api/does-not-exist") as not_found:
            self.assertEqual(not_found.status_code, 404)
            self.assertEqual(not_found.get_json()["code"], "not_found")

        with client.get("/api/sync") as wrong_method:
            self.assertEqual(wrong_method.status_code, 405)
            self.assertEqual(wrong_method.get_json()["code"], "method_not_allowed")

    def test_thumbs_serving_cache_headers_and_path_security(self):
        thumbs_dir = self.config.cache_dir / "thumbs"
        thumbs_dir.mkdir(parents=True)
        valid_file = thumbs_dir / f"{self.sample_sha}.jpg"
        valid_file.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

        symlink_sha = hashlib.sha256(b"symlink").hexdigest()
        outside_file = self.config.cache_dir / "outside.jpg"
        outside_file.write_bytes(b"secret")
        (thumbs_dir / f"{symlink_sha}.jpg").symlink_to(outside_file)

        app = create_app(self.config)
        app.testing = True
        client = app.test_client()

        with client.get(f"/thumbs/{self.sample_sha}.jpg") as ok:
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.mimetype, "image/jpeg")
            self.assertEqual(ok.data, b"\xff\xd8\xff\xe0fake-jpeg")
            self.assertEqual(
                ok.headers.get("Cache-Control"),
                "public, max-age=31536000, immutable",
            )

        missing_sha = hashlib.sha256(b"missing").hexdigest()
        for bad_path in (
            f"/thumbs/{missing_sha}.jpg",
            f"/thumbs/{self.sample_sha.upper()}.jpg",
            "/thumbs/not-a-sha.jpg",
            f"/thumbs/../{self.sample_sha}.jpg",
            f"/thumbs/..%2F{self.sample_sha}.jpg",
            f"/thumbs/{symlink_sha}.jpg",
        ):
            with self.subTest(path=bad_path):
                with client.get(bad_path) as bad_resp:
                    self.assertEqual(bad_resp.status_code, 404)

    def test_status_degraded_modes(self):
        app = create_app(self.config, ipc_timeout=0.5)
        app.testing = True
        client = app.test_client()

        # Both down
        with client.get("/api/status") as both_down:
            self.assertEqual(both_down.status_code, 200)
            data = both_down.get_json()
        self.assertTrue(data["degraded"])
        self.assertTrue(data["services"]["web"]["ok"])
        self.assertFalse(data["services"]["tv"]["ok"])
        self.assertFalse(data["services"]["library"]["ok"])
        self.assertIsNone(data["tv"])
        self.assertIsNone(data["library"])

        # Only library up
        self._start_library_server()
        with client.get("/api/status") as resp:
            lib_only = resp.get_json()
        self.assertTrue(lib_only["degraded"])
        self.assertFalse(lib_only["services"]["tv"]["ok"])
        self.assertTrue(lib_only["services"]["library"]["ok"])
        self.assertIsNone(lib_only["tv"])
        self.assertIsNotNone(lib_only["library"])

        # Both up, but library returns 500
        self._start_tv_server()
        self.library_state.status_http_code = HTTP_INTERNAL_ERROR
        with client.get("/api/status") as resp:
            lib_err = resp.get_json()
        self.assertTrue(lib_err["degraded"])
        self.assertTrue(lib_err["services"]["tv"]["ok"])
        self.assertFalse(lib_err["services"]["library"]["ok"])
        self.assertEqual(lib_err["services"]["library"]["error"], "daemon error")
        self.assertIsNone(lib_err["library"])

    def test_proxy_endpoints_forwarding_conflict_not_found_and_offline_503(self):
        self._start_tv_server()
        self._start_library_server()
        _, client, csrf, _, _ = self._get_csrf_client()
        headers = {"X-CSRF-Token": csrf}

        # check-tv
        with client.post("/api/check-tv", json={}, headers=headers) as check_resp:
            self.assertEqual(check_resp.status_code, 200)
            self.assertTrue(check_resp.get_json()["tv_ready"])

        # settings (full payload)
        with client.post(
            "/api/settings",
            json={
                "set_id": "occasion:Party Time",
                "random": False,
                "mix_art_store_favorites": True,
                "interval_minutes": 10,
            },
            headers=headers,
        ) as settings_resp:
            self.assertEqual(settings_resp.status_code, 200)
        self.assertEqual(
            self.tv_state.requests[-1],
            (
                "POST",
                "/settings",
                {
                    "set_id": "occasion:Party Time",
                    "random": False,
                    "mix_art_store_favorites": True,
                    "interval_minutes": 10,
                },
            ),
        )

        # settings (partial update preserves omitted fields without injecting defaults)
        with client.post(
            "/api/settings",
            json={"set_id": "root", "interval_minutes": 25},
            headers=headers,
        ) as partial_settings_resp:
            self.assertEqual(partial_settings_resp.status_code, 200)
        self.assertEqual(
            self.tv_state.requests[-1],
            ("POST", "/settings", {"set_id": "root", "interval_minutes": 25}),
        )

        # sync
        with client.post(
            "/api/sync",
            json={"set_id": "root", "settings": {"random": True, "interval_minutes": 5}},
            headers=headers,
        ) as sync_resp:
            self.assertEqual(sync_resp.status_code, 202)
        self.assertEqual(
            self.tv_state.requests[-1],
            (
                "POST",
                "/sync",
                {
                    "set_id": "root",
                    "settings": {
                        "random": True,
                        "mix_art_store_favorites": False,
                        "interval_minutes": 5,
                    },
                },
            ),
        )

        # 409 conflict passthrough across TV & library mutations
        for endpoint, payload, state_obj, upstream_path in (
            ("/api/check-tv", {}, self.tv_state, "/check"),
            ("/api/settings", {"set_id": "root", "random": True}, self.tv_state, "/settings"),
            ("/api/sync", {"set_id": "root"}, self.tv_state, "/sync"),
            ("/api/preview", {"content_id": "MY_F0001"}, self.tv_state, "/preview"),
            ("/api/library/refresh", {"set_id": "root"}, self.library_state, "/refresh"),
        ):
            state_obj.next_post_responses[upstream_path] = (
                HTTP_CONFLICT,
                make_error_envelope("operation conflict", "conflict"),
            )
            with client.post(endpoint, json=payload, headers=headers) as conflict_resp:
                self.assertEqual(conflict_resp.status_code, 409, endpoint)
                self.assertEqual(conflict_resp.get_json()["code"], "conflict")

        # 404 forwarding for preview & library/refresh
        self.tv_state.next_post_responses["/preview"] = (
            HTTP_NOT_FOUND,
            make_error_envelope("unknown content_id", "not_found"),
        )
        with client.post(
            "/api/preview",
            json={"content_id": "MY_MISSING"},
            headers=headers,
        ) as preview_404:
            self.assertEqual(preview_404.status_code, 404)
            self.assertEqual(preview_404.get_json()["code"], "not_found")

        self.library_state.next_post_responses["/refresh"] = (
            HTTP_NOT_FOUND,
            make_error_envelope("unknown set", "not_found"),
        )
        with client.post(
            "/api/library/refresh",
            json={"set_id": "occasion:Missing"},
            headers=headers,
        ) as refresh_404:
            self.assertEqual(refresh_404.status_code, 404)
            self.assertEqual(refresh_404.get_json()["code"], "not_found")

        # preview success
        with client.post(
            "/api/preview",
            json={"content_id": "MY_F0001"},
            headers=headers,
        ) as preview_resp:
            self.assertEqual(preview_resp.status_code, 202)
            self.assertEqual(preview_resp.get_json()["preview"]["content_id"], "MY_F0001")
        self.assertEqual(
            self.tv_state.requests[-1],
            ("POST", "/preview", {"content_id": "MY_F0001"}),
        )

        # library refresh success
        with client.post(
            "/api/library/refresh",
            json={"set_id": "root"},
            headers=headers,
        ) as refresh_resp:
            self.assertEqual(refresh_resp.status_code, 202)
        self.assertEqual(
            self.library_state.requests[-1],
            ("POST", "/refresh", {"set_id": "root"}),
        )

        # explicit abort targets
        with client.post(
            "/api/abort",
            json={"target": "tv"},
            headers=headers,
        ) as abort_tv_resp:
            self.assertEqual(abort_tv_resp.status_code, 200)
            self.assertTrue(abort_tv_resp.get_json()["aborted"])
        with client.post(
            "/api/abort",
            json={"target": "library"},
            headers=headers,
        ) as abort_lib_resp:
            self.assertEqual(abort_lib_resp.status_code, 200)
            self.assertTrue(abort_lib_resp.get_json()["aborted"])

        # Stop servers -> 503 service_unavailable
        self._stop_servers()
        for endpoint, payload in (
            ("/api/check-tv", {}),
            ("/api/settings", {"set_id": "root"}),
            ("/api/sync", {"set_id": "root"}),
            ("/api/preview", {"content_id": "MY_F0001"}),
            ("/api/library/refresh", {}),
            ("/api/abort", {"target": "tv"}),
            ("/api/abort", {"target": "library"}),
            ("/api/abort", {"target": "auto"}),
        ):
            with client.post(endpoint, json=payload, headers=headers) as resp:
                self.assertEqual(resp.status_code, 503, endpoint)
                self.assertEqual(resp.get_json()["code"], "service_unavailable")

    def test_abort_auto_targets_active_daemon_even_when_other_offline(self):
        # Library active while TV daemon is offline
        self._start_library_server()
        self.library_state.status_payload["operation"] = {
            "kind": "refresh",
            "phase": "scanning",
            "message": "Scanning images",
            "current_item": "sunset.jpg",
            "completed_ops": 1,
            "total_ops": 5,
            "abort_requested": False,
            "started_at": "2026-11-24T05:00:00+00:00",
        }
        _, client, csrf, _, _ = self._get_csrf_client()
        headers = {"X-CSRF-Token": csrf}

        with client.post("/api/abort", json={"target": "auto"}, headers=headers) as resp:
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["aborted"])
        self.assertTrue(any(req[:2] == ("POST", "/abort") for req in self.library_state.requests))

        # Idle reachable library daemon while TV offline
        self.library_state.requests.clear()
        self.library_state.status_payload["operation"] = None
        self.library_state.next_post_responses["/abort"] = (HTTP_OK, {"ok": True, "aborted": False})
        with client.post("/api/abort", json={}, headers=headers) as idle_resp:
            self.assertEqual(idle_resp.status_code, 200)
            self.assertFalse(idle_resp.get_json()["aborted"])
        self.assertTrue(any(req[:2] == ("POST", "/abort") for req in self.library_state.requests))

    def test_abort_auto_multi_active_checks_boolean_aborted_and_all_offline_returns_503(self):
        self._start_tv_server()
        self._start_library_server()
        active_op = {
            "kind": "sync",
            "phase": "uploading",
            "message": "Working",
            "current_item": None,
            "completed_ops": 1,
            "total_ops": 3,
            "abort_requested": False,
            "started_at": "2026-11-24T05:00:00+00:00",
        }
        self.tv_state.status_payload["operation"] = dict(active_op)
        self.library_state.status_payload["operation"] = dict(active_op, kind="refresh")
        self.tv_state.next_post_responses["/abort"] = (HTTP_OK, {"ok": True})
        self.library_state.next_post_responses["/abort"] = (HTTP_OK, {"ok": True, "aborted": False})

        _, client, csrf, _, _ = self._get_csrf_client()
        with client.post(
            "/api/abort",
            json={"target": "auto"},
            headers={"X-CSRF-Token": csrf},
        ) as resp:
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
        self.assertFalse(data["aborted"])
        self.assertIn("tv", data["targets"])
        self.assertIn("library", data["targets"])

        # Simulate both daemons reporting active operations on GET /status,
        # then going offline before POST /abort runs -> must return 503 service_unavailable
        def fake_request_json(sock_path, method, path, body=None, timeout=5.0):
            if method == "GET" and path == "/status":
                if sock_path == self.config.tv_sock:
                    return HTTP_OK, {"operation": dict(active_op)}
                return HTTP_OK, {"operation": dict(active_op, kind="refresh")}
            raise OSError("connection refused")

        with mock.patch("frame_art_mode.web_server.request_json") as mocked_req:
            from frame_art_mode.models import IPCError

            def side_effect(sock_path, method, path, body=None, timeout=5.0):
                if method == "GET" and path == "/status":
                    return fake_request_json(sock_path, method, path, body, timeout)
                raise IPCError(
                    "daemon went offline",
                    status_code=503,
                    code="service_unavailable",
                    offline=True,
                )

            mocked_req.side_effect = side_effect
            with client.post(
                "/api/abort",
                json={"target": "auto"},
                headers={"X-CSRF-Token": csrf},
            ) as offline_resp:
                self.assertEqual(offline_resp.status_code, 503)
                self.assertEqual(offline_resp.get_json()["code"], "service_unavailable")

    def test_check_tv_uses_check_timeout_while_status_uses_ipc_timeout(self):
        delay_seconds = 0.3

        def slow_status(body: dict) -> tuple[int, dict]:
            time.sleep(delay_seconds)
            return self.tv_state.get_status(body)

        def slow_check(body: dict) -> tuple[int, dict]:
            time.sleep(delay_seconds)
            return self.tv_state.handle_post(
                "/check",
                body,
                (
                    HTTP_OK,
                    {
                        "ok": True,
                        "tv_ready": True,
                        "current_display": self.tv_state.status_payload["current_display"],
                        "error": None,
                    },
                ),
            )

        server = UnixHTTPServer(
            self.config.tv_sock,
            routes={
                ("GET", "/status"): slow_status,
                ("POST", "/check"): slow_check,
            },
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._servers.append((server, thread))
        self._start_library_server()

        _, client, csrf, _, _ = self._get_csrf_client(ipc_timeout=0.15, check_timeout=1.5)
        headers = {"X-CSRF-Token": csrf}

        with client.post("/api/check-tv", json={}, headers=headers) as check_resp:
            self.assertEqual(check_resp.status_code, 200)
            self.assertTrue(check_resp.get_json()["tv_ready"])

        with client.get("/api/status") as status_resp:
            self.assertEqual(status_resp.status_code, 200)
            status_data = status_resp.get_json()
        self.assertTrue(status_data["degraded"])
        self.assertFalse(status_data["services"]["tv"]["ok"])
        self.assertIsNone(status_data["tv"])
        self.assertTrue(status_data["services"]["library"]["ok"])

    def test_check_tv_passes_through_tv_ready_false_and_error(self):
        self._start_tv_server()
        self._start_library_server()
        _, client, csrf, _, _ = self._get_csrf_client()
        headers = {"X-CSRF-Token": csrf}

        self.tv_state.next_post_responses["/check"] = (
            HTTP_OK,
            {
                "ok": True,
                "tv_ready": False,
                "current_display": self.tv_state.status_payload["current_display"],
                "error": "TV is not in Art Mode",
            },
        )
        with client.post("/api/check-tv", json={}, headers=headers) as resp:
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["tv_ready"])
        self.assertEqual(payload["error"], "TV is not in Art Mode")

    def test_upstream_http_200_non_dict_or_malformed_json_returns_500_invalid_response(self):
        self._start_library_server()
        _, client, csrf, _, _ = self._get_csrf_client()
        headers = {"X-CSRF-Token": csrf}

        for raw_body in (b"[]", b"{bad!"):
            with self.subTest(raw_body=raw_body):
                ensure_private_dir(self.config.tv_sock.parent)
                self.config.tv_sock.unlink(missing_ok=True)
                srv_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                srv_sock.bind(str(self.config.tv_sock))
                srv_sock.listen(8)
                stop_raw = threading.Event()

                def serve_raw(sock_obj: socket.socket, body_bytes: bytes, stop_flag: threading.Event) -> None:
                    sock_obj.settimeout(0.05)
                    while not stop_flag.is_set():
                        try:
                            conn, _ = sock_obj.accept()
                        except TimeoutError:
                            continue
                        except OSError:
                            break
                        with conn:
                            conn.settimeout(1.0)
                            try:
                                conn.recv(4096)
                            except OSError:
                                pass
                            response = (
                                b"HTTP/1.1 200 OK\r\n"
                                b"Content-Type: application/json\r\n"
                                + f"Content-Length: {len(body_bytes)}\r\n".encode("ascii")
                                + b"Connection: close\r\n\r\n"
                                + body_bytes
                            )
                            conn.sendall(response)

                thread = threading.Thread(
                    target=serve_raw,
                    args=(srv_sock, raw_body, stop_raw),
                    daemon=True,
                )
                thread.start()
                try:
                    for endpoint, payload in (
                        ("/api/check-tv", {}),
                        ("/api/sync", {"set_id": "root"}),
                        ("/api/abort", {"target": "tv"}),
                    ):
                        with client.post(endpoint, json=payload, headers=headers) as resp:
                            self.assertEqual(resp.status_code, 500, endpoint)
                            err = resp.get_json()
                        self.assertEqual(err["code"], "invalid_response")
                        self.assertTrue(err["error"])
                finally:
                    stop_raw.set()
                    srv_sock.close()
                    thread.join(timeout=2.0)
                    self.config.tv_sock.unlink(missing_ok=True)

    def test_sse_events_degraded_to_online_keepalive_and_clean_shutdown(self):
        # Start degraded (no daemons running), then bring daemons online mid-stream
        stop_event = threading.Event()
        app = create_app(
            self.config,
            ipc_timeout=0.3,
            sse_poll_interval=0.04,
            sse_active_poll_interval=0.02,
            sse_heartbeat_interval=0.08,
            sse_stop_event=stop_event,
        )
        app.testing = True
        client = app.test_client()

        with client.get("/api/events", buffered=False) as resp:
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.mimetype, "text/event-stream")
            self.assertEqual(resp.headers.get("Cache-Control"), "no-cache")
            self.assertEqual(resp.headers.get("Connection"), "keep-alive")
            self.assertEqual(resp.headers.get("X-Accel-Buffering"), "no")

            stream_iter = resp.iter_encoded()

            def read_next_chunk() -> str:
                raw = next(stream_iter)
                return raw.decode("utf-8") if isinstance(raw, bytes) else raw

            first = read_next_chunk()
            self.assertIn("retry: 3000\n", first)
            self.assertIn("event: status\n", first)
            first_json = json.loads(first.split("data: ", 1)[1].strip())
            self.assertTrue(first_json["degraded"])

            # Wait for at least one keepalive while degraded state is unchanged
            deadline = time.monotonic() + 2.0
            saw_keepalive = False
            while time.monotonic() < deadline:
                text = read_next_chunk()
                if ": keepalive" in text:
                    saw_keepalive = True
                    break
            self.assertTrue(saw_keepalive)

            # Bring both daemons online -> live transition from degraded -> online
            self._start_tv_server()
            self._start_library_server()
            deadline = time.monotonic() + 2.0
            saw_online = False
            while time.monotonic() < deadline:
                text = read_next_chunk()
                if "event: status\n" in text:
                    payload = json.loads(text.split("data: ", 1)[1].strip())
                    if payload["degraded"] is False:
                        saw_online = True
                        break
            self.assertTrue(saw_online)

            # Mutate TV status -> SSE should emit updated status event
            with self.tv_state.lock:
                self.tv_state.status_payload["tv_ready"] = True
                self.tv_state.status_payload["operation"] = {
                    "kind": "sync",
                    "phase": "uploading",
                    "message": "Uploading sunset.jpg",
                    "current_item": "sunset.jpg",
                    "completed_ops": 1,
                    "total_ops": 3,
                    "abort_requested": False,
                    "started_at": "2026-11-24T05:10:00+00:00",
                }

            deadline = time.monotonic() + 2.0
            saw_update = False
            while time.monotonic() < deadline:
                text = read_next_chunk()
                if "event: status\n" in text:
                    payload = json.loads(text.split("data: ", 1)[1].strip())
                    if payload["tv"] and payload["tv"]["tv_ready"] is True:
                        saw_update = True
                        break
            self.assertTrue(saw_update)

            # Stop SSE cleanly and verify zero POST /check calls occurred
            stop_event.set()
            remaining = list(stream_iter)
            self.assertFalse(
                any(req[:2] == ("POST", "/check") for req in self.tv_state.requests)
            )
            self.assertIsInstance(remaining, list)


class WebServerCLITests(unittest.TestCase):
    def test_cli_serve_and_default_command_invoke_app_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg_path = root / "config.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "tv_ip": "192.168.1.50",
                        "bind_ip": "192.168.1.20",
                        "port": 8080,
                        "image_root": str(root / "images"),
                        "state_dir": str(root / "state"),
                    }
                ),
                encoding="utf-8",
            )

            for argv in (
                ["serve", "--config", str(cfg_path), "--port", "9091"],
                ["--config", str(cfg_path), "--port", "9092"],
            ):
                with self.subTest(argv=argv):
                    with mock.patch("flask.Flask.run") as mock_run:
                        exit_code = main(argv)
                    self.assertEqual(exit_code, 0)
                    expected_port = 9091 if "9091" in argv else 9092
                    mock_run.assert_called_once_with(
                        host="192.168.1.20",
                        port=expected_port,
                        threaded=True,
                        use_reloader=False,
                    )

    def test_cli_invalid_config_returns_exit_code_2(self):
        with tempfile.TemporaryDirectory() as temporary:
            bad_cfg = Path(temporary) / "config.json"
            bad_cfg.write_text("{}", encoding="utf-8")
            with self.assertLogs("frame_art_mode.web_server", level="ERROR"):
                exit_code = main(["serve", "--config", str(bad_cfg)])
            self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
