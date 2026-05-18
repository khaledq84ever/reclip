"""Native Kick.com extractor — kick-video.download proxy + ffmpeg HLS download.

Ported from the standalone Kick converter (`/home/khaled/kick converter/app.py`).
Trick: kick-video.download proxies Kick's API to bypass Cloudflare, returning
the raw JSON Kick.com would return. We then download the HLS m3u8 with ffmpeg.
Live streams fall back to yt-dlp --impersonate chrome.
"""

import os
import re
import json
import glob
import shutil
import subprocess
import uuid

import requests as req_lib


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_KVD_HEADERS = {
    **_HEADERS,
    "Accept": "*/*",
    "Referer": "https://kick-video.download/",
    "Origin":  "https://kick-video.download",
}
_KVD_API = "https://kick-video.download/api/get-kick-video"

MAX_VOD_SECONDS = 60 * 60
LIVE_MAX_SECONDS = 5 * 60

_RESERVED = {"video", "videos", "category", "categories", "browse", "login",
             "signup", "subscriptions", "following", "channels", "help",
             "about", "tos", "privacy", "community", "search", "discover",
             "api", "static", "assets"}

_KICK_RE = re.compile(
    r"(?:^|[/.])kick\.com/"
    r"(?:[\w\-]+/(?:videos?|clips?)/[\w\-]+"
    r"|[\w\-]+\?clip=[\w]+"
    r"|[\w\-]+/?(?:$|\?|#))",
    re.IGNORECASE,
)


def is_valid_url(url):
    if not _KICK_RE.search(url):
        return False
    m = re.search(r"kick\.com/([\w\-]+)(?:/?$|\?(?!clip=))", url, re.IGNORECASE)
    if m and m.group(1).lower() in _RESERVED:
        return False
    return True


def _kind_for_url(url):
    if re.search(r"[?&]clip=", url, re.I): return "clip"
    if re.search(r"/clips?/", url, re.I): return "clip"
    if re.search(r"/videos?/", url, re.I): return "vod"
    return "live"


def _extract_id(url):
    m = re.search(r"/clips?/(clip_[\w]+)", url, re.I)
    if m: return "clip", m.group(1)
    m = re.search(r"[?&]clip=(clip_[\w]+)", url, re.I)
    if m: return "clip", m.group(1)
    m = re.search(r"/videos?/([a-f0-9-]{36})", url, re.I)
    if m: return "vod", m.group(1)
    m = re.search(r"kick\.com/([\w\-]+)", url, re.I)
    if m and m.group(1).lower() not in _RESERVED:
        return "live", m.group(1)
    return None, None


def _api_url(kind, ident):
    if kind == "clip": return f"https://kick.com/api/v2/clips/{ident}"
    if kind == "vod":  return f"https://kick.com/api/v1/video/{ident}"
    if kind == "live": return f"https://kick.com/api/v2/channels/{ident}"
    return None


def _find_ffmpeg():
    return shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


def _safe_filename(title, ext):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#@]', "", title or "kick").strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:80] or "kick") + "." + ext


# ── kick-video.download proxy ─────────────────────────────────────────────────

def _kvd_fetch(kick_api_url):
    try:
        r = req_lib.get(_KVD_API, params={"url": kick_api_url},
                        headers=_KVD_HEADERS, timeout=25)
        body = r.text
        if body.startswith("No URL found"):
            return None, "Not found"
        if r.status_code >= 500:
            return None, f"Proxy error {r.status_code}"
        try:
            return json.loads(body), None
        except json.JSONDecodeError:
            return None, "Bad response"
    except req_lib.Timeout:
        return None, "Proxy timeout"
    except Exception:
        return None, "Proxy unreachable"


def _ytdlp_live_info(url):
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "--skip-download",
           "--dump-single-json", "--geo-bypass", "--impersonate", "chrome", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
        if proc.returncode != 0:
            stderr = (proc.stderr or "").lower()
            if "offline" in stderr or "not currently live" in stderr:
                return None, "This channel is offline — try a clip or VOD URL instead."
            return None, "Live stream not available right now."
        return json.loads(proc.stdout), None
    except subprocess.TimeoutExpired:
        return None, "Kick is responding slowly. Please try again."
    except Exception:
        return None, "Could not fetch live channel info."


def _kick_info(url):
    kind, ident = _extract_id(url)
    if not kind:
        return None, "Could not parse Kick URL."
    if kind == "live":
        d, err = _ytdlp_live_info(url)
        if err or not d:
            return None, err
        return {
            "_hls_url":  d.get("url") or "",
            "title":     d.get("title") or "Kick live",
            "uploader":  d.get("uploader") or "",
            "thumbnail": d.get("thumbnail") or "",
            "duration":  int(d.get("duration") or 0),
            "_kind":     "live",
        }, None

    api = _api_url(kind, ident)
    data, err = _kvd_fetch(api)
    if err == "Not found" or not data:
        return None, "Clip or VOD not found. It may be deleted or private."
    if err:
        return None, "Kick is responding slowly. Please try again."

    if kind == "clip":
        clip = data.get("clip") or data
        m3u8 = clip.get("clip_url") or clip.get("video_url")
        if not m3u8:
            return None, "No playable URL in clip metadata."
        return {
            "_hls_url":  m3u8,
            "title":     clip.get("title") or "Kick clip",
            "uploader":  ((clip.get("channel") or {}).get("username")
                          or (clip.get("creator") or {}).get("username") or ""),
            "thumbnail": clip.get("thumbnail_url") or "",
            "duration":  int(clip.get("duration") or 0),
            "_kind":     "clip",
        }, None

    if kind == "vod":
        m3u8 = data.get("source") or (data.get("video") or {}).get("source")
        livestream = data.get("livestream") or {}
        channel = (data.get("livestream") or {}).get("channel") or {}
        if not m3u8:
            return None, "No playable URL in VOD metadata."
        return {
            "_hls_url":  m3u8,
            "title":     livestream.get("session_title") or data.get("title") or "Kick VOD",
            "uploader":  ((channel.get("user") or {}).get("username")
                          or channel.get("slug") or ""),
            "thumbnail": (livestream.get("thumbnail") or {}).get("src") or "",
            "duration":  (livestream.get("duration") or 0) // 1000,
            "_kind":     "vod",
        }, None
    return None, "Unsupported Kick URL type."


def _hls_to_mp4(m3u8, output_mp4, max_seconds=None, on_progress=None):
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None
    cmd = [ffmpeg, "-y", "-i", m3u8, "-c", "copy",
           "-bsf:a", "aac_adtstoasc", "-progress", "pipe:2"]
    if max_seconds:
        cmd[3:3] = ["-t", str(max_seconds)]
    cmd.append(output_mp4)

    dur_re = re.compile(r"Duration:\s*(\d+):(\d+):([\d.]+)")
    time_re = re.compile(r"out_time_ms=(\d+)")
    total_us = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        for line in proc.stderr:
            if total_us is None:
                m = dur_re.search(line)
                if m:
                    h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                    total_us = int((h * 3600 + mn * 60 + s) * 1_000_000)
            m = time_re.search(line)
            if m and total_us and on_progress:
                pct = min(int(int(m.group(1)) / total_us * 90), 90)
                on_progress(pct)
        proc.wait(timeout=900)
        return output_mp4 if (proc.returncode == 0 and os.path.exists(output_mp4)) else None
    except Exception:
        return None


def _ytdlp_live_record(url, output_mp4, on_progress=None):
    cmd = ["yt-dlp", "--geo-bypass", "--no-playlist", "--newline",
           "--impersonate", "chrome",
           "--live-from-start", "--downloader", "ffmpeg",
           "--downloader-args", f"ffmpeg_i:-t {LIVE_MAX_SECONDS}",
           "-f", "best[ext=mp4]/best", "-o", output_mp4, url]
    progress_re = re.compile(r"\[download\]\s+([\d.]+)%")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        for line in proc.stderr:
            m = progress_re.search(line)
            if m and on_progress:
                on_progress(min(int(float(m.group(1))), 90))
        proc.wait(timeout=600)
        return output_mp4 if (proc.returncode == 0 and os.path.exists(output_mp4)) else None
    except Exception:
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def info(url):
    data, err = _kick_info(url)
    if err or not data:
        return None, err or "Could not fetch this Kick URL."
    return {
        "title":     data.get("title") or "Kick",
        "thumbnail": data.get("thumbnail") or "",
        "duration":  data.get("duration") or None,
        "uploader":  data.get("uploader") or "",
        "formats":   [{"id": "best", "label": "Best quality", "height": 0}],
    }, None


def download(url, fmt, dest_dir, on_progress=None):
    data, err = _kick_info(url)
    if err or not data:
        return None, None, err or "Could not fetch this Kick URL."

    file_id = uuid.uuid4().hex[:12]
    mp4_path = os.path.join(dest_dir, f"{file_id}.mp4")

    kind = data.get("_kind", "clip")
    if "_hls_url" in data and data["_hls_url"]:
        max_s = LIVE_MAX_SECONDS if kind == "live" else (MAX_VOD_SECONDS if kind == "vod" else None)
        path = _hls_to_mp4(data["_hls_url"], mp4_path, max_seconds=max_s, on_progress=on_progress)
    else:
        path = _ytdlp_live_record(url, mp4_path, on_progress)

    if not path or not os.path.exists(path):
        msg = ("Could not download. The channel may be offline."
               if kind == "live" else
               "Could not download this video. Try again in a moment.")
        return None, None, msg

    final_path = path
    final_ext = "mp4"
    if fmt == "audio":
        mp3_path = os.path.join(dest_dir, f"{file_id}.mp3")
        try:
            subprocess.run([_find_ffmpeg(), "-i", path, "-q:a", "0", "-map", "a",
                            mp3_path, "-y"], capture_output=True, timeout=180)
            if os.path.exists(mp3_path):
                os.remove(path)
                final_path = mp3_path
                final_ext = "mp3"
        except Exception:
            pass

    filename = _safe_filename(data.get("title") or "kick", final_ext)
    if on_progress:
        on_progress(100)
    return final_path, filename, None
