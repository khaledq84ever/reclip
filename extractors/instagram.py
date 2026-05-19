"""Native Instagram extractor — snapsave.app primary + yt-dlp fallback.

Ported from the standalone Instagram downloader (`/home/khaled/instagram downloader/app.py`).
Same trick as snaptik on the TikTok side: snapsave.app's obfuscated eval()
response gets decoded into HTML, then media URLs are pulled out.
"""

import os
import re
import json
import shutil
import subprocess
import urllib.parse
import uuid
from html import unescape as _html_unescape

import requests as req_lib


_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)
_HEADERS = {
    "User-Agent":      _MOBILE_UA,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}

_IG_RE = re.compile(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
_SNAPSAVE_EVAL_RE = re.compile(
    r'\("([^"]+)",\s*\d+\s*,\s*"([^"]+)"\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*\d+\)\)\s*$'
)
_SNAPSAVE_CHARSET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/"


def is_valid_url(url):
    return bool(_IG_RE.search(url))


def extract_shortcode(url):
    m = _IG_RE.search(url)
    return m.group(1) if m else None


def _safe_filename(title, ext):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#@]', "", title or "instagram").strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:80] or "instagram") + "." + ext


def _find_ffmpeg():
    return shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


# ── snapsave.app decoder ──────────────────────────────────────────────────────

def _snapsave_decode(js_body):
    m = _SNAPSAVE_EVAL_RE.search(js_body.strip())
    if not m:
        return None
    h, n, t, e = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
    if e <= 0 or e > len(_SNAPSAVE_CHARSET) or e >= len(n):
        return None
    sep = n[e]
    out = []
    i = 0
    L = len(h)
    while i < L:
        s = ""
        while i < L and h[i] != sep:
            s += h[i]
            i += 1
        i += 1
        digits = "".join(str(n.find(c)) for c in s if 0 <= n.find(c) < e)
        if digits:
            try:
                out.append(chr(int(digits, e) - t))
            except (ValueError, OverflowError):
                pass
    raw = "".join(out)
    try:
        return urllib.parse.unquote(raw)
    except Exception:
        return raw


def _unescape_js_string(s):
    return (s.replace("\\/", "/").replace('\\"', '"')
             .replace("\\'", "'").replace("\\\\", "\\"))


def _parse_snapsave_html(decoded):
    if not decoded:
        return None
    if "Unable to" in decoded or "cannot be downloaded" in decoded.lower():
        return None
    inner_m = re.search(r'innerHTML\s*=\s*"((?:\\.|[^"\\])*)"', decoded, re.DOTALL)
    html = _unescape_js_string(inner_m.group(1)) if inner_m else decoded
    blocks = re.findall(
        r'<div[^>]*class="[^"]*download-items\b(?!__)[^"]*"[^>]*>(.*?)'
        r'(?=<div[^>]*class="[^"]*download-items\b(?!__)|</section>|$)',
        html, re.DOTALL)
    if not blocks:
        blocks = [html]
    videos, images, thumbs = [], [], []
    for blk in blocks:
        href_m = re.search(r'<a[^>]+href="([^"]+)"', blk)
        img_m = re.search(r'<img[^>]+src="([^"]+)"', blk)
        is_video = "icon-dlvideo" in blk
        href = _html_unescape(href_m.group(1)) if href_m else ""
        thumb = _html_unescape(img_m.group(1)) if img_m else ""
        if not href:
            continue
        (videos if is_video else images).append(href)
        if thumb:
            thumbs.append(thumb)
    if not videos and not images:
        urls = re.findall(r"https?://[^\s\"'<>]+", html)
        for u in urls:
            if "rapidcdn.app/v2" in u or "rapidcdn.app/video" in u:
                videos.append(u)
            elif "rapidcdn.app" in u and "thumb" not in u:
                images.append(u)
        thumbs = [u for u in urls if "rapidcdn.app/thumb" in u or ".jpg" in u]
    if not videos and not images:
        return None
    cap_m = (re.search(r'<p[^>]*class="[^"]*caption[^"]*"[^>]*>([^<]+)<', html, re.I) or
             re.search(r"<h\d[^>]*>([^<]+)</h\d>", html) or
             re.search(r'alt="([^"]{8,})"', html))
    title = _html_unescape(cap_m.group(1).strip())[:120] if cap_m else "Instagram Post"
    if title.lower().startswith("download instagram"):
        title = "Instagram Post"
    thumb_url = thumbs[0] if thumbs else (images[0] if images else "")
    if videos:
        return {"video_url": videos[0], "thumb_url": thumb_url, "title": title,
                "uploader": "", "is_video": True}
    return {"video_url": "", "thumb_url": images[0], "title": title,
            "uploader": "", "is_video": False}


# ── snapinsta.to (different operator from snapsave.app, separate proxy pool) ──
# Discovered 2026-05-19 when snapsave was down. Flow:
#   1) GET /en2 (cloudscraper handles CF, warms cookies)
#   2) POST /api/userverify {url} → {success, token: "JWT...."}
#   3) POST /api/ajaxSearch {q, t, v, lang, cftoken, html} → HTML payload
#      with the same .download-items structure as snapsave (icon-dlvideo /
#      icon-dlimage). Final media URLs come back as dl.snapcdn.app/get?token=
#      proxies — snapinsta's CDN un-wraps to the real IG URL.

def _snapinsta_fetch(url):
    """snapinsta.to: warm /en2 → POST /api/userverify (JWT) → POST /api/ajaxSearch
    (with `cftoken=<JWT>`). Uses curl_cffi with chrome124 TLS impersonation —
    cloudscraper's pure-Python challenge solver doesn't pass snapinsta's
    Cloudflare WAF from datacenter IPs, but curl_cffi's real-Chrome TLS
    fingerprint does."""
    try:
        from curl_cffi import requests as cf_req
    except ImportError:
        return None, "curl_cffi not installed"

    try:
        sess = cf_req.Session(impersonate="chrome124")

        r = sess.get("https://snapinsta.to/en2", timeout=20)
        if r.status_code != 200:
            return None, f"snapinsta home HTTP {r.status_code}"

        r = sess.post(
            "https://snapinsta.to/api/userverify",
            data={"url": url},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://snapinsta.to",
                "Referer": "https://snapinsta.to/en2",
                "Accept": "application/json, text/plain, */*",
            }, timeout=20)
        if r.status_code != 200:
            return None, f"snapinsta verify HTTP {r.status_code}"
        try:
            token = (r.json() or {}).get("token")
        except Exception:
            return None, "snapinsta verify: non-JSON"
        if not token:
            return None, "snapinsta verify: no token"

        r2 = sess.post(
            "https://snapinsta.to/api/ajaxSearch",
            data={"q": url, "t": "media", "v": "7", "lang": "en",
                  "cftoken": token, "html": ""},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://snapinsta.to",
                "Referer": "https://snapinsta.to/en2",
                "Accept": "*/*",
            }, timeout=30)
        if r2.status_code != 200:
            return None, f"snapinsta search HTTP {r2.status_code}"
        try:
            body = r2.json()
        except Exception:
            return None, "snapinsta search: non-JSON"

        if body.get("status") != "ok":
            return None, body.get("mess", "snapinsta error")
        html = body.get("data") or ""
        if not html:
            return None, body.get("mess") or "snapinsta: empty data"

        parsed = _parse_snapsave_html(html)
        if not parsed:
            return None, "snapinsta: could not parse download HTML"
        return parsed, None
    except Exception as e:
        return None, f"snapinsta error: {e}"


def _snapsave_fetch(url, attempts=3):
    """Retry snapsave up to N times. Their backend pool rotates — when one
    proxy can't reach IG, a retry seconds later often hits a different one."""
    import time as _time
    last_err = None
    for i in range(attempts):
        try:
            try:
                import cloudscraper
                sess = cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "windows", "mobile": False})
            except ImportError:
                sess = req_lib.Session()
                sess.headers.update({"User-Agent":
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
            sess.get("https://snapsave.app/", timeout=15)
            r = sess.post(
                "https://snapsave.app/action.php?lang=en",
                data={"url": url},
                headers={"Origin": "https://snapsave.app",
                         "Referer": "https://snapsave.app/",
                         "X-Requested-With": "XMLHttpRequest",
                         "Accept": "*/*"},
                timeout=25)
            if r.status_code != 200 or not r.text:
                last_err = f"snapsave HTTP {r.status_code}"
            else:
                decoded = _snapsave_decode(r.text)
                if not decoded:
                    last_err = "Could not decode snapsave response."
                elif "Unable to connect" in decoded or '"error_' in decoded:
                    last_err = "snapsave's proxy pool currently can't reach Instagram."
                else:
                    parsed = _parse_snapsave_html(decoded)
                    if parsed:
                        return parsed, None
                    last_err = "No download links in snapsave response."
        except Exception as e:
            last_err = f"snapsave error: {e}"
        if i < attempts - 1:
            _time.sleep(4 + i * 3)  # 4s, 7s, 10s backoff
    return None, last_err or "snapsave failed."


def _ytdlp_fetch(url, cookies_file=None):
    try:
        # --impersonate chrome (via curl_cffi) is required on Railway/datacenter IPs:
        # Instagram's anti-bot inspects TLS fingerprint + HTTP/2 frame ordering.
        cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--no-playlist",
               "--impersonate", "chrome", url]
        if cookies_file and os.path.exists(cookies_file):
            cmd += ["--cookies", cookies_file]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return {
                "video_url": data.get("url", ""),
                "thumb_url": data.get("thumbnail", ""),
                "title":     data.get("title", "Instagram Post"),
                "uploader":  data.get("uploader", "") or data.get("channel", ""),
                "is_video":  data.get("ext", "") in ("mp4", "mov", "webm"),
            }, None
        return None, (result.stderr.strip()[:200] or "yt-dlp returned no data")
    except subprocess.TimeoutExpired:
        return None, "yt-dlp timed out"
    except Exception as e:
        return None, f"yt-dlp error: {e}"


# ── Direct Instagram public endpoints (no third-party site) ──────────────────
# Two well-known tricks for fetching public IG posts without login:
#   1) /api/v1/media/<media_id>/info/ with X-IG-App-ID header (mobile-app proxy)
#   2) GraphQL query_hash for shortcode_media (public iframe data)
# These work from datacenter IPs when the post is public — no proxy, no cookies.

_IG_APP_ID = "936619743392459"   # public web App ID (same one ig.com sends)
_IG_ASBD_ID = "129477"
_IG_ALPHA = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "abcdefghijklmnopqrstuvwxyz0123456789-_")


def _shortcode_to_media_id(sc):
    n = 0
    for ch in sc:
        i = _IG_ALPHA.find(ch)
        if i < 0:
            return None
        n = n * 64 + i
    return n


def _ig_extract_from_media_obj(m):
    """Normalize IG's `xdt_shortcode_media` / `items[0]` object → reclip dict."""
    if not m:
        return None
    # video_versions[].url or video_url; image_versions2 has display thumb
    video_url = ""
    if m.get("video_versions"):
        # Sorted high→low quality; first is best
        video_url = m["video_versions"][0].get("url") or ""
    elif m.get("video_url"):
        video_url = m["video_url"]

    thumb = ""
    iv = m.get("image_versions2") or {}
    if iv.get("candidates"):
        thumb = iv["candidates"][0].get("url") or ""
    elif m.get("display_url"):
        thumb = m["display_url"]
    elif m.get("thumbnail_url"):
        thumb = m["thumbnail_url"]

    is_video = bool(video_url) or bool(m.get("is_video"))
    user = (m.get("user") or {}).get("username") \
           or (m.get("owner") or {}).get("username") \
           or ""
    caption_obj = m.get("caption") or (m.get("edge_media_to_caption", {})
                                       .get("edges", [{}])[0]
                                       .get("node", {}) if m.get("edge_media_to_caption") else {})
    if isinstance(caption_obj, dict):
        caption = caption_obj.get("text", "")
    else:
        caption = str(caption_obj or "")
    title = (caption or "Instagram Post").strip()[:120] or "Instagram Post"

    if not video_url and not thumb:
        return None
    return {
        "video_url": video_url,
        "thumb_url": thumb,
        "title":     title,
        "uploader":  user,
        "is_video":  is_video,
    }


def _ig_web_api(shortcode):
    """Hit IG's web v1 endpoint: /api/v1/media/<id>/info/ with X-IG-App-ID.
    No login required for public posts. Works from datacenter IPs."""
    mid = _shortcode_to_media_id(shortcode)
    if not mid:
        return None, "Could not encode shortcode."
    try:
        try:
            import cloudscraper
            sess = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False})
        except ImportError:
            sess = req_lib.Session()
        hdrs = {
            "User-Agent":
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept":          "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "X-IG-App-ID":     _IG_APP_ID,
            "X-ASBD-ID":       _IG_ASBD_ID,
            "X-IG-WWW-Claim":  "0",
            "X-Requested-With": "XMLHttpRequest",
            "Origin":   "https://www.instagram.com",
            "Referer":  f"https://www.instagram.com/p/{shortcode}/",
        }
        r = sess.get(
            f"https://www.instagram.com/api/v1/media/{mid}/info/",
            headers=hdrs, timeout=20)
        if r.status_code != 200:
            return None, f"IG web v1 HTTP {r.status_code}"
        try:
            d = r.json()
        except Exception:
            return None, "IG web v1: non-JSON response (likely login wall)"
        items = d.get("items") or []
        if not items:
            return None, "IG web v1: no items"
        info = _ig_extract_from_media_obj(items[0])
        if not info:
            return None, "IG web v1: could not extract media"
        return info, None
    except Exception as e:
        return None, f"IG web v1 error: {e}"


def _ig_graphql(shortcode):
    """Older public GraphQL endpoint — falls back when v1 is rate-limited."""
    try:
        sess = req_lib.Session()
        hdrs = {
            "User-Agent":
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "*/*",
            "X-IG-App-ID":  _IG_APP_ID,
            "X-Requested-With": "XMLHttpRequest",
            "Origin":  "https://www.instagram.com",
            "Referer": f"https://www.instagram.com/p/{shortcode}/",
        }
        # The "PolarisPostActionLoadPostQueryQuery" GraphQL doc shipped publicly.
        # Fall back to plain shortcode lookup which returns shortcode_media.
        r = sess.get(
            "https://www.instagram.com/graphql/query/",
            params={
                "doc_id":    "8845758582119845",   # public PostPage query
                "variables": '{"shortcode":"%s"}' % shortcode,
            },
            headers=hdrs, timeout=20)
        if r.status_code != 200:
            return None, f"IG graphql HTTP {r.status_code}"
        try:
            d = r.json()
        except Exception:
            return None, "IG graphql: non-JSON"
        m = (d.get("data") or {}).get("xdt_shortcode_media") \
            or (d.get("data") or {}).get("shortcode_media")
        info = _ig_extract_from_media_obj(m)
        if not info:
            return None, "IG graphql: no media"
        return info, None
    except Exception as e:
        return None, f"IG graphql error: {e}"


def _scrape(url):
    sc = extract_shortcode(url)
    if not sc:
        return None, "Could not parse Instagram URL."

    # Strategy cascade — first one that returns wins. Each strategy uses a
    # different trick: direct IG public endpoints first (most reliable), then
    # snapsave (when working), then yt-dlp last.
    errors = []

    # Canonical URLs: try /reel/ first for reels (some converters detect type
    # by URL path), then /p/ as a fallback.
    canon_reel = f"https://www.instagram.com/reel/{sc}/"
    canon_p    = f"https://www.instagram.com/p/{sc}/"

    def _try_snapinsta():
        d, e = _snapinsta_fetch(canon_reel)
        if d: return d, None
        return _snapinsta_fetch(canon_p)

    for name, fn in [
        ("snapinsta",  _try_snapinsta),
        ("ig_web_api", lambda: _ig_web_api(sc)),
        ("ig_graphql", lambda: _ig_graphql(sc)),
        ("snapsave",   lambda: _snapsave_fetch(canon_p)),
        ("yt-dlp",     lambda: _ytdlp_fetch(canon_p, os.environ.get("INSTA_COOKIE_FILE"))),
    ]:
        try:
            data, err = fn()
        except Exception as e:
            data, err = None, f"{name} crashed: {e}"
        if data and (data.get("video_url") or data.get("thumb_url")):
            return data, None
        errors.append(f"{name}: {err}")
        print(f"[instagram] {name} failed: {err}", flush=True)

    # All 4 strategies failed. Give the user a clearer message than yt-dlp's
    # raw error — IG is hard from datacenter IPs when post is non-public or
    # all proxy pools are rate-limited. Keep the strategy log for diagnostics.
    print(f"[instagram] all strategies failed: {' | '.join(errors)}", flush=True)
    return None, ("Instagram is currently blocking this download. "
                  "The post may be private, age-restricted, or our proxy "
                  "providers are temporarily rate-limited. Please try again "
                  "in a few minutes.")


# ── Public API used by reclip ────────────────────────────────────────────────

def info(url):
    """Return (info_dict, error). info_dict matches reclip's /api/info shape."""
    data, err = _scrape(url)
    if err or not data:
        return None, err or "Could not fetch post."
    return {
        "title":     data.get("title") or "Instagram Post",
        "thumbnail": data.get("thumb_url") or "",
        "duration":  None,
        "uploader":  data.get("uploader") or "",
        "formats":   [{"id": "best", "label": "Best quality", "height": 0}],
    }, None


def download(url, fmt, dest_dir, on_progress=None):
    """Stream the media into dest_dir. Returns (path, filename, error)."""
    data, err = _scrape(url)
    if err or not data:
        return None, None, err or "Could not fetch post."

    src_url = data["video_url"] if data["is_video"] else data["thumb_url"]
    if not src_url:
        return None, None, "No media URL found in this post."

    file_id = uuid.uuid4().hex[:12]
    tmp_ext = "mp4" if data["is_video"] else "jpg"
    tmp_path = os.path.join(dest_dir, f"{file_id}.{tmp_ext}")

    try:
        r = req_lib.get(src_url, stream=True, timeout=120,
                        headers={**_HEADERS, "Referer": "https://www.instagram.com/"})
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if total and on_progress:
                        on_progress(min(int(done / total * 90), 90))
    except Exception as e:
        return None, None, f"Download failed: {e}"

    final_path = tmp_path
    final_ext = tmp_ext
    if fmt == "audio" and data["is_video"]:
        mp3_path = os.path.join(dest_dir, f"{file_id}.mp3")
        try:
            subprocess.run([_find_ffmpeg(), "-i", tmp_path, "-q:a", "0", "-map", "a",
                            mp3_path, "-y"], capture_output=True, timeout=120)
            if os.path.exists(mp3_path):
                os.remove(tmp_path)
                final_path = mp3_path
                final_ext = "mp3"
        except Exception:
            pass  # keep the mp4 if ffmpeg fails

    filename = _safe_filename(data.get("title") or "instagram", final_ext)
    if on_progress:
        on_progress(100)
    return final_path, filename, None
