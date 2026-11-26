"""Cortex Blog Studio API.

Posts and images are stored in Turso. The portfolio site reads published posts from /api/posts
at build time. Publishing or editing a live post can trigger a rebuild through a Render deploy hook.
"""

import base64
import hashlib
import hmac
import html as html_lib
import io
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import bleach
import markdown
import requests
import turso_serverless
from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, request, send_from_directory, session

load_dotenv()

BASE = Path(__file__).resolve().parent

TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")

WRITER_KEY = os.environ.get("WRITER_KEY", "")
# Sessions must be signed with the same key in every worker and after every restart.
SECRET_KEY = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("cortex-session:" + WRITER_KEY).encode()
).hexdigest()

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # this service, e.g. https://cortex-blog.onrender.com
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")  # the portfolio, e.g. https://my-portfolio.onrender.com
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*")

# Render deploy hook for the portfolio. Publishing or editing a live post rebuilds the site.
RENDER_DEPLOY_HOOK = os.environ.get("RENDER_DEPLOY_HOOK", "")
REBUILD_DELAY_SECONDS = float(os.environ.get("REBUILD_DELAY", "45"))

ALLOWED_IMAGES = {"png", "jpg", "jpeg", "webp", "gif"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_IMAGE_EDGE = 1600
IMAGE_QUALITY = 85

app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 512 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


# ---------- database (Turso) ----------

POSTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    thumbnail_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT
)
"""

IMAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,
    mime TEXT NOT NULL,
    data TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    created_at TEXT NOT NULL
)
"""


def connect():
    if not TURSO_DATABASE_URL or not TURSO_AUTH_TOKEN:
        raise RuntimeError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be configured.")
    return turso_serverless.connect(TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)


def init_db():
    conn = connect()
    try:
        conn.execute(POSTS_SCHEMA)
        conn.execute(IMAGES_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def query(sql, args=()):
    conn = connect()
    try:
        cur = conn.execute(sql, args)
        if cur.description is None:
            return []
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def query_one(sql, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql, args=()):
    conn = connect()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------- content ----------

ALLOWED_TAGS = list(bleach.sanitizer.ALLOWED_TAGS) + [
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "code", "img", "blockquote", "hr", "br",
    "table", "thead", "tbody", "tr", "th", "td", "ul", "ol", "li", "em", "strong", "a", "span",
    "figure", "figcaption",
]
ALLOWED_ATTRS = {
    "a": ["href", "title", "rel", "target"],
    "img": ["src", "alt", "title", "width", "height", "loading"],
    "code": ["class"],
    "span": ["class"],
    "th": ["align"],
    "td": ["align"],
}


def render_markdown(text):
    raw = markdown.markdown(text or "", extensions=["fenced_code", "tables", "sane_lists"])
    return bleach.clean(raw, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, protocols=["http", "https", "mailto"])


def make_slug(title):
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80] or "post"
    slug, n = base, 2
    while query_one("SELECT id FROM posts WHERE slug = ?", (slug,)):
        slug = f"{base}-{n}"
        n += 1
    return slug


def post_out(row):
    """Shaped like the WordPress REST API, so the portfolio reads it the same way, plus a thumbnail field."""
    return {
        "id": row["id"],
        "slug": row["slug"],
        "date": row["published_at"] or row["created_at"],
        "link": f"{SITE_URL}/blog/{row['slug']}/" if SITE_URL else "",
        "title": {"rendered": html_lib.escape(row["title"])},
        "excerpt": {"rendered": html_lib.escape(row["description"])},
        "content": {"rendered": render_markdown(row["body"])},
        "thumbnail": row["thumbnail_url"],
    }


def admin_out(row):
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "description": row["description"],
        "body": row["body"],
        "thumbnail_url": row["thumbnail_url"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "published_at": row["published_at"],
    }


# ---------- rebuild trigger ----------

_rebuild_timer = None
_rebuild_lock = threading.Lock()


def _fire_rebuild():
    try:
        requests.post(RENDER_DEPLOY_HOOK, timeout=20)
    except requests.RequestException:
        pass


def schedule_site_rebuild():
    """Start one portfolio deploy shortly after the last change, however many saves happen in between."""
    global _rebuild_timer
    if not RENDER_DEPLOY_HOOK:
        return
    with _rebuild_lock:
        if _rebuild_timer:
            _rebuild_timer.cancel()
        _rebuild_timer = threading.Timer(REBUILD_DELAY_SECONDS, _fire_rebuild)
        _rebuild_timer.daemon = True
        _rebuild_timer.start()


# ---------- auth ----------

def is_writer():
    if session.get("writer"):
        return True
    header_key = request.headers.get("X-Writer-Key", "")
    return bool(WRITER_KEY) and hmac.compare_digest(header_key, WRITER_KEY)


def writer_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_writer():
            return jsonify({"error": "Please enter the access key."}), 401
        return view(*args, **kwargs)

    return wrapper


# ---------- images ----------

def optimise_image(raw):
    """Resize to at most 1600 px and re-encode as WebP. Returns (dict, error)."""
    from PIL import Image, ImageOps

    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return None, "That file isn't a valid image."
    if im.width * im.height > 40_000_000:
        return None, "That image is too large to process."

    im = ImageOps.exif_transpose(im)
    im.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if has_alpha else "RGB")

    out = io.BytesIO()
    im.save(out, "WEBP", quality=IMAGE_QUALITY, method=4)
    return {"bytes": out.getvalue(), "width": im.width, "height": im.height}, ""


# ---------- pages and media ----------

@app.get("/")
def studio():
    return send_from_directory(BASE / "static", "write.html")


@app.get("/media/<image_id>")
def media(image_id):
    row = query_one("SELECT mime, data FROM images WHERE id = ?", (image_id,))
    if not row:
        abort(404)
    resp = Response(base64.b64decode(row["data"]), mimetype=row["mime"])
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ---------- session ----------

@app.get("/api/session")
def session_status():
    return jsonify({"ok": is_writer()})


@app.post("/api/login")
def login():
    if not WRITER_KEY:
        return jsonify({"error": "The access key is not set on the server."}), 500
    data = request.get_json(silent=True) or {}
    if hmac.compare_digest(str(data.get("key", "")), WRITER_KEY):
        session["writer"] = True
        return jsonify({"ok": True})
    time.sleep(0.5)  # slow down guessing
    return jsonify({"error": "That key isn't right."}), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


# ---------- public read API (used by the portfolio) ----------

@app.get("/api/posts")
def public_posts():
    rows = query("SELECT * FROM posts WHERE status = 'published' ORDER BY published_at DESC")
    resp = jsonify([post_out(r) for r in rows])
    resp.headers["Access-Control-Allow-Origin"] = CORS_ORIGINS
    return resp


@app.get("/api/posts/<slug>")
def public_post(slug):
    row = query_one("SELECT * FROM posts WHERE slug = ? AND status = 'published'", (slug,))
    if not row:
        abort(404)
    resp = jsonify(post_out(row))
    resp.headers["Access-Control-Allow-Origin"] = CORS_ORIGINS
    return resp


# ---------- writer API ----------

def read_fields():
    data = request.get_json(silent=True) or {}
    return {
        "title": str(data.get("title", "")).strip()[:200],
        "description": str(data.get("description", "")).strip()[:300],
        "body": str(data.get("body", "")),
        "thumbnail_url": str(data.get("thumbnail_url", "")).strip()[:500],
    }


def get_post(post_id):
    return query_one("SELECT * FROM posts WHERE id = ?", (post_id,))


def save_fields(post_id, fields):
    execute(
        """UPDATE posts SET title = ?, description = ?, body = ?, thumbnail_url = ?, updated_at = ?
           WHERE id = ?""",
        (fields["title"], fields["description"], fields["body"], fields["thumbnail_url"], now_iso(), post_id),
    )


@app.get("/api/admin/posts")
@writer_required
def admin_list():
    rows = query("SELECT * FROM posts ORDER BY updated_at DESC")
    return jsonify([admin_out(r) for r in rows])


@app.get("/api/admin/posts/<int:post_id>")
@writer_required
def admin_get(post_id):
    row = get_post(post_id)
    if not row:
        return jsonify({"error": "Post not found."}), 404
    return jsonify(admin_out(row))


@app.post("/api/admin/posts")
@writer_required
def admin_create():
    fields = read_fields()
    if not fields["title"]:
        return jsonify({"error": "Give the post a title first."}), 400
    stamp = now_iso()
    new_id = execute(
        """INSERT INTO posts (slug, title, description, body, thumbnail_url, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)""",
        (make_slug(fields["title"]), fields["title"], fields["description"], fields["body"],
         fields["thumbnail_url"], stamp, stamp),
    )
    return jsonify(admin_out(get_post(new_id))), 201


@app.put("/api/admin/posts/<int:post_id>")
@writer_required
def admin_update(post_id):
    row = get_post(post_id)
    if not row:
        return jsonify({"error": "Post not found."}), 404
    fields = read_fields()
    if not fields["title"]:
        return jsonify({"error": "The title can't be empty."}), 400
    save_fields(post_id, fields)
    if row["status"] == "published":
        schedule_site_rebuild()
    return jsonify(admin_out(get_post(post_id)))


@app.post("/api/admin/posts/<int:post_id>/publish")
@writer_required
def admin_publish(post_id):
    row = get_post(post_id)
    if not row:
        return jsonify({"error": "Post not found."}), 404
    fields = read_fields()
    if not fields["title"]:
        return jsonify({"error": "Give the post a title before publishing."}), 400
    save_fields(post_id, fields)
    execute(
        "UPDATE posts SET status = 'published', published_at = COALESCE(published_at, ?) WHERE id = ?",
        (now_iso(), post_id),
    )
    schedule_site_rebuild()
    return jsonify(admin_out(get_post(post_id)))


@app.post("/api/admin/posts/<int:post_id>/unpublish")
@writer_required
def admin_unpublish(post_id):
    row = get_post(post_id)
    if not row:
        return jsonify({"error": "Post not found."}), 404
    execute("UPDATE posts SET status = 'draft', updated_at = ? WHERE id = ?", (now_iso(), post_id))
    if row["status"] == "published":
        schedule_site_rebuild()
    return jsonify(admin_out(get_post(post_id)))


@app.delete("/api/admin/posts/<int:post_id>")
@writer_required
def admin_delete(post_id):
    row = get_post(post_id)
    if not row:
        return jsonify({"error": "Post not found."}), 404
    execute("DELETE FROM posts WHERE id = ?", (post_id,))
    if row["status"] == "published":
        schedule_site_rebuild()
    return jsonify({"ok": True})


@app.post("/api/admin/preview")
@writer_required
def admin_preview():
    data = request.get_json(silent=True) or {}
    return jsonify({"html": render_markdown(str(data.get("body", "")))})


@app.post("/api/admin/upload")
@writer_required
def admin_upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Choose an image to upload."}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_IMAGES:
        return jsonify({"error": "Use a PNG, JPG, WebP or GIF image."}), 400

    raw = file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        return jsonify({"error": "Images must be 5 MB or smaller."}), 400

    image, err = optimise_image(raw)
    if err:
        return jsonify({"error": err}), 400

    image_id = secrets.token_urlsafe(9)
    execute(
        "INSERT INTO images (id, mime, data, width, height, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (image_id, "image/webp", base64.b64encode(image["bytes"]).decode("ascii"),
         image["width"], image["height"], now_iso()),
    )
    url = f"{PUBLIC_URL or request.host_url.rstrip('/')}/media/{image_id}"
    return jsonify({"url": url}), 201


@app.after_request
def no_store_admin(resp):
    if request.path.startswith("/api/admin") or request.path.startswith("/api/login"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
