"""Native Facebook extractor — snapsave.app proxy + yt-dlp fallback.

Same trick as Instagram: snapsave.app accepts any social URL and returns the
direct CDN download link. We reuse the IG extractor's snapsave decoder.
"""

import os
import re
import shutil
import subprocess
import uuid

import requests as req_lib

from . import instagram as _ig  # reuse snapsave decode + HTML parse


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.facebook.com/",
}

_FB_RE = re.compile(
    r"(?:facebook\.com|fb\.watch|fb\.com|m\.facebook\.com)/",
    re.IGNORECASE,
)


def is_valid_url(url):
    return bool(_FB_RE.search(url))


def _find_ffmpeg():
    return shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


def _safe_filename(title, ext):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#@]', "", title or "facebook").strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:80] or "facebook") + "." + ext


def _scrape(url):
    """Try snapsave first (works for public FB videos), then yt-dlp."""
    data, err1 = _ig._snapsave_fetch(url)
    if data:
        # The IG parser stamps title="Instagram Post" as its fallback default —
        # rewrite to a FB-shaped default so reclip's card isn't misleading.
        if data.get("title") in (None, "", "Instagram Post"):
            data["title"] = "Facebook video"
        return data, None
    data2, err2 = _ytdlp_fetch_impersonate(url)
    if data2:
        return data2, None
    return None, err1 or err2 or "Could not fetch this Facebook video."


def _ytdlp_fetch_impersonate(url):
    """yt-dlp with --impersonate chrome (curl_cffi) — defeats FB's anti-bot."""
    import json as _json
    cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--no-playlist",
           "--impersonate", "chrome", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if result.returncode == 0 and result.stdout.strip():
            d = _json.loads(result.stdout)
            return {
                "video_url": d.get("url", ""),
                "thumb_url": d.get("thumbnail", ""),
                "title":     d.get("title") or "Facebook video",
                "uploader":  d.get("uploader") or d.get("channel") or "",
                "is_video":  d.get("ext", "") in ("mp4", "mov", "webm", "m4v"),
            }, None
        return None, (result.stderr.strip().splitlines() or ["yt-dlp failed"])[-1][:200]
    except subprocess.TimeoutExpired:
        return None, "yt-dlp timed out"
    except Exception as e:
        return None, str(e)


# ── Public API ────────────────────────────────────────────────────────────────

def info(url):
    data, err = _scrape(url)
    if err or not data:
        return None, err or "Could not fetch this Facebook video."
    return {
        "title":     data.get("title") or "Facebook video",
        "thumbnail": data.get("thumb_url") or "",
        "duration":  None,
        "uploader":  data.get("uploader") or "",
        "formats":   [{"id": "best", "label": "Best quality", "height": 0}],
    }, None


def download(url, fmt, dest_dir, on_progress=None):
    data, err = _scrape(url)
    if err or not data:
        return None, None, err or "Could not fetch this Facebook video."

    src_url = data.get("video_url") if data.get("is_video", True) else data.get("thumb_url")
    if not src_url:
        return None, None, "No media URL found in this Facebook post."

    file_id = uuid.uuid4().hex[:12]
    tmp_ext = "mp4" if data.get("is_video", True) else "jpg"
    tmp_path = os.path.join(dest_dir, f"{file_id}.{tmp_ext}")

    try:
        r = req_lib.get(src_url, stream=True, timeout=120, headers=_HEADERS)
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
    if fmt == "audio" and data.get("is_video", True):
        mp3_path = os.path.join(dest_dir, f"{file_id}.mp3")
        try:
            subprocess.run([_find_ffmpeg(), "-i", tmp_path, "-q:a", "0", "-map", "a",
                            mp3_path, "-y"], capture_output=True, timeout=120)
            if os.path.exists(mp3_path):
                os.remove(tmp_path)
                final_path = mp3_path
                final_ext = "mp3"
        except Exception:
            pass

    filename = _safe_filename(data.get("title") or "facebook", final_ext)
    if on_progress:
        on_progress(100)
    return final_path, filename, None
