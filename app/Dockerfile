# A32: pinned base image digest-less but minor-locked
FROM python:3.12.7-slim

# A22: curl needed for HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY login.html .
COPY pwa/ ./pwa/
COPY ui/ ./ui/

# A33: drop VOLUME — the config dir is a bind-mount from the host,
# and a declared VOLUME hides bind-mount metadata issues on Synology.
ENV CONFIG_FILE=/config/config.json \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 5004 34343

# A21: run as unprivileged user
RUN groupadd -r iptv && useradd -r -g iptv -d /app -s /sbin/nologin iptv \
    && chown -R iptv:iptv /app
USER iptv

# E2: /health checks channels are loaded (more meaningful than /discover.json)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5004/health || exit 1

CMD ["python", "main.py"]
