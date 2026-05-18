# reclip — Universal Video Downloader

## What it is
Flask app wrapping yt-dlp with per-site extractor modules. One backend serves all platforms.

- **Live**: https://reclip-production-3442.up.railway.app
- **GitHub**: https://github.com/khaledq84ever/reclip
- **Stack**: Flask 3 + yt-dlp[curl-cffi] + cloudscraper + bgutil PO Token server
- **Port**: 8899
- **Deploy**: Railway service `reclip-production-3442`, Dockerfile-only (no railway.toml)

## Layout
```
app.py                  # Flask app + routing
extractors/             # Per-site extractor modules
├── __init__.py
├── youtube.py          # Uses bgutil PO Token bypass
├── tiktok.py
├── instagram.py
├── twitter.py
├── snapchat.py
├── kick.py             # Uses curl_cffi for Cloudflare bypass
└── facebook.py
templates/index.html    # Single-page UI
static/                 # favicon, CSS
```

To add a site: create `extractors/<site>.py` with the extractor function, wire it into `extractors/__init__.py`.

## Critical conventions

### yt-dlp is INTENTIONALLY unpinned
`requirements.txt` says `yt-dlp>=2025.1.15` and the Dockerfile runs `pip install -U yt-dlp` on every build. **Do not "fix" this by pinning.** yt-dlp ships site fixes weekly — pinning means extractors break within weeks. The tradeoff: an occasional yt-dlp release introduces breaking changes, requiring a quick rollback. That's accepted.

### bgutil PO Token server (YouTube bot bypass)
The Dockerfile builds `bgutil-ytdlp-pot-provider` from source. This bypasses YouTube's bot detection on datacenter IPs (Railway runs in datacenters → would fail without it) **without needing cookies**. If YouTube extraction breaks, suspect bgutil first.
- See [reference: y2mate / iotacloud trick](../.claude/projects/-home-khaled/memory/reference_y2mate_iotacloud_trick.md) for alternate MP3-only bypass.

### curl_cffi for Cloudflare bypass
Used in `extractors/kick.py` (and possibly others). Kick.com sits behind Cloudflare and rejects normal Python requests. `curl_cffi` impersonates a real browser TLS fingerprint.

### Mobile downloads: server-queue + blob-fetch
Never expose CDN URLs directly to mobile clients. Never use `<a download>` for cross-origin URLs on mobile. Queue server-side, return a blob URL the client fetches on tap. See `feedback_mobile_downloads.md` in memory.

### Never use gevent worker
If you ever migrate from `python app.py` to gunicorn, use `gthread`. `gevent` hangs ffmpeg subprocesses. See `feedback_ytdl_gevent.md` in memory.

## Common operations

```bash
# Run locally
python app.py                # serves on :8899

# Test an extractor without redeploying
python -c "from extractors import youtube; print(youtube.extract('https://youtu.be/...'))"

# Deploy to Railway
railway up                   # uses Dockerfile

# Tail prod logs
railway logs --tail 50

# Smoke test prod
curl -s https://reclip-production-3442.up.railway.app/ -o /dev/null -w '%{http_code}\n'
```

## Deploy gotchas
- **Docker build is slow** (~3-5 min) because it clones + builds bgutil from source. Don't skip the bgutil verify step — silent failures break YouTube without warning.
- **No `railway.toml`** — Railway auto-detects Dockerfile. If you add a railway.toml, set `healthcheckPath = "/"`.
- **`pip install -U yt-dlp`** in Dockerfile re-downloads latest every build, so cached layers below it become stale. Worth keeping anyway (see "yt-dlp intentionally unpinned" above).

## Rules (never ask, just do)
- Edit and deploy without confirmation
- Never pin yt-dlp without an explicit request
- Always run a smoke `curl` against the prod URL after deploy
