"""Native Snapchat extractor — yt-dlp with --geo-bypass.

Ported from the standalone Snapchat converter (`/home/khaled/snapchat converter/app.py`).
Snapchat URLs supported: Spotlight, public profile stories, story shortlinks.
"""

import os
import re
import json
import glob
import shutil
import subprocess
import uuid


_SNAP_RE = re.compile(
    r"(?:^|[/.])(?:snapchat\.com|story\.snapchat\.com)/"
    r"(?:spotlight|p|t|s|add)/[\w\-./]+",
    re.IGNORECASE,
)


def is_valid_url(url):
    return bool(_SNAP_RE.search(url))


def _find_ffmpeg():
    return shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


def _safe_filename(title, ext):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#@]', "", title or "snap").strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:80] or "snap") + "." + ext


def _ytdlp_info(url):
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "--skip-download",
           "--dump-single-json", "--geo-bypass", "--impersonate", "chrome", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if proc.returncode != 0:
            msg = (proc.stderr or "").strip().splitlines()
            last = msg[-1] if msg else "Could not fetch snap info."
            if "private" in last.lower() or "not available" in last.lower():
                return None, "This snap is private or no longer available."
            if "unsupported" in last.lower():
                return None, "This Snapchat URL is not supported — try a Spotlight or Story link."
            return None, "Could not fetch snap. Make sure the link is public."
        return json.loads(proc.stdout), None
    except subprocess.TimeoutExpired:
        return None, "Snapchat is responding slowly. Please try again."
    except Exception:
        return None, "Could not parse snap info."


def _ytdlp_download(url, out_template, on_progress=None):
    cmd = ["yt-dlp", "--geo-bypass", "--no-playlist", "--newline",
           "--impersonate", "chrome",
           "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
           "--merge-output-format", "mp4",
           "-o", out_template, url]
    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        cmd += ["--ffmpeg-location", os.path.dirname(ffmpeg)]
    progress_re = re.compile(r"\[download\]\s+([\d.]+)%")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            m = progress_re.search(line)
            if m and on_progress:
                on_progress(min(int(float(m.group(1))), 90))
        proc.wait(timeout=300)
        if proc.returncode != 0:
            return None
        files = glob.glob(out_template.replace("%(ext)s", "*"))
        return files[0] if files else None
    except Exception:
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def info(url):
    data, err = _ytdlp_info(url)
    if err or not data:
        return None, err or "Could not fetch snap."
    title = data.get("title") or data.get("description") or "Snapchat video"
    return {
        "title":     str(title)[:200],
        "thumbnail": data.get("thumbnail", "") or "",
        "duration":  int(data.get("duration") or 0) or None,
        "uploader":  (data.get("uploader") or data.get("uploader_id")
                      or data.get("channel") or ""),
        "formats":   [{"id": "best", "label": "Best quality", "height": 0}],
    }, None


def download(url, fmt, dest_dir, on_progress=None):
    file_id = uuid.uuid4().hex[:12]
    out_template = os.path.join(dest_dir, f"{file_id}.%(ext)s")
    path = _ytdlp_download(url, out_template, on_progress)
    if not path or not os.path.exists(path):
        return None, None, "Could not download this snap."

    final_path = path
    final_ext = os.path.splitext(path)[1].lstrip(".") or "mp4"
    if fmt == "audio" and final_ext != "mp3":
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

    info_data, _ = _ytdlp_info(url)
    title = (info_data or {}).get("title") or "snapchat"
    filename = _safe_filename(title, final_ext)
    if on_progress:
        on_progress(100)
    return final_path, filename, None
