# AQ-IPTV Tuner — Claude Setup Assistant

You are helping a user deploy AQ-IPTV Tuner on their Synology NAS.
Your job is to gather the required information, generate all config files,
and walk them through every step. Do not proceed past a step until it is confirmed.

---

## How to use this file

Work through the sections below **in order**. Each section lists:
- What information you need from the user
- What you will generate or instruct them to do
- How to verify the step succeeded

Ask for one section's worth of information at a time. Do not dump all questions at once.

---

## Section 1 — NAS basics

Ask the user for:

1. **NAS LAN IP address** — e.g. `192.168.1.100`
   - Used in: `deploy.sh`, `config.json` (server.base_url, xtream.server_url)

2. **Synology username** they will run the container as
   - You will need their UID and GID for the `--user` flag in deploy.sh
   - Instruct them to run this command in DSM → Control Panel → Task Scheduler
     (create a one-time task, run as their user, script body: `id > /volume1/docker/uid.txt`)
     then read `/volume1/docker/uid.txt`
   - OR: if they have terminal access, run `id USERNAME`
   - You need: `uid=XXXX` and `gid=XXX`

3. **Where they want to install** — default is `/volume1/docker/AQ-iptv-tuner`
   - If they want a different path, note it — replace all occurrences in configs

Once you have this, generate the corrected `deploy.sh` by replacing:
- `YOUR-NAS-IP` → their actual NAS IP
- `1026:100` → their actual `uid:gid`

---

## Section 2 — Admin password

Ask the user to **choose a password** for the admin UI login.
Also ask what **username** they want (default: `admin`).

Generate the SHA-256 hash. You can do this by running:
```bash
python3 -c "import hashlib; print(hashlib.sha256('THEIR_PASSWORD'.encode()).hexdigest())"
```

Save the hash — you will need it for `config.json` → `auth.password_hash`.
Tell the user their hash (they do not need to store the plaintext password anywhere — only the hash goes in config).

---

## Section 3 — IPTV subscriptions

The app supports any provider that uses the **Xtream Codes** protocol.

Ask the user for each subscription they want to add:
1. **Provider name** — a short friendly label, e.g. `My Provider`
2. **Provider base URL** — e.g. `http://provider-domain.com` (no path, no trailing slash)
3. **Username** for that subscription
4. **Password** for that subscription
5. **Max concurrent streams** allowed by their plan (ask their provider if unsure; default: 1)
6. **Priority** — if they have multiple providers, which should be tried first? (1 = highest)

Repeat for each subscription. Most users will have 1–3.

For each subscription, you will generate:
- An entry in `config.json` → `subscriptions[]`
- Two lines in `.env`: `SUB_SLUG_USERNAME` and `SUB_SLUG_PASSWORD`

**Slug rule:** Take the provider name, uppercase it, replace every non-alphanumeric character with `_`.
Examples:
- `My Provider` → `MY_PROVIDER`
- `KSA4You.com` → `KSA4YOU_COM`
- `IGate TV` → `IGATE_TV`

The `username` and `password` fields in `config.json` subscriptions should be set to
`${SUB_SLUG_USERNAME}` and `${SUB_SLUG_PASSWORD}` — the app resolves them from `.env` at runtime.

---

## Section 4 — Telegram alerts (optional)

Ask: "Do you want Telegram alerts? (daily summary, stream errors, container health)"

If **yes**, ask:
1. **Bot token** — create a bot via [@BotFather](https://t.me/BotFather), copy the token
2. **Chat ID** — send any message to [@userinfobot](https://t.me/userinfobot), it replies with your chat ID
3. **Daily summary hour** — what hour (0–23, their local time) to receive the daily report? Default: 8

Set `telegram.enabled: true` in config and populate `bot_token` and `chat_id`.

If **no**, set `telegram.enabled: false` and leave token/chat_id as placeholders.

---

## Section 5 — Plex (optional)

Ask: "Are you using Plex Media Server for live TV?"

If **yes**, ask:
1. **Plex URL** — usually `http://YOUR-NAS-IP:32400` (same NAS) or another host
2. **Plex token** — how to find it:
   - Open Plex Web, sign in
   - Open any item, click the `...` menu → Get Info → View XML
   - In the URL bar you will see `X-Plex-Token=XXXX` — copy that value

Set `plex.enabled: true` and populate `url` and `token`.

If **no**, set `plex.enabled: false`.

---

## Section 6 — Hostname / branding

Ask: "What name should appear in the admin UI header and Telegram alerts?"
Default: `iptv-tuner`
Example: `home-tv` or `nas-iptv`

This sets `config.json` → `"hostname"`.

---

## Section 7 — Generate all files

Once sections 1–6 are complete, generate and output the following files in full.
Tell the user exactly where to save each one.

### File 1: `/volume1/docker/AQ-iptv-tuner/app/.env`
```
# AQ-IPTV Tuner — runtime secrets. chmod 600. Never commit.
RELOAD_TOKEN=<generate with: openssl rand -hex 32 — or generate it yourself>

SUB_SLUG_USERNAME=their-username
SUB_SLUG_PASSWORD=their-password
# ... repeat per subscription
```

For `RELOAD_TOKEN`: generate a random 64-character hex string yourself (python3 `os.urandom(32).hex()`)
and include it directly in the file. Tell the user this token is used by the hourly refresh script.

### File 2: `/volume1/docker/AQ-iptv-tuner/config/config.json`
Use `config.example.json` as the template. Fill in all values gathered in sections 1–6.
Remove all `_comment` keys from the final output (they are for reference only).
Populate subscriptions array, auth, telegram, plex, hostname, server, xtream sections.
Leave `channel_merges`, `channel_renames`, `quality_overrides`, etc. as empty objects/arrays.

### File 3: `deploy.sh` (already in the app folder)
Output the corrected version with:
- `YOUR-NAS-IP` replaced with their IP
- `1026:100` replaced with their `uid:gid`

---

## Section 8 — Docker deploy

Instruct the user to:

1. Copy the entire `AQ-iptv-tuner-Public` folder to their NAS at:
   `/volume1/docker/AQ-iptv-tuner/app/`

   In DSM File Station: upload the folder, or use whatever method they have.

2. Create the config directory and place `config.json`:
   ```
   /volume1/docker/AQ-iptv-tuner/config/config.json
   ```

3. Place `.env` at:
   ```
   /volume1/docker/AQ-iptv-tuner/app/.env
   ```
   Then set permissions (via DSM Task Scheduler or terminal):
   ```bash
   chmod 600 /volume1/docker/AQ-iptv-tuner/app/.env
   ```

4. Run the deploy script via DSM → Control Panel → Task Scheduler:
   - Create Scheduled Task → User-defined script → Run as root
   - Script: `bash /volume1/docker/AQ-iptv-tuner/app/deploy.sh`
   - Run it once manually (Action → Run)

5. Check the deploy log:
   ```
   /volume1/docker/AQ-iptv-tuner/logs/deploy.log
   ```
   The last line should say `Deploy complete`.

6. Open the admin UI at: `http://THEIR-NAS-IP:5004/`

---

## Section 9 — Scheduled tasks

Instruct the user to create two tasks in DSM → Control Panel → Task Scheduler:

**Task 1: Hourly channel refresh**
- Type: Scheduled Task → User-defined script
- User: root
- Schedule: Every 1 hour
- Script: `bash /volume1/docker/AQ-iptv-tuner/app/scripts/task-refresh.sh`

Before creating: update `TUNER_URL` in `scripts/task-refresh.sh`:
- Replace `YOUR-NAS-IP` with their NAS IP
- Replace `your-email@example.com` with their email (or leave placeholder if no DSM email configured)
- The `RELOAD_TOKEN` is read automatically from `.env` — no change needed there

**Task 2: Boot-up container start**
- Type: Triggered Task → Boot-up
- User: root
- Script: `bash /volume1/docker/AQ-iptv-tuner/app/scripts/task-boot.sh`

Before creating: update `EMAIL` in `scripts/task-boot.sh` (or leave placeholder).

---

## Section 10 — Connect a client

Once the admin UI shows channels, help them connect their preferred client:

| Client | What to enter |
|---|---|
| **TiviMate / Sparkle** | Xtream server: `http://NAS-IP:34343`, username + password from config.json auth |
| **Plex DVR** | Plex auto-discovers — go to Settings → Live TV & DVR → Set Up |
| **Any M3U app** | `http://NAS-IP:5004/m3u` |
| **EPG** | `http://NAS-IP:5004/epg.xml` |

---

## Checklist — do not finish until all are confirmed

- [ ] `config.json` has no placeholder values remaining
- [ ] `.env` has real credentials for every subscription in config.json
- [ ] `deploy.sh` has real NAS IP and real uid:gid
- [ ] Container started and `/health` returns 200
- [ ] Admin UI loads and shows channels
- [ ] At least one client can play a channel
