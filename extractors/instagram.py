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


def _snapsave_fetch(url):
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
            return None, f"snapsave HTTP {r.status_code}"
        decoded = _snapsave_decode(r.text)
        if not decoded:
            return None, "Could not decode snapsave response."
        if "Unable to connect" in decoded or '"error_' in decoded:
            return None, "snapsave could not reach Instagram for this post."
        parsed = _parse_snapsave_html(decoded)
        if not parsed:
            return None, "No download links found in snapsave response."
        return parsed, None
    except Exception as e:
        return None, f"snapsave error: {e}"


def _ytdlp_fetch(url, cookies_file=None):
    try:
        cmd = ["yt-dlp", "--dump-json", "--no-warnings", url]
        if cookies_file and os.path.exists(cookies_file):
            cmd += ["--cookies", cookies_file]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
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


def _scrape(url):
    sc = extract_shortcode(url)
    if not sc:
        return None, "Could not parse Instagram URL."
    canon = f"https://www.instagram.com/p/{sc}/"
    data, err1 = _snapsave_fetch(canon)
    if data:
        return data, None
    data, err2 = _ytdlp_fetch(canon, os.environ.get("INSTA_COOKIE_FILE"))
    if data:
        return data, None
    return None, err1 or err2 or "Could not fetch this post."


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
