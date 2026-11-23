from __future__ import annotations

import base64
import io
from pathlib import Path
import secrets

from flask import Flask, abort, flash, redirect, render_template_string, request, send_file, session, url_for
from PIL import Image, ImageOps

from .images import discover_sets
from .service import FrameService, ServiceError


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Frame Art Mode</title>
<style>
body{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}
label{display:inline-block;margin:.5rem 1rem .5rem 0}button,select,input{font:inherit;padding:.45rem}
.notice{padding:.7rem;background:#fff2cc;margin:.5rem 0}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:1rem}
.card img{width:100%;height:130px;object-fit:cover;background:#181818}.muted{color:#555;font-size:.9rem}
</style></head><body>
<h1>Frame Art Mode</h1>
<p>Active set: <strong>{{ display_names.get(status.active_set, 'None') }}</strong>.
{% if status.tv_ready is sameas true %}TV is in Art Mode.{% elif status.tv_ready is sameas false %}TV is unavailable or not in Art Mode.{% else %}TV status not checked yet.{% endif %}</p>
{% if status.last_error %}<p class="notice">{{ status.last_error }}</p>{% endif %}
{% for warning in status.warnings %}<p class="notice">{{ warning }}</p>{% endfor %}
{% if status.cleanup_pending %}<p class="notice">Cleanup pending for {{ status.cleanup_pending|length }} app-managed TV item(s); retry with Sync to TV.</p>{% endif %}
{% for message in get_flashed_messages() %}<p class="notice">{{ message }}</p>{% endfor %}
<form method="post" action="{{ url_for('sync') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
<label>Image set <select name="set_id" onchange="window.location='/?set_id='+encodeURIComponent(this.value)">{% for set_id in sets %}
<option value="{{ set_id }}" {% if set_id == selected %}selected{% endif %}>{{ display_names[set_id] }} ({{ sets[set_id]|length }})</option>
{% endfor %}</select></label>
{% set settings = service.settings_for(selected) %}
<label><input type="checkbox" name="random" value="1" {% if settings.random %}checked{% endif %}> Random order</label>
<label>Minutes per image <input type="number" min="1" name="interval_minutes" value="{{ settings.interval_minutes }}"></label>
<button formaction="{{ url_for('save_settings') }}">Save settings</button>
<button type="submit">Sync to TV</button>
</form>
{% for warning in scan_warnings %}<p class="notice">{{ warning }}</p>{% endfor %}
<h2>{{ display_names.get(selected, 'Image set') }} images</h2>
<p class="muted">Changes on disk are picked up only when you explicitly sync. Hidden items, @eaDir and non-image files are ignored.</p>
<div class="grid">{% for image in images %}
<div class="card"><img loading="lazy" src="{{ url_for('thumbnail', encoded=encode_path(image.source_rel)) }}" alt="">
<div>{{ image.name_rel }}</div><form method="post" action="{{ url_for('preview') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
<input type="hidden" name="content_id" value="{{ content_by_source.get(image.source_rel, '') }}">
{% if content_by_source.get(image.source_rel) %}<button type="submit">Preview 15 seconds</button>{% endif %}
</form></div>{% endfor %}</div>
</body></html>"""


def create_app(service: FrameService) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_bytes(32)

    @app.before_request
    def protect_posts():
        if request.method == "POST":
            submitted = request.form.get("csrf_token", "")
            if not submitted or submitted != session.get("csrf_token"):
                abort(400)
        else:
            session.setdefault("csrf_token", secrets.token_urlsafe(32))

    @app.get("/")
    def index():
        sets, scan_warnings = service.list_sets()
        names = {"root": "Default (root-level images)"}
        names.update({set_id: set_id.removeprefix("occasion:") for set_id in sets if set_id.startswith("occasion:")})
        selected = request.args.get("set_id", service.status()["active_set"] or "root")
        if selected not in sets:
            selected = "root"
        content_by_source = {
            entry["source_rel"]: entry["content_id"]
            for entry in service.status()["entries"]
        }
        return render_template_string(
            PAGE,
            service=service,
            status=service.status(),
            sets=sets,
            display_names=names,
            selected=selected,
            images=sets[selected],
            content_by_source=content_by_source,
            encode_path=lambda path: base64.urlsafe_b64encode(path.encode()).decode().rstrip("="),
            scan_warnings=scan_warnings,
            csrf_token=session["csrf_token"],
        )

    @app.post("/settings")
    def save_settings():
        set_id = request.form.get("set_id", "root")
        try:
            interval = int(request.form.get("interval_minutes", "15"))
            service.save_settings(set_id, request.form.get("random") == "1", interval)
            flash("Settings saved.")
        except (ValueError, ServiceError) as exc:
            flash(str(exc))
        return redirect(url_for("index", set_id=set_id))

    @app.post("/sync")
    def sync():
        set_id = request.form.get("set_id", "root")
        try:
            interval = int(request.form.get("interval_minutes", "15"))
            service.save_settings(set_id, request.form.get("random") == "1", interval)
            service.sync(set_id)
            flash("Sync completed.")
        except (ValueError, ServiceError) as exc:
            flash(str(exc))
        return redirect(url_for("index", set_id=set_id))

    @app.post("/preview")
    def preview():
        try:
            service.preview(request.form.get("content_id", ""))
            flash("Preview started for 15 seconds.")
        except ServiceError as exc:
            flash(str(exc))
        return redirect(url_for("index"))

    @app.get("/thumbnail/<encoded>")
    def thumbnail(encoded: str):
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            relative = base64.urlsafe_b64decode(padded.encode()).decode()
            candidate = Path(relative)
            if candidate.is_absolute() or ".." in candidate.parts:
                abort(404)
            path = service.config.image_root / candidate
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source)
                image.thumbnail((360, 240), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.convert("RGB").save(output, "JPEG", quality=78)
                output.seek(0)
                return send_file(output, mimetype="image/jpeg", max_age=0)
        except (OSError, ValueError, UnicodeDecodeError):
            abort(404)

    return app
