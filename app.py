import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests
import yt_dlp
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Config (all optional, set in Render > Environment)
# ---------------------------------------------------------------------------
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "12"))      # requests per window
RATE_WINDOW = int(os.environ.get("RATE_WINDOW", "60"))    # seconds
CACHE_TTL = int(os.environ.get("CACHE_TTL", "600"))       # seconds
PROXY_URL = os.environ.get("PROXY_URL") or None           # optional, used by yt-dlp
DEBUG_ERRORS = os.environ.get("DEBUG_ERRORS") == "1"      # show real error in the UI
# Order matters. Render's servers are datacenter IPs, which TikTok often blocks
# for yt-dlp, so "tikwm" goes first. Use PROVIDERS=ytdlp to disable the third party.
PROVIDERS = [p.strip() for p in os.environ.get("PROVIDERS", "tikwm,ytdlp").split(",") if p.strip()]

UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
URL_RE = re.compile(r"https?://[^\s]+")


class ResolveError(Exception):
    pass


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
_hits = defaultdict(deque)


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or request.remote_addr or "unknown"


def rate_limited():
    now = time.time()
    q = _hits[client_ip()]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def extract_url(text):
    """Accept a bare link or the whole share text and pull the link out."""
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(".,)") if match else ""


def is_tiktok_url(url):
    """Only real TikTok hosts, so the server can't be used as an open proxy."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme in ("http", "https") and (
        host == "tiktok.com" or host.endswith(".tiktok.com")
    )


def error(message, status, detail=None):
    body = {"error": message}
    if DEBUG_ERRORS and detail:
        body["detail"] = str(detail)[:300]
    return jsonify(body), status


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def iso_date(epoch=None, yyyymmdd=None):
    try:
        if epoch:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")
        if yyyymmdd and len(str(yyyymmdd)) == 8:
            s = str(yyyymmdd)
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    except (ValueError, OSError, OverflowError):
        pass
    return None


# ---------------------------------------------------------------------------
# Cache (links expire, so keep them only for a few minutes)
# ---------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if item and item[0] > time.time():
            return item[1]
        _cache.pop(key, None)
    return None


def cache_set(key, value):
    with _cache_lock:
        if len(_cache) > 200:
            now = time.time()
            for k in [k for k, (exp, _) in _cache.items() if exp < now]:
                _cache.pop(k, None)
            if len(_cache) > 200:
                _cache.clear()
        _cache[key] = (time.time() + CACHE_TTL, value)


# ---------------------------------------------------------------------------
# Provider 1: TikWM (works from datacenter IPs where yt-dlp usually fails)
# ---------------------------------------------------------------------------
_tikwm_lock = threading.Lock()
_tikwm_last = 0.0


def _abs(u):
    if not u:
        return None
    return u if u.startswith("http") else "https://www.tikwm.com" + u


def from_tikwm(url):
    global _tikwm_last
    with _tikwm_lock:  # the free API allows about 1 request per second
        wait = 1.2 - (time.time() - _tikwm_last)
        if wait > 0:
            time.sleep(wait)
        _tikwm_last = time.time()

    r = requests.get(
        "https://www.tikwm.com/api/",
        params={"url": url, "hd": 1},
        headers={"User-Agent": UA},
        timeout=25,
    )
    r.raise_for_status()
    payload = r.json()
    d = payload.get("data")
    if payload.get("code") != 0 or not d:
        raise ResolveError(f"tikwm: {payload.get('msg') or 'no data'}")

    formats = []
    if d.get("hdplay"):
        formats.append({"id": "hd", "label": "HD", "kind": "video",
                        "size": to_int(d.get("hd_size")), "url": _abs(d["hdplay"])})
    if d.get("play"):
        formats.append({"id": "sd", "label": "Standard", "kind": "video",
                        "size": to_int(d.get("size")), "url": _abs(d["play"])})
    if d.get("wmplay"):
        formats.append({"id": "wm", "label": "With TikTok watermark", "kind": "video",
                        "size": to_int(d.get("wm_size")), "url": _abs(d["wmplay"])})
    if d.get("music"):
        formats.append({"id": "mp3", "label": "Audio only (MP3)", "kind": "audio",
                        "size": None, "url": _abs(d["music"])})

    author = d.get("author") or {}
    return {
        "id": str(d.get("id") or "video"),
        "title": (d.get("title") or "TikTok video").strip(),
        "author": author.get("nickname") or "",
        "handle": author.get("unique_id") or "",
        "thumbnail": _abs(d.get("origin_cover") or d.get("cover")),
        "duration": to_int(d.get("duration")),
        "views": to_int(d.get("play_count")),
        "likes": to_int(d.get("digg_count")),
        "comments": to_int(d.get("comment_count")),
        "shares": to_int(d.get("share_count")),
        "created": iso_date(epoch=d.get("create_time")),
        "formats": formats,
    }


# ---------------------------------------------------------------------------
# Provider 2: yt-dlp
# ---------------------------------------------------------------------------
def from_ytdlp(url):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }
    if PROXY_URL:
        opts["proxy"] = PROXY_URL

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        candidates = info.get("formats") or [info]
        candidates = sorted(
            candidates,
            key=lambda f: (f.get("height") or 0, f.get("filesize") or f.get("filesize_approx") or 0),
            reverse=True,
        )

        formats, seen = [], set()
        for f in candidates:
            if not f.get("url") or f.get("vcodec") == "none":
                continue
            if (f.get("protocol") or "https") not in ("http", "https"):
                continue
            note = (f.get("format_note") or "").lower()
            if f.get("format_id") == "download" or "watermark" in note:
                label = "Original (watermark)"
            elif f.get("height"):
                label = f"{f['height']}p"
            else:
                label = str(f.get("format_id") or "Video")
            if label in seen:
                continue
            seen.add(label)

            headers = dict(f.get("http_headers") or info.get("http_headers") or {})
            cookie = ydl.cookiejar.get_cookie_header(f["url"])
            if cookie:
                headers["Cookie"] = cookie
            formats.append({
                "id": f"y{len(formats)}",
                "label": label,
                "kind": "video",
                "size": to_int(f.get("filesize") or f.get("filesize_approx")),
                "url": f["url"],
                "headers": headers,
                "proxy": bool(PROXY_URL),
            })

    return {
        "id": str(info.get("id") or "video"),
        "title": (info.get("title") or info.get("description") or "TikTok video").strip(),
        "author": info.get("uploader") or info.get("creator") or "",
        "handle": info.get("uploader_id") or info.get("channel") or "",
        "thumbnail": info.get("thumbnail"),
        "duration": to_int(info.get("duration")),
        "views": to_int(info.get("view_count")),
        "likes": to_int(info.get("like_count")),
        "comments": to_int(info.get("comment_count")),
        "shares": to_int(info.get("repost_count")),
        "created": iso_date(epoch=info.get("timestamp"), yyyymmdd=info.get("upload_date")),
        "formats": formats,
    }


PROVIDER_FUNCS = {"tikwm": from_tikwm, "ytdlp": from_ytdlp}


def resolve(url, fresh=False):
    if not fresh:
        hit = cache_get(url)
        if hit:
            return hit

    errors = []
    for name in PROVIDERS:
        fn = PROVIDER_FUNCS.get(name)
        if not fn:
            continue
        try:
            data = fn(url)
            if data and data["formats"]:
                cache_set(url, data)
                return data
            errors.append(f"{name}: no formats")
        except Exception as exc:  # noqa: BLE001 - we want to try the next provider
            app.logger.warning("provider %s failed for %s: %s", name, url, exc)
            errors.append(f"{name}: {exc}")
    raise ResolveError(" | ".join(errors) or "no provider configured")


def pick_format(data, fid):
    for f in data["formats"]:
        if f["id"] == fid:
            return f
    return data["formats"][0]


def open_upstream(fmt):
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if fmt.get("proxy") and PROXY_URL else None
    upstream = requests.get(
        fmt["url"],
        headers=fmt.get("headers") or {"User-Agent": UA},
        stream=True,
        timeout=30,
        proxies=proxies,
    )
    upstream.raise_for_status()
    return upstream


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
    url = extract_url(data.get("url"))
    if not is_tiktok_url(url):
        return error("That doesn't look like a TikTok link.", 400)

    try:
        info = resolve(url)
    except ResolveError as exc:
        app.logger.error("resolve failed for %s: %s", url, exc)
        return error(
            "Couldn't get this video. It may be private or removed, or the download service is busy. Try again in a moment.",
            422,
            exc,
        )
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("unexpected error for %s", url)
        return error("Something went wrong on our side. Try again.", 500, exc)

    return jsonify({
        **{k: v for k, v in info.items() if k != "formats"},
        "formats": [{"id": f["id"], "label": f["label"], "kind": f["kind"], "size": f["size"]}
                    for f in info["formats"]],
        "download": "/download?url=" + quote(url, safe=""),
    })


@app.get("/download")
def download():
    if rate_limited():
        return error("Too many requests. Wait a minute and try again.", 429)

    url = extract_url(request.args.get("url"))
    if not is_tiktok_url(url):
        return error("That doesn't look like a TikTok link.", 400)
    fid = (request.args.get("f") or "").strip()

    upstream = None
    try:
        info = resolve(url)
        fmt = pick_format(info, fid)
        upstream = open_upstream(fmt)
    except Exception as first_exc:  # noqa: BLE001 - links may have expired, retry once
        app.logger.warning("download first try failed for %s: %s", url, first_exc)
        try:
            info = resolve(url, fresh=True)
            fmt = pick_format(info, fid)
            upstream = open_upstream(fmt)
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("download failed for %s", url)
            return error("Couldn't download this video. Try again in a moment.", 502, exc)

    def stream():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    is_audio = fmt["kind"] == "audio"
    ext = "mp3" if is_audio else "mp4"
    default_type = "audio/mpeg" if is_audio else "video/mp4"
    filename = f"tiktok_{re.sub(r'[^A-Za-z0-9_-]', '', info['id'])}_{fmt['id']}.{ext}"

    resp = Response(stream(), mimetype=upstream.headers.get("Content-Type", default_type))
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    if upstream.headers.get("Content-Length"):
        resp.headers["Content-Length"] = upstream.headers["Content-Length"]
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests
import yt_dlp
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Config (all optional, set in Render > Environment)
# ---------------------------------------------------------------------------
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "12"))      # requests per window
RATE_WINDOW = int(os.environ.get("RATE_WINDOW", "60"))    # seconds
CACHE_TTL = int(os.environ.get("CACHE_TTL", "600"))       # seconds
PROXY_URL = os.environ.get("PROXY_URL") or None           # optional, used by yt-dlp
DEBUG_ERRORS = os.environ.get("DEBUG_ERRORS") == "1"      # show real error in the UI
# Order matters. Render's servers are datacenter IPs, which TikTok often blocks
# for yt-dlp, so "tikwm" goes first. Use PROVIDERS=ytdlp to disable the third party.
PROVIDERS = [p.strip() for p in os.environ.get("PROVIDERS", "tikwm,ytdlp").split(",") if p.strip()]

UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
URL_RE = re.compile(r"https?://[^\s]+")


class ResolveError(Exception):
    pass


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
_hits = defaultdict(deque)


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or request.remote_addr or "unknown"


def rate_limited():
    now = time.time()
    q = _hits[client_ip()]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def extract_url(text):
    """Accept a bare link or the whole share text and pull the link out."""
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(".,)") if match else ""


def is_tiktok_url(url):
    """Only real TikTok hosts, so the server can't be used as an open proxy."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme in ("http", "https") and (
        host == "tiktok.com" or host.endswith(".tiktok.com")
    )


def error(message, status, detail=None):
    body = {"error": message}
    if DEBUG_ERRORS and detail:
        body["detail"] = str(detail)[:300]
    return jsonify(body), status


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def iso_date(epoch=None, yyyymmdd=None):
    try:
        if epoch:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")
        if yyyymmdd and len(str(yyyymmdd)) == 8:
            s = str(yyyymmdd)
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    except (ValueError, OSError, OverflowError):
        pass
    return None


# ---------------------------------------------------------------------------
# Cache (links expire, so keep them only for a few minutes)
# ---------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if item and item[0] > time.time():
            return item[1]
        _cache.pop(key, None)
    return None


def cache_set(key, value):
    with _cache_lock:
        if len(_cache) > 200:
            now = time.time()
            for k in [k for k, (exp, _) in _cache.items() if exp < now]:
                _cache.pop(k, None)
            if len(_cache) > 200:
                _cache.clear()
        _cache[key] = (time.time() + CACHE_TTL, value)


# ---------------------------------------------------------------------------
# Provider 1: TikWM (works from datacenter IPs where yt-dlp usually fails)
# ---------------------------------------------------------------------------
_tikwm_lock = threading.Lock()
_tikwm_last = 0.0


def _abs(u):
    if not u:
        return None
    return u if u.startswith("http") else "https://www.tikwm.com" + u


def from_tikwm(url):
    global _tikwm_last
    with _tikwm_lock:  # the free API allows about 1 request per second
        wait = 1.2 - (time.time() - _tikwm_last)
        if wait > 0:
            time.sleep(wait)
        _tikwm_last = time.time()
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests
import yt_dlp
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Config (all optional, set in Render > Environment)
# ---------------------------------------------------------------------------
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "12"))      # requests per window
RATE_WINDOW = int(os.environ.get("RATE_WINDOW", "60"))    # seconds
CACHE_TTL = int(os.environ.get("CACHE_TTL", "600"))       # seconds
PROXY_URL = os.environ.get("PROXY_URL") or None           # optional, used by yt-dlp
DEBUG_ERRORS = os.environ.get("DEBUG_ERRORS") == "1"      # show real error in the UI
# Order matters. Render's servers are datacenter IPs, which TikTok often blocks
# for yt-dlp, so "tikwm" goes first. Use PROVIDERS=ytdlp to disable the third party.
PROVIDERS = [p.strip() for p in os.environ.get("PROVIDERS", "tikwm,ytdlp").split(",") if p.strip()]

UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
URL_RE = re.compile(r"https?://[^\s]+")


class ResolveError(Exception):
    pass


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
_hits = defaultdict(deque)


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or request.remote_addr or "unknown"


def rate_limited():
    now = time.time()
    q = _hits[client_ip()]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def extract_url(text):
    """Accept a bare link or the whole share text and pull the link out."""
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(".,)") if match else ""


def is_tiktok_url(url):
    """Only real TikTok hosts, so the server can't be used as an open proxy."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme in ("http", "https") and (
        host == "tiktok.com" or host.endswith(".tiktok.com")
    )


def error(message, status, detail=None):
    body = {"error": message}
    if DEBUG_ERRORS and detail:
        body["detail"] = str(detail)[:300]
    return jsonify(body), status


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def iso_date(epoch=None, yyyymmdd=None):
    try:
        if epoch:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")
        if yyyymmdd and len(str(yyyymmdd)) == 8:
            s = str(yyyymmdd)
            return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    except (ValueError, OSError, OverflowError):
        pass
    return None


# ---------------------------------------------------------------------------
# Cache (links expire, so keep them only for a few minutes)
# ---------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()


def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if item and item[0] > time.time():
            return item[1]
        _cache.pop(key, None)
    return None


def cache_set(key, value):
    with _cache_lock:
        if len(_cache) > 200:
            now = time.time()
            for k in [k for k, (exp, _) in _cache.items() if exp < now]:
                _cache.pop(k, None)
            if len(_cache) > 200:
                _cache.clear()
        _cache[key] = (time.time() + CACHE_TTL, value)


# ---------------------------------------------------------------------------
# Provider 1: TikWM (works from datacenter IPs where yt-dlp usually fails)
# ---------------------------------------------------------------------------
_tikwm_lock = threading.Lock()
_tikwm_last = 0.0


def _abs(u):
    if not u:
        return None
    return u if u.startswith("http") else "https://www.tikwm.com" + u


def from_tikwm(url):
    global _tikwm_last
    with _tikwm_lock:  # the free API allows about 1 request per second
        wait = 1.2 - (time.time() - _tikwm_last)
        if wait > 0:
            time.sleep(wait)
        _tikwm_last = time.time()

    r = requests.get(
        "https://www.tikwm.com/api/",
        params={"url": url, "hd": 1},
        headers={"User-Agent": UA},
        timeout=25,
    )
    r.raise_for_status()
    payload = r.json()
    d = payload.get("data")
    if payload.get("code") != 0 or not d:
        raise ResolveError(f"tikwm: {payload.get('msg') or 'no data'}")

    formats = []
    if d.get("hdplay"):
        formats.append({"id": "hd", "label": "HD", "kind": "video",
                        "size": to_int(d.get("hd_size")), "url": _abs(d["hdplay"])})
    if d.get("play"):
        formats.append({"id": "sd", "label": "Standard", "kind": "video",
                        "size": to_int(d.get("size")), "url": _abs(d["play"])})
    if d.get("wmplay"):
        formats.append({"id": "wm", "label": "With TikTok watermark", "kind": "video",
                        "size": to_int(d.get("wm_size")), "url": _abs(d["wmplay"])})
    if d.get("music"):
        formats.append({"id": "mp3", "label": "Audio only (MP3)", "kind": "audio",
                        "size": None, "url": _abs(d["music"])})

    author = d.get("author") or {}
    return {
        "id": str(d.get("id") or "video"),
        "title": (d.get("title") or "TikTok video").strip(),
        "author": author.get("nickname") or "",
        "handle": author.get("unique_id") or "",
        "thumbnail": _abs(d.get("origin_cover") or d.get("cover")),
        "duration": to_int(d.get("duration")),
        "views": to_int(d.get("play_count")),
        "likes": to_int(d.get("digg_count")),
        "comments": to_int(d.get("comment_count")),
        "shares": to_int(d.get("share_count")),
        "created": iso_date(epoch=d.get("create_time")),
        "formats": formats,
    }


# ---------------------------------------------------------------------------
# Provider 2: yt-dlp
# ---------------------------------------------------------------------------
def from_ytdlp(url):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }
    if PROXY_URL:
        opts["proxy"] = PROXY_URL

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        candidates = info.get("formats") or [info]
        candidates = sorted(
            candidates,
            key=lambda f: (f.get("height") or 0, f.get("filesize") or f.get("filesize_approx") or 0),
            reverse=True,
        )

        formats, seen = [], set()
        for f in candidates:
            if not f.get("url") or f.get("vcodec") == "none":
                continue
            if (f.get("protocol") or "https") not in ("http", "https"):
                continue
            note = (f.get("format_note") or "").lower()
            if f.get("format_id") == "download" or "watermark" in note:
                label = "Original (watermark)"
            elif f.get("height"):
                label = f"{f['height']}p"
            else:
                label = str(f.get("format_id") or "Video")
            if label in seen:
                continue
            seen.add(label)

            headers = dict(f.get("http_headers") or info.get("http_headers") or {})
            cookie = ydl.cookiejar.get_cookie_header(f["url"])
            if cookie:
                headers["Cookie"] = cookie
            formats.append({
                "id": f"y{len(formats)}",
                "label": label,
                "kind": "video",
                "size": to_int(f.get("filesize") or f.get("filesize_approx")),
                "url": f["url"],
                "headers": headers,
                "proxy": bool(PROXY_URL),
            })

    return {
        "id": str(info.get("id") or "video"),
        "title": (info.get("title") or info.get("description") or "TikTok video").strip(),
        "author": info.get("uploader") or info.get("creator") or "",
        "handle": info.get("uploader_id") or info.get("channel") or "",
        "thumbnail": info.get("thumbnail"),
        "duration": to_int(info.get("duration")),
        "views": to_int(info.get("view_count")),
        "likes": to_int(info.get("like_count")),
        "comments": to_int(info.get("comment_count")),
        "shares": to_int(info.get("repost_count")),
        "created": iso_date(epoch=info.get("timestamp"), yyyymmdd=info.get("upload_date")),
        "formats": formats,
    }


PROVIDER_FUNCS = {"tikwm": from_tikwm, "ytdlp": from_ytdlp}


def resolve(url, fresh=False):
    if not fresh:
        hit = cache_get(url)
        if hit:
            return hit

    errors = []
    for name in PROVIDERS:
        fn = PROVIDER_FUNCS.get(name)
        if not fn:
            continue
        try:
            data = fn(url)
            if data and data["formats"]:
                cache_set(url, data)
                return data
            errors.append(f"{name}: no formats")
        except Exception as exc:  # noqa: BLE001 - we want to try the next provider
            app.logger.warning("provider %s failed for %s: %s", name, url, exc)
            errors.append(f"{name}: {exc}")
    raise ResolveError(" | ".join(errors) or "no provider configured")


def pick_format(data, fid):
    for f in data["formats"]:
        if f["id"] == fid:
            return f
    return data["formats"][0]


def open_upstream(fmt):
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if fmt.get("proxy") and PROXY_URL else None
    upstream = requests.get(
        fmt["url"],
        headers=fmt.get("headers") or {"User-Agent": UA},
        stream=True,
        timeout=30,
        proxies=proxies,
    )
    upstream.raise_for_status()
    return upstream


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
    url = extract_url(data.get("url"))
    if not is_tiktok_url(url):
        return error("That doesn't look like a TikTok link.", 400)

    try:
        info = resolve(url)
    except ResolveError as exc:
        app.logger.error("resolve failed for %s: %s", url, exc)
        return error(
            "Couldn't get this video. It may be private or removed, or the download service is busy. Try again in a moment.",
            422,
            exc,
        )
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("unexpected error for %s", url)
        return error("Something went wrong on our side. Try again.", 500, exc)

    return jsonify({
        **{k: v for k, v in info.items() if k != "formats"},
        "formats": [{"id": f["id"], "label": f["label"], "kind": f["kind"], "size": f["size"]}
                    for f in info["formats"]],
        "download": "/download?url=" + quote(url, safe=""),
    })


@app.get("/download")
def download():
    if rate_limited():
        return error("Too many requests. Wait a minute and try again.", 429)

    url = extract_url(request.args.get("url"))
    if not is_tiktok_url(url):
        return error("That doesn't look like a TikTok link.", 400)
    fid = (request.args.get("f") or "").strip()

    upstream = None
    try:
        info = resolve(url)
        fmt = pick_format(info, fid)
        upstream = open_upstream(fmt)
    except Exception as first_exc:  # noqa: BLE001 - links may have expired, retry once
        app.logger.warning("download first try failed for %s: %s", url, first_exc)
        try:
            info = resolve(url, fresh=True)
            fmt = pick_format(info, fid)
            upstream = open_upstream(fmt)
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("download failed for %s", url)
            return error("Couldn't download this video. Try again in a moment.", 502, exc)

    def stream():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    is_audio = fmt["kind"] == "audio"
    ext = "mp3" if is_audio else "mp4"
    default_type = "audio/mpeg" if is_audio else "video/mp4"
    filename = f"tiktok_{re.sub(r'[^A-Za-z0-9_-]', '', info['id'])}_{fmt['id']}.{ext}"

    resp = Response(stream(), mimetype=upstream.headers.get("Content-Type", default_type))
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    if upstream.headers.get("Content-Length"):
        resp.headers["Content-Length"] = upstream.headers["Content-Length"]
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
