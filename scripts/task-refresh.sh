#!/bin/bash
# ─────────────────────────────────────────────────────────────
# Task: iptv-tuner-refresh
# Trigger: Scheduled Task → Every 1 hour
# User: root
# Description: Forces the IPTV tuner to re-fetch M3U channel
#              lists from all providers without restarting.
# ─────────────────────────────────────────────────────────────

# Ensure docker is in PATH (Synology cron runs with minimal PATH)
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

CONTAINER="iptv-tuner"
TUNER_URL="http://YOUR-NAS-IP:5004"  # replace with your NAS IP
LOGFILE="/volume1/docker/AQ-iptv-tuner/logs/refresh.log"
ENV_FILE="/volume1/docker/AQ-iptv-tuner/app/.env"
EMAIL="your-email@example.com"  # optional: DSM email for refresh alerts
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
MAX_LOG_LINES=500   # Rotate log when it exceeds this

# ── Ensure log directory ──────────────────────────────────────
mkdir -p /volume1/docker/AQ-iptv-tuner/logs

# ── Log rotation (keep last 500 lines) ───────────────────────
if [ -f "$LOGFILE" ]; then
    LINES=$(wc -l < "$LOGFILE")
    if [ "$LINES" -gt "$MAX_LOG_LINES" ]; then
        tail -n 400 "$LOGFILE" > "${LOGFILE}.tmp" && mv "${LOGFILE}.tmp" "$LOGFILE"
    fi
fi

echo "[$TIMESTAMP] Refresh task started" >> "$LOGFILE"

# ── Load RELOAD_TOKEN from .env (never hardcode secrets in-script) ──
RELOAD_TOKEN=""
if [ -r "$ENV_FILE" ]; then
    RELOAD_TOKEN=$(grep -E '^RELOAD_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)
fi
if [ -z "$RELOAD_TOKEN" ]; then
    echo "[$TIMESTAMP] WARNING: RELOAD_TOKEN not found in $ENV_FILE — request will 401." >> "$LOGFILE"
fi

# ── 1. Check container is running ────────────────────────────
STATUS=$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)

if [ "$STATUS" != "true" ]; then
    echo "[$TIMESTAMP] WARNING: Container not running. Attempting to start..." >> "$LOGFILE"
    docker start "$CONTAINER" >> "$LOGFILE" 2>&1
    sleep 8

    STATUS=$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)
    if [ "$STATUS" != "true" ]; then
        echo "[$TIMESTAMP] ERROR: Could not start container. Aborting refresh." >> "$LOGFILE"
        /usr/syno/bin/synonotify mail \
            --to "$EMAIL" \
            --subject "[IPTV Tuner] ERROR: Container down — refresh failed" \
            --body "The iptv-tuner container was not running on $(hostname) at $TIMESTAMP and could not be restarted automatically. Channel list refresh was skipped. Please check Container Manager."
        exit 1
    fi

    echo "[$TIMESTAMP] Container restarted successfully." >> "$LOGFILE"
fi

# ── 2. Call /reload endpoint ──────────────────────────────────
echo "[$TIMESTAMP] Calling $TUNER_URL/reload ..." >> "$LOGFILE"

HTTP_RESPONSE=$(curl --silent --show-error --max-time 30 \
    --write-out "HTTPSTATUS:%{http_code}" \
    --request POST \
    --header "X-Reload-Token: $RELOAD_TOKEN" \
    "$TUNER_URL/reload" 2>&1)

HTTP_BODY=$(echo "$HTTP_RESPONSE" | sed -e 's/HTTPSTATUS\:.*//g')
HTTP_STATUS=$(echo "$HTTP_RESPONSE" | tr -d '\n' | sed -e 's/.*HTTPSTATUS://')

echo "[$TIMESTAMP] Response: HTTP $HTTP_STATUS — $HTTP_BODY" >> "$LOGFILE"

# ── 3. Check result ───────────────────────────────────────────
if [ "$HTTP_STATUS" = "200" ]; then
    # Extract channel count from JSON response {"status":"ok","channels":NNNN}
    CHANNEL_COUNT=$(echo "$HTTP_BODY" | grep -o '"channels":[0-9]*' | grep -o '[0-9]*')
    if [ -n "$CHANNEL_COUNT" ]; then
        echo "[$TIMESTAMP] ✓ Channel list refreshed — $CHANNEL_COUNT channels loaded." >> "$LOGFILE"
    else
        echo "[$TIMESTAMP] ✓ Refresh succeeded." >> "$LOGFILE"
    fi
else
    echo "[$TIMESTAMP] ERROR: Refresh failed with HTTP $HTTP_STATUS." >> "$LOGFILE"
    /usr/syno/bin/synonotify mail \
        --to "$EMAIL" \
        --subject "[IPTV Tuner] ERROR: Channel refresh failed (HTTP $HTTP_STATUS)" \
        --body "The hourly channel list refresh failed on $(hostname) at $TIMESTAMP.

URL:  $TUNER_URL/reload
Code: HTTP $HTTP_STATUS
Body: $HTTP_BODY

Please check that the iptv-tuner container is healthy at:
http://YOUR-NAS-IP:5004/"
    exit 1
fi

echo "[$TIMESTAMP] Refresh task finished." >> "$LOGFILE"
exit 0
