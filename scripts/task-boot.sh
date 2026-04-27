#!/bin/bash
# ─────────────────────────────────────────────────────────────
# Task: iptv-tuner-boot
# Trigger: Triggered Task → Boot-up
# User: root
# Description: Ensures the iptv-tuner container is running
#              after every Synology NAS reboot.
# ─────────────────────────────────────────────────────────────

# Ensure docker is in PATH (Synology cron runs with minimal PATH)
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

CONTAINER="iptv-tuner"
LOGFILE="/volume1/docker/AQ-iptv-tuner/logs/boot.log"
EMAIL="your-email@example.com"  # optional: DSM email for boot notifications
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# Ensure log directory exists
mkdir -p /volume1/docker/AQ-iptv-tuner/logs

echo "[$TIMESTAMP] Boot task started" >> "$LOGFILE"

# Wait for Docker daemon to be fully ready
sleep 10

# Check if container exists
if ! docker inspect "$CONTAINER" &>/dev/null; then
    echo "[$TIMESTAMP] ERROR: Container '$CONTAINER' not found. Was it ever created?" >> "$LOGFILE"
    /usr/syno/bin/synonotify mail \
        --to "$EMAIL" \
        --subject "[IPTV Tuner] ERROR: Container not found on boot" \
        --body "The iptv-tuner container was not found on $(hostname) after reboot at $TIMESTAMP. Please check that the container was created correctly."
    exit 1
fi

# Start the container if it is not already running
STATUS=$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)

if [ "$STATUS" = "true" ]; then
    echo "[$TIMESTAMP] Container already running — nothing to do." >> "$LOGFILE"
else
    echo "[$TIMESTAMP] Starting container '$CONTAINER'..." >> "$LOGFILE"
    docker start "$CONTAINER" >> "$LOGFILE" 2>&1

    # Verify it started
    sleep 5
    STATUS=$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)

    if [ "$STATUS" = "true" ]; then
        echo "[$TIMESTAMP] Container started successfully." >> "$LOGFILE"
    else
        echo "[$TIMESTAMP] ERROR: Container failed to start." >> "$LOGFILE"
        /usr/syno/bin/synonotify mail \
            --to "$EMAIL" \
            --subject "[IPTV Tuner] ERROR: Failed to start on boot" \
            --body "The iptv-tuner container failed to start on $(hostname) after reboot at $TIMESTAMP. Please check Container Manager for details."
        exit 1
    fi
fi

echo "[$TIMESTAMP] Boot task finished." >> "$LOGFILE"
exit 0
