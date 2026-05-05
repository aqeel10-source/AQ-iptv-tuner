# AQ-IPTV Tuner

A self-hosted IPTV proxy that aggregates multiple Xtream Codes subscriptions, de-duplicates channels, and exposes a unified endpoint for Plex, Emby, TiviMate, and other clients.

---

## Screenshots

### Admin Dashboard
![Admin Dashboard](screenshots/dashboard.png)

### Channel List
![Channel List](screenshots/channels.png)

### Subscription Management
![Subscriptions](screenshots/subscriptions.png)

### User Management (Xtream)
![Users](screenshots/users.png)

### Live Stream Monitor
![Stream Monitor](screenshots/stream-monitor.png)

### Group & Channel Editor
![Group Editor](screenshots/group-editor.png)

---

## Features

- **Multi-provider aggregation** — combine multiple Xtream Codes subscriptions into one unified channel list
- **Channel deduplication** — merge duplicate channels across providers with configurable renames and merges
- **HDHomeRun emulation** — Plex discovers it as a network tuner automatically
- **Xtream Codes server** — full compatibility with TiviMate, IPTV Smarters, and other Xtream clients
- **M3U + EPG** — standard endpoints for any IPTV player
- **Admin web UI** — manage channels, subscriptions, users, and groups from a browser
- **Per-viewer session tracking** — see who is watching what in real time
- **Jitter buffer** — smooths upstream delivery gaps for Plex live TV
- **Plex-aware viewer names** — shows actual Plex usernames instead of IP addresses
- **Telegram alerts** — notifications for stream events and failovers
- **Automatic failover** — switches providers on stall or quality degradation
- **Logo caching** — channel logos cached locally to reduce upstream requests
- **Blocklist** — automatic IP blocking after repeated auth failures

---

## Quick Start

See [SETUP.md](SETUP.md) for full installation instructions.

**Requirements:**
- Docker (Synology NAS or any Linux host)
- One or more IPTV subscriptions using Xtream Codes protocol

**Ports:**

| Port | Purpose |
|---|---|
| `5004` | Admin UI, M3U, EPG, HDHomeRun (LAN only) |
| `34343` | Xtream Codes streaming (public) |

---

## Connecting Clients

| Client | How |
|---|---|
| **Plex Live TV** | Auto-discovered as HDHomeRun tuner via `http://NAS-IP:5004` |
| **TiviMate / Xtream** | Server: `http://NAS-IP:34343` |
| **M3U players** | `http://NAS-IP:5004/m3u` |
| **EPG** | `http://NAS-IP:5004/epg.xml` |
