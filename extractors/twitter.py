"""Native Twitter/X extractor — fxtwitter.com API primary + yt-dlp fallback.

Ported from the standalone Twitter downloader (`/home/khaled/twitter downloader/app.py`).
The trick: api.fxtwitter.com mirrors the public Twitter API but doesn't require
auth and returns clean JSON with direct video/photo URLs.
"""

import os
import re
import json
import shutil
import subprocess
import uuid
import glob

import requests as req_lib


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_TW_RE = re.compile(r"(?:twitter\.com|x\.com)/(\w+)/status/(\d+)", re.I)


def is_valid_url(url):
    return bool(_TW_RE.search(url))


def _extract_parts(url):
    m = _TW_RE.search(url)
    return (m.group(1), m.group(2)) if m else (None, None)


def _find_ffmpeg():
    return shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


def _safe_filename(title, ext):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#@]', "", title or "tweet").strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:80] or "tweet") + "." + ext


# ── fxtwitter.com scraper ────────────────────────────────────────────────────

def _fx_scrape(username, tweet_id):
    try:
        r = req_lib.get(
            f"https://api.fxtwitter.com/{username}/status/{tweet_id}",
            headers=_HEADERS, timeout=20,
        )
        data = r.json()
        if data.get("code") != 200 or not data.get("tweet"):
            return None, "Tweet not found or is private."
        tweet = data["tweet"]
        text = (tweet.get("text")
                or tweet.get("raw_text", {}).get("text")
                or f"tweet_{tweet_id}")
        author = tweet.get("author", {}).get("screen_name", username)
        media = tweet.get("media") or {}
        videos = media.get("videos") or []
        photos = media.get("photos") or []

        if videos:
            v = videos[0]
            return {
                "title":     text[:200],
                "uploader":  author,
                "thumb_url": v.get("thumbnail_url", "") or "",
                "video_url": v.get("url") or v.get("variants", [{}])[0].get("url", ""),
                "duration":  v.get("duration", 0) or 0,
                "is_video":  True,
            }, None
        elif photos:
            photo_urls = [p.get("url", "") for p in photos if p.get("url")]
            return {
                "title":      text[:200],
                "uploader":   author,
                "thumb_url":  photo_urls[0] if photo_urls else "",
                "photo_urls": photo_urls,
                "duration":   0,
                "is_video":   False,
            }, None
        return None, "No video or photo found in this tweet."
    except Exception:
        return None, "Could not reach fxtwitter API. Please try again."


def _ytdlp_fetch(url):
    try:
        cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--no-playlist",
               "--impersonate", "chrome", url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if result.returncode == 0 and result.stdout.strip():
            d = json.loads(result.stdout)
            return {
                "title":     d.get("title", "") or "Tweet",
                "uploader":  d.get("uploader", "") or d.get("channel", ""),
                "thumb_url": d.get("thumbnail", ""),
                "video_url": d.get("url", ""),
                "duration":  int(d.get("duration") or 0),
                "is_video":  True,
            }, None
        return None, (result.stderr.strip()[:200] or "yt-dlp returned no data")
    except subprocess.TimeoutExpired:
        return None, "yt-dlp timed out"
    except Exception as e:
        return None, str(e)


def _scrape(url):
    u, t = _extract_parts(url)
    if not u or not t:
        return None, "Could not parse tweet URL."
    data, err = _fx_scrape(u, t)
    if data:
        return data, None
    data2, err2 = _ytdlp_fetch(url)
    if data2:
        return data2, None
    return None, err or err2 or "Could not fetch tweet."


# ── Public API ────────────────────────────────────────────────────────────────

def info(url):
    data, err = _scrape(url)
    if err or not data:
        return None, err or "Could not fetch tweet."
    return {
        "title":     data.get("title") or "Tweet",
        "thumbnail": data.get("thumb_url") or "",
        "duration":  data.get("duration") or None,
        "uploader":  data.get("uploader") or "",
        "formats":   [{"id": "best", "label": "Best quality", "height": 0}],
    }, None


def download(url, fmt, dest_dir, on_progress=None):
    data, err = _scrape(url)
    if err or not data:
        return None, None, err or "Could not fetch tweet."

    src_url = data.get("video_url") or (data.get("photo_urls") or [None])[0]
    if not src_url:
        return None, None, "No media URL found in this tweet."

    file_id = uuid.uuid4().hex[:12]
    tmp_ext = "mp4" if data["is_video"] else "jpg"
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
            pass

    filename = _safe_filename(data.get("title") or "tweet", final_ext)
    if on_progress:
        on_progress(100)
    return final_path, filename, None
