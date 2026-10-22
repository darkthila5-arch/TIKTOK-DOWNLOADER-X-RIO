import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests
import yt_dlp
from flask import Flask, Response, jsonify, render_template, request, send_file

try:
    from curl_cffi import requests as cffi  # Chrome-like TLS fingerprint
except ImportError:  # the app still starts, the "own" provider just reports it
    cffi = None

try:
    from PIL import Image, ImageDraw, ImageFont  # draws the watermark picture
except ImportError:
    Image = None
try:
    import imageio_ffmpeg  # ships a ready-made ffmpeg binary
except ImportError:
    imageio_ffmpeg = None

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
# Order matters: "own" is our own scraper, "tikwm" a third-party API, "ytdlp" yt-dlp.
# Example: PROVIDERS=own,ytdlp to never use the third-party API.
PROVIDERS = [p.strip() for p in os.environ.get("PROVIDERS", "own,tikwm,ytdlp").split(",") if p.strip()]
IMPERSONATE = os.environ.get("IMPERSONATE", "chrome")

# Watermark (needs Pillow + ffmpeg, both installed by requirements.txt)
WATERMARK_ENABLED = os.environ.get("WATERMARK", "1") != "0"
WATERMARK_TEXT = os.environ.get("WATERMARK_TEXT", "TIKTOK DOWNLOADER x RIO")
FFMPEG_PRESET = os.environ.get("FFMPEG_PRESET", "ultrafast")  # faster = bigger file
FFMPEG_TIMEOUT = int(os.environ.get("FFMPEG_TIMEOUT", "240"))  # seconds
MAX_SOURCE_MB = int(os.environ.get("MAX_SOURCE_MB", "80"))
MAX_ACTIVE_JOBS = int(os.environ.get("MAX_ACTIVE_JOBS", "5"))
FILE_TTL = 600   # how long a finished watermarked file stays downloadable
JOB_TTL = 900

# Live download counter. Set the two Upstash variables to keep the number
# forever; without them it is saved to a temp file that Render wipes on restart.
UPSTASH_URL = (os.environ.get("UPSTASH_REDIS_REST_URL") or "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or ""
COUNTER_KEY = os.environ.get("COUNTER_KEY", "tt_downloads")
COUNTER_FILE = os.environ.get("COUNTER_FILE", os.path.join(tempfile.gettempdir(), "tt_counter.json"))

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


# ---------------------------------------------------------------------------
# Provider 0: our own scraper (reads the data TikTok embeds in the video page)
# ---------------------------------------------------------------------------
PAGE_HEADERS = {"Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.tiktok.com/"}
DATA_RE = re.compile(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)


def cffi_proxies():
    return {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None


def parse_item(html):
    """Pull the video object out of the JSON blob TikTok puts in the page."""
    match = DATA_RE.search(html or "")
    if not match:
        raise ResolveError("own: page had no video data (TikTok probably served a block page)")
    try:
        data = json.loads(match.group(1))
    except ValueError:
        raise ResolveError("own: video data was not valid JSON")
    detail = (data.get("__DEFAULT_SCOPE__") or {}).get("webapp.video-detail") or {}
    code = detail.get("statusCode")
    if code not in (None, 0):
        raise ResolveError(f"own: TikTok status {code} {detail.get('statusMsg') or ''}".strip())
    item = (detail.get("itemInfo") or {}).get("itemStruct")
    if not item:
        raise ResolveError("own: video not found in page")
    return item


def quality_of(bitrate_info, addr):
    """TikTok names quality by the shorter side, e.g. 720p for 720x1280."""
    w, h = addr.get("Width") or 0, addr.get("Height") or 0
    short = min(w, h) if w and h else 0
    if not short:
        m = re.search(r"_(\d{3,4})_", bitrate_info.get("GearName") or "")
        short = int(m.group(1)) if m else 0
    return short


def own_data(item, cookies):
    video = item.get("video") or {}
    if item.get("imagePost") and not video.get("playAddr"):
        raise ResolveError("own: this is a photo post, not a video")

    media = {
        "headers": {"Referer": "https://www.tiktok.com/"},
        "cookies": cookies,
        "impersonate": True,
        "proxy": bool(PROXY_URL),
    }

    options = []
    for b in video.get("bitrateInfo") or []:
        addr = b.get("PlayAddr") or {}
        urls = addr.get("UrlList") or []
        if not urls:
            continue
        codec = (b.get("CodecType") or "").lower()
        options.append((quality_of(b, addr), "h264" in codec, b.get("Bitrate") or 0, addr, urls[0]))
    # best quality first; within the same quality prefer h264 (plays everywhere)
    options.sort(key=lambda o: (o[0], o[1], o[2]), reverse=True)

    formats, seen = [], set()
    for short, _, _, addr, url in options:
        label = f"{short}p" if short else "Video"
        if label in seen:
            continue
        seen.add(label)
        formats.append({"id": f"o{len(formats)}", "label": label, "kind": "video",
                        "size": to_int(addr.get("DataSize")), "url": url, **media})

    play, dl = video.get("playAddr"), video.get("downloadAddr")
    if not formats and (play or dl):
        formats.append({"id": "o0", "label": video.get("definition") or "Standard", "kind": "video",
                        "size": None, "url": play or dl, **media})
    if dl and all(f["url"] != dl for f in formats):
        formats.append({"id": f"o{len(formats)}", "label": "Original (download link)", "kind": "video",
                        "size": None, "url": dl, **media})
    if not formats:
        raise ResolveError("own: no video links in page")

    audio = (item.get("music") or {}).get("playUrl")
    if audio:
        formats.append({"id": "mp3", "label": "Audio only", "kind": "audio",
                        "size": None, "url": audio, **media})

    stats = item.get("stats") or {}
    author = item.get("author") or {}
    return {
        "id": str(item.get("id") or "video"),
        "title": (item.get("desc") or "TikTok video").strip(),
        "author": author.get("nickname") or "",
        "handle": author.get("uniqueId") or "",
        "thumbnail": video.get("originCover") or video.get("cover") or video.get("dynamicCover"),
        "duration": to_int(video.get("duration")),
        "views": to_int(stats.get("playCount")),
        "likes": to_int(stats.get("diggCount")),
        "comments": to_int(stats.get("commentCount")),
        "shares": to_int(stats.get("shareCount")),
        "created": iso_date(epoch=item.get("createTime")),
        "formats": formats,
    }


def from_own(url):
    if cffi is None:
        raise ResolveError("own: curl_cffi is not installed")
    sess = cffi.Session(impersonate=IMPERSONATE, proxies=cffi_proxies())
    try:
        r = sess.get(url, headers=PAGE_HEADERS, timeout=25, allow_redirects=True)
        if r.status_code >= 400:
            raise ResolveError(f"own: TikTok returned HTTP {r.status_code}")
        try:
            item = parse_item(r.text)
        except ResolveError:
            # second try through the generic video page, which is sometimes less strict
            m = re.search(r"/(?:video|photo)/(\d+)", str(r.url))
            if not m:
                raise
            r = sess.get(f"https://www.tiktok.com/@i/video/{m.group(1)}",
                         headers=PAGE_HEADERS, timeout=25)
            item = parse_item(r.text)
        cookies = sess.cookies.get_dict()
    finally:
        sess.close()
    return own_data(item, cookies)


PROVIDER_FUNCS = {"own": from_own, "tikwm": from_tikwm, "ytdlp": from_ytdlp}


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
    if fmt.get("impersonate"):
        if cffi is None:
            raise ResolveError("curl_cffi is not installed")
        upstream = cffi.get(
            fmt["url"],
            headers={**(fmt.get("headers") or {}), "Range": "bytes=0-"},
            cookies=fmt.get("cookies") or None,
            impersonate=IMPERSONATE,
            stream=True,
            timeout=30,
            proxies=cffi_proxies() if fmt.get("proxy") else None,
        )
        upstream.raise_for_status()
        return upstream

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


def fmt_duration(seconds):
    if not seconds:
        return "-"
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_count(n):
    return f"{n:,}" if isinstance(n, int) else "-"


def build_details(info, url):
    """A plain-text sheet with the video's details (UTF-8, works with any language)."""
    tags = re.findall(r"#[^\s#]+", info["title"])
    who = info["author"] or "-"
    if info["handle"]:
        who += f" (@{info['handle']})"
    lines = [
        "TikTok video details",
        "====================",
        "",
        f"Caption : {info['title']}",
        f"Creator : {who}",
        f"Posted  : {info['created'] or '-'}",
        f"Length  : {fmt_duration(info['duration'])}",
        "",
        f"Views    : {fmt_count(info['views'])}",
        f"Likes    : {fmt_count(info['likes'])}",
        f"Comments : {fmt_count(info['comments'])}",
        f"Shares   : {fmt_count(info['shares'])}",
        "",
        f"Hashtags: {' '.join(tags) if tags else '-'}",
        f"Link    : {url}",
        "",
        "Saved with TikTok Downloader x Rio",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Live download counter
# ---------------------------------------------------------------------------
_count = 0
_count_lock = threading.Lock()
_persist_lock = threading.Lock()


def _upstash(path):
    r = requests.get(f"{UPSTASH_URL}/{path}", headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=5)
    r.raise_for_status()
    return r.json().get("result")


def load_count():
    global _count
    n = 0
    try:
        if UPSTASH_URL and UPSTASH_TOKEN:
            n = to_int(_upstash(f"get/{COUNTER_KEY}")) or 0
        elif os.path.exists(COUNTER_FILE):
            with open(COUNTER_FILE) as fh:
                n = to_int(json.load(fh).get("downloads")) or 0
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("could not load the download counter: %s", exc)
    with _count_lock:
        _count = max(_count, n)


def _persist_count(value):
    global _count
    try:
        with _persist_lock:
            if UPSTASH_URL and UPSTASH_TOKEN:
                n = to_int(_upstash(f"incr/{COUNTER_KEY}"))
                if n:
                    with _count_lock:
                        _count = max(_count, n)
            else:
                tmp = COUNTER_FILE + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump({"downloads": value}, fh)
                os.replace(tmp, COUNTER_FILE)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("could not save the download counter: %s", exc)


def bump_count():
    global _count
    with _count_lock:
        _count += 1
        value = _count
    threading.Thread(target=_persist_count, args=(value,), daemon=True).start()


def is_first_request():
    """Browsers sometimes re-request a file in ranges; count only the first."""
    rng = request.headers.get("Range", "")
    return request.method == "GET" and (not rng or rng.startswith("bytes=0-"))


load_count()


# ---------------------------------------------------------------------------
# Watermark: download the video, stamp the site name on it with ffmpeg
# ---------------------------------------------------------------------------
_ffmpeg = None
_wm_sem = threading.Semaphore(1)  # one ffmpeg at a time keeps the free plan alive
_jobs, _jobs_lock = {}, threading.Lock()
_files, _files_lock = {}, threading.Lock()


def ffmpeg_path():
    global _ffmpeg
    if _ffmpeg is None:
        found = shutil.which("ffmpeg")
        if not found and imageio_ffmpeg:
            try:
                found = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:  # noqa: BLE001
                found = None
        _ffmpeg = found or ""
    return _ffmpeg or None


def watermark_ready():
    return WATERMARK_ENABLED and Image is not None and ffmpeg_path() is not None


def make_watermark_png(path, video_width):
    """A small black label with the site name in white, sized to the video."""
    target = max(150, int(video_width * 0.40))
    size = 40
    font = ImageFont.load_default(size=size)
    box = font.getbbox(WATERMARK_TEXT)
    size = max(12, int(size * (target * 0.86) / max(1, box[2] - box[0])))
    font = ImageFont.load_default(size=size)
    box = font.getbbox(WATERMARK_TEXT)
    tw, th = box[2] - box[0], box[3] - box[1]
    pad = int(size * 0.5)
    w, h = tw + 2 * pad, th + 2 * pad
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=(0, 0, 0, 170))
    draw.text((pad - box[0], pad - box[1]), WATERMARK_TEXT, font=font, fill=(255, 255, 255, 240))
    img.save(path)


def probe_size(path):
    p = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", path],
                       capture_output=True, text=True, timeout=30)
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", p.stderr)
    if not m:
        raise RuntimeError("could not read the video size")
    return int(m.group(1)), int(m.group(2))


def run_ffmpeg(cmd, duration, on_progress):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timer = threading.Timer(FFMPEG_TIMEOUT, proc.kill)
    timer.start()
    try:
        for line in proc.stdout:
            if duration and line.startswith("out_time_ms="):
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                on_progress(max(0, min(99, int(done / duration * 100))))
        proc.wait()
        err = proc.stderr.read()
    finally:
        timer.cancel()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + (err or "")[-300:])


def purge_files():
    now = time.time()
    with _files_lock:
        for token in [t for t, f in _files.items() if f["exp"] < now]:
            shutil.rmtree(_files.pop(token)["dir"], ignore_errors=True)
        while len(_files) > 20:
            oldest = min(_files, key=lambda t: _files[t]["exp"])
            shutil.rmtree(_files.pop(oldest)["dir"], ignore_errors=True)


def purge_jobs():
    now = time.time()
    with _jobs_lock:
        for jid in [j for j, v in _jobs.items() if now - v["created"] > JOB_TTL]:
            _jobs.pop(jid, None)


def register_file(path, name, workdir):
    purge_files()
    token = secrets.token_urlsafe(16)
    with _files_lock:
        _files[token] = {"path": path, "name": name, "dir": workdir, "exp": time.time() + FILE_TTL}
    return token


def run_watermark_job(job_id, url, fid):
    job = _jobs[job_id]
    workdir = tempfile.mkdtemp(prefix="wm_")
    got_slot = False
    try:
        if not _wm_sem.acquire(timeout=180):
            raise ResolveError("The server is busy. Try again in a minute.")
        got_slot = True

        job["stage"] = "downloading"
        info, fmt, upstream = open_format(url, fid)
        if fmt["kind"] != "video":
            upstream.close()
            raise ResolveError("Only videos can get a watermark.")

        src = os.path.join(workdir, "source.mp4")
        size = 0
        try:
            with open(src, "wb") as fh:
                for chunk in upstream.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_SOURCE_MB * 1024 * 1024:
                        raise ResolveError("This video is too large to add a watermark.")
                    fh.write(chunk)
        finally:
            upstream.close()

        job["stage"] = "watermarking"
        width, _ = probe_size(src)
        wm_png = os.path.join(workdir, "watermark.png")
        make_watermark_png(wm_png, width)
        margin = max(10, int(width * 0.04))
        out = os.path.join(workdir, "output.mp4")
        cmd = [
            ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-y",
            "-i", src, "-i", wm_png,
            "-filter_complex", f"[0:v][1:v]overlay=W-w-{margin}:H-h-{margin}[v]",
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", "28", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", out,
        ]
        run_ffmpeg(cmd, info.get("duration"), lambda pct: job.update(percent=pct))

        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", info["id"])
        token = register_file(out, f"tiktok_{safe_id}_rio.mp4", workdir)
        workdir = None  # the file registry owns the folder from now on
        job.update(state="done", stage="done", percent=100, file=f"/file/{token}")
    except ResolveError as exc:
        job.update(state="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("watermark job failed for %s", url)
        msg = "Couldn't add the watermark. Try again, or switch it off to get the original video."
        job.update(state="error", error=msg + (f" ({str(exc)[:200]})" if DEBUG_ERRORS else ""))
    finally:
        if got_slot:
            _wm_sem.release()
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def open_format(url, fid):
    """Find the link for this option and open it; retry once with fresh links."""
    last = None
    for fresh in (False, True):
        try:
            info = resolve(url, fresh=fresh)
            fmt = pick_format(info, fid)
            return info, fmt, open_upstream(fmt)
        except Exception as exc:  # noqa: BLE001
            last = exc
            app.logger.warning("open_format try failed for %s: %s", url, exc)
    raise last


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
                    for f in info["formats"]]
                   + [{"id": "doc", "label": "Video details (TXT)", "kind": "document", "size": None}],
        "download": "/download?url=" + quote(url, safe=""),
        "watermark": watermark_ready(),
    })


@app.get("/download")
def download():
    if rate_limited():
        return error("Too many requests. Wait a minute and try again.", 429)

    url = extract_url(request.args.get("url"))
    if not is_tiktok_url(url):
        return error("That doesn't look like a TikTok link.", 400)
    fid = (request.args.get("f") or "").strip()

    if fid == "doc":
        try:
            info = resolve(url)
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("details failed for %s", url)
            return error("Couldn't get the video details. Try again in a moment.", 502, exc)
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", info["id"])
        if is_first_request():
            bump_count()
        resp = Response(build_details(info, url), mimetype="text/plain")
        resp.headers["Content-Disposition"] = f'attachment; filename="tiktok_{safe_id}_details.txt"'
        return resp

    try:
        info, fmt, upstream = open_format(url, fid)
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("download failed for %s", url)
        return error("Couldn't download this video. Try again in a moment.", 502, exc)
    if is_first_request():
        bump_count()

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


@app.get("/api/stats")
def api_stats():
    resp = jsonify({"downloads": _count})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/prepare")
def api_prepare():
    if rate_limited():
        return error("Too many requests. Wait a minute and try again.", 429)
    if not watermark_ready():
        return error("Watermarking isn't available on this server.", 501)

    data = request.get_json(silent=True) or {}
    url = extract_url(data.get("url"))
    fid = str(data.get("f") or "").strip()
    if not is_tiktok_url(url):
        return error("That doesn't look like a TikTok link.", 400)

    purge_jobs()
    with _jobs_lock:
        active = sum(1 for j in _jobs.values() if j["state"] == "working")
        if active >= MAX_ACTIVE_JOBS:
            return error("Lots of videos are being processed right now. Try again in a minute.", 503)
        job_id = secrets.token_urlsafe(8)
        _jobs[job_id] = {"state": "working", "stage": "queued", "percent": 0, "created": time.time()}
    threading.Thread(target=run_watermark_job, args=(job_id, url, fid), daemon=True).start()
    return jsonify({"job": job_id})


@app.get("/api/job/<job_id>")
def api_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        return error("This job has expired. Start the download again.", 404)
    resp = jsonify({k: job.get(k) for k in ("state", "stage", "percent", "file", "error")})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/file/<token>")
def serve_file(token):
    purge_files()
    with _files_lock:
        item = _files.get(token)
    if not item or not os.path.exists(item["path"]):
        return error("This download link has expired. Start the download again.", 404)
    if is_first_request():
        bump_count()
    return send_file(item["path"], as_attachment=True, download_name=item["name"],
                     mimetype="video/mp4", max_age=0)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
