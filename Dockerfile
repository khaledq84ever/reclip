FROM python:3.12-slim

# System deps + Node 20 (needed for bgutil PO Token server build)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# TypeScript globally so `npx tsc` always works for bgutil
RUN npm install -g --no-audit --no-fund typescript@5

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install -U yt-dlp bgutil-ytdlp-pot-provider

# bgutil PO Token server — bypasses YouTube bot detection from datacenter IPs without cookies
RUN git clone --depth=1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /app/bgutil-ytdlp-pot-provider && \
    cd /app/bgutil-ytdlp-pot-provider/server && \
    npm install --no-audit --no-fund && \
    (npm run build 2>/dev/null || tsc) && \
    test -f build/main.js && echo "bgutil build OK" || (echo "bgutil build FAILED" && exit 1)

# Verify the yt-dlp plugin loaded
RUN python -c "from yt_dlp_plugins.extractor import getpot_bgutil_http; print('bgutil yt-dlp plugin OK')"

COPY . .

EXPOSE 8899
ENV HOST=0.0.0.0
CMD ["python", "app.py"]
