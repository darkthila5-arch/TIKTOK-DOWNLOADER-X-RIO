import os
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

import requests
import yt_dlp
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "12"))  # requests per window
RATE_WINDOW = int(os.environ.get("RATE_WINDOW", "60"))  # seconds

YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "format": "best[ext=mp4]/best",
    "socket_timeout": 20,
}

_hits = defaultdict(deque)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() or request.remote_addr or "unknown")


def rate_limited():
    now = time.time()
    q = _hits[client_ip()]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def is_tiktok_url(url):
    """Only allow real TikTok hosts so the server can't be used as an open proxy."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme in ("http", "https") and (
        host == "tiktok.com" or host.endswith(".tiktok.com")
    )


def resolve(url):
    """Ask yt-dlp for video details plus the headers needed to fetch the file."""
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
        media_url = info.get("url")
        if not media_url and info.get("requested_formats"):
            media_url = info["requested_formats"][0].get("url")
        if not media_url:
            raise ValueError("No downloadable video found.")

        headers = dict(info.get("http_headers") or {})
        cookie = ydl.cookiejar.get_cookie_header(media_url)
        if cookie:
            headers["Cookie"] = cookie
    return info, media_url, headers


def error(message, status):
    return jsonify({"error": message}), status


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.post("/api/info")
def api_info():
    if rate_limited():
        return error("Too many requests. Wait a minute and try again.", 429)

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not is_tiktok_url(url):
        return error("That doesn't look like a TikTok link.", 400)

    try:
        info, _, _ = resolve(url)
    except yt_dlp.utils.DownloadError:
        return error(
            "Couldn't get this video. It may be private, removed, or TikTok blocked the request.",
            422,
        )
    except Exception:
        return error("Something went wrong on our side. Try again.", 500)

    return jsonify(
        {
            "title": info.get("title") or info.get("description") or "TikTok video",
            "uploader": info.get("uploader") or info.get("creator") or "",
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "download": "/download?url=" + requests.utils.quote(url, safe=""),
        }
    )


@app.get("/download")
def download():
    if rate_limited():
        return error("Too many requests. Wait a minute and try again.", 429)

    url = (request.args.get("url") or "").strip()
    if not is_tiktok_url(url):
        return error("That doesn't look like a TikTok link.", 400)

    try:
        info, media_url, headers = resolve(url)
        upstream = requests.get(media_url, headers=headers, stream=True, timeout=30)
        upstream.raise_for_status()
    except Exception:
        return error("Couldn't download this video. Try again in a moment.", 502)

    def stream():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    filename = f"tiktok_{info.get('id', 'video')}.mp4"
    resp = Response(stream(), mimetype=upstream.headers.get("Content-Type", "video/mp4"))
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    if upstream.headers.get("Content-Length"):
        resp.headers["Content-Length"] = upstream.headers["Content-Length"]
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
