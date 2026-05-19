"""Native TikTok extractor — tikwm.com → yt-dlp → snaptik.app cascade.

Ported from the standalone TikTok downloader (`/home/khaled/tiktok downloader/app.py`).
tikwm runs first because it returns IP-independent direct URLs with explicit
HD/SD/MP3 variants, avoiding yt-dlp's "audio-only file" trap on TikTok.
"""

import os
import re
import json
import shutil
import subprocess
import urllib.parse
import uuid

import requests as req_lib


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://snaptik.app/",
}

_TT_RE = re.compile(r"(?:https?://)?(?:www\.|vm\.|vt\.|m\.)?tiktok\.com", re.I)


def is_valid_url(url):
    return bool(_TT_RE.search(url))


def _normalize(url):
    url = url.strip().split("?")[0]
    if not url.startswith("http"):
        url = "https://" + url
    return url


def _find_ffmpeg():
    return shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


def _safe_filename(title, ext, src_url=""):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title or "").strip()
    name = re.sub(r"\s+", " ", name)[:60]
    vid = ""
    if src_url:
        m = (re.search(r"/video/(\d+)", src_url) or re.search(r"/(?:v|t)/(\w+)", src_url)
             or re.search(r"tiktok\.com/(\w{8,})", src_url))
        if m:
            vid = m.group(1)[-8:]
    if not vid:
        vid = uuid.uuid4().hex[:6]
    base = (name + " " if name else "") + vid
    return base[:80] + "." + ext


# ── tikwm.com (primary, IP-independent direct URLs) ───────────────────────────

def _tikwm_info(url):
    try:
        r = req_lib.post(
            "https://www.tikwm.com/api/",
            data={"url": url, "hd": 1},
            headers={**_HEADERS, "Referer": "https://www.tikwm.com/"},
            timeout=20,
        )
        if r.status_code != 200:
            return None, f"tikwm HTTP {r.status_code}"
        body = r.json()
        if body.get("code") != 0 or not body.get("data"):
            return None, body.get("msg") or "tikwm returned no data."
        d = body["data"]

        def _abs(u):
            if not u: return ""
            return u if u.startswith("http") else "https://www.tikwm.com" + u

        downloads = []
        if d.get("hdplay"):
            downloads.append({"label": "HD MP4 (no watermark)", "url": _abs(d["hdplay"]), "kind": "video", "ext": "mp4"})
        if d.get("play"):
            downloads.append({"label": "MP4 (no watermark)",    "url": _abs(d["play"]),   "kind": "video", "ext": "mp4"})
        if d.get("wmplay"):
            downloads.append({"label": "MP4 (with watermark)",  "url": _abs(d["wmplay"]), "kind": "video", "ext": "mp4"})
        if d.get("music"):
            downloads.append({"label": "MP3 (audio only)",      "url": _abs(d["music"]),  "kind": "audio", "ext": "mp3"})
        if not downloads:
            return None, "tikwm returned no playable URL."

        author = d.get("author") or {}
        if isinstance(author, dict):
            author = author.get("nickname") or author.get("unique_id") or ""

        return {
            "title":     d.get("title") or "",
            "thumbnail": _abs(d.get("cover") or d.get("origin_cover") or ""),
            "uploader":  author or "",
            "duration":  int(d.get("duration") or 0),
            "downloads": downloads,
        }, None
    except Exception as e:
        return None, str(e)


# ── snaptik.app (fallback) ────────────────────────────────────────────────────

def _decode_snaptik(js):
    idx = js.find("eval(")
    if idx < 0:
        return None
    depth = 0
    end_idx = idx
    for i, c in enumerate(js[idx:]):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end_idx = idx + i + 1
                break
    eval_content = js[idx:end_idx]
    m = re.search(r'"([^"]+)"\s*,(\d+)\s*,"([^"]+)"\s*,(\d+)\s*,(\d+)\s*,(\d+)\s*\)\)$', eval_content)
    if not m:
        return None
    encoded, n_charset, t_offset = m.group(1), m.group(3), int(m.group(4))
    e_base, sep_idx = int(m.group(5)), int(m.group(6))
    sep_char = n_charset[sep_idx]
    result = ""
    for part in encoded.split(sep_char):
        if not part:
            continue
        ns = "".join(str(n_charset.find(c)) for c in part if n_charset.find(c) >= 0)
        if ns:
            result += chr(int(ns, e_base) - t_offset)
    return urllib.parse.unquote(result)


def _snaptik_info(url):
    try:
        r = req_lib.get("https://snaptik.app/en2", headers=_HEADERS, timeout=15)
        token_m = re.search(r'name="token"\s*value="([^"]+)"', r.text)
        token = token_m.group(1) if token_m else ""
        r = req_lib.post("https://snaptik.app/abc2.php",
                         data={"url": url, "lang": "en2", "token": token},
                         headers=_HEADERS, timeout=20)
        decoded = _decode_snaptik(r.text)
        if not decoded:
            return None, "Could not decode snaptik response."
        if "showAlert" in decoded:
            em = re.search(r'showAlert\("([^"]+)"', decoded)
            return None, em.group(1) if em else "TikTok server unavailable."
        urls = re.findall(r'https?://[^\s"\'<>,;)\]]+\.(?:mp4|jpg|png)', decoded)
        if urls:
            return {"downloads": [{"label": "MP4 (snaptik)", "url": urls[0], "kind": "video", "ext": "mp4"}],
                    "title": "TikTok Video", "thumbnail": "", "uploader": "", "duration": 0}, None
        return None, "No download URL found."
    except Exception as e:
        return None, str(e)


# ── yt-dlp fallback ───────────────────────────────────────────────────────────

def _ytdlp_info(url):
    try:
        # --impersonate chrome defeats TikTok's TLS-fingerprint IP block; without
        # it datacenter IPs get "Your IP address is blocked from accessing this post".
        cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--no-playlist",
               "--impersonate", "chrome", url]
        cf = os.environ.get("TIKTOK_COOKIE_FILE")
        if cf and os.path.exists(cf):
            cmd += ["--cookies", cf]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return {
                "title":     data.get("title", "") or "TikTok Video",
                "thumbnail": data.get("thumbnail", ""),
                "uploader":  data.get("uploader", "") or data.get("channel", ""),
                "duration":  int(data.get("duration") or 0),
                "downloads": [{"label": "MP4 (yt-dlp)", "url": data.get("url", ""),
                               "kind": "video", "ext": "mp4"}],
            }, None
        return None, result.stderr.strip()[:200] or "yt-dlp failed."
    except subprocess.TimeoutExpired:
        return None, "yt-dlp timed out."
    except Exception as e:
        return None, str(e)


def _resolve(url):
    url = _normalize(url)
    data, err1 = _tikwm_info(url)
    if data:
        return data, None
    data, err2 = _ytdlp_info(url)
    if data:
        return data, None
    data, err3 = _snaptik_info(url)
    if data:
        return data, None
    # Prefer yt-dlp's error since it usually explains *why* (IP block, removed
    # post, etc.); tikwm's "msg" is often a terse "Free Api Limit" string.
    return None, err2 or err3 or err1 or "All TikTok sources failed."


# ── Public API ────────────────────────────────────────────────────────────────

def info(url):
    data, err = _resolve(url)
    if err or not data:
        return None, err or "Could not fetch TikTok info."
    # Expose tikwm's labelled variants as reclip-format selectable formats.
    formats = []
    for i, d in enumerate(data.get("downloads", [])):
        formats.append({"id": str(i), "label": d["label"], "height": 0})
    if not formats:
        formats = [{"id": "best", "label": "Best quality", "height": 0}]
    return {
        "title":     data.get("title") or "TikTok Video",
        "thumbnail": data.get("thumbnail") or "",
        "duration":  data.get("duration") or None,
        "uploader":  data.get("uploader") or "",
        "formats":   formats,
    }, None


def download(url, fmt, dest_dir, on_progress=None, format_id=None):
    """Stream the chosen variant into dest_dir."""
    data, err = _resolve(url)
    if err or not data:
        return None, None, err or "Could not fetch TikTok info."

    downloads = data.get("downloads", []) or []
    chosen = None
    if format_id and format_id != "best" and format_id.isdigit():
        ix = int(format_id)
        if 0 <= ix < len(downloads):
            chosen = downloads[ix]
    if not chosen:
        if fmt == "audio":
            chosen = next((d for d in downloads if d["kind"] == "audio"), None) or downloads[0]
        else:
            # Prefer H.264 720p over HEVC for browser compatibility — matches the
            # standalone converter's ordering.
            order = ["MP4 (no watermark)", "MP4 (with watermark)", "HD MP4 (no watermark)",
                     "MP4 (yt-dlp)", "MP4 (snaptik)"]
            by_label = {d["label"]: d for d in downloads}
            for lbl in order:
                if lbl in by_label:
                    chosen = by_label[lbl]
                    break
            chosen = chosen or downloads[0]

    src_url = chosen["url"]
    file_id = uuid.uuid4().hex[:12]
    tmp_ext = chosen["ext"]
    tmp_path = os.path.join(dest_dir, f"{file_id}.{tmp_ext}")

    try:
        r = req_lib.get(src_url, stream=True, timeout=120,
                        headers={**_HEADERS, "Referer": "https://www.tiktok.com/"})
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

    # If user wants MP3 but we got an MP4, transcode.
    final_path = tmp_path
    final_ext = tmp_ext
    if fmt == "audio" and tmp_ext != "mp3":
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

    filename = _safe_filename(data.get("title") or "tiktok", final_ext, url)
    if on_progress:
        on_progress(100)
    return final_path, filename, None
