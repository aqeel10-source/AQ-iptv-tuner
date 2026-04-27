#!/bin/bash
# AQ-IPTV Tuner deploy script.
#
# Synology-specific notes:
#  - Docker socket access requires sudo. The original script called bare
#    `docker-compose` which only worked when run as `sudo bash deploy.sh`
#    interactively (typing the sudo password). Non-interactive SSH deploys
#    failed with "Permission denied" on the docker socket.
#  - Even with sudo, `docker-compose`'s child `docker` invocation broke
#    because the sudoers `secure_path` strips /usr/local/bin, so
#    docker-compose's internal `subprocess.Popen('docker', ...)` could not
#    locate `docker` and crashed with FileNotFoundError.
#  - Fix: bypass docker-compose entirely. Both `/usr/local/bin/docker` and
#    `/usr/local/bin/docker-compose` are in the user's NOPASSWD sudoers
#    list, but only `docker` is safe to call from a non-interactive shell
#    (it talks to the daemon over the unix socket without forking children).
#    All container parameters that used to live in docker-compose.yml are
#    now inlined into the `docker run` command below — keep the two in sync
#    if either changes.
#
# This script is idempotent and runnable as plain `bash deploy.sh` (no
# wrapping sudo needed).

set -euo pipefail
IFS=$'\n\t'

# Belt-and-braces: even though we sudo into /usr/local/bin/docker directly,
# this PATH export keeps interactive `docker` invocations working if the
# script is sourced or pasted into a shell.
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

APP_DIR="/volume1/docker/AQ-iptv-tuner/app"
LOG_DIR="/volume1/docker/AQ-iptv-tuner/logs"
DEPLOY_LOG="$LOG_DIR/deploy.log"
CONFIG_DIR="/volume1/docker/AQ-iptv-tuner/config"
ENV_FILE="$APP_DIR/.env"
CONTAINER="iptv-tuner"
IMAGE="app_iptv-tuner"

# Direct path to docker so sudo doesn't need PATH lookups.
DOCKER="/usr/local/bin/docker"

mkdir -p "$LOG_DIR"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$DEPLOY_LOG"; }

on_error() {
    local lineno=$1
    log "ERROR: deploy failed at line $lineno"
    exit 1
}
trap 'on_error $LINENO' ERR

log "Deploy started"

cd "$APP_DIR"

log "Stopping container..."
sudo "$DOCKER" stop "$CONTAINER" 2>/dev/null || true
sudo "$DOCKER" rm "$CONTAINER"   2>/dev/null || true

log "Removing old image..."
sudo "$DOCKER" rmi "$IMAGE" 2>/dev/null || true

log "Building fresh image..."
sudo "$DOCKER" build --no-cache -t "$IMAGE" .

log "Starting container..."
# Port bindings (Phase 13 hardening, 2026-04-09):
#  - 5004 (admin) is bound to TWO host IPs only:
#      * 127.0.0.1  — so DSM's reverse-proxy vhost (proxy_pass http://localhost:5004)
#                     keeps working.
#      * YOUR-NAS-IP — the LAN interface IP, so phones/Plex on the
#                     local network can hit it directly without going
#                     through the reverse proxy.
#    Anything not in that list (docker bridges, tailnet/tun0 if it ever
#    comes back, link-local v6, etc.) gets nothing — kernel won't even
#    answer SYN on 5004 from those interfaces.
#  - 34343 (public stream) stays on 0.0.0.0 — must be reachable wherever
#    Xtream clients hit the NAS from. Hardening for this port lives inside
#    the app via StreamPortFilterMiddleware (only Xtream-credentialed paths
#    are served here; the channel inventory and HDHR endpoints were trimmed
#    in the same hardening pass).
# `--user UID:GID` — set to your Synology user's UID:GID so the container can
# write to the bind-mounted /config dir without being root.
# Find yours with: id YOUR_USERNAME  (e.g. uid=1026 gid=100)
sudo "$DOCKER" run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    --user 1026:100 \
    -p 127.0.0.1:5004:5004 \
    -p YOUR-NAS-IP:5004:5004 \
    -p 34343:34343 \
    -v "$CONFIG_DIR:/config" \
    --env-file "$ENV_FILE" \
    -e CONFIG_FILE=/config/config.json \
    -e TZ=Asia/Riyadh \
    --log-driver json-file \
    --log-opt max-size=10m \
    --log-opt max-file=3 \
    "$IMAGE"

log "Waiting for startup..."
# Poll the discover endpoint instead of a fixed sleep.
# Cold-cache M3U fetch from upstream providers can take 2-3 minutes
# (e.g. KSA4YOu + IGate TV combined parse ~450k channels). 90s used to
# false-report failure even though the container was healthy ~3 min in.
HEALTH_TIMEOUT=240
for i in $(seq 1 "$HEALTH_TIMEOUT"); do
    if curl -fsS --max-time 2 http://127.0.0.1:5004/health >/dev/null 2>&1; then
        log "Container is healthy (after ${i}s)"
        break
    fi
    sleep 1
    if [ "$i" = "$HEALTH_TIMEOUT" ]; then
        log "ERROR: container did not become healthy in ${HEALTH_TIMEOUT}s"
        sudo "$DOCKER" logs "$CONTAINER" --tail=40 | tee -a "$DEPLOY_LOG"
        exit 1
    fi
done

echo ""
log "Verifying..."
MUX=$(sudo "$DOCKER" exec "$CONTAINER" grep -c "ChannelMux"   /app/main.py 2>/dev/null || echo 0)
AUTH=$(sudo "$DOCKER" exec "$CONTAINER" grep -c "_check_session" /app/main.py 2>/dev/null || echo 0)
LOGS=$(sudo "$DOCKER" exec "$CONTAINER" grep -c "log_event"  /app/main.py 2>/dev/null || echo 0)
log "ChannelMux : $MUX"
log "Auth       : $AUTH"
log "log_event  : $LOGS"

echo ""
echo "════════════════════════════════════════"
echo "  Logs (last 20 lines):"
echo "════════════════════════════════════════"
sudo "$DOCKER" logs "$CONTAINER" --tail=20

echo ""
log "Deploy complete"
log "  Login : http://YOUR-NAS-IP:5004/"
log "  Status: http://YOUR-NAS-IP:5004/api/status"
