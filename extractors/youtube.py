"""Native YouTube extractor — Invidious + oEmbed (bot-check bypass).

Why: YouTube blocks datacenter IPs ("Sign in to confirm you're not a bot")
unless we use a PO Token (bgutil) or proxy through a 3rd-party. The trick the
standalone YT downloader uses: race public alt-frontends (Piped, Invidious,
oEmbed). Invidious returns *direct googlevideo.com URLs* the user's browser
can fetch, no auth, no bot check.

This module:
  - info():     race oEmbed (instant title/thumb) + Invidious (duration + audio bitrates)
  - download(): grab the audio stream from Invidious; for video, grab best video
                AND best audio then merge with ffmpeg. mp3 = transcode audio.
"""

import os
import re
import json
import shutil
import subprocess
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed


# Known public Invidious instances — ordered by reliability for streams from
# datacenter IPs (Railway). f5.si reliably returns googlevideo URLs; others are
# fallback only.
_INVIDIOUS = [
    "https://invidious.f5.si",          # primary — serves googlevideo URLs
    "https://iv.duti.dev",
    "https://invidious.nerdvpn.de",
    "https://invidious.materialio.us",
    "https://invidious.einfach.tech",
    "https://invidious.adminforge.de",
    "https://yewtu.be",
    "https://inv.nadeko.net",
    "https://invidious.privacydev.net",
    "https://iv.datura.network",
    "https://invidious.perennialte.ch",
    "https://invidious.lunar.icu",
    "https://invidious.projectsegfau.lt",
    "https://invidious.privacyredirect.com",
    "https://invidious.drgns.space",
    "https://invidious.flokinet.to",
]

_YT_RE = re.compile(
    r"(?:youtube\.com|youtu\.be|m\.youtube\.com|music\.youtube\.com)",
    re.I,
)
_VID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# ── y2mate.nu / iotacloud.org trick — primary YouTube MP3 path ────────────────
# Reverse-engineered: the conversion page loads y2mate.js with a base64 data-key.
# That decodes to a Bearer token for https://iotacloud.org/api/. A GET returns
# JSON like {progress:"completed", title, url} where `url` is a direct MP3
# attachment URL.

import base64 as _b64
import time as _time

def _y2mate_scrape_key(vid):
    """Scrape the per-page bearer key from v3.y2mate.nu/mp3/<vid>/."""
    import requests as _rq
    try:
        r = _rq.get(f"https://v3.y2mate.nu/mp3/{vid}/",
                    headers={"User-Agent": _UA}, timeout=12)
        if r.status_code != 200:
            return None
        m = re.search(r'data-key="([^"]+)"', r.text)
        if not m:
            return None
        return _b64.b64decode(m.group(1)).decode("ascii", errors="ignore")
    except Exception:
        return None


def _y2mate_mp3(vid, max_rounds=6):
    """Hit iotacloud.org/api/ with the scraped bearer until the conversion
    completes (or errors out). Returns (direct_url, title) or (None, err_str)."""
    import requests as _rq
    bearer = _y2mate_scrape_key(vid)
    if not bearer:
        return None, "y2mate: could not scrape data-key"
    headers = {
        "Authorization": f"Bearer {bearer}",
        "Origin": "https://v3.y2mate.nu",
        "Referer": "https://v3.y2mate.nu/",
        "User-Agent": _UA,
    }
    for r in range(1, max_rounds + 1):
        ts = int(_time.time())
        try:
            resp = _rq.get(
                f"https://iotacloud.org/api/?r={r}&v={vid}&_={ts}",
                headers=headers, timeout=20,
            )
        except Exception as e:
            return None, f"y2mate round {r} network: {e}"
        if resp.status_code != 200:
            return None, f"y2mate HTTP {resp.status_code}"
        try:
            d = resp.json()
        except Exception:
            return None, "y2mate: non-JSON response"
        prog = d.get("progress")
        if prog == "completed" and d.get("url"):
            return (d["url"], d.get("title") or ""), None
        if prog == "error":
            return None, "y2mate: backend reported error"
        # "converting" or other — wait then retry
        _time.sleep(4 if r < 3 else 6)
    return None, "y2mate: still converting after retries"


def is_valid_url(url):
    return bool(_YT_RE.search(url))


def _extract_video_id(url):
    m = _VID_RE.search(url)
    return m.group(1) if m else None


def _find_ffmpeg():
    return shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


def _safe_filename(title, ext):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title or "youtube").strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:80] or "youtube") + "." + ext


def _http_json(url, timeout=8):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ── oEmbed (instant, never blocked) ───────────────────────────────────────────

def _oembed(video_id):
    try:
        u = f"https://www.youtube.com/watch?v={video_id}"
        d = _http_json(
            f"https://www.youtube.com/oembed?url={urllib.parse.quote(u)}&format=json",
            timeout=8)
        if d.get("title"):
            return {
                "title":     d["title"],
                "uploader":  d.get("author_name", ""),
                "thumbnail": d.get("thumbnail_url", "")
                             or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            }
    except Exception:
        return None
    return None


# ── Invidious (real metadata + direct stream URLs) ────────────────────────────

def _invidious_fetch(video_id, instance, fields=None):
    try:
        path = f"/api/v1/videos/{video_id}"
        if fields:
            path += "?fields=" + urllib.parse.quote(",".join(fields))
        return _http_json(instance + path, timeout=8)
    except Exception:
        return None


def _invidious_race(video_id, fields, require_streams=False):
    """Try Invidious instances in parallel; return first usable response.

    require_streams: only accept responses that include playable adaptiveFormats
    (some instances strip the field). Triggers a retry without `fields` filter
    if no instance returned streams on the first pass.
    """
    def _ok(d):
        if not d or not d.get("title"):
            return False
        if require_streams:
            af = d.get("adaptiveFormats") or []
            return any(f.get("url") for f in af)
        return True

    def _try(inst, flds):
        try:
            d = _invidious_fetch(video_id, inst, fields=flds)
        except Exception:
            return None
        if _ok(d):
            d["_instance"] = inst
            return d
        return None

    # Pass 1: parallel race with the requested field filter
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_try, inst, fields): inst for inst in _INVIDIOUS[:10]}
        try:
            for fut in as_completed(futures, timeout=20):
                res = fut.result()
                if res:
                    for f in futures: f.cancel()
                    return res
        except Exception:
            pass

    if not require_streams:
        return None

    # Pass 2: some instances mangle the `fields` query — retry the f5.si
    # primary without the filter (full response is ~80 KB but more reliable).
    for inst in _INVIDIOUS[:6]:
        try:
            d = _invidious_fetch(video_id, inst, fields=None)
        except Exception:
            continue
        if _ok(d):
            d["_instance"] = inst
            return d
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def info(url):
    vid = _extract_video_id(url)
    if not vid:
        return None, "Could not parse YouTube URL."

    # Race oEmbed (~1s) and Invidious (~3-6s) — whichever returns first wins
    # for title/thumb. We always need Invidious for duration + stream URLs,
    # but we don't block info() on it: title/thumb alone are enough to show
    # the card.
    oe = _oembed(vid)
    iv = _invidious_race(vid, fields=("title", "lengthSeconds", "author",
                                       "videoThumbnails"))

    if not oe and not iv:
        return None, "YouTube blocked from this server (no working public mirror found)."

    title = (iv or {}).get("title") or (oe or {}).get("title") or "YouTube video"
    uploader = (iv or {}).get("author") or (oe or {}).get("uploader") or ""
    duration = int((iv or {}).get("lengthSeconds") or 0) or None
    thumbnail = (oe or {}).get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    return {
        "title":     title,
        "uploader":  uploader,
        "duration":  duration,
        "thumbnail": thumbnail,
        "formats":   [
            {"id": "audio", "label": "Audio (MP3)", "height": 0},
            {"id": "best",  "label": "Best video (MP4)", "height": 0},
        ],
    }, None


def _ytdlp_download_fallback(url, fmt, dest_dir, on_progress=None):
    """yt-dlp with bgutil PO Token — last resort when Invidious is unreachable.
    The bgutil server runs at 127.0.0.1:4416 (started in app.py).

    Tries several player_client orderings since different videos require different
    YouTube client types to bypass the bot check."""
    import glob as _glob
    file_id = uuid.uuid4().hex[:12]

    # Try multiple player_client orderings — Yt occasionally only respects one.
    client_variants = [
        "tv_simply,tv_embedded,web_safari,mweb,android,ios,default",
        "default,tv_embedded,mweb,android,ios",
        "web_creator,android_creator,tv_embedded",
        "ios,android,mweb",
    ]
    last_err = "no attempt"
    for variant in client_variants:
        out_template = os.path.join(dest_dir, f"{file_id}.%(ext)s")
        cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--newline",
               "--retries", "2", "--fragment-retries", "2",
               "--user-agent",
               "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
               "--extractor-args",
               f"youtube:player_client={variant};getpot_bgutil_baseurl=http://127.0.0.1:4416"]
        if fmt == "audio":
            cmd += ["-f", "bestaudio", "-x", "--audio-format", "mp3",
                    "-o", out_template, url]
        else:
            cmd += ["-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                    "--merge-output-format", "mp4",
                    "-o", out_template, url]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                files = _glob.glob(out_template.replace("%(ext)s", "*"))
                if files:
                    path = files[0]
                    ext = os.path.splitext(path)[1].lstrip(".") or "mp4"
                    if on_progress:
                        on_progress(100)
                    return path, _safe_filename("youtube", ext), None
            err_tail = (result.stderr or "")[-300:].strip()
            last_err = err_tail or last_err
            # Clean up any partial files before trying next variant
            for p in _glob.glob(out_template.replace("%(ext)s", "*")):
                try: os.remove(p)
                except Exception: pass
        except subprocess.TimeoutExpired:
            last_err = f"variant '{variant}' timed out"
        except Exception as e:
            last_err = f"variant '{variant}' error: {e}"

    # All clients failed. Surface a friendly message that points to MP3 (works
    # via the y2mate path) or asks the operator to set YT_COOKIES on Railway.
    return None, None, (
        "YouTube blocks video downloads from this server right now. "
        "MP3 audio works — try switching to MP3. To enable MP4, set the "
        "YT_COOKIES env var on Railway with a browser cookie export."
    )


def _stream_to_file(src_url, dest_path, on_progress=None):
    """Stream a googlevideo URL to disk. Use range-less GET with chunked iter."""
    import requests as _rq
    r = _rq.get(src_url, stream=True, timeout=120,
                headers={"User-Agent": _UA,
                         "Referer": "https://www.youtube.com/"})
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    done = 0
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=128 * 1024):
            if chunk:
                f.write(chunk)
                done += len(chunk)
                if total and on_progress:
                    on_progress(min(int(done / total * 85), 85))


def download(url, fmt, dest_dir, on_progress=None):
    vid = _extract_video_id(url)
    if not vid:
        return None, None, "Could not parse YouTube URL."

    # ── PRIMARY (audio): y2mate.nu / iotacloud.org direct-link trick ──────────
    # Fast, reliable, no bot-check. Only handles MP3 — for video we fall
    # through to Invidious / yt-dlp.
    if fmt == "audio":
        if on_progress: on_progress(5)
        res, err = _y2mate_mp3(vid)
        if res:
            direct_url, title = res
            file_id = uuid.uuid4().hex[:12]
            mp3 = os.path.join(dest_dir, f"{file_id}.mp3")
            try:
                _stream_to_file(direct_url, mp3,
                                on_progress=lambda p: on_progress and on_progress(10 + int(p * 0.9)))
            except Exception as e:
                # y2mate URLs are sometimes IP-bound; on failure fall through
                # to the Invidious/yt-dlp paths below.
                print(f"[youtube] y2mate stream failed: {e}; falling through", flush=True)
            else:
                if on_progress: on_progress(100)
                return mp3, _safe_filename(title or "youtube", "mp3"), None
        else:
            print(f"[youtube] y2mate path failed: {err}; falling through", flush=True)

    # Full Invidious response — need adaptiveFormats for stream URLs.
    iv = _invidious_race(vid, fields=("title", "author", "adaptiveFormats"),
                         require_streams=True)
    if not iv or not (iv.get("adaptiveFormats") or []):
        # Last resort: yt-dlp with bgutil PO Token (works when bot-check is mild).
        try:
            return _ytdlp_download_fallback(url, fmt, dest_dir, on_progress)
        except Exception as e:
            return None, None, f"YouTube alt-frontends unreachable and yt-dlp fallback failed: {e}"

    fmts = iv.get("adaptiveFormats") or []
    if not fmts:
        return None, None, "No streams available from public YouTube mirrors."


    audios = [f for f in fmts if "audio" in (f.get("type") or "")]
    audios.sort(key=lambda x: int(x.get("bitrate") or 0), reverse=True)
    if not audios:
        return None, None, "No audio stream found."

    file_id = uuid.uuid4().hex[:12]
    ffmpeg = _find_ffmpeg()

    if fmt == "audio":
        # Pick best opus or m4a; transcode to MP3 so any device plays it.
        a = audios[0]
        raw_audio = os.path.join(dest_dir, f"{file_id}.audio.bin")
        try:
            _stream_to_file(a["url"], raw_audio, on_progress)
        except Exception as e:
            return None, None, f"Audio download failed: {e}"
        mp3 = os.path.join(dest_dir, f"{file_id}.mp3")
        try:
            subprocess.run([ffmpeg, "-i", raw_audio, "-vn", "-codec:a", "libmp3lame",
                            "-qscale:a", "2", mp3, "-y"],
                           capture_output=True, timeout=240)
        finally:
            try: os.remove(raw_audio)
            except Exception: pass
        if not os.path.exists(mp3):
            return None, None, "Audio transcode failed (ffmpeg)."
        if on_progress: on_progress(100)
        return mp3, _safe_filename(iv.get("title") or "youtube", "mp3"), None

    # Video path: pick best mp4 video stream + best m4a audio, merge with ffmpeg.
    mp4_videos = [f for f in fmts
                  if "video/mp4" in (f.get("type") or "") and "av01" not in (f.get("type") or "")]
    mp4_videos.sort(key=lambda x: int(x.get("bitrate") or 0), reverse=True)
    if not mp4_videos:
        # Fall back to best video of any codec — ffmpeg will transcode container
        mp4_videos = sorted([f for f in fmts if "video" in (f.get("type") or "")],
                            key=lambda x: int(x.get("bitrate") or 0), reverse=True)
    if not mp4_videos:
        return None, None, "No video stream found."
    v = mp4_videos[0]
    a = next((f for f in audios if "mp4" in (f.get("type") or "")), audios[0])

    vraw = os.path.join(dest_dir, f"{file_id}.video.bin")
    araw = os.path.join(dest_dir, f"{file_id}.audio.bin")

    try:
        # Download video first (largest), update progress 0-60
        _stream_to_file(v["url"], vraw,
                        on_progress=lambda p: on_progress and on_progress(int(p * 0.6)))
        # Then audio, progress 60-85
        _stream_to_file(a["url"], araw,
                        on_progress=lambda p: on_progress and on_progress(60 + int(p * 0.25)))
    except Exception as e:
        for p in (vraw, araw):
            try: os.remove(p)
            except Exception: pass
        return None, None, f"Download failed: {e}"

    out = os.path.join(dest_dir, f"{file_id}.mp4")
    try:
        subprocess.run([ffmpeg, "-i", vraw, "-i", araw,
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart", out, "-y"],
                       capture_output=True, timeout=600)
    finally:
        for p in (vraw, araw):
            try: os.remove(p)
            except Exception: pass

    if not os.path.exists(out):
        return None, None, "ffmpeg merge failed."
    if on_progress: on_progress(100)
    return out, _safe_filename(iv.get("title") or "youtube", "mp4"), None
