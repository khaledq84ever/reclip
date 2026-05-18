import os
import uuid
import glob
import json
import subprocess
import threading
import tempfile
import time
import shutil
import urllib.request
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}

# --- Cookies support: if YT_COOKIES env var is set (Netscape cookies.txt format), write it once for yt-dlp to use
COOKIES_FILE = None
if os.environ.get("YT_COOKIES"):
    _cookies_path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    with open(_cookies_path, "w") as _f:
        _f.write(os.environ["YT_COOKIES"])
    COOKIES_FILE = _cookies_path

# ── bgutil PO token HTTP server ───────────────────────────────────────────────
# Starts the Node.js server that generates PO tokens, which lets yt-dlp bypass
# YouTube's bot detection from datacenter IPs without needing cookies.
BGUTIL_PORT = 4416
BGUTIL_BASE_URL = f"http://127.0.0.1:{BGUTIL_PORT}"
_bgutil_proc = None
_bgutil_ready = False


def _start_bgutil_server():
    global _bgutil_proc, _bgutil_ready
    main_js = "/app/bgutil-ytdlp-pot-provider/server/build/main.js"
    if not os.path.isfile(main_js):
        print("[bgutil] server not found, skipping (YouTube may fail)", flush=True)
        return
    node = shutil.which("node") or "node"
    try:
        _bgutil_proc = subprocess.Popen(
            [node, main_js, "-p", str(BGUTIL_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"[bgutil] failed to launch: {e}", flush=True)
        return
    # Wait up to 10s for server to respond on /ping
    for _ in range(20):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(f"{BGUTIL_BASE_URL}/ping", timeout=1) as r:
                if r.getcode() == 200:
                    _bgutil_ready = True
                    print(f"[bgutil] PO Token server ready on :{BGUTIL_PORT}", flush=True)
                    return
        except Exception:
            continue
    print("[bgutil] failed to come up within 10s", flush=True)


# Start in background so app boot isn't blocked
threading.Thread(target=_start_bgutil_server, daemon=True).start()


import random

# Rotation of realistic browser User-Agents (random per-request)
_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
]


def yt_dlp_base_args(url):
    """Common yt-dlp flags incl. anti-bot bypass for cloud IPs.

    - youtube: use iOS+web_safari player clients (less aggressive bot checks)
    - everything else: --impersonate chrome via curl_cffi (defeats Cloudflare JS challenges,
      bot detection that examines TLS fingerprint, HTTP/2 frame ordering, etc.)
    """
    args = []
    if "youtube.com" in url or "youtu.be" in url:
        # Use multiple player clients + bgutil PO Token (if running) to defeat bot detection.
        ya = "youtube:player_client=default,tv_embedded,mweb,android,ios"
        if _bgutil_ready:
            args += ["--extractor-args", f"{ya};getpot_bgutil_baseurl={BGUTIL_BASE_URL}"]
        else:
            args += ["--extractor-args", ya]
    else:
        # curl_cffi impersonation — only safe to enable on non-youtube (youtube needs the WebSocket-ish JS bits)
        args += ["--impersonate", "chrome"]
    args += ["--user-agent", random.choice(_UA_POOL)]
    args += ["--add-header", "Accept-Language:en-US,en;q=0.9"]
    if COOKIES_FILE:
        args += ["--cookies", COOKIES_FILE]
    args += ["--no-check-certificate", "--retries", "5", "--fragment-retries", "5", "--socket-timeout", "30"]
    return args


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template] + yt_dlp_base_args(url)

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/debug/yt")
def debug_yt():
    """Probe each Invidious instance from inside the container to see which are
    reachable. Visit /debug/yt?vid=dQw4w9WgXcQ"""
    vid = request.args.get("vid", "dQw4w9WgXcQ")
    out = []
    from extractors.youtube import _INVIDIOUS, _http_json
    import time as _t
    for inst in _INVIDIOUS:
        t0 = _t.time()
        try:
            d = _http_json(f"{inst}/api/v1/videos/{vid}?fields=title,adaptiveFormats",
                           timeout=8)
            af = d.get("adaptiveFormats") or []
            out.append({"inst": inst, "ok": True, "title": (d.get("title") or "")[:40],
                        "formats": len(af),
                        "has_url": bool(af and af[0].get("url")),
                        "ms": round((_t.time() - t0) * 1000)})
        except Exception as e:
            out.append({"inst": inst, "ok": False, "err": str(e)[:80],
                        "ms": round((_t.time() - t0) * 1000)})
    return jsonify({"results": out})


# ─────────────────────────── Specialized-backend router ───────────────────────────
# Reclip auto-detects the URL host and proxies to the user's dedicated converter
# for that site (each lives on its own Railway deployment), falling back to the
# local yt-dlp pipeline for anything not matched.
import re
from urllib.parse import urlparse

import requests as _rq  # noqa: E402

# Native in-process extractors (same logic as standalone converters, no proxy hops).
from extractors import instagram as _ig_extractor
from extractors import tiktok    as _tt_extractor
from extractors import twitter   as _tw_extractor
from extractors import snapchat  as _sc_extractor
from extractors import kick      as _kk_extractor
from extractors import facebook  as _fb_extractor
from extractors import youtube   as _yt_extractor

BACKENDS = {
    "youtube":   {"name": "YouTube",   "native": _yt_extractor, "hosts": ("youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com")},
    "tiktok":    {"name": "TikTok",    "native": _tt_extractor, "hosts": ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com")},
    "instagram": {"name": "Instagram", "native": _ig_extractor, "hosts": ("instagram.com",)},
    "facebook":  {"name": "Facebook",  "native": _fb_extractor, "hosts": ("facebook.com", "fb.watch", "fb.com", "m.facebook.com")},
    "twitter":   {"name": "Twitter/X", "native": _tw_extractor, "hosts": ("twitter.com", "x.com", "mobile.twitter.com")},
    "snapchat":  {"name": "Snapchat",  "native": _sc_extractor, "hosts": ("snapchat.com", "story.snapchat.com")},
    "kick":      {"name": "Kick",      "native": _kk_extractor, "hosts": ("kick.com",)},
}


def detect_backend(url):
    try:
        h = (urlparse(url).hostname or "").lower().lstrip(".")
    except Exception:
        return None, None
    for key, cfg in BACKENDS.items():
        for pat in cfg["hosts"]:
            if h == pat or h.endswith("." + pat):
                return key, cfg
    return None, None


def _proxy_info(cfg, url):
    """Resolve info via native extractor (if defined) or by POSTing to the
    specialized backend's /info endpoint. Returns (normalized_dict, error_str).
    """
    # Native extractor: same code as the standalone converter, in-process.
    native = cfg.get("native")
    if native:
        try:
            data, err = native.info(url)
        except Exception as e:
            return None, f"native extractor error: {e}"
        if err or not data:
            return None, err or "Native extractor returned no data."
        data["_backend"] = cfg["name"]
        return data, None

    try:
        # Tight timeout so we fall back to local yt-dlp quickly if a specialized
        # Railway service is cold-starting or sluggish.
        r = _rq.post(cfg["base"] + "/info", json={"url": url}, timeout=12)
        data = r.json() if r.content else {}
    except Exception as e:
        return None, f"Backend unreachable: {e}"
    if r.status_code >= 400 or data.get("error"):
        return None, data.get("error") or f"HTTP {r.status_code}"
    # Each specialized backend returns at least {title, uploader, duration, thumbnail}.
    # Few return a real `formats` list — synthesize a single best option if absent so
    # the reclip frontend can show a download button.
    dur = data.get("duration")
    if isinstance(dur, str):
        # convert "1:23" → 83 sec for the frontend formatter
        m = re.match(r"^(?:(\d+):)?(\d+):(\d+)$", dur.strip())
        if m:
            h = int(m.group(1) or 0); mi = int(m.group(2)); s = int(m.group(3))
            dur = h * 3600 + mi * 60 + s
        else:
            dur = data.get("duration_sec") or None
    formats = data.get("formats")
    if not formats:
        formats = [{"id": "best", "label": "Best quality", "height": 0}]
    return ({
        "title":     data.get("title", "") or "",
        "thumbnail": data.get("thumbnail", "") or "",
        "duration":  dur,
        "uploader":  data.get("uploader", "") or "",
        "formats":   formats,
        "_backend":  cfg["name"],
    }, None)


def _native_start(cfg, url, format_choice, format_id):
    """Run a native extractor's download() in a background thread, hand back a
    reclip job_id. The downloaded file lands in DOWNLOAD_DIR so /api/file can
    serve it via send_file() — same path the local yt-dlp pipeline uses."""
    native = cfg["native"]
    local_id = uuid.uuid4().hex[:10]
    jobs[local_id] = {"status": "downloading", "url": url, "progress": 0}

    def _run():
        def _on_progress(pct):
            if local_id in jobs:
                jobs[local_id]["progress"] = pct
        try:
            kwargs = {}
            if format_id and format_id != "best":
                kwargs["format_id"] = format_id
            try:
                path, filename, err = native.download(
                    url, format_choice, DOWNLOAD_DIR,
                    on_progress=_on_progress, **kwargs)
            except TypeError:
                # extractor that doesn't accept format_id kwarg
                path, filename, err = native.download(
                    url, format_choice, DOWNLOAD_DIR, on_progress=_on_progress)
            if err or not path:
                jobs[local_id].update({"status": "error", "error": err or "Native download failed."})
                return
            jobs[local_id].update({
                "status": "done", "file": path, "filename": filename, "progress": 100,
            })
        except Exception as e:
            jobs[local_id].update({"status": "error", "error": str(e)})

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return local_id, None


def _proxy_start(cfg, url, format_choice, format_id):
    """Start a download. Routes to native extractor if configured; otherwise
    POSTs to <backend>/start (or /direct for tiktok HTTP variant)."""
    if cfg.get("native"):
        return _native_start(cfg, url, format_choice, format_id)
    path = "/direct" if "tiktok.com" in url else "/start"
    payload = {"url": url, "format": "audio" if format_choice == "audio" else "video"}
    if format_id and format_id != "best":
        payload["format_id"] = format_id
    try:
        r = _rq.post(cfg["base"] + path, json=payload, timeout=20)
        data = r.json() if r.content else {}
    except Exception as e:
        return None, f"Backend unreachable: {e}"
    if r.status_code >= 400 or data.get("error"):
        return None, data.get("error") or f"HTTP {r.status_code}"

    local_id = uuid.uuid4().hex[:10]
    # tiktok /direct returns immediate URL; others return a {job_id} async handle
    if data.get("job_id"):
        jobs[local_id] = {
            "status": "downloading",
            "url": url,
            "remote_backend": cfg["base"],
            "remote_job_id": data["job_id"],
        }
    else:
        # Synchronous backend (TikTok-style) — already done, file URL provided.
        direct = data.get("video") or data.get("audio_only") or data.get("file") or data.get("url")
        jobs[local_id] = {
            "status": "done" if direct else "error",
            "url": url,
            "direct_url": direct,
            "filename": data.get("filename") or "reclip_download",
            "error": None if direct else "Backend returned no file URL",
        }
    return local_id, None


def _proxy_status(job):
    """Refresh a backend-backed job from its remote /status endpoint."""
    if "remote_job_id" not in job:
        return  # local or already-resolved
    try:
        r = _rq.get(f"{job['remote_backend']}/status/{job['remote_job_id']}", timeout=15)
        d = r.json() if r.content else {}
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"Backend unreachable: {e}"
        return
    if d.get("error"):
        job["status"] = "error"
        job["error"] = d["error"]
        return
    st = d.get("status") or d.get("state")
    if st in ("done", "ready", "complete", "completed", "finished"):
        job["status"] = "done"
        job["filename"] = d.get("filename") or "reclip_download"
        job["direct_url"] = f"{job['remote_backend']}/download/{job['remote_job_id']}"
    elif st in ("error", "failed"):
        job["status"] = "error"
        job["error"] = d.get("error") or "Backend reported failure"
    else:
        job["status"] = "downloading"


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # ── Try specialized backend first; on failure fall through to yt-dlp.
    key, cfg = detect_backend(url)
    if cfg:
        normalized, err = _proxy_info(cfg, url)
        if normalized:
            return jsonify(normalized)
        # else: log and continue to local yt-dlp as a fallback
        print(f"[router] {cfg['name']} backend failed ({err}); falling back to yt-dlp", flush=True)

    cmd = ["yt-dlp", "--no-playlist", "-j"] + yt_dlp_base_args(url) + [url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # Try specialized backend first; fall back to local yt-dlp on failure.
    _key, cfg = detect_backend(url)
    if cfg:
        local_id, err = _proxy_start(cfg, url, format_choice, format_id)
        if local_id:
            jobs[local_id]["title"] = title
            return jsonify({"job_id": local_id})
        print(f"[router] {cfg['name']} /start failed ({err}); falling back to yt-dlp", flush=True)

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if "remote_job_id" in job and job["status"] == "downloading":
        _proxy_status(job)
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404

    # Backend-backed jobs: stream the bytes through reclip so the browser sees
    # a same-origin attachment download. Required for mobile Safari/Chrome —
    # cross-origin <a download> opens inline instead of saving the file.
    if job.get("direct_url"):
        from flask import Response, stream_with_context
        fname = job.get("filename") or "reclip_download"
        try:
            upstream = _rq.get(job["direct_url"], stream=True, timeout=30, allow_redirects=True)
        except Exception as e:
            return jsonify({"error": f"Upstream unreachable: {e}"}), 502
        if upstream.status_code >= 400:
            return jsonify({"error": f"Upstream HTTP {upstream.status_code}"}), 502

        ctype = upstream.headers.get("Content-Type", "application/octet-stream")
        # Force a real attachment download regardless of the upstream's headers.
        headers = {
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Content-Type": ctype,
        }
        if upstream.headers.get("Content-Length"):
            headers["Content-Length"] = upstream.headers["Content-Length"]

        def _gen():
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk

        return Response(stream_with_context(_gen()), headers=headers)

    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
