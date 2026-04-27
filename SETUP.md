# AQ-IPTV Tuner — Setup Guide

A self-hosted IPTV proxy that aggregates multiple Xtream Codes subscriptions,
de-duplicates channels, and exposes a unified M3U + HDHomeRun + Xtream endpoint
for Plex, Emby, TiviMate, and other clients.

---

## Prerequisites

- Synology NAS (or any Linux host) with Docker installed
- One or more IPTV subscriptions that use the Xtream Codes protocol
- (Optional) Plex Media Server for live TV
- (Optional) Telegram bot for alerts

---

## Quick start

### 1. Copy files to your NAS

```
/volume1/docker/AQ-iptv-tuner/
├── app/            ← application code (from this repo)
├── config/         ← your config lives here (created in step 3)
└── logs/           ← auto-created on first run
```

### 2. Create the config directory

```bash
mkdir -p /volume1/docker/AQ-iptv-tuner/config
```

### 3. Create config.json

Copy `config.example.json` to `/volume1/docker/AQ-iptv-tuner/config/config.json`
and fill in every field (all `YOUR-...` placeholders must be replaced).

Key fields:

| Field | What to put |
|---|---|
| `hostname` | Friendly name shown in the admin UI header and Telegram alerts |
| `server.base_url` | `http://YOUR-NAS-IP:5004` (your NAS LAN IP) |
| `server.device_id` | Any 8-character hex string, e.g. `A1B2C3D4` |
| `subscriptions[].url` | Your provider's Xtream base URL (no path) |
| `auth.username` | Admin UI login username |
| `auth.password_hash` | SHA-256 hash of your chosen password (see below) |
| `xtream.server_url` | `http://YOUR-NAS-IP:34343` (the public stream port) |

**Generate password hash:**
```bash
python3 -c "import hashlib; print(hashlib.sha256('yourpassword'.encode()).hexdigest())"
```

### 4. Create .env

Copy `app/.env.example` to `app/.env` and fill in:

```bash
cp app/.env.example app/.env
chmod 600 app/.env
```

**Generate a secure RELOAD_TOKEN:**
```bash
openssl rand -hex 32
```

For each subscription in config.json, add credentials using the slug pattern:
- Subscription name `"My Provider"` → `SUB_MY_PROVIDER_USERNAME` / `SUB_MY_PROVIDER_PASSWORD`
- Subscription name `"KSA4You.com"` → `SUB_KSA4YOU_COM_USERNAME` / `SUB_KSA4YOU_COM_PASSWORD`

Rule: uppercase the name, replace any non-alphanumeric character with `_`.

### 5. Edit deploy.sh

Replace `YOUR-NAS-IP` with your actual NAS LAN IP address.
Replace `YOUR-USERNAME` and the UID:GID with your Synology user's values:

```bash
id YOUR_USERNAME   # shows uid=XXXX gid=XXX
```

### 6. Deploy

```bash
bash deploy.sh
```

The script will build the Docker image and start the container. It polls
`/health` for up to 4 minutes (initial M3U fetch from providers can be slow).

### 7. Access the admin UI

```
http://YOUR-NAS-IP:5004/
```

Log in with the username and password you set in step 3.

---

## Scheduled tasks (optional but recommended)

Two scripts in `scripts/` should be registered as Synology DSM Scheduled Tasks:

### Hourly channel refresh

Calls `/reload` to re-fetch M3U lists from providers without restarting.

- Script: `scripts/task-refresh.sh`
- Trigger: Every 1 hour
- User: root
- Edit `TUNER_URL` and `EMAIL` in the script before installing

### Boot-up container check

Ensures the container starts after every NAS reboot.

- Script: `scripts/task-boot.sh`
- Trigger: Boot-up
- User: root
- Edit `EMAIL` in the script before installing

**DSM → Control Panel → Task Scheduler → Create → Scheduled/Triggered Task → User-defined script**

---

## Connecting clients

| Client | URL |
|---|---|
| **M3U (generic)** | `http://YOUR-NAS-IP:5004/m3u` |
| **EPG (XMLTV)** | `http://YOUR-NAS-IP:5004/epg.xml` |
| **Xtream Codes** | Server: `http://YOUR-NAS-IP:34343`, use your admin credentials |
| **HDHomeRun (Plex)** | Plex will auto-discover via `http://YOUR-NAS-IP:5004/discover.json` |
| **Admin UI** | `http://YOUR-NAS-IP:5004/` |

---

## Plex Live TV setup

1. In Plex: Settings → Live TV & DVR → Set Up Plex DVR
2. Plex will scan and find the tuner automatically
3. In `config.json`, set `plex.enabled: true`, add your Plex URL and token
4. The admin UI will then show Plex viewer names instead of "Plex"

**Get your Plex token:** Sign in at plex.tv, open Developer Tools (F12),
Network tab, filter for `X-Plex-Token` in any request header.

---

## Port reference

| Port | Purpose | Bind |
|---|---|---|
| `5004` | Admin UI + M3U + EPG + HDHR emulation | LAN only (see deploy.sh) |
| `34343` | Xtream Codes stream port | 0.0.0.0 (all interfaces) |

Restrict port 5004 to localhost + LAN in `deploy.sh` — it has no rate limiting.
Port 34343 is protected per-path by session token validation.

---

## Troubleshooting

**Container won't start / health check timeout**
- Check logs: `sudo docker logs iptv-tuner --tail 50`
- Usually a bad `config.json` (JSON syntax error or missing required field)

**No channels loading**
- Verify subscription credentials in `.env` and `config.json`
- Test the provider URL directly: `curl "http://provider.com/get.php?username=X&password=Y&type=m3u_plus"`

**Admin UI shows 0 channels**
- After first start, trigger a manual reload: `POST /reload` with `X-Reload-Token` header
- Or wait — initial M3U fetch runs automatically on startup

**Plex can't find tuner**
- Ensure Plex and this container are on the same network (or same Docker bridge)
- Plex looks for HDHomeRun on UDP port 65001 multicast — on Synology this usually works out of the box
