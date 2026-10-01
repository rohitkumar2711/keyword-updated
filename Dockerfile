FROM python:3.12-slim

# System tools:
#  - unrar (Debian non-free): real RAR support incl. RAR5 + passwords (rarfile auto-detects it)
#  - p7zip-full: 7z fallback for archives
#  - curl: used by the log-channel upload fallback
#  - ca-certificates: TLS for remotezip / outbound HTTPS
RUN set -eux; \
    echo "deb http://deb.debian.org/debian bookworm non-free non-free-firmware" >> /etc/apt/sources.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends unrar p7zip-full curl ca-certificates; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for better layer caching.
# tgcrypto and zipfile-deflate64 ship no wheels for this Python, so they compile
# from source: install gcc for the build, then remove it to keep the image slim.
COPY requirements.txt .
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends gcc libc6-dev; \
    pip install --no-cache-dir --break-system-packages -r requirements.txt; \
    apt-get purge -y --auto-remove gcc libc6-dev; \
    rm -rf /var/lib/apt/lists/*

COPY . .

# Persistent data (SQLite db + pyrogram .session) lives here; mount a volume at /app/data.
ENV DATA_DIR=/app/data
# Flush print() output immediately so crash messages show up in the container logs
ENV PYTHONUNBUFFERED=1
RUN mkdir -p /app/data

# Pure worker (no web server). Set HEALTHCHECK_PORT if your platform needs an HTTP check.
CMD ["python", "bot.py"]
