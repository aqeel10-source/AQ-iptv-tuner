#!/usr/bin/env python3
import asyncio, collections, gzip, hashlib, html, ipaddress, json, logging, os, re, secrets, signal, socket, sqlite3, string, tempfile, time, uuid
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, unquote
import bcrypt, httpx, psutil
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("iptv")
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
# Silence httpx's per-request INFO logs — they leak the full upstream URL
# including `?username=...&password=...` query params into the container log.
logging.getLogger("httpx").setLevel(logging.WARNING)

CONFIG_FILE = os.getenv("CONFIG_FILE", "config.json")
APP_DIR     = os.path.dirname(os.path.abspath(__file__))
RELOAD_TOKEN = os.getenv("RELOAD_TOKEN", "")  # shared secret for /reload; empty = allow

# Phase 9: persistent rolling log on /config/app.log so container restarts don't
# lose the event history. Attached to the root logger so every log.info/.warning
# call (and the tee'd state.log_event entries) lands in the same grep-friendly
# audit trail. 10 MB per file × 5 backups = ~50 MB retained.
APP_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)) or ".", "app.log")
# Guard against double-attach: `python main.py` runs this module once as
# __main__, then uvicorn.run("main:app") re-imports it as `main`, which would
# otherwise add the file handler twice and double-write every log line.
_root_logger = logging.getLogger()
_already_attached = any(
    isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == os.path.abspath(APP_LOG_FILE)
    for h in _root_logger.handlers)
if not _already_attached:
    try:
        _file_handler = RotatingFileHandler(
            APP_LOG_FILE, maxBytes=10_000_000, backupCount=5)
        _file_handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        _root_logger.addHandler(_file_handler)
        log.info("Phase 9: rolling log -> %s", APP_LOG_FILE)
    except Exception as _exc:
        log.warning("Phase 9: rolling file log init failed: %s", _exc)

# A36: redact provider credentials that sneak into log messages via httpx
# exceptions (HTTPStatusError includes the full request URL).
_CRED_RE = re.compile(r"(username|password)=[^&\s'\"]*", re.IGNORECASE)
def _redact(s) -> str:
    return _CRED_RE.sub(r"\1=<redacted>", str(s))

# A36: derive an env-var slug from a subscription name so creds can live
# in .env instead of config.json.  'IGate TV' -> 'IGATE_TV'.
def _sub_env_slug(name: str) -> str:
    return re.sub(r'[^A-Z0-9]+', '_', name.upper()).strip('_')

# ── Config (cached with mtime invalidation + atomic write) ───────────────────
_CFG_CACHE: Dict[str, object] = {"mtime": 0.0, "data": None}

def load_config():
    """Return parsed config. Cached in memory; reloaded only when file mtime changes."""
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
    except OSError:
        mtime = 0.0
    if _CFG_CACHE["data"] is None or _CFG_CACHE["mtime"] != mtime:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        # Env-var overrides for secrets (A30 — avoids storing rotated tokens in config.json)
        tg = cfg.setdefault("telegram", {})
        if os.getenv("TELEGRAM_BOT_TOKEN"): tg["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN")
        if os.getenv("TELEGRAM_CHAT_ID"):   tg["chat_id"]   = os.getenv("TELEGRAM_CHAT_ID")
        plx = cfg.setdefault("plex", {})
        if os.getenv("PLEX_TOKEN"): plx["token"] = os.getenv("PLEX_TOKEN")
        if os.getenv("PLEX_URL"):   plx["url"]   = os.getenv("PLEX_URL")
        _CFG_CACHE["data"]  = cfg
        _CFG_CACHE["mtime"] = mtime
    return _CFG_CACHE["data"]

# Phase 9: config snapshot on every save — rollback without git, prep for
# Phase 10 user-account edits. Snapshots live beside config.json.
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)) or ".", "backups")

def _ops_cfg() -> dict:
    raw = load_config().get("ops")
    raw = raw if isinstance(raw, dict) else {}
    return {"backup_count": int(raw.get("backup_count", 3))}

def _backup_config_snapshot(cfg) -> None:
    """Write a timestamped snapshot of cfg into BACKUP_DIR, then prune old ones
    keeping the newest N (ops.backup_count). Best-effort — never raises."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(BACKUP_DIR, f"config-{stamp}.json")
        # If a snapshot for this exact second already exists, append a counter
        # so back-to-back saves don't clobber each other.
        n = 1
        while os.path.exists(path):
            path = os.path.join(BACKUP_DIR, f"config-{stamp}-{n}.json")
            n += 1
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        try:
            keep = max(1, int(_ops_cfg()["backup_count"]))
        except Exception:
            keep = 3
        entries = []
        for name in os.listdir(BACKUP_DIR):
            if name.startswith("config-") and name.endswith(".json"):
                p = os.path.join(BACKUP_DIR, name)
                try: entries.append((os.path.getmtime(p), p))
                except OSError: pass
        entries.sort(key=lambda x: x[0], reverse=True)
        for _, old in entries[keep:]:
            try: os.unlink(old)
            except OSError: pass
    except Exception as exc:
        log.warning("config backup failed: %s", _redact(exc))

_LAST_ADMIN_PW_HASH: str = ""   # Phase 15c: track admin password hash for session invalidation

def save_config(cfg):
    """Atomic write: temp file + os.replace so a crash can't corrupt config.json."""
    global _LAST_ADMIN_PW_HASH
    cfg_dir = os.path.dirname(CONFIG_FILE) or "."
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=cfg_dir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise
    # Phase 15c: if admin password changed, invalidate all dashboard sessions
    new_hash = (cfg.get("auth") or {}).get("password_hash", "")
    if _LAST_ADMIN_PW_HASH and new_hash and new_hash != _LAST_ADMIN_PW_HASH:
        _sessions.clear()
        log.info("Admin password changed — all dashboard sessions invalidated")
    if new_hash:
        _LAST_ADMIN_PW_HASH = new_hash
    # Invalidate cache so next read picks up fresh mtime
    _CFG_CACHE["data"] = None
    # Phase 9: snapshot + prune. Runs after the atomic swap so a backup is
    # only created when the real config has already been persisted.
    _backup_config_snapshot(cfg)

# ── SSRF guard for outbound fetches of attacker-controllable URLs (A5) ───────
def _is_safe_public_url(url: str) -> bool:
    """Allow only http(s) URLs pointing at a public IP. Blocks file://, data:,
    gopher://, and private/loopback/link-local/multicast addresses."""
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme not in ("http", "https"): return False
    host = u.hostname
    if not host: return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for fam, _, _, _, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False
    return True

# ── Pydantic models ───────────────────────────────────────────────────────────
class SubModel(BaseModel):
    name: str; url: str; username: str; password: str
    max_streams: int = 1; priority: int = 1; enabled: bool = True
class GroupModel(BaseModel):
    groups: List[str]

class _QualityOverridesBody(BaseModel):
    overrides: Dict[str, Optional[str]]  # channel_name -> "HD"|"FHD"|"4K"|"SD"|"HEVC"|null

class _ChannelGroupsBody(BaseModel):
    group_order: List[str]                # ordered list of logical group names
    assignments: Dict[str, str]           # channel_key -> group_name

class _ProviderColorBody(BaseModel):
    colors: Dict[str, str]                # sub_name -> "#hexcolor"

# ── Data classes ──────────────────────────────────────────────────────────────
@dataclass
class Subscription:
    name: str; url: str; username: str; password: str; max_streams: int
    active_streams: int = 0
    priority: int = 1          # 1 = primary, 2 = backup, 3 = tertiary
    enabled: bool = True
    failed_at: float = 0.0     # timestamp of last failure (0 = healthy)
    fail_count: int = 0        # consecutive failures
    def m3u_url(self):
        return f"{self.url}/get.php?username={self.username}&password={self.password}&type=m3u_plus&output=ts"
    def stream_url(self, sid):
        return f"{self.url}/live/{self.username}/{self.password}/{sid}.ts"
    @property
    def available(self): return self.active_streams < self.max_streams
    @property
    def load(self): return self.active_streams / self.max_streams if self.max_streams else 1.0

QPRIO = {'4K':50,'UHD':50,'FHD':40,'HD':30,'HEVC':15,'SD':10}

_QTOK = re.compile(
    r'(?<![a-zA-Z0-9])'
    r'(4[Kk]|[Uu][Hh][Dd]|[Uu]ltra|[Ff][Hh][Dd]|1080[pPiI]?'
    r'|[Hh][Ee][Vv][Cc]|[Hh]\.?265|[Hh]265|265'
    r'|[Hh][Dd]|[Ss][Dd]\+?|[Ll]ow|[Ee]xtra'
    r'|720[pP]?|480[pP]?)'
    r'(?![a-zA-Z])'
)

def _normalize(t: str) -> str:
    t = re.sub(r'[._|]+', ' ', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip(' -()[]★⚽:\u00ad')

def _detect_quality_token(text: str):
    m = _QTOK.search(_normalize(text))
    if not m: return None, 0
    raw = m.group(1).upper().rstrip('+')
    if   raw in ('4K','UHD','ULTRA'):                           q = '4K'
    elif raw in ('FHD','1080','1080P','1080I'):               q = 'FHD'
    elif raw in ('HEVC','H265','H.265','265'):                q = 'HEVC'
    elif raw in ('SD','LOW','480','480P','720','720P','EXTRA'): q = 'SD'
    else:                                                        q = 'HD'
    return q, QPRIO.get(q, 20)

def _detect_quality(name: str, group: str = '') -> tuple:
    q, p = _detect_quality_token(group)
    if not q: q, p = _detect_quality_token(name)
    if not q: q, p = 'HD', 30
    return q, p

def _base_name(name: str) -> str:
    text = _normalize(name)
    text = _QTOK.sub('', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip(' -()[]★⚽:\u00ad')

@dataclass
class QualityVariant:
    stream_id: str
    quality:   str
    priority:  int
    orig_name: str
    sub_name:  str = ""   # which subscription this variant came from

# ── Phase 11: multi-candidate channel model ──────────────────────────────────
# A ChannelCandidate is one upstream possibility for a logical channel: a
# specific (sub, stream_id, quality) tuple. The dispatcher (Phase 11 Item 2)
# orders these by score and the mux (Item 3) tries them top-down before the
# first chunk and on mid-stream failure (Item 4). The currently-selected
# candidate is kept in sync onto the legacy scalar Channel.{stream_id,quality,
# sub_name} fields so existing /api/channels, /lineup.json, and the HDHR
# pipeline keep working unchanged.
@dataclass
class ChannelCandidate:
    sub_name:     str          # which Subscription this comes from
    stream_id:    str          # provider-specific stream ID
    tvg_id:       str = ""     # may differ per source
    logo:         str = ""
    quality:      str = "HD"
    priority:     int = 1      # inherited from Subscription.priority at refresh
    last_fail_ts: float = 0.0  # most-recent failure timestamp (sliding window)
    fail_count:   int = 0      # rolling failure count for this candidate

@dataclass
class Channel:
    id: str; name: str; number: str; logo: str; group: str; stream_id: str
    quality: str = 'HD'
    variants: object = None
    tvg_id:  str = ""     # Phase 7: preserved from M3U for XMLTV lookup
    # Phase 11: which subscription is currently primary for this channel.
    # Updated whenever the dispatcher picks a fresh candidate. Empty until
    # the first refresh has run.
    sub_name: str = ""
    # Phase 11: ordered upstream candidates from refresh_channels. The
    # dispatcher reorders these per-request via score; the mux iterates them
    # for pre-dispatch retry + mid-stream failover.
    candidates: List["ChannelCandidate"] = field(default_factory=list)

@dataclass
class Alert:
    ts: float; level: str; message: str
    def to_dict(self):
        return {"ts": self.ts, "time": datetime.fromtimestamp(self.ts).strftime("%H:%M:%S"),
                "level": self.level, "message": self.message}

@dataclass
class LogEntry:
    ts: float; level: str; event: str; detail: str
    sub: str = ""  # Phase 8: optional subscription tag for /api/logs?subscription= filter
    def to_dict(self):
        return {"ts": self.ts, "time": datetime.fromtimestamp(self.ts).strftime("%H:%M:%S"),
                "level": self.level, "event": self.event, "detail": self.detail,
                "sub": self.sub}

# ── Stream Multiplexer ────────────────────────────────────────────────────────
# Phase 11: A 188-byte MPEG-TS null packet (PID 0x1FFF, "stuffing"). Played by
# any compliant decoder as a no-op. We feed these into the viewer queues
# during a mid-stream failover so the client TCP buffer doesn't drain to zero
# in the 100-500ms it takes to open the next upstream.
_TS_NULL_PACKET = b"\x47\x1f\xff\x10" + (b"\x00" * 184)
_TS_NULL_BURST  = _TS_NULL_PACKET * 14   # ~2.6 KB worth (~30 ms at 1 Mbps)

# Light 7A: HDHR/Plex jitter-smoothing buffer (Xtream path unchanged)
_HDHR_PREFILL_CHUNKS = 5   # chunks to accumulate before starting delivery (~2-3s)
_HDHR_BUFFER_MAXSIZE = 64  # max intermediate buffer depth per HDHR viewer (~10s)

class ChannelMux:
    BUFFER_CHUNKS = 64
    MAX_VIEWERS   = 10
    def __init__(self, key, ch, sub, decision: Optional["Decision"] = None):
        self.channel_key = key; self.ch = ch; self.sub = sub
        # Phase 11: ordered upstream candidates from the dispatcher. Pre-Phase-11
        # callers can construct a Mux without one — in that case we synthesize a
        # single-candidate Decision from the (ch, sub) pair so _fetch's loop
        # logic stays uniform.
        if decision is None:
            decision = Decision(
                channel_key=key, user=None,
                ordered=[ChannelCandidate(
                    sub_name=sub.name, stream_id=ch.stream_id,
                    tvg_id=ch.tvg_id, logo=ch.logo, quality=ch.quality,
                    priority=sub.priority)],
                picked_at=time.time(),
                reason=f"legacy:{sub.name}",
            )
        self.decision = decision
        self._cand_idx = 0  # current candidate index inside decision.ordered
        self.buffer = collections.deque(maxlen=self.BUFFER_CHUNKS)
        self.queues: List[asyncio.Queue] = []
        self.queue_users: Dict[int, str] = {}  # id(queue) → xtream_user or ip
        self.queue_ids:   Dict[int, str] = {}  # id(queue) → short viewer id (all viewers incl. HDHR/Plex)
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.started_at = time.time()
        self.bytes_total = 0
        self.last_chunk_at = time.time()
        self.viewer_ips: List[str] = []
        self.drops: Dict[int, int] = {}  # queue id -> dropped chunk count (A20)
        self._failover_in_progress = False  # A11: skip finally's slot release when swapping sub
        self.hdhr_buffers: Dict[int, asyncio.Queue] = {}  # Light 7A: id(q) → jitter buf
        self.hdhr_tasks:   Dict[int, asyncio.Task]  = {}  # Light 7A: id(q) → drain task
        self._jitter_failed_subs: set = set()  # Jitter failover: providers to skip this session
        self._stall_alert_times: list = []    # Throttle: timestamps of stall-restart alerts
    @property
    def viewer_count(self): return len(self.queues)
    @property
    def bitrate_kbps(self):
        e = time.time() - self.started_at
        return round((self.bytes_total / 1024) / e, 1) if e > 0 else 0
    @property
    def is_stalled(self):
        if not self._running: return False
        cfg = _health_cfg()
        # Phase 8: grace period so a freshly-started stream isn't killed before
        # it has a chance to deliver the first segment.
        if (time.time() - self.started_at) < cfg["stall_grace_seconds"]:
            return False
        return (time.time() - self.last_chunk_at) > cfg["stall_seconds"]
    async def start(self):
        self._running = True
        self.sub.active_streams += 1
        state.daily_stats["streams_started"] += 1
        state.log_event("stream", "STREAM START", f"{self.ch.name} via {self.sub.name} ({self.sub.active_streams}/{self.sub.max_streams} slots)")
        log.info("MUX START  %s", self.ch.name)
        self._task = asyncio.create_task(self._fetch())
    async def _publish(self, chunk: bytes) -> None:
        """Phase 11: single fan-out path used by both the normal stream loop
        and the null-packet stuffer that runs during mid-stream failover.
        Light 7A: HDHR/Plex queues are routed through their jitter buffers."""
        self.bytes_total += len(chunk)
        self.last_chunk_at = time.time()
        self.buffer.append(chunk)
        for q in self.queues:
            qid = id(q)
            buf = self.hdhr_buffers.get(qid)
            target = buf if buf is not None else q
            try: target.put_nowait(chunk)
            except asyncio.QueueFull:
                self.drops[qid] = self.drops.get(qid, 0) + 1
                if self.drops[qid] % 50 == 1:
                    log.warning("MUX DROP  %s — slow viewer dropped %d chunks",
                                self.ch.name, self.drops[qid])

    async def _switch_to_candidate(self, idx: int) -> bool:
        """Move from current candidate to candidate `idx`. Releases the
        outgoing sub's slot, claims the incoming one, updates self.sub /
        self._cand_idx / self.ch.{stream_id,quality,sub_name} for backwards
        compat. Returns False if `idx` is unreachable (no such sub or no
        slots), in which case the caller should try the next index."""
        if idx >= len(self.decision.ordered):
            return False
        next_cand = self.decision.ordered[idx]
        next_sub  = state.get_subscription(next_cand.sub_name)
        if next_sub is None or not next_sub.enabled or not next_sub.available:
            return False
        prev_sub = self.sub
        try:
            prev_sub.active_streams = max(0, prev_sub.active_streams - 1)
        except Exception:
            pass
        next_sub.active_streams += 1
        self.sub        = next_sub
        self._cand_idx  = idx
        self.ch.stream_id = next_cand.stream_id
        self.ch.quality   = next_cand.quality
        self.ch.sub_name  = next_cand.sub_name
        return True

    async def _fetch(self):
        """Phase 11: candidate-aware streaming loop with pre-dispatch retry
        + bounded mid-stream failover.

        Behavior:
          - Iterate self.decision.ordered top-down.
          - Pre-first-chunk failures (HTTP 4xx/5xx, connect error, network
            error) are silent — try the next candidate without informing
            downstream viewers (their TCP connection is still waiting for
            headers, so the swap is invisible).
          - Mid-stream failures (after first byte has flowed) emit
            null-packet stuffing into the viewer queues, swap upstream,
            and resume. Bounded by dispatch.max_failovers_per_session.
          - All-candidates-exhausted = mux ends naturally; finally clause
            tells viewers via queue=None.
        """
        global _DISPATCH_NO_CANDIDATE_EVENTS, _DISPATCH_FAILOVER_LIMIT_HITS
        first_chunk_seen = False
        cfg_d = _dispatch_cfg()
        max_failovers = max(0, cfg_d["max_failovers_per_session"])
        midstream_enabled = cfg_d["failover_midstream"]
        # Track which candidates we've already burned this session so we
        # don't ping-pong endlessly between two flapping subs.
        try:
            while self._cand_idx < len(self.decision.ordered):
                cand = self.decision.ordered[self._cand_idx]
                # Stats: count this attempt as a "stream start" for the sub
                _DISPATCH_STATS.setdefault(cand.sub_name,
                    {"streams": 0, "failovers_from": 0, "failovers_to": 0, "rejections": 0})
                _DISPATCH_STATS[cand.sub_name]["streams"] += 1
                upstream_url = self.sub.stream_url(cand.stream_id)
                try:
                    # Phase 11 Item 5: use the dedicated stream client so
                    # admin traffic can never starve viewer reads.
                    async with state.stream_http.stream("GET", upstream_url) as resp:
                        if resp.status_code >= 400:
                            # HTTP error — counts as a failure on this candidate
                            dispatcher.mark_failure(self.decision, self._cand_idx,
                                                    f"http_{resp.status_code}")
                            if not first_chunk_seen:
                                _record_channel_failure(self.channel_key)
                                self._cand_idx += 1
                                if not await self._advance_until_usable():
                                    state.add_alert("error",
                                        f"All upstreams failed: {self.ch.name}")
                                    _DISPATCH_NO_CANDIDATE_EVENTS += 1
                                    state.log_event("error", "NO CANDIDATES",
                                        f"{self.ch.name}: all "
                                        f"{len(self.decision.ordered)} upstreams failed",
                                        sub=cand.sub_name)
                                    return
                                continue
                            # Mid-stream HTTP error — let the exception handler
                            # below run the failover-swap logic.
                            raise httpx.ReadError(f"http_{resp.status_code}")

                        _prev_chunk_ts = time.time()
                        _chunk_count = 0
                        _gap_sum = 0.0
                        _gap_max = 0.0
                        _stutter_count = 0  # gaps > 1s
                        _LOG_INTERVAL = 60  # log timing summary every 60s
                        _next_log_ts = _prev_chunk_ts + _LOG_INTERVAL
                        _jitter_break = False
                        async for chunk in resp.aiter_bytes(188 * 1024):
                            if not self._running: return
                            now = time.time()
                            gap = now - _prev_chunk_ts
                            _prev_chunk_ts = now
                            _chunk_count += 1
                            _gap_sum += gap
                            if gap > _gap_max:
                                _gap_max = gap
                            if gap > 1.0:
                                _stutter_count += 1
                                log.warning("CHUNK GAP  %s  %.1fs gap (chunk #%d, %d KB, sub=%s)",
                                            self.ch.name, gap, _chunk_count,
                                            len(chunk) // 1024, cand.sub_name)
                            # Stall failover: stream went silent for 10s — provider is dead.
                            # Only switch on a true stall, not on jitter, to avoid showing a
                            # different time-offset of the same live channel to the viewer.
                            if gap > 10.0 and self.decision and first_chunk_seen:
                                ordered = self.decision.ordered
                                self._jitter_failed_subs.add(cand.sub_name)
                                next_idx = next(
                                    (i for i in range(len(ordered))
                                     if i != self._cand_idx
                                     and ordered[i].sub_name not in self._jitter_failed_subs),
                                    None)
                                if next_idx is not None:
                                    next_sub = ordered[next_idx].sub_name
                                    log.warning(
                                        "STALL FAILOVER  %s  %.1fs stall  %s → %s",
                                        self.ch.name, gap, cand.sub_name, next_sub)
                                    asyncio.create_task(tg_send(tg_alert(
                                        "🔀", "Stall Failover",
                                        f"Channel: <b>{self.ch.name}</b>\n"
                                        f"{cand.sub_name} → <b>{next_sub}</b>\n"
                                        f"Stall: {gap:.0f}s")))
                                    self._cand_idx = next_idx
                                    _jitter_break = True
                                    break
                                else:
                                    log.warning(
                                        "STALL FAILOVER  %s  %.1fs stall"
                                        "  — no alternative provider",
                                        self.ch.name, gap)
                                    self._jitter_failed_subs.discard(cand.sub_name)
                            if now >= _next_log_ts:
                                avg = (_gap_sum / _chunk_count) if _chunk_count else 0
                                log.info("CHUNK STATS  %s  %d chunks in %ds, "
                                         "avg=%.0fms max=%.0fms stutters=%d (>1s), "
                                         "kbps=%d sub=%s",
                                         self.ch.name, _chunk_count,
                                         int(now - (now - _gap_sum)),
                                         avg * 1000, _gap_max * 1000,
                                         _stutter_count, self.bitrate_kbps,
                                         cand.sub_name)
                                # Log jitter stats for monitoring — no failover here.
                                # Stall failover (gap > 10s) is handled per-chunk above.
                                _chunk_count = 0
                                _gap_sum = 0.0
                                _gap_max = 0.0
                                _stutter_count = 0
                                _next_log_ts = now + _LOG_INTERVAL
                            if not first_chunk_seen:
                                first_chunk_seen = True
                                _record_channel_success(self.channel_key)
                                state.log_event("stream", "DISPATCH",
                                    f"{self.ch.name} → {cand.sub_name} "
                                    f"({self.decision.reason})",
                                    sub=cand.sub_name)
                            await self._publish(chunk)
                        # Jitter failover: break out of aiter_bytes → swap provider.
                        if _jitter_break:
                            self.decision.failover_count += 1
                            if cfg_d["null_packet_fill_during_swap"]:
                                await self._publish(_TS_NULL_BURST)
                            if not await self._advance_until_usable():
                                state.log_event("error", "NO CANDIDATES",
                                    f"{self.ch.name}: jitter failover exhausted",
                                    sub=cand.sub_name)
                                return
                            state.log_event("stream", "STALL FAILOVER",
                                f"{self.ch.name}: switched to "
                                f"{self.decision.ordered[self._cand_idx].sub_name}",
                                sub=self.decision.ordered[self._cand_idx].sub_name)
                            continue
                        # Normal stream end (clean EOF from upstream)
                        return
                except asyncio.CancelledError:
                    raise
                except (httpx.NetworkError, httpx.ReadError, httpx.ReadTimeout,
                        httpx.RemoteProtocolError, httpx.ConnectError) as exc:
                    dispatcher.mark_failure(self.decision, self._cand_idx,
                                            f"{type(exc).__name__}")
                    self.sub.failed_at = time.time()
                    self.sub.fail_count += 1
                    if not first_chunk_seen:
                        _record_channel_failure(self.channel_key)
                        # Pre-dispatch retry: try next candidate silently.
                        self._cand_idx += 1
                        if not await self._advance_until_usable():
                            state.add_alert("error",
                                f"All upstreams failed: {self.ch.name}")
                            _DISPATCH_NO_CANDIDATE_EVENTS += 1
                            state.log_event("error", "NO CANDIDATES",
                                f"{self.ch.name}: all "
                                f"{len(self.decision.ordered)} upstreams failed",
                                sub=cand.sub_name)
                            return
                        continue
                    # ── Mid-stream failover ──
                    if not midstream_enabled:
                        log.warning("Mid-stream failover disabled — ending mux %s",
                                    self.ch.name)
                        return
                    self.decision.failover_count += 1
                    if self.decision.failover_count > max_failovers:
                        _DISPATCH_FAILOVER_LIMIT_HITS += 1
                        state.log_event("error", "FAILOVER LIMIT",
                            f"{self.ch.name}: gave up after "
                            f"{self.decision.failover_count} failovers",
                            sub=cand.sub_name)
                        return
                    if cfg_d["null_packet_fill_during_swap"]:
                        # Stuff a brief burst of MPEG-TS null packets so the
                        # client TCP buffer keeps draining smoothly during the
                        # 100-500ms swap window.
                        await self._publish(_TS_NULL_BURST)
                    prev_sub_name = cand.sub_name
                    self._cand_idx += 1
                    if not await self._advance_until_usable():
                        state.log_event("error", "NO CANDIDATES",
                            f"{self.ch.name}: failover exhausted after "
                            f"{self.decision.failover_count}",
                            sub=cand.sub_name)
                        return
                    new_cand = self.decision.ordered[self._cand_idx]
                    state.log_event("stream", "FAILOVER",
                        f"{self.ch.name}: {prev_sub_name} → {new_cand.sub_name}",
                        sub=new_cand.sub_name)
                    continue
                except Exception as exc:
                    state.add_alert("warning", f"Stream error: {self.ch.name} — {exc}")
                    state.log_event("stream", "STREAM ERROR",
                                    f"{self.ch.name}: {exc}", sub=self.sub.name)
                    if not first_chunk_seen:
                        _record_channel_failure(self.channel_key)
                    return
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            try:
                self.sub.active_streams = max(0, self.sub.active_streams - 1)
            except Exception:
                pass
            uptime = int(time.time() - self.started_at)
            state.log_event("stream", "STREAM STOP",
                f"{self.ch.name} — uptime {uptime}s, "
                f"{self.bytes_total//1024} KB, "
                f"{self.decision.failover_count} failover(s)")
            log.info("MUX STOP   %s", self.ch.name)
            async with self._lock:
                for q in self.queues:
                    # Light 7A: route sentinel through jitter buffer so drain
                    # task delivers it after flushing remaining buffered chunks.
                    buf = self.hdhr_buffers.get(id(q))
                    target = buf if buf is not None else q
                    try: target.put_nowait(None)
                    except asyncio.QueueFull: pass
            state.mux.pop(self.channel_key, None)

    async def _advance_until_usable(self) -> bool:
        """Walk forward through self.decision.ordered until we find a
        candidate whose subscription is reachable (enabled, has slots).
        Updates self.sub / self._cand_idx as a side effect. Returns True
        when a usable candidate has been claimed, False when the list is
        exhausted."""
        while self._cand_idx < len(self.decision.ordered):
            if await self._switch_to_candidate(self._cand_idx):
                return True
            self._cand_idx += 1
        return False
    async def _hdhr_drain(self, q: asyncio.Queue, buf: asyncio.Queue) -> None:
        """Light 7A: steady-rate drain of the per-viewer jitter buffer into the
        HDHR/Plex viewer queue. Absorbs upstream gaps up to ~_HDHR_PREFILL_CHUNKS
        chunks deep. Xtream viewers never enter this path."""
        # Wait for initial pre-fill before starting delivery
        for _ in range(100):
            if buf.qsize() >= _HDHR_PREFILL_CHUNKS or not self._running:
                break
            await asyncio.sleep(0.05)
        while True:
            try:
                chunk = await asyncio.wait_for(buf.get(), timeout=2.0)
            except asyncio.TimeoutError:
                if not self._running:
                    return
                continue
            except asyncio.CancelledError:
                return
            if chunk is None:
                try: q.put_nowait(None)
                except asyncio.QueueFull: pass
                return
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                pass
            # bitrate_kbps is KB/s (bytes_total/1024 / elapsed). Pace at that rate.
            # floor 250 KB/s ≈ 2 Mbps to avoid over-sleeping on startup.
            br_kbs = max(self.bitrate_kbps, 250)  # KB/s
            await asyncio.sleep(len(chunk) / (br_kbs * 1024))

    async def subscribe(self, viewer_ip="", xtream_user: Optional[str] = None):
        q = asyncio.Queue(maxsize=self.BUFFER_CHUNKS * 2)
        for chunk in self.buffer:
            try: q.put_nowait(chunk)
            except asyncio.QueueFull: break
        async with self._lock:
            self.queues.append(q)
            self.viewer_ips.append(viewer_ip)
            self.queue_users[id(q)] = xtream_user  # None for HDHR/Plex (resolved later via _active_session_users)
            self.queue_ids[id(q)]   = secrets.token_hex(4)  # stable id for per-viewer kill (all client types)
            # Light 7A: HDHR/Plex viewers get a jitter-smoothing intermediate buffer
            if xtream_user is None:
                buf = asyncio.Queue(maxsize=_HDHR_BUFFER_MAXSIZE)
                self.hdhr_buffers[id(q)] = buf
                self.hdhr_tasks[id(q)] = asyncio.create_task(self._hdhr_drain(q, buf))
        total_v = sum(m.viewer_count for m in state.mux.values())
        if total_v > state.daily_stats["peak_viewers"]:
            state.daily_stats["peak_viewers"] = total_v
        state.daily_stats["total_viewers"] += 1
        # Display label: xtream username takes precedence over the (often-NAT'd
        # 172.17.0.1) bridge IP. Falls back to bare IP for HDHR/Plex sessions
        # where there is no Xtream user identity.
        viewer_label = f"{xtream_user}@{viewer_ip}" if xtream_user else viewer_ip
        state.log_event("viewer", "VIEWER JOIN",
                        f"{viewer_label} watching {self.ch.name} ({len(self.queues)} viewers)")
        asyncio.create_task(session_start(self.channel_key, self.ch.name,
                                          viewer_ip, xtream_user=xtream_user))
        return q
    async def unsubscribe(self, q, viewer_ip=""):
        async with self._lock:
            try:
                idx = self.queues.index(q); self.queues.pop(idx)
                if idx < len(self.viewer_ips): self.viewer_ips.pop(idx)
                self.queue_users.pop(id(q), None)
                self.queue_ids.pop(id(q), None)
                # Light 7A: cancel drain task and drop jitter buffer
                task = self.hdhr_tasks.pop(id(q), None)
                if task: task.cancel()
                self.hdhr_buffers.pop(id(q), None)
            except ValueError: pass
        # Use whichever label session_start stored (xtream user if set, else IP)
        # so VIEWER LEFT mirrors VIEWER JOIN.
        stored = _active_session_users.get(f"{self.channel_key}:{viewer_ip}", viewer_ip)
        state.log_event("viewer", "VIEWER LEFT",
                        f"{stored} left {self.ch.name} ({len(self.queues)} remaining)")
        asyncio.create_task(session_end(self.channel_key, self.ch.name, viewer_ip, self.bytes_total))
        if not self.queues:
            self._running = False
            if self._task: self._task.cancel()
            state.mux.pop(self.channel_key, None)
    async def kill_all(self):
        self._running = False
        if self._task: self._task.cancel()
        async with self._lock:
            for q in self.queues:
                buf = self.hdhr_buffers.get(id(q))
                target = buf if buf is not None else q
                try: target.put_nowait(None)
                except: pass
        state.mux.pop(self.channel_key, None)
    async def kick_queue_by_id(self, qid: str) -> bool:
        """Terminate a single HDHR/Plex viewer by their queue_id (sends None sentinel)."""
        async with self._lock:
            for q in self.queues:
                if self.queue_ids.get(id(q)) == qid:
                    try: q.put_nowait(None)
                    except asyncio.QueueFull: pass
                    return True
        return False
        # Clean up any Xtream user sessions tied to this channel so the
        # active_connections counter doesn't get stuck when the viewer's
        # finally-block races with the mux teardown.
        orphaned: list[str] = []
        async with _USER_SESSIONS_LOCK:
            for sid, sess in list(_USER_SESSIONS.items()):
                if sess.channel_key == self.channel_key:
                    orphaned.append(sid)
        for sid in orphaned:
            await _unregister_user_session(sid)
    def to_dict(self):
        # Use per-queue user mapping (avoids IP collision when 2 users share the same IP).
        # Xtream users are keyed by queue id (avoids IP collision when 2 users
        # share the same NAT/bridge IP). Plex/HDHR viewers have no Xtream user
        # so we fall back to _active_session_users which holds the async-resolved
        # Plex display name for those sessions.
        viewer_users = [
            self.queue_users.get(id(q)) or
            _active_session_users.get(f"{self.channel_key}:{ip}", ip)
            for q, ip in zip(self.queues, self.viewer_ips)
        ]
        viewer_sids = [
            next((sid for sid, s in _USER_SESSIONS.items() if s.queue is q), None)
            or self.queue_ids.get(id(q))   # fallback: queue_id for HDHR/Plex viewers
            for q in self.queues
        ]
        return {"channel_key": self.channel_key, "channel": self.ch.name,
                "group": self.ch.group, "subscription": self.sub.name,
                "sub_active_streams": sum(1 for m in state.mux.values() if m.sub is self.sub and m._running),
                "sub_max_streams": self.sub.max_streams,
                "quality": self.ch.quality,
                "variant_count": len(self.ch.variants) if self.ch.variants else 1,
                # Phase 11: dispatcher state
                "candidate_count": len(self.decision.ordered) if self.decision else 1,
                "candidate_idx":   self._cand_idx,
                "failover_count":  self.decision.failover_count if self.decision else 0,
                "dispatch_reason": self.decision.reason if self.decision else "",
                "viewers": self.viewer_count, "viewer_ips": self.viewer_ips,
                "viewer_users": viewer_users, "viewer_sids": viewer_sids,
                "bitrate_kbps": self.bitrate_kbps, "bytes_total": self.bytes_total,
                "started_at": datetime.fromtimestamp(self.started_at).strftime("%H:%M:%S"),
                "uptime_s": int(time.time() - self.started_at), "is_stalled": self.is_stalled}

# ── Auth ──────────────────────────────────────────────────────────────────────
_sessions: Dict[str, float] = {}

def _auth_cfg():
    try: return load_config().get("auth")
    except: return None

def _verify_admin_password(plaintext: str, stored: str) -> bool:
    """Verify admin password against bcrypt hash or legacy SHA256 hex digest."""
    if stored.startswith(("$2b$", "$2a$")):
        return bcrypt.checkpw(plaintext.encode(), stored.encode())
    return secrets.compare_digest(
        hashlib.sha256(plaintext.encode()).hexdigest(), stored)

def _check_session(request: Request) -> bool:
    cfg = _auth_cfg()
    if not cfg: return True
    token = request.cookies.get("iptv_session", "")
    if not token or token not in _sessions: return False
    if time.time() > _sessions[token]: del _sessions[token]; return False
    return True

async def _session_gc_loop():
    """A34: periodically purge expired auth sessions so _sessions can't grow unbounded.
    Phase 10: also reaps stale Xtream live sessions and trims the auth-failure log."""
    while True:
        try:
            await asyncio.sleep(30)  # every 30s — fast enough to reap stale sessions promptly
            now = time.time()
            # Dashboard session tokens
            expired = [t for t, exp in _sessions.items() if now > exp]
            for t in expired:
                _sessions.pop(t, None)
            if expired:
                log.debug("session GC — purged %d expired tokens", len(expired))

            # Phase 10: stale Xtream live sessions (no chunk delivered in >idle window —
            # viewer loop must have died without hitting the finally clause).
            idle = max(15, _xtream_cfg().get("session_idle_seconds", 60))
            stale_sessions: List["UserSession"] = []
            async with _USER_SESSIONS_LOCK:
                for sid, sess in list(_USER_SESSIONS.items()):
                    if (now - sess.last_chunk_at) > idle:
                        stale_sessions.append(sess)
                        _USER_SESSIONS.pop(sid, None)
            if stale_sessions:
                for sess in stale_sessions:
                    # Unsubscribe the zombie viewer from the mux so viewer_count
                    # and sub_active_streams are accurate again.
                    if sess.queue is not None:
                        mux = state.mux.get(sess.channel_key)
                        if mux is not None:
                            await mux.unsubscribe(sess.queue, sess.client_ip)
                    # Decrement user's active_connections counter.
                    user = _find_user(sess.username)
                    if user is not None:
                        user.active_connections = _user_session_count(user.username)
                log.info("Xtream session GC — reaped %d stale session(s)", len(stale_sessions))

            # Phase 10: prune auth-failure log entries outside the window
            try:
                window = max(1, _xtream_cfg()["auth_failure_window_seconds"])
                cutoff = now - window
                async with _AUTH_FAIL_LOCK:
                    for ip in list(_AUTH_FAIL_LOG.keys()):
                        _AUTH_FAIL_LOG[ip] = [t for t in _AUTH_FAIL_LOG[ip] if t >= cutoff]
                        if not _AUTH_FAIL_LOG[ip]:
                            _AUTH_FAIL_LOG.pop(ip, None)
            except Exception as exc:
                log.debug("auth-fail GC error: %s", exc)

            # Step 4: prune expired blocklist entries + blocklist fail log
            try:
                expired_ips = [ip for ip, exp in _IP_BLOCKLIST.items() if now >= exp]
                if expired_ips:
                    for ip in expired_ips:
                        _IP_BLOCKLIST.pop(ip, None)
                    _save_blocklist()
                    log.info("Blocklist GC — expired %d IP(s)", len(expired_ips))
                bl_cutoff = now - _BLOCKLIST_WINDOW
                async with _BLOCKLIST_LOCK:
                    for ip in list(_BLOCK_FAIL_LOG.keys()):
                        _BLOCK_FAIL_LOG[ip] = [t for t in _BLOCK_FAIL_LOG[ip] if t >= bl_cutoff]
                        if not _BLOCK_FAIL_LOG[ip]:
                            _BLOCK_FAIL_LOG.pop(ip, None)
                # Prune login rate-limit entries
                lr_cutoff = now - _LOGIN_RATE_WINDOW
                for ip in list(_LOGIN_ATTEMPTS.keys()):
                    _LOGIN_ATTEMPTS[ip] = [t for t in _LOGIN_ATTEMPTS[ip] if t >= lr_cutoff]
                    if not _LOGIN_ATTEMPTS[ip]:
                        _LOGIN_ATTEMPTS.pop(ip, None)
            except Exception as exc:
                log.debug("blocklist GC error: %s", exc)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.debug("session GC error: %s", exc)


# ── Telegram ──────────────────────────────────────────────────────────────────
async def tg_send(text: str, parse_mode: str = "HTML"):
    """Send a Telegram message. Fire-and-forget — never blocks the caller."""
    try:
        cfg = load_config().get("telegram", {})
        if not cfg.get("enabled"): return
        token = cfg.get("bot_token", "")
        chat  = cfg.get("chat_id", "")
        if not token or not chat: return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(url, json={"chat_id": chat, "text": text,
                                    "parse_mode": parse_mode,
                                    "disable_web_page_preview": True})
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)

def tg_alert(icon: str, title: str, detail: str) -> str:
    host = load_config().get("hostname", "iptv-tuner")
    ts = datetime.now().strftime("%H:%M:%S")
    return f"{icon} <b>{title}</b>\n{'─' * 24}\n{detail}\n{'─' * 24}\n🕐 {ts}  ·  <i>{host}</i>"

# ── Telegram bot command handler ─────────────────────────────────────────────
# Long-polls getUpdates and dispatches commands. Only responds to the configured
# chat_id — all other senders are silently ignored.
_TG_LAST_UPDATE = 0
_tg_task: Optional[asyncio.Task] = None  # module-level ref so it can be restarted

async def _restart_tg_loop():
    """Cancel the current Telegram polling task and start a fresh one.
    Called after bot_token or chat_id changes so the new credentials take effect
    immediately without a full app restart."""
    global _tg_task
    if _tg_task and not _tg_task.done():
        _tg_task.cancel()
        try: await _tg_task
        except asyncio.CancelledError: pass
    _tg_task = asyncio.create_task(_tg_command_loop())
    log.info("Telegram bot task restarted with updated credentials")

async def _tg_reply(token: str, chat_id: str, text: str):
    """Send a reply to the admin's Telegram chat."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(url, json={"chat_id": chat_id, "text": text,
                                    "parse_mode": "HTML",
                                    "disable_web_page_preview": True})
    except Exception as exc:
        log.warning("tg_reply failed: %s", exc)

async def _tg_cmd_status() -> str:
    s = _status_dict()
    sys = s.get("system", {})
    h = s["uptime_s"] // 3600; m = (s["uptime_s"] % 3600) // 60
    sep = "─" * 24
    lines = [
        f"📊 <b>System Status</b>",
        sep,
        f"🟢  Uptime        {h}h {m}m",
        f"📺  Channels      {s['total_channels']:,}",
        f"▶️  Streams       {s['active_streams']}",
        f"👥  Viewers       {s['total_viewers']}",
        sep,
        f"💻  CPU           {sys.get('cpu_pct', '?')}%",
        f"🧠  RAM           {sys.get('mem_mb', '?')} MB ({sys.get('mem_pct', '?')}%)",
        f"👤  Xtream users  {len(state.users)}",
        sep,
        f"🕐 {datetime.now().strftime('%H:%M:%S')}  ·  <i>{load_config().get('hostname', 'iptv-tuner')}</i>",
    ]
    return "\n".join(lines)

async def _tg_cmd_streams() -> str:
    active = [m for m in state.mux.values() if m._running]
    if not active:
        return "📺 No active streams"
    sep = "─" * 24
    lines = [f"📺 <b>Active Streams</b>  ({len(active)})", sep]
    for m in active:
        d = m.to_dict()
        viewers = d.get("viewer_users") or d.get("viewer_ips") or []
        viewer_str = ", ".join(html.escape(v) for v in viewers) if viewers else "—"
        up_s = d.get("uptime_s", 0); up_m = up_s // 60; up_sec = up_s % 60
        lines.append(
            f"▶️ <b>{html.escape(m.ch.name)}</b>\n"
            f"   📡 {html.escape(m.sub.name)}  ·  {d.get('quality', '—')}\n"
            f"   👤 {viewer_str}\n"
            f"   📊 {d.get('bitrate_kbps', 0)} kbps  ·  ⏱ {up_m}m {up_sec}s")
    lines.append(sep)
    lines.append(f"🕐 {datetime.now().strftime('%H:%M:%S')}  ·  <i>{load_config().get('hostname', 'iptv-tuner')}</i>")
    return "\n".join(lines)

async def _tg_cmd_users() -> str:
    if not state.users:
        return "👥 No Xtream users"
    sep = "─" * 24
    lines = [f"👥 <b>Xtream Users</b>  ({len(state.users)})", sep]
    for u in state.users:
        icon = "🟢" if u.enabled and not u.is_expired() else "🔴"
        conns = f"{u.active_connections}/{u.max_connections}"
        exp = "never" if not u.expires_at else time.strftime('%Y-%m-%d', time.localtime(u.expires_at))
        last = "never" if not u.last_seen_ts else time.strftime('%m-%d %H:%M', time.localtime(u.last_seen_ts))
        lines.append(
            f"{icon} <b>{html.escape(u.username)}</b>\n"
            f"   🔌 {conns} conns  ·  📅 exp {exp}\n"
            f"   👁 last seen {last}")
    lines.append(sep)
    lines.append(f"🕐 {datetime.now().strftime('%H:%M:%S')}  ·  <i>{load_config().get('hostname', 'iptv-tuner')}</i>")
    return "\n".join(lines)

async def _tg_cmd_kick(args: str) -> str:
    username = args.strip()
    if not username:
        return "Usage: /kick &lt;username&gt;"
    user = _find_user(username)
    if not user:
        return f"❌ User '{html.escape(username)}' not found"
    n = await _kick_all_user_sessions(user.username)
    return f"✅ Kicked {n} session(s) for <b>{html.escape(user.username)}</b>"

async def _tg_cmd_block(args: str) -> str:
    ip = args.strip()
    if not ip:
        return "Usage: /block &lt;ip&gt;"
    if _is_blocked(ip):
        return f"⚠️ <code>{html.escape(ip)}</code> is already blocked"
    _IP_BLOCKLIST[ip] = time.time() + _BLOCKLIST_DURATION
    _save_blocklist()
    mins = _BLOCKLIST_DURATION // 60
    state.log_event("auth", "IP BLOCKED", f"ip={ip} reason=telegram_command")
    return f"🚫 Blocked <code>{html.escape(ip)}</code> for {mins} min"

async def _tg_cmd_unblock(args: str) -> str:
    ip = args.strip()
    if not ip:
        return "Usage: /unblock &lt;ip&gt;"
    if not _is_blocked(ip):
        return f"⚠️ <code>{html.escape(ip)}</code> is not blocked"
    _IP_BLOCKLIST.pop(ip, None)
    _save_blocklist()
    state.log_event("auth", "IP UNBLOCKED", f"ip={ip} reason=telegram_command")
    return f"✅ Unblocked <code>{html.escape(ip)}</code>"

async def _tg_cmd_refresh() -> str:
    await state.refresh_channels()
    return f"✅ Refreshed — {len(state.channels):,} channels loaded"

async def _tg_cmd_adduser(args: str) -> str:
    """Create Xtream user. Usage: /adduser <username> <password> <YYYY-MM-DD> [max_conns]"""
    parts = args.strip().split()
    if len(parts) < 3:
        return "Usage: /adduser &lt;username&gt; &lt;password&gt; &lt;YYYY-MM-DD|never&gt; [max_connections]"
    username = parts[0]
    if _find_user(username):
        return f"❌ User '{html.escape(username)}' already exists"
    cfg = _xtream_cfg()
    plaintext_pw = parts[1]
    date_str = parts[2]
    max_conns = int(parts[3]) if len(parts) > 3 else cfg["default_max_connections"]
    if date_str.lower() == "never":
        expires_at = 0.0
    else:
        try:
            from datetime import datetime as _dt
            expires_at = _dt.strptime(date_str, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59).timestamp()
        except ValueError:
            return "❌ Invalid date format. Use YYYY-MM-DD or 'never'"
    user = User(
        username         = username,
        password         = _hash_password(plaintext_pw),
        enabled          = True,
        created_at       = time.time(),
        expires_at       = expires_at,
        max_connections  = max(1, max_conns),
    )
    state.users.append(user)
    _save_users(state.users)
    state.log_event("xtream", "USER CREATED",
                    f"username={user.username} max_conn={user.max_connections} via=telegram")
    _publish_user_event("created", user.username,
                        max_connections=user.max_connections,
                        expires_at=int(expires_at))
    exp_str = time.strftime('%Y-%m-%d', time.localtime(expires_at)) if expires_at else "never"
    url_value = (cfg.get("server_url") or state.base_url or "").rstrip("/") or "—"
    return (
        f"👤 <b>User Created</b>\n\n"
        f"<b>Username:</b> <code>{html.escape(username)}</code>\n"
        f"<b>Password:</b> <code>{html.escape(plaintext_pw)}</code>\n"
        f"<b>URL:</b> <code>{html.escape(url_value)}</code>\n"
        f"<b>Expires:</b> {html.escape(exp_str)}\n"
        f"<b>Max conns:</b> {max_conns}"
    )

async def _tg_cmd_help() -> str:
    sep = "─" * 24
    return (
        f"🤖 <b>IPTV Bot Commands</b>\n"
        f"{sep}\n"
        f"📊  /status  — System stats\n"
        f"📺  /streams — Active streams\n"
        f"👥  /users   — Xtream users\n"
        f"{sep}\n"
        f"🔪  /kick &lt;user&gt;     — Kill sessions\n"
        f"🚫  /block &lt;ip&gt;      — Block IP 1h\n"
        f"✅  /unblock &lt;ip&gt;    — Unblock IP\n"
        f"🔄  /refresh         — Reload channels\n"
        f"{sep}\n"
        f"➕  /adduser &lt;name&gt; &lt;pass&gt; &lt;date|never&gt; [conns]\n"
        f"🗑  /deleteuser &lt;user&gt;\n"
        f"{sep}"
    )

async def _tg_cmd_deleteuser(args: str) -> str:
    username = args.strip()
    if not username:
        return "Usage: /deleteuser &lt;username&gt;"
    user = _find_user(username)
    if not user:
        return f"❌ User '{html.escape(username)}' not found"
    kicked = await _kick_all_user_sessions(user.username)
    state.users = [u for u in state.users if u.username.lower() != username.lower()]
    _save_users(state.users)
    state.log_event("xtream", "USER DELETED",
                    f"username={user.username} sessions_kicked={kicked} via=telegram")
    _publish_user_event("deleted", user.username, kicked=kicked)
    return f"🗑 Deleted <b>{html.escape(user.username)}</b> — {kicked} session(s) kicked"

_TG_COMMANDS = {
    "status":     lambda a: _tg_cmd_status(),
    "streams":    lambda a: _tg_cmd_streams(),
    "users":      lambda a: _tg_cmd_users(),
    "kick":       _tg_cmd_kick,
    "block":      _tg_cmd_block,
    "unblock":    _tg_cmd_unblock,
    "refresh":    lambda a: _tg_cmd_refresh(),
    "adduser":    _tg_cmd_adduser,
    "deleteuser": _tg_cmd_deleteuser,
    "help":       lambda a: _tg_cmd_help(),
    "start":      lambda a: _tg_cmd_help(),
}

async def _tg_command_loop():
    """Long-poll Telegram getUpdates and dispatch commands from the admin only."""
    global _TG_LAST_UPDATE
    while True:
        try:
            cfg = load_config().get("telegram", {})
            if not cfg.get("enabled"):
                await asyncio.sleep(30)
                continue
            token = cfg.get("bot_token", "")
            chat_id = str(cfg.get("chat_id", ""))
            if not token or not chat_id:
                await asyncio.sleep(30)
                continue
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            async with httpx.AsyncClient(timeout=35) as c:
                r = await c.get(url, params={
                    "offset": _TG_LAST_UPDATE + 1,
                    "timeout": 30,
                    "allowed_updates": '["message"]',
                })
                data = r.json()
            if not data.get("ok"):
                await asyncio.sleep(10)
                continue
            for update in data.get("result", []):
                _TG_LAST_UPDATE = update["update_id"]
                msg = update.get("message")
                if not msg or not msg.get("text"):
                    continue
                # Security: only respond to the configured admin chat_id
                sender_id = str(msg.get("chat", {}).get("id", ""))
                if sender_id != chat_id:
                    continue
                text = msg["text"].strip()
                if not text.startswith("/"):
                    continue
                # Parse /command@botname args
                cmd_part = text.split()[0].lstrip("/").split("@")[0].lower()
                args = text[len(text.split()[0]):].strip() if " " in text else ""
                handler = _TG_COMMANDS.get(cmd_part)
                if handler:
                    try:
                        reply = await handler(args)
                    except Exception as exc:
                        reply = f"❌ Error: {html.escape(str(exc))}"
                    await _tg_reply(token, chat_id, reply)
                else:
                    await _tg_reply(token, chat_id,
                        f"❓ Unknown command: /{html.escape(cmd_part)}\nTry /help")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.warning("tg_command_loop error: %s", exc)
            await asyncio.sleep(10)

# ── Stream-start Telegram notification ────────────────────────────────────────
# Tracks recent (ip, channel) opens to suppress Plex's probe-then-tune duplicate.
_recent_opens: Dict[str, float] = {}
_OPEN_DEDUPE_WINDOW = 30.0  # seconds

async def _notify_stream_start(channel_key: str, channel_name: str,
                               quality: str, sub_name: str, viewer_ip: str,
                               xtream_user: Optional[str] = None):
    """Fire a Telegram message ~3s after a viewer tunes in, reporting whether
    the upstream actually delivered bytes (working) or failed (not working)."""
    try:
        cfg = load_config().get("telegram", {})
        if not cfg.get("enabled"): return
        if not cfg.get("notify_stream_start", True): return

        # Dedupe: skip if the same ip+channel triggered a notification very recently.
        # When the request came from an Xtream user, dedupe on the username instead
        # of the bridge IP — otherwise two different 9xtream users sharing 172.17.0.1
        # would silently swallow each other's notifications.
        dedupe_key = f"{xtream_user or viewer_ip}:{channel_key}"
        now = time.time()
        last = _recent_opens.get(dedupe_key, 0)
        if now - last < _OPEN_DEDUPE_WINDOW: return
        _recent_opens[dedupe_key] = now
        # Opportunistic GC so the dict can't grow unbounded.
        if len(_recent_opens) > 500:
            cutoff = now - _OPEN_DEDUPE_WINDOW
            for k in [k for k, t in _recent_opens.items() if t < cutoff]:
                _recent_opens.pop(k, None)

        # Wait briefly so the upstream fetch has a chance to deliver bytes
        # or fail over.  After this, we can tell "working" from "not working".
        await asyncio.sleep(3)

        mux = state.mux.get(channel_key)
        is_working = bool(mux and mux._running and mux.bytes_total > 0)
        # The sub may have changed between subscribe and now (failover);
        # report whichever sub is actually serving the stream right now.
        effective_sub = mux.sub.name if mux and mux.sub else sub_name

        # Xtream callers know exactly who the viewer is — use that and skip the
        # Plex lookup, which is both pointless (Plex doesn't know about Xtream
        # users) and wrong (the bridge IP 172.17.0.1 happens to map to a real
        # Plex device, so the lookup returns a misleading display name like
        # "HOMESERVER_AQ"). HDHR/Plex sessions still go through the Plex path.
        if xtream_user:
            user_display = xtream_user
        else:
            user_display = await plex_user_for_channel(channel_name, channel_key)
            if not user_display:
                plex_user = await resolve_plex_user(viewer_ip)
                user_display = plex_user if plex_user and plex_user != viewer_ip else viewer_ip

        status_icon = "🟢" if is_working else "🔴"
        status_text = "Working" if is_working else "Not working"

        msg = (
            f"📺 <b>Channel Opened</b>\n"
            f"<b>Channel:</b> {html.escape(channel_name)}\n"
            f"<b>Quality:</b> {html.escape(quality or '—')}\n"
            f"<b>User:</b> {html.escape(user_display)}\n"
            f"<b>Provider:</b> {html.escape(effective_sub)}\n"
            f"<b>Status:</b> {status_icon} {status_text}"
        )
        await tg_send(msg)
    except Exception as exc:
        log.warning("stream-notify: failed — %s", exc)

# ── Event Bus (SSE pub/sub) ───────────────────────────────────────────────────
class EventBus:
    """In-memory pub/sub for SSE clients. One bounded queue per connection;
    publish drops messages on full to avoid blocking the producer."""
    def __init__(self):
        self._subs: Set[asyncio.Queue] = set()
    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subs.add(q)
        return q
    def unsubscribe(self, q: asyncio.Queue):
        self._subs.discard(q)
    def publish(self, topic: str, data: dict):
        msg = {"topic": topic, "data": data}
        for q in list(self._subs):
            try: q.put_nowait(msg)
            except asyncio.QueueFull: pass  # slow consumer; drop
    @property
    def subscriber_count(self) -> int: return len(self._subs)

bus = EventBus()

# ── Tuner State ───────────────────────────────────────────────────────────────
class TunerState:
    def __init__(self):
        self.subscriptions: List[Subscription] = []
        self.channels: Dict[str, Channel] = {}
        self.all_groups: List[str] = []
        self.raw_groups: List[str] = []    # raw M3U group names seen across all subs
        self.raw_groups_by_sub: Dict[str, List[str]] = {}   # sub_name -> all raw group names
        self.new_groups_by_sub: Dict[str, Set[str]]  = {}   # sub_name -> new group names since last fetch
        self.allowed_groups: Set[str] = set()
        self.device_id = ""; self.base_url = ""; self.total_tuners = 0
        # Phase 11 Item 5: split admin and stream HTTP clients onto separate
        # connection pools so a 25-second M3U refresh can never queue behind
        # active stream reads (or vice versa). The legacy `_http` attribute
        # is preserved as an alias to `_admin_http` for the lifespan teardown.
        self._admin_http:  Optional[httpx.AsyncClient] = None
        self._stream_http: Optional[httpx.AsyncClient] = None
        self._refresh_lock = asyncio.Lock()
        self.mux: Dict[str, ChannelMux] = {}
        self.alerts: List[Alert] = []
        self.logs: List[LogEntry] = []
        self.health: Dict[str, dict] = {}
        self.prev_channel_count = 0
        self.started_at = time.time()
        self.daily_stats = {"streams_started": 0, "total_viewers": 0, "peak_viewers": 0}
        self.account_info: Dict[str, dict] = {}  # sub.name -> account info
        self.shutting_down: bool = False  # Phase 9: drain flag set during lifespan teardown
        # Phase 10: Xtream Codes user accounts (loaded from /config/users.json on startup)
        self.users: List["User"] = []
    @property
    def admin_http(self):
        """Phase 11 Item 5: dedicated client for M3U refresh, EPG fetches,
        Plex API, account-info polls, logo prefetches, _probe_subscription,
        Telegram and any other admin/control-plane traffic. Smaller pool +
        bounded read timeout — never blocks on a stalled stream."""
        if self._admin_http is None or self._admin_http.is_closed:
            self._admin_http = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5, read=120, write=10, pool=5),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                headers={"User-Agent": "AQ-IPTV-Tuner/admin"})
        return self._admin_http

    @property
    def stream_http(self):
        """Phase 11 Item 5: dedicated client for ChannelMux upstream reads.
        Larger pool, no read timeout (live streams are unbounded). Isolated
        from admin traffic so a slow EPG fetch can't starve viewers."""
        if self._stream_http is None or self._stream_http.is_closed:
            self._stream_http = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=None, write=10, pool=30),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
                headers={"User-Agent": "AQ-IPTV-Tuner/stream"})
        return self._stream_http

    @property
    def http(self):
        """Backwards-compat alias used by callers that don't care which pool
        they hit. Always routes to the admin client; new call sites should
        use `admin_http` or `stream_http` explicitly."""
        return self.admin_http
    def pick_subscription(self, exclude: list = None):
        """Pick best available subscription by priority then load.
        Primary (priority=1) always preferred over backup (priority=2).
        Unhealthy subs (failed recently) are deprioritized but not excluded.
        """
        exclude = exclude or []
        av = [s for s in self.subscriptions
              if s.available and s.enabled and s.name not in exclude]
        if not av: return None
        # Sort: priority first, then recency of failure, then load
        return min(av, key=lambda s: (s.priority, s.fail_count, s.load))

    def get_subscription(self, name: str) -> Optional["Subscription"]:
        """Phase 11: lookup a Subscription by name. Used by the dispatcher and
        the candidate-aware ChannelMux to translate ChannelCandidate.sub_name
        back into the live Subscription record."""
        if not name: return None
        for s in self.subscriptions:
            if s.name == name:
                return s
        return None

    def pick_subscription_for_failover(self, failed_sub_name: str):
        """Pick any available subscription excluding the failed one."""
        av = [s for s in self.subscriptions
              if s.available and s.enabled and s.name != failed_sub_name]
        if not av: return None
        return min(av, key=lambda s: (s.priority, s.load))
    def _group_allowed(self, group):
        if not self.allowed_groups: return True
        return any(g.lower() == group.lower() for g in self.allowed_groups)
    def add_alert(self, level, message):
        a = Alert(ts=time.time(), level=level, message=message)
        self.alerts.append(a)
        if len(self.alerts) > 200: self.alerts = self.alerts[-200:]
        log.info("ALERT [%s] %s", level.upper(), message)
        bus.publish("alert", a.to_dict())
    def log_event(self, level, event, detail, sub: str = ""):
        e = LogEntry(ts=time.time(), level=level, event=event, detail=detail, sub=sub)
        self.logs.append(e)
        if len(self.logs) > 500: self.logs = self.logs[-500:]
        bus.publish("log", e.to_dict())
        # Phase 9: tee to the rolling file log so events survive restarts and
        # are grep-friendly in /config/app.log. Pick a Python log level based
        # on the category so errors/warnings stand out in the audit trail.
        _suffix = f" sub={sub}" if sub else ""
        _msg = f"EVT [{event}] {detail}{_suffix}"
        lv = (level or "").lower()
        if lv == "error":     log.error(_msg)
        elif lv == "warning": log.warning(_msg)
        else:                 log.info(_msg)
    async def refresh_channels(self):
        async with self._refresh_lock:
            log.info("Refreshing channels...")
            channels: Dict[str, Channel] = {}
            groups_seen: Dict[str, int] = {}
            for sub in self.subscriptions:
                # ── Phase 6: fetch with retry + error classification ──
                # Network / 5xx errors retry up to 3 times with exponential backoff.
                # Auth / 4xx errors do not retry (pointless — URL or creds are wrong).
                parsed: List[Channel] = []
                total_raw: int = 0          # all channels from upstream (before filter)
                groups_raw: Dict[str, int] = {}  # group counts for all upstream channels
                err_class:    Optional[str] = None
                err_msg:      Optional[str] = None
                fetch_ms      = 0
                attempts_used = 0
                _from_cache   = False

                # ── Channel cache: skip M3U download if cache is fresh ────────
                _cached = _load_channel_cache(sub.name)
                if _cached is not None:
                    parsed      = _cached
                    total_raw   = len(parsed)
                    for _cc in parsed:
                        groups_raw[_cc.group] = groups_raw.get(_cc.group, 0) + 1
                    _from_cache = True
                    log.info("  [%s] loaded %d channels from cache (skipping M3U fetch)",
                             sub.name, len(parsed))

                if not _from_cache:
                    attempts_used = 0
                    MAX_ATTEMPTS = 3
                    BASE_DELAY   = 1.0  # seconds → 1s, 3s, 9s
                    fetch_start = time.time()  # Phase 8: per-sub fetch latency tracking
                    for attempt in range(1, MAX_ATTEMPTS + 1):
                        attempts_used = attempt
                        parsed = []
                        total_raw = 0
                        groups_raw = {}
                        transient = False
                        try:
                            async with self.admin_http.stream("GET", sub.m3u_url(), timeout=120) as resp:
                                if resp.status_code in (401, 403):
                                    err_class = "auth"
                                    err_msg   = f"HTTP {resp.status_code}"
                                    break  # no retry
                                if 400 <= resp.status_code < 500:
                                    err_class = "http_4xx"
                                    err_msg   = f"HTTP {resp.status_code}"
                                    break  # no retry
                                if resp.status_code >= 500:
                                    err_class = "http_5xx"
                                    err_msg   = f"HTTP {resp.status_code}"
                                    transient = True
                                    raise RuntimeError(err_msg)  # fall through to retry
                                # Phase 17: filter during parse — only keep channels that
                                # pass the group filter so we never hold 250K+ Channel objects
                                # in memory simultaneously. Rejected objects are freed
                                # immediately, reducing peak RSS from ~450 MB to <1 MB.
                                async for ch in _parse_m3u(resp):
                                    groups_raw[ch.group] = groups_raw.get(ch.group, 0) + 1
                                    total_raw += 1
                                    if self._group_allowed(ch.group):
                                        parsed.append(ch)
                                err_class = None  # success
                                break
                        except (httpx.TimeoutException, httpx.NetworkError,
                                httpx.RemoteProtocolError, httpx.ReadError) as exc:
                            err_class = "network"
                            err_msg   = _redact(exc) or type(exc).__name__
                            transient = True
                        except RuntimeError:
                            pass  # already classified above
                        except Exception as exc:
                            err_class = "unknown"
                            err_msg   = _redact(exc) or type(exc).__name__
                            break  # don't retry unknown
                        if transient and attempt < MAX_ATTEMPTS:
                            delay = BASE_DELAY * (3 ** (attempt - 1))
                            self.log_event("channel", "FETCH RETRY",
                                           f"{sub.name}: attempt {attempt}/{MAX_ATTEMPTS} ({err_class}: {err_msg}) — retrying in {delay:.0f}s")
                            await asyncio.sleep(delay)
                            continue
                        break

                    fetch_ms = int((time.time() - fetch_start) * 1000)
                    if err_class:
                        # Try stale cache as fallback before giving up
                        _stale = _load_channel_cache(sub.name, max_age=None)
                        if _stale is not None:
                            log.warning(
                                "  [%s] fetch failed (%s) — using stale channel cache (%d channels)",
                                sub.name, err_class, len(_stale))
                            self.add_alert("warning",
                                           f"M3U fetch failed [{sub.name}] ({err_class}) — using cached channels")
                            self.log_event("channel", "FETCH FAILED",
                                           f"[{err_class}] {err_msg} — using {len(_stale)} cached channels",
                                           sub=sub.name)
                            parsed    = _stale
                            total_raw = len(parsed)
                            for _cc in parsed:
                                groups_raw[_cc.group] = groups_raw.get(_cc.group, 0) + 1
                            err_class = None
                        else:
                            _get_sub_health(sub.name).record_fetch(
                                ok=False, ms=fetch_ms, err_class=err_class, err_msg=err_msg or "")
                            log.error("  [%s] failed [%s] after %d attempt(s): %s",
                                      sub.name, err_class, attempts_used, err_msg)
                            self.add_alert("error",
                                           f"M3U fetch failed [{sub.name}] ({err_class}): {err_msg}")
                            self.log_event("channel", "FETCH FAILED",
                                           f"[{err_class}] after {attempts_used} attempt(s): {err_msg}",
                                           sub=sub.name)
                            continue
                    else:
                        # Successful live fetch — stamp sub_name on all variants before caching
                        for _pc in parsed:
                            if not _pc.sub_name:
                                _pc.sub_name = sub.name
                            for _pv in (_pc.variants or []):
                                if not _pv.sub_name:
                                    _pv.sub_name = sub.name
                        # Persist channel cache + raw groups cache
                        _save_channel_cache(sub.name, parsed)
                        # Detect new groups (in this fetch but not in previous known list)
                        _prev_raw = set(self.raw_groups_by_sub.get(sub.name, []))
                        _all_raw  = sorted(groups_raw.keys(), key=str.lower)
                        _new_now  = {g for g in groups_raw if g not in _prev_raw}
                        if _new_now:
                            self.new_groups_by_sub.setdefault(sub.name, set()).update(_new_now)
                        self.raw_groups_by_sub[sub.name] = _all_raw
                        _save_raw_groups_cache(sub.name, _all_raw)

                # ── Merge parsed channels into the shared dict ──
                # Phase 11: in addition to the legacy `variants` list (which
                # only carries quality variants from a single sub), we also
                # accumulate a `candidates` list — every (sub, stream_id)
                # tuple becomes a ChannelCandidate that the StreamDispatcher
                # can pick from per-request and that ChannelMux can fail
                # over between mid-stream.
                # Phase 17: parsed is pre-filtered (all entries pass _group_allowed).
                # Merge per-sub group counts into the outer groups_seen dict.
                groups_seen.update(
                    {g: groups_seen.get(g, 0) + n for g, n in groups_raw.items()}
                )
                total = total_raw
                added = 0
                for ch in parsed:
                    # group filter already applied — no skip needed
                    base = _base_name(ch.name)
                    base_norm = re.sub(r'[_\s]+', ' ', base.lower()).strip()
                    base_id = hashlib.md5(base_norm.encode()).hexdigest()[:10]
                    qpref = load_config().get("quality_preference", ["HD","FHD","4K","SD","HEVC"])
                    def _pref(q, _qpref=qpref): return _qpref.index(q) if q in _qpref else 99
                    new_cand = ChannelCandidate(
                        sub_name  = sub.name,
                        stream_id = ch.stream_id,
                        tvg_id    = ch.tvg_id,
                        logo      = ch.logo,
                        quality   = ch.quality,
                        priority  = sub.priority,
                    )
                    if base_id in channels:
                        existing = channels[base_id]
                        if existing.variants is None: existing.variants = []
                        v = ch.variants[0] if ch.variants else QualityVariant(
                            stream_id=ch.stream_id, quality=ch.quality,
                            priority=QPRIO.get(ch.quality,20), orig_name=ch.name,
                            sub_name=ch.sub_name or "")
                        # Only add if this stream_id not already present
                        if not any(ev.stream_id == v.stream_id
                                   for ev in existing.variants):
                            existing.variants.append(v)
                        # Phase 11: dedupe candidates by (sub_name, stream_id)
                        if not any(c.sub_name == new_cand.sub_name and
                                   c.stream_id == new_cand.stream_id
                                   for c in existing.candidates):
                            existing.candidates.append(new_cand)
                        best = min(existing.variants,
                                   key=lambda x: (_pref(x.quality), -x.priority))
                        existing.stream_id = best.stream_id
                        existing.quality   = best.quality
                        existing.name      = base
                    else:
                        ch.id         = base_id
                        ch.name       = base
                        ch.sub_name   = sub.name
                        ch.candidates = [new_cand]
                        # Propagate sub_name to the variant (parser couldn't know it)
                        if ch.variants:
                            for v in ch.variants:
                                if not v.sub_name:
                                    v.sub_name = sub.name
                        channels[base_id] = ch
                        added += 1
                _get_sub_health(sub.name).record_fetch(
                    ok=True, ms=fetch_ms, channels=added)
                suffix = f" ({attempts_used} attempt{'s' if attempts_used != 1 else ''})" if attempts_used > 1 else ""
                log.info("  [%s] %d -> %d%s", sub.name, total, added, suffix)
                self.log_event("channel", "CHANNELS LOADED",
                               f"{total:,} total → {added} loaded{suffix}",
                               sub=sub.name)
            if self.prev_channel_count > 0:
                drop = self.prev_channel_count - len(channels)
                if drop > 50:
                    self.add_alert("warning", f"Channel count dropped by {drop}")
                    self.log_event("channel", "COUNT DROP", f"{self.prev_channel_count} → {len(channels)} (−{drop})")
                    asyncio.create_task(tg_send(tg_alert("⚠️", "Channel Count Dropped", f"{self.prev_channel_count:,} → {len(channels):,} channels (−{drop})")))
            # ── Re-apply saved renames ───────────────────────────
            cfg_saved    = load_config()
            saved_renames = cfg_saved.get("channel_renames", {})
            for ch in list(channels.values()):
                if ch.name in saved_renames:
                    old_name = ch.name
                    ch.name  = saved_renames[old_name]
                    log.debug("Renamed: %s → %s", old_name, ch.name)

            # ── Re-apply saved manual merges ──────────────────────
            saved_merges = cfg_saved.get("channel_merges", {})
            for target_name, absorbed_names in saved_merges.items():
                # Find target channel (by current name after renames)
                target_ch = next(
                    (c for c in channels.values() if c.name == target_name), None)
                if not target_ch:
                    log.debug("Merge target not found: %s", target_name)
                    continue
                for abs_name in absorbed_names:
                    abs_ch = next(
                        (c for c in channels.values() if c.name == abs_name), None)
                    if not abs_ch or abs_ch is target_ch:
                        continue
                    # Move variants from absorbed → target
                    if target_ch.variants is None:
                        target_ch.variants = []
                    for v in (abs_ch.variants or [QualityVariant(
                            stream_id=abs_ch.stream_id,
                            quality=abs_ch.quality,
                            priority=QPRIO.get(abs_ch.quality, 20),
                            orig_name=abs_ch.name,
                            sub_name=abs_ch.sub_name or "")]):
                        # Only add if this stream_id not already present
                        if not any(ev.stream_id == v.stream_id
                                   for ev in target_ch.variants):
                            target_ch.variants.append(v)
                    # Phase 11: also move ChannelCandidate entries so the
                    # dispatcher sees the merged candidate set.
                    for c in abs_ch.candidates:
                        if not any(tc.sub_name == c.sub_name and
                                   tc.stream_id == c.stream_id
                                   for tc in target_ch.candidates):
                            target_ch.candidates.append(c)
                    # Re-elect best stream per preference
                    qpref3 = cfg_saved.get("quality_preference",
                                           ["HD","FHD","4K","SD","HEVC"])
                    def _pref3(q):
                        return qpref3.index(q) if q in qpref3 else 99
                    best3 = min(target_ch.variants,
                                key=lambda v: (_pref3(v.quality), -v.priority))
                    target_ch.stream_id    = best3.stream_id
                    target_ch.quality      = best3.quality
                    # Remove absorbed channel
                    abs_key = next(
                        (k for k, c in channels.items() if c is abs_ch), None)
                    if abs_key:
                        del channels[abs_key]
                        log.debug("Absorbed %s into %s", abs_name, target_name)

            if saved_merges or saved_renames:
                log.info("Re-applied %d manual merge(s), %d rename(s) — channels now: %d",
                         len(saved_merges), len(saved_renames), len(channels))
            else:
                log.info("No saved merges/renames in config")

            # ── Deduplicate exact same-name channels ──────────────
            # (can happen when two groups have same channel with same quality)
            name_seen: Dict[str, str] = {}  # name → first key seen
            for key in list(channels.keys()):
                ch = channels[key]
                if ch.name in name_seen:
                    primary_key = name_seen[ch.name]
                    primary     = channels[primary_key]
                    # Merge variants from duplicate into primary
                    if primary.variants is None: primary.variants = []
                    for v in (ch.variants or [QualityVariant(
                            stream_id=ch.stream_id, quality=ch.quality,
                            priority=QPRIO.get(ch.quality, 20),
                            orig_name=ch.name, sub_name=ch.sub_name or "")]):
                        # Only add if this stream_id not already present
                        if not any(ev.stream_id == v.stream_id
                                   for ev in primary.variants):
                            primary.variants.append(v)
                    # Phase 11: merge candidate lists too
                    for c in ch.candidates:
                        if not any(pc.sub_name == c.sub_name and
                                   pc.stream_id == c.stream_id
                                   for pc in primary.candidates):
                            primary.candidates.append(c)
                    del channels[key]
                    log.debug("Dedup: removed duplicate '%s'", ch.name)
                else:
                    name_seen[ch.name] = key

            # ── Apply channel blacklist ────────────────────────────────────────
            saved_blacklist = set(cfg_saved.get("channel_blacklist", []))
            if saved_blacklist:
                _bl_names = [ch.name for ch in channels.values()
                             if ch.name in saved_blacklist]
                for k in [k for k, ch in channels.items()
                          if ch.name in saved_blacklist]:
                    del channels[k]
                if _bl_names:
                    log.info("Blacklisted %d channel(s): %s",
                             len(_bl_names), ", ".join(_bl_names))

            # ── Apply channel group assignments ────────────────────────────────
            # 1. Regex rules from channel_groups (name-based, e.g. "BeIN.*" → "BeIN Sports")
            saved_group_rules = [r for r in cfg_saved.get("channel_groups", [])
                                 if not r[0].startswith("^__direct__")]
            if saved_group_rules:
                _compiled_groups = [(re.compile(pat), grp)
                                    for pat, grp in saved_group_rules]
                _grp_count = 0
                for ch in channels.values():
                    for pat, grp in _compiled_groups:
                        if pat.match(ch.name):
                            ch.group = grp
                            _grp_count += 1
                            break
                if _grp_count:
                    log.info("Group-assigned %d channel(s) across %d folders",
                             _grp_count,
                             len({g for _, g in _compiled_groups}))
            # 2. Direct key→group assignments (from channel-groups UI) — highest priority
            direct_assignments = cfg_saved.get("channel_assignments", {})
            _direct_count = 0
            for key, grp in direct_assignments.items():
                if key in channels and grp:
                    channels[key].group = grp
                    _direct_count += 1
            if _direct_count:
                log.info("Direct-assigned %d channel(s) to logical groups", _direct_count)

            # ── Sequential channel renumbering by group (Phase 18+) ──────────
            # Assign clean 1-N numbers ordered by logical group, then alpha
            # within each group. Runs AFTER group assignment so ch.group
            # reflects the 10 logical folders. Unmatched channels append last.
            _GROUP_ORDER = cfg_saved.get("group_order") or [
                "AD Sport",
                "BeIN Sports",
                "BeIN AFC / Al-Kass",
                "BeIN Xtra",
                "Thmanayh",
                "BeIN Entertainment",
                "OSN",
                "Alwan",
                "Shahid",
                "STC",
            ]
            _grp_idx = {g: i for i, g in enumerate(_GROUP_ORDER)}
            _sorted_chs = sorted(
                channels.items(),
                key=lambda kv: (
                    _grp_idx.get(kv[1].group, len(_GROUP_ORDER)),
                    kv[1].group or "",
                    kv[1].name,
                )
            )
            for _seq, (key, ch) in enumerate(_sorted_chs, start=1):
                ch.number = str(_seq)
            log.info("Renumbered %d channel(s) sequentially by group", len(channels))

            # ── Apply per-channel quality overrides ────────────
            saved_overrides = load_config().get("quality_overrides", {})
            for ch in channels.values():
                if ch.name in saved_overrides:
                    wanted = saved_overrides[ch.name]
                    if ch.variants:
                        match = next((v for v in ch.variants
                                      if v.quality == wanted), None)
                        if match:
                            ch.stream_id = match.stream_id
                            ch.quality   = match.quality
            if saved_overrides:
                log.info("Applied %d quality override(s)", len(saved_overrides))

            # ── Phase 11: order candidates by (priority, quality_pref) and
            # sync ch.sub_name to candidates[0]. The dispatcher will reorder
            # per-request via score, but the *static* primary is what shows
            # up in /api/channels and /lineup.json before any stream opens.
            qpref_final = cfg_saved.get("quality_preference",
                                        ["HD","FHD","4K","SD","HEVC"])
            def _qrank(q): return qpref_final.index(q) if q in qpref_final else 99
            for ch in channels.values():
                if ch.candidates:
                    ch.candidates.sort(key=lambda c: (c.priority, _qrank(c.quality)))
                    ch.sub_name = ch.candidates[0].sub_name
                    # Don't blindly overwrite stream_id/quality — the variant
                    # picker above may have chosen a non-primary candidate
                    # explicitly via quality_overrides. Only sync if our scalar
                    # fields are still unset (first time after refresh).
                    if not ch.sub_name:
                        ch.sub_name = ch.candidates[0].sub_name

            self.channels = channels
            self.prev_channel_count = len(channels)
            # Build all_groups from the final assigned channel groups (post-rename/
            # post-channel_groups assignment) so that _category_id_for() can find
            # custom group names like "AD Sport", "BeIN Sports" etc.  The upstream
            # groups_seen dict contains raw subscription group names which never
            # match the custom groups, causing every category_id to fall back to "0".
            self.all_groups = sorted(
                {ch.group for ch in channels.values() if ch.group},
                key=str.lower,
            )
            # Persist raw M3U group names (before channel_groups reassignment)
            # so /api/groups can show providers' original group names for filtering.
            self.raw_groups = sorted(groups_seen.keys(), key=str.lower)
            # Phase 8: clear dead-channel records — give everything a fresh start.
            if _DEAD_CHANNELS:
                log.info("Cleared %d dead-channel records on refresh", len(_DEAD_CHANNELS))
                _DEAD_CHANNELS.clear()
            log.info("Ready: %d channels, %d groups", len(channels), len(self.all_groups))
            # Phase 17: release parsed/groups_raw locals, run gc, then trim the
            # glibc heap so the OS gets the pages back immediately after each refresh
            # (previously deferred to malloc_trim_loop which skips during active streams).
            import gc as _gc
            _gc.collect()
            try:
                import ctypes as _ct
                def _rss_mb():
                    try:
                        with open("/proc/self/status") as _f:
                            for _l in _f:
                                if _l.startswith("VmRSS:"):
                                    return int(_l.split()[1]) // 1024
                    except Exception:
                        pass
                    return 0
                _libc = _ct.cdll.LoadLibrary("libc.so.6")
                _before = _rss_mb()
                _libc.malloc_trim(0)
                _after = _rss_mb()
                if _before != _after:
                    log.info("post-refresh trim: %d MB → %d MB (freed %d MB)",
                             _before, _after, _before - _after)
            except Exception:
                pass
            # Phase 6: warm the disk logo cache in background so first client hit is fast.
            asyncio.create_task(self._prefetch_logos())
            # Phase 10: blow the per-user XMLTV cache — channel set has changed,
            # so any cached filtered xmltv is potentially stale.
            _USER_EPG_CACHE.clear()
            # Phase 11: clear the dispatcher's sticky cache for the same reason —
            # candidates may have appeared, disappeared, or been re-priced.
            try:
                n_dropped = dispatcher.invalidate_all()
                if n_dropped:
                    log.info("Dispatch cache cleared (%d decisions)", n_dropped)
            except NameError:
                pass  # dispatcher not yet defined on first import

    async def _prefetch_logos(self):
        """Download any missing / stale logos in the background, concurrency-capped.
        Stale = older than LOGO_TTL (~30 days, so upstream logo changes are picked
        up monthly). Also prunes orphan files for channels no longer in the M3U so
        the cache directory tracks the live channel set."""
        # Orphan prune: any cached file whose key isn't a current channel goes.
        live_keys = {_logo_safe_key(k) for k in self.channels.keys()}
        try:
            for entry in os.listdir(LOGO_DIR):
                if entry.startswith("."):  # tempfile leftovers
                    continue
                stem = entry.rsplit(".", 1)[0]
                lookup = stem[len("custom_"):] if stem.startswith("custom_") else stem
                if lookup not in live_keys:
                    try:
                        os.remove(os.path.join(LOGO_DIR, entry))
                    except OSError:
                        pass
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.debug("logo orphan prune skipped: %s", _redact(exc))

        sem = asyncio.Semaphore(6)
        async def one(key, ch):
            async with sem:
                path, mtime = _find_cached_logo(key)
                if path and (time.time() - mtime) < LOGO_TTL:
                    return False
                result = await _download_logo(ch, key)
                return result is not None
        tasks = [asyncio.create_task(one(k, c))
                 for k, c in list(self.channels.items()) if c.logo]
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        fetched = sum(1 for r in results if r is True)
        if fetched:
            log.info("Logo prefetch: %d new / %d total", fetched, len(tasks))

    async def fetch_account_info(self):
        """Fetch Xtream Codes account info (expiry, status, connections) for each subscription."""
        for sub in self.subscriptions:
            try:
                url = f"{sub.url}/player_api.php?username={sub.username}&password={sub.password}"
                resp = await self.admin_http.get(url, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                ui = data.get("user_info", {})
                exp_ts = ui.get("exp_date")
                if exp_ts:
                    try:
                        exp_dt = datetime.fromtimestamp(int(exp_ts))
                        days_left = (exp_dt - datetime.now()).days
                        exp_str = exp_dt.strftime("%Y-%m-%d")
                    except (ValueError, TypeError, OSError):
                        exp_str = str(exp_ts); days_left = -1
                else:
                    exp_str = "Unknown"; days_left = -1

                self.account_info[sub.name] = {
                    "status":          ui.get("status", "Unknown"),
                    "exp_date":        exp_str,
                    "days_left":       days_left,
                    "max_connections": ui.get("max_connections", "?"),
                    "active_cons":     ui.get("active_cons", "0"),
                    "created_at":      datetime.fromtimestamp(int(ui["created_at"])).strftime("%Y-%m-%d") if ui.get("created_at") else "Unknown",
                    "is_trial":        ui.get("is_trial", "0") == "1",
                }
                log.info("Account [%s] — expires %s (%d days)", sub.name, exp_str, days_left)

                # Alert if expiring soon
                if 0 < days_left <= 7:
                    self.add_alert("warning", f"Subscription expiring in {days_left} days: {sub.name}")
                    asyncio.create_task(tg_send(tg_alert(
                        "⏰", "Subscription Expiring Soon",
                        f"<b>{sub.name}</b> expires on {exp_str} ({days_left} days left)")))
                elif days_left == 0:
                    self.add_alert("error", f"Subscription expired today: {sub.name}")
                    asyncio.create_task(tg_send(tg_alert(
                        "🔴", "Subscription Expired",
                        f"<b>{sub.name}</b> expired on {exp_str}")))

            except Exception as exc:
                err = _redact(exc)
                log.warning("Account info fetch failed [%s]: %s", sub.name, err)
                self.account_info[sub.name] = {
                    "status": "Error", "exp_date": "—", "days_left": -1,
                    "max_connections": "?", "active_cons": "0",
                    "created_at": "—", "is_trial": False,
                    "error": err,
                }

    def reload_from_config(self):
        cfg = load_config()
        self.subscriptions.clear()
        for s in cfg.get("subscriptions", []):
            # A36: env-var overrides so per-sub creds can live in .env
            # instead of plaintext in config.json.  Falls back to the
            # config value if the env var is unset.
            slug = _sub_env_slug(s["name"])
            username = os.getenv(f"SUB_{slug}_USERNAME") or s.get("username", "")
            password = os.getenv(f"SUB_{slug}_PASSWORD") or s.get("password", "")
            self.subscriptions.append(Subscription(
                name=s["name"], url=s["url"].rstrip("/"),
                username=username, password=password,
                max_streams=int(s.get("max_streams", 1)),
                priority=int(s.get("priority", 1)),
                enabled=s.get("enabled", True)))
        self.allowed_groups = set(cfg.get("filters", {}).get("groups", []))
        self.total_tuners = sum(s.max_streams for s in self.subscriptions)

state = TunerState()

# ── Analytics DB ─────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "/config/analytics.db")

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db_connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS watch_sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_key TEXT NOT NULL,
            channel     TEXT NOT NULL,
            viewer_ip   TEXT NOT NULL,
            plex_user   TEXT,
            started_at  REAL NOT NULL,
            ended_at    REAL,
            duration_s  INTEGER,
            bytes_total INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_started ON watch_sessions(started_at);
        CREATE INDEX IF NOT EXISTS idx_channel ON watch_sessions(channel_key);
        CREATE INDEX IF NOT EXISTS idx_user    ON watch_sessions(plex_user);

        CREATE TABLE IF NOT EXISTS ip_user_map (
            ip          TEXT PRIMARY KEY,
            plex_user   TEXT NOT NULL,
            updated_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_bandwidth (
            date        TEXT PRIMARY KEY,
            bytes_total INTEGER NOT NULL DEFAULT 0,
            sessions    INTEGER NOT NULL DEFAULT 0,
            peak_kbps   INTEGER NOT NULL DEFAULT 0
        );
        """)
    log.info("Analytics DB ready: %s", DB_PATH)

db_init()

# ── Plex API ──────────────────────────────────────────────────────────────────
_ip_user_cache: Dict[str, str] = {}

async def _plex_get_sessions_xml():
    """Raw Plex /status/sessions XML root, or None on failure."""
    cfg = load_config().get("plex", {})
    if not cfg.get("enabled"): return None
    try:
        # A7: send token via header (not URL) so it doesn't leak into access logs
        url = f"{cfg['url']}/status/sessions"
        resp = await state.admin_http.get(url, timeout=10,
                                          headers={"X-Plex-Token": cfg["token"],
                                                   "Accept": "application/xml"})
        resp.raise_for_status()
        return ET.fromstring(resp.text)
    except Exception as exc:
        log.debug("Plex session fetch failed: %s", exc)
        return None

async def plex_get_users() -> Dict[str, str]:
    """Fetch active Plex sessions and return {ip: username} map."""
    root = await _plex_get_sessions_xml()
    if root is None: return {}
    result = {}
    for video in root.findall(".//Video"):
        player = video.find("Player")
        user   = video.find("User")
        if player is not None and user is not None:
            ip    = player.get("address", "")
            uname = user.get("title", "Unknown")
            if ip: result[ip] = uname
    return result

def _norm_chan(s: str) -> str:
    """Normalise a channel name for fuzzy matching: lowercase, strip non-alnum."""
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())

async def plex_user_for_channel(channel_name: str, channel_key: str = "") -> Optional[str]:
    """Find the Plex user whose active session is playing this channel.

    Plex needs a beat to register the session after playback starts (it sits
    in "buffering" briefly), and Live-TV sessions often don't appear for 3-8s
    during fast channel-changes, so we poll for up to ~12s. Matching strategy:
      1. Title-ish fields — for Live TV with EPG, `title` is
         "<Channel> · <Program>", so a substring match on the channel name
         catches it. We also compare normalised (alnum-only) forms so extra
         punctuation/spacing doesn't block a match.
      2. Media/Part @key or @file containing the channel_key (fallback for
         sessions with no title metadata at all).
      3. Newest video (highest addedAt) — common single-user / fast-switch
         case; the just-started session is almost always the newest one."""
    if not channel_name and not channel_key: return None
    needle_name = (channel_name or "").lower().strip()
    needle_key  = (channel_key  or "").lower().strip()
    needle_norm = _norm_chan(channel_name)

    # Poll: sessions may not be registered the instant we're called.
    # 8 attempts × 1.5s = up to 12s, which covers slow channel-change handoffs.
    videos = []
    for attempt in range(8):
        root = await _plex_get_sessions_xml()
        if root is not None:
            videos = root.findall(".//Video")
            if videos: break
        await asyncio.sleep(1.5)
    if not videos:
        log.info("plex-resolve: no active Plex sessions after poll for %r", channel_name)
        return None

    def _user_of(v):
        u = v.find("User")
        return u.get("title", "Unknown") if u is not None else None

    # Pass 1: title-ish fields (title, grandparentTitle, etc.).
    if needle_name:
        for v in videos:
            fields_raw = [
                v.get("title")            or "",
                v.get("grandparentTitle") or "",
                v.get("parentTitle")      or "",
                v.get("originalTitle")    or "",
            ]
            for f in fields_raw:
                fl = f.lower()
                if not fl: continue
                if fl == needle_name or needle_name in fl or fl in needle_name:
                    u = _user_of(v)
                    if u: return u
                # Fallback: normalised (strip punctuation/spaces) substring compare.
                fn = _norm_chan(f)
                if needle_norm and fn and (needle_norm in fn or fn in needle_norm):
                    u = _user_of(v)
                    if u: return u

    # Pass 2: look inside Media/Part for our channel_key.
    if needle_key:
        for v in videos:
            for part in v.findall(".//Part"):
                blob = " ".join(filter(None, [
                    part.get("key", ""), part.get("file", ""),
                    part.get("container", ""),
                ])).lower()
                if needle_key in blob:
                    u = _user_of(v)
                    if u: return u

    # Pass 3: newest video wins (fast channel-change — just-opened session
    # is almost always the newest). Safer than "only one session" because
    # if the user is idle-browsing other content, there might be multiple.
    def _added(v):
        try: return int(v.get("addedAt", "0"))
        except: return 0
    newest = max(videos, key=_added)
    u = _user_of(newest)
    if u:
        # Log so we know when we fell through to the heuristic.
        titles = [v.get("title", "") for v in videos]
        log.info("plex-resolve: fell back to newest session for %r (videos=%s) → %s",
                 channel_name, titles, u)
        return u
    log.info("plex-resolve: no match for %r (videos=%s)",
             channel_name, [v.get("title", "") for v in videos])
    return None

async def resolve_plex_user(ip: str) -> str:
    """Get Plex username for an IP, using cache then live lookup."""
    if ip in _ip_user_cache:
        return _ip_user_cache[ip]
    # Check DB cache first
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT plex_user FROM ip_user_map WHERE ip=? AND updated_at > ?",
                (ip, time.time() - 3600)).fetchone()
            if row:
                _ip_user_cache[ip] = row["plex_user"]
                return row["plex_user"]
    except: pass
    # Live Plex lookup
    users = await plex_get_users()
    for pip, uname in users.items():
        _ip_user_cache[pip] = uname
        try:
            with db_connect() as conn:
                conn.execute("INSERT OR REPLACE INTO ip_user_map VALUES (?,?,?)",
                             (pip, uname, time.time()))
        except: pass
    return _ip_user_cache.get(ip, ip)  # fallback to IP if no match

# ── Session tracking ──────────────────────────────────────────────────────────
_active_sessions: Dict[str, int] = {}        # "{channel_key}:{ip}" -> db row id
_active_session_users: Dict[str, str] = {}   # same key -> resolved plex user

async def _resolve_and_update_user(row_id: int, active_key: str,
                                   channel: str, channel_key: str, viewer_ip: str):
    """Resolve the real Plex username (polls Plex) and update the row + cache.

    Plex live-TV sessions via HDHR can take 20-60s to appear in /status/sessions.
    We retry up to 3 more times (each a 12s poll, spaced 10s apart = ~78s total)
    and bail early if the viewer has already left.
    """
    try:
        plex_user = await plex_user_for_channel(channel, channel_key)
        if not plex_user:
            for retry in range(3):
                await asyncio.sleep(10)
                if active_key not in _active_sessions:
                    return  # viewer left before we resolved — nothing to update
                plex_user = await plex_user_for_channel(channel, channel_key)
                if plex_user:
                    log.info("plex-resolve: resolved on retry %d for %r → %s",
                             retry + 1, channel, plex_user)
                    break
        if not plex_user:
            fallback = await resolve_plex_user(viewer_ip)
            # Only use the fallback if it resolved to a real name (not just the IP back).
            # If resolution completely failed, keep "Plex" as the display label.
            plex_user = fallback if fallback and fallback != viewer_ip else "Plex"
        _active_session_users[active_key] = plex_user
        with db_connect() as conn:
            conn.execute("UPDATE watch_sessions SET plex_user=? WHERE id=?",
                         (plex_user, row_id))
        state.log_event("viewer", "SESSION START",
                        f"{plex_user} ({viewer_ip}) started watching {channel}")
    except Exception as exc:
        log.warning("session resolve-user failed: %s", exc)

async def session_start(channel_key: str, channel: str, viewer_ip: str,
                        xtream_user: Optional[str] = None):
    # Insert the row immediately so session_end can find it. If the caller
    # already knows the Xtream username (the request came in via /live/ with
    # creds), use it directly and skip the Plex lookup — it's both pointless
    # (Plex doesn't know about Xtream users) and wrong (the bridge IP
    # 172.17.0.1 happens to map to a real Plex device, so the lookup returns
    # a misleading display name like "HOMESERVER_AQ"). For HDHR/Plex sessions
    # the original Plex resolution path still runs in the background.
    # Xtream users: use the known username directly.
    # Plex/HDHR viewers: default to "Plex" until async resolution fills in the
    # real account name. This prevents raw IPs appearing in the streams panel
    # when Plex hasn't registered the session yet (common during fast channel
    # changes or when Plex session polling returns nothing).
    initial_user = xtream_user or "Plex"
    try:
        with db_connect() as conn:
            cur = conn.execute(
                "INSERT INTO watch_sessions (channel_key,channel,viewer_ip,plex_user,started_at) VALUES (?,?,?,?,?)",
                (channel_key, channel, viewer_ip, initial_user, time.time()))
            row_id = cur.lastrowid
    except Exception as exc:
        log.warning("session_start DB error: %s", exc)
        return
    active_key = f"{channel_key}:{viewer_ip}"
    _active_sessions[active_key] = row_id
    _active_session_users[active_key] = initial_user
    if xtream_user:
        state.log_event("viewer", "SESSION START",
                        f"{xtream_user} ({viewer_ip}) started watching {channel}",
                        sub=xtream_user)
    else:
        asyncio.create_task(_resolve_and_update_user(
            row_id, active_key, channel, channel_key, viewer_ip))

async def session_end(channel_key: str, channel: str, viewer_ip: str, bytes_total: int = 0):
    key = f"{channel_key}:{viewer_ip}"
    row_id = _active_sessions.pop(key, None)
    plex_user = _active_session_users.pop(key, viewer_ip)
    if not row_id: return
    try:
        with db_connect() as conn:
            row = conn.execute("SELECT started_at FROM watch_sessions WHERE id=?", (row_id,)).fetchone()
            if row:
                duration = int(time.time() - row["started_at"])
                conn.execute(
                    "UPDATE watch_sessions SET ended_at=?, duration_s=?, bytes_total=? WHERE id=?",
                    (time.time(), duration, bytes_total, row_id))
                state.log_event("viewer", "SESSION END",
                    f"{plex_user} watched {channel} for {duration}s ({bytes_total//1024} KB)")
                # Accumulate daily bandwidth
                today = time.strftime("%Y-%m-%d")
                conn.execute("""
                    INSERT INTO daily_bandwidth(date, bytes_total, sessions)
                    VALUES (?,?,1)
                    ON CONFLICT(date) DO UPDATE SET
                        bytes_total = bytes_total + excluded.bytes_total,
                        sessions    = sessions + 1
                """, (today, bytes_total))
    except Exception as exc:
        log.warning("session_end DB error: %s", exc)

# ── M3U Parser ────────────────────────────────────────────────────────────────
async def _parse_m3u(resp: httpx.Response):
    """Parse an M3U stream into Channel objects."""
    auto_num = 1; info = ""; buf = ""
    header_seen = False
    async for raw in resp.aiter_bytes(32768):
        buf += raw.decode("utf-8", errors="replace")
        lines = buf.split("\n"); buf = lines.pop()
        for line in lines:
            line = line.strip()
            if not line: continue
            if not header_seen and line.startswith("#EXTM3U"):
                header_seen = True
                continue
            header_seen = True
            if line.startswith("#EXTINF"): info = line
            elif info and not line.startswith("#"):
                def _a(pat, d="", il=info):
                    m = re.search(pat, il); return m.group(1).strip() if m else d
                name   = _a(r",(.+)$") or f"Ch {auto_num}"
                tvg_id = _a(r'tvg-id="([^"]*)"')
                logo   = _a(r'tvg-logo="([^"]*)"')
                group  = _a(r'group-title="([^"]*)"') or "Uncategorized"
                ch_num = _a(r'tvg-chno="([^"]*)"') or str(auto_num)
                sid    = re.sub(r"\.\w+$", "", line.rstrip("/").split("/")[-1])
                ch_id  = tvg_id or hashlib.md5(name.encode()).hexdigest()[:10]
                if sid:
                    q, qp = _detect_quality(name, group)
                    yield Channel(id=ch_id, name=name, number=ch_num,
                                  logo=logo, group=group, stream_id=sid,
                                  quality=q,
                                  tvg_id=tvg_id,
                                  variants=[QualityVariant(
                                      stream_id=sid, quality=q,
                                      priority=qp, orig_name=name)])
                    auto_num += 1
                info = ""

# ── Health Monitor ────────────────────────────────────────────────────────────
async def health_monitor():
    # Prime the host CPU sampler once before entering the loop.
    # psutil.cpu_percent(interval=None) is non-blocking (returns the delta
    # since the previous call) but the first call always returns 0.0 — so
    # we throw away one reading here, then every loop iteration gets a real
    # ~30s average matching the sleep window. Avoids the previous
    # interval=1 call which blocked the asyncio event loop for 1s.
    psutil.cpu_percent(interval=None)
    _proc = psutil.Process(os.getpid())
    while True:
        await asyncio.sleep(30)
        try:
            for sub in state.subscriptions:
                prev = state.health.get(f"srv_{sub.name}", {}).get("status")
                probe_start = time.time()
                try:
                    host = sub.url.split("//")[-1].split(":")[0]
                    port_s = sub.url.split(":")[-1].split("/")[0]
                    port = int(port_s) if port_s.isdigit() else 80
                    _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
                    w.close()
                    probe_ms = int((time.time() - probe_start) * 1000)
                    state.health[f"srv_{sub.name}"] = {"status": "ok", "label": sub.name, "checked": time.strftime("%H:%M:%S")}
                    _get_sub_health(sub.name).record_tcp(ok=True, ms=probe_ms)  # Phase 8
                    if prev and prev != "ok":
                        state.log_event("health", "SERVER ONLINE",
                                        f"{sub.name} is reachable again", sub=sub.name)
                        asyncio.create_task(tg_send(tg_alert("✅", "Server Back Online", f"{sub.name} is reachable again")))
                except Exception as exc:
                    probe_ms = int((time.time() - probe_start) * 1000)
                    state.health[f"srv_{sub.name}"] = {"status": "error", "label": sub.name, "error": str(exc), "checked": time.strftime("%H:%M:%S")}
                    _get_sub_health(sub.name).record_tcp(ok=False, ms=probe_ms)  # Phase 8
                    state.add_alert("error", f"Server unreachable: {sub.name}")
                    if prev != "error":
                        state.log_event("health", "SERVER DOWN",
                                        f"{sub.name}: {exc}", sub=sub.name)
                        asyncio.create_task(tg_send(tg_alert("🔴", "Server Unreachable", f"<b>{sub.name}</b>\nError: {exc}")))
            # Phase 8: persist SubHealth + dead-channel state each cycle so a
            # restart doesn't lose the rolling history used by the score.
            _save_sub_health_snapshot()
            for key, mux in list(state.mux.items()):
                if mux.is_stalled:
                    state.add_alert("warning", f"Stream stalled, restarting: {mux.ch.name}")
                    idle = int(time.time()-mux.last_chunk_at)
                    state.log_event("health", "STALL RESTART", f"{mux.ch.name} — no data for {idle}s")
                    # Alert only if this channel stalls 3+ times within an hour
                    now_ts = time.time()
                    mux._stall_alert_times = [t for t in mux._stall_alert_times if now_ts - t < 3600]
                    mux._stall_alert_times.append(now_ts)
                    if len(mux._stall_alert_times) >= 3:
                        asyncio.create_task(tg_send(tg_alert(
                            "⚠️", "Stream Stalling Repeatedly",
                            f"Channel: <b>{mux.ch.name}</b>\n"
                            f"{len(mux._stall_alert_times)}× stalls in last hour\n"
                            f"Last idle: {idle}s")))
                    ch = mux.ch; sub = mux.sub
                    await mux.kill_all(); await asyncio.sleep(2)
                    nm = ChannelMux(key, ch, sub); state.mux[key] = nm; await nm.start()
            # Refresh Plex user → IP map
            users = await plex_get_users()
            for pip, uname in users.items():
                _ip_user_cache[pip] = uname
                try:
                    with db_connect() as conn:
                        conn.execute("INSERT OR REPLACE INTO ip_user_map VALUES (?,?,?)",
                                     (pip, uname, time.time()))
                except: pass

            # Host-wide CPU (intentional — gives visibility into the whole
            # Synology, not just iptv-tuner). interval=None is non-blocking;
            # it reports the average since the previous call, which is the
            # 30s sleep above.
            cpu_pct = psutil.cpu_percent(interval=None)
            mem_mb  = round(_proc.memory_info().rss / 1024 / 1024, 1)
            mem_pct = round(_proc.memory_percent(), 1)

            # Track peak bandwidth across all active streams
            total_kbps = sum(
                round(m.bytes_total / max(1, time.time()-m.started_at) / 128)
                for m in state.mux.values() if m._running)
            if total_kbps > 0:
                today = time.strftime("%Y-%m-%d")
                try:
                    with db_connect() as conn:
                        conn.execute("""
                            INSERT INTO daily_bandwidth(date, bytes_total, sessions, peak_kbps)
                            VALUES (?,0,0,?)
                            ON CONFLICT(date) DO UPDATE SET
                                peak_kbps = MAX(peak_kbps, excluded.peak_kbps)
                        """, (today, total_kbps))
                except: pass

            state.health["system"] = {
                "cpu_pct":  cpu_pct,
                "mem_mb":   mem_mb,
                "mem_pct":  mem_pct,
                "uptime_s": int(time.time() - state.started_at),
                "live_kbps": total_kbps,
                "checked":  time.strftime("%H:%M:%S")}

            # ── CPU / Memory threshold alerts ─────────────────────────────
            cfg_tg = load_config().get("telegram", {})
            cpu_warn  = cfg_tg.get("cpu_alert_pct",  85)
            mem_warn  = cfg_tg.get("mem_alert_pct",  90)
            cpu_crit  = cfg_tg.get("cpu_critical_pct", 95)

            prev_cpu = state.health.get("_prev_cpu", 0)
            prev_mem = state.health.get("_prev_mem", 0)

            # CPU critical (only alert once per crossing). Thresholds apply
            # to host-wide CPU — alerts may fire when other containers
            # (Plex, indexers, etc.) load the box, not just iptv-tuner.
            if cpu_pct >= cpu_crit and prev_cpu < cpu_crit:
                state.add_alert("error", f"Host CPU critical: {cpu_pct}%")
                asyncio.create_task(tg_send(tg_alert(
                    "🔥", "Host CPU Critical",
                    f"Host CPU: <b>{cpu_pct}%</b> (threshold: {cpu_crit}%)\nActive streams: {sum(s.active_streams for s in state.subscriptions)}")))
            # CPU warning
            elif cpu_pct >= cpu_warn and prev_cpu < cpu_warn:
                state.add_alert("warning", f"Host CPU high: {cpu_pct}%")
                asyncio.create_task(tg_send(tg_alert(
                    "⚠️", "Host CPU High",
                    f"Host CPU: <b>{cpu_pct}%</b> (threshold: {cpu_warn}%)\nActive streams: {sum(s.active_streams for s in state.subscriptions)}")))
            # CPU recovered
            elif cpu_pct < cpu_warn and prev_cpu >= cpu_warn:
                state.add_alert("info", f"Host CPU recovered: {cpu_pct}%")
                asyncio.create_task(tg_send(tg_alert(
                    "✅", "Host CPU Recovered",
                    f"Host CPU back to normal: <b>{cpu_pct}%</b>")))

            # Memory warning
            if mem_pct >= mem_warn and prev_mem < mem_warn:
                state.add_alert("warning", f"Memory high: {mem_mb} MB ({mem_pct}%)")
                asyncio.create_task(tg_send(tg_alert(
                    "⚠️", "Memory High",
                    f"Memory: <b>{mem_mb} MB ({mem_pct}%)</b> (threshold: {mem_warn}%)")))
            elif mem_pct < mem_warn and prev_mem >= mem_warn:
                asyncio.create_task(tg_send(tg_alert(
                    "✅", "Memory Recovered",
                    f"Memory back to normal: <b>{mem_mb} MB ({mem_pct}%)</b>")))

            # Save previous values for edge detection
            state.health["_prev_cpu"] = cpu_pct
            state.health["_prev_mem"] = mem_pct

            # Push fresh health snapshot to SSE subscribers
            if bus.subscriber_count > 0:
                bus.publish("health", _health_dict())
        except Exception as exc:
            log.error("Health monitor error: %s", exc)

# ── Helpers ───────────────────────────────────────────────────────────────────
def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except: return "127.0.0.1"

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _LAST_ADMIN_PW_HASH
    cfg = load_config(); srv = cfg.get("server", {}); port = srv.get("port", 5004)
    state.device_id = srv.get("device_id") or uuid.uuid4().hex[:8].upper()
    state.base_url  = (srv.get("base_url") or f"http://{_local_ip()}:{port}").rstrip("/")
    # Phase 15c: seed admin password hash tracker for session invalidation
    _LAST_ADMIN_PW_HASH = (cfg.get("auth") or {}).get("password_hash", "")
    state.reload_from_config()
    # Phase 10: load Xtream Codes user accounts from /config/users.json (separate
    # from config.json so dashboard config edits don't churn the user list).
    state.users = _load_users()
    if state.users and _migrate_plaintext_passwords(state.users):
        _save_users(state.users)
        log.info("Migrated plaintext passwords to bcrypt in %s", USERS_FILE)
    log.info("Device ID  : %s", state.device_id)
    log.info("Base URL   : %s", state.base_url)
    log.info("Tuners     : %d", state.total_tuners)
    log.info("Groups     : %d active filters", len(state.allowed_groups))
    log.info("Xtream     : %d user(s) loaded from %s", len(state.users), USERS_FILE)
    # Pre-load raw groups caches from disk so /api/groups has full provider
    # group lists even when channels are served from the channel cache.
    for sub in state.subscriptions:
        cached_raw = _load_raw_groups_cache(sub.name)
        if cached_raw:
            state.raw_groups_by_sub[sub.name] = cached_raw
    await state.refresh_channels()
    # Phase 8: after the first refresh (which resets _DEAD_CHANNELS) restore
    # the previous health history and dead-channel map from disk so the
    # rolling score, probe history and pruned set survive a restart.
    _load_sub_health_snapshot()
    # Step 4: restore persisted IP blocklist
    _load_blocklist()

    # Phase 9: startup self-test — probe every enabled subscription concurrently
    # so /api/subscriptions/health has real data immediately, and silent
    # credential rot pages us inside seconds instead of waiting for the first
    # health_monitor cycle.
    async def _startup_probe_one(sub):
        try:
            result = await _probe_subscription(sub.name, sub.url, sub.username, sub.password)
        except Exception as exc:
            log.warning("Startup probe: %s exception — %s", sub.name, _redact(exc))
            return (sub, False, f"exception: {_redact(exc)}")
        # Seed _SUB_HEALTH immediately — use the fine-grained TCP step so a
        # slow m3u fetch doesn't wrongly mark the server as down.
        _get_sub_health(sub.name).record_tcp(
            ok=bool(result.get("tcp")),
            ms=int(result.get("latency_ms", 0)))
        if result.get("ok"):
            log.info("Startup probe: %s ok (%dms, player_api ok, m3u ok)",
                     sub.name, result.get("latency_ms", 0))
            state.log_event("health", "STARTUP PROBE",
                            f"{sub.name} ok ({result.get('latency_ms', 0)} ms, "
                            f"{result.get('channels_head', 0)}+ channels)",
                            sub=sub.name)
            return (sub, True, "")
        err = result.get("error", "unknown")
        log.warning("Startup probe: %s FAIL — %s", sub.name, err)
        state.log_event("error", "STARTUP PROBE FAIL",
                        f"{sub.name}: {err}", sub=sub.name)
        return (sub, False, err)

    enabled_subs = [s for s in state.subscriptions if s.enabled]
    if enabled_subs:
        results = await asyncio.gather(
            *[_startup_probe_one(s) for s in enabled_subs],
            return_exceptions=False)
        failures = [(s.name, err) for s, ok, err in results if not ok]
        if failures:
            detail = "\n".join(f"<b>{html.escape(n)}</b>: {html.escape(e)}"
                               for n, e in failures)
            asyncio.create_task(tg_send(tg_alert(
                "⚠️", "Startup Probe Failed",
                f"{len(failures)} provider(s) failed:\n{detail}")))
    async def _refresh_loop():
        while True:
            await asyncio.sleep(6 * 3600)
            try:
                await state.refresh_channels()
                await state.fetch_account_info()
            except Exception as e: log.error("Auto-refresh failed: %s", e)
    async def _daily_summary():
        """Send a daily summary to Telegram at configured hour."""
        while True:
            await asyncio.sleep(60)
            now = datetime.now()
            cfg_tg = load_config().get("telegram", {})
            target_hour = cfg_tg.get("daily_summary_hour", 8)
            if now.hour == target_hour and now.minute == 0:
                uptime = int(time.time() - state.started_at)
                h = uptime // 3600; m = (uptime % 3600) // 60
                sep = "─" * 24
                sys_info = (state.health or {}).get("system", {})
                hostname = load_config().get("hostname", "iptv-tuner")
                msg = (
                    f"📊 <b>Daily Summary</b>\n"
                    f"{sep}\n"
                    f"🟢  Uptime          {h}h {m}m\n"
                    f"📺  Channels        {len(state.channels):,}\n"
                    f"{sep}\n"
                    f"▶️  Streams today   {state.daily_stats['streams_started']}\n"
                    f"👥  Viewer sessions {state.daily_stats['total_viewers']}\n"
                    f"📈  Peak viewers    {state.daily_stats['peak_viewers']}\n"
                    f"🔔  Alerts          {len(state.alerts)}\n"
                    f"{sep}\n"
                    f"💻  CPU  {sys_info.get('cpu_pct', '?')}%  ·  🧠  RAM  {sys_info.get('mem_mb', '?')} MB\n"
                    f"{sep}\n"
                    f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}  ·  <i>{hostname}</i>"
                )
                await tg_send(msg)
                # Phase 10: warn about Xtream users about to expire so the
                # admin has a chance to renew before clients break.
                try:
                    if _xtream_cfg().get("user_alerts_enabled", True):
                        soon_cutoff = time.time() + (3 * 86400)  # 3 days
                        soon = [u for u in state.users
                                if u.expires_at and 0 < (u.expires_at - time.time()) < (3 * 86400)
                                and not u.is_expired()]
                        if soon:
                            lines = []
                            for u in soon:
                                days = max(0, int((u.expires_at - time.time()) // 86400))
                                lines.append(f"• <b>{html.escape(u.username)}</b> — {days}d")
                            await tg_send(tg_alert(
                                "⏳", f"{len(soon)} Xtream user(s) expiring soon",
                                "\n".join(lines)))
                except Exception as exc:
                    log.debug("expiring-soon check failed: %s", exc)
                # Reset daily stats
                state.daily_stats = {"streams_started": 0, "total_viewers": 0, "peak_viewers": 0}
                await asyncio.sleep(61)  # avoid double-send

    async def _status_broadcaster():
        """Push a status snapshot every 2s while at least one SSE client is connected."""
        while True:
            await asyncio.sleep(2)
            if bus.subscriber_count == 0: continue
            try: bus.publish("status", _status_dict())
            except Exception as e: log.warning("status broadcaster: %s", e)

    async def _malloc_trim_loop():
        """Return fragmented heap pages to the OS every 6 hours.
        Skips the trim if any stream is active — no viewer impact."""
        import ctypes
        try:
            libc = ctypes.cdll.LoadLibrary("libc.so.6")
        except OSError:
            log.warning("malloc_trim: libc not found, heap-trim disabled")
            return
        await asyncio.sleep(3600 * 6)   # first trim after 6h
        while True:
            try:
                active = sum(1 for m in state.mux.values() if m._running)
                if active == 0:
                    before = _mem_mb()
                    libc.malloc_trim(0)
                    after = _mem_mb()
                    log.info("malloc_trim: %d MB → %d MB (freed %d MB)", before, after, before - after)
                else:
                    log.debug("malloc_trim: skipped (%d active stream(s))", active)
            except Exception as exc:
                log.warning("malloc_trim error: %s", exc)
            await asyncio.sleep(3600 * 6)

    def _mem_mb():
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) // 1024
        except Exception:
            pass
        return 0

    t1 = asyncio.create_task(_refresh_loop())
    t2 = asyncio.create_task(health_monitor())
    t3 = asyncio.create_task(_daily_summary())
    t4 = asyncio.create_task(_session_gc_loop())  # A34
    t5 = asyncio.create_task(_status_broadcaster())
    t6 = asyncio.create_task(_malloc_trim_loop())
    global _tg_task
    _tg_task = asyncio.create_task(_tg_command_loop())

    # Fetch account info on startup
    await state.fetch_account_info()

    # Send startup notification
    asyncio.create_task(tg_send(tg_alert("🚀", "IPTV Tuner Started",
        f"📺  Channels   {len(state.channels):,}\n"
        f"📡  Providers  {len(state.subscriptions)}\n"
        f"🔍  Filters    {len(state.allowed_groups)} groups\n"
        f"👤  Users      {len(state.users)}")))

    yield
    # Phase 9: graceful drain — reject new streams, wait for active muxes to
    # finish naturally, then force-kill any stragglers. 10-second ceiling so
    # redeploys stay snappy but in-flight viewers don't get yanked mid-stream.
    state.shutting_down = True
    drain_seconds = _health_cfg()["shutdown_drain_seconds"]
    drain_deadline = time.time() + drain_seconds
    active = sum(1 for m in state.mux.values() if m._running)
    if active:
        log.info("Shutdown: draining %d active stream(s) (up to %ds)", active, drain_seconds)
        while time.time() < drain_deadline:
            if not any(m._running for m in state.mux.values()): break
            await asyncio.sleep(0.5)
    # Force-kill anything still running after the grace window.
    for key in list(state.mux.keys()):
        mux = state.mux.get(key)
        if mux and mux._running:
            try: await mux.kill_all()
            except Exception as exc:
                log.warning("Shutdown: kill %s failed: %s", key, exc)
    t1.cancel(); t2.cancel(); t3.cancel(); t4.cancel(); t5.cancel(); t6.cancel()
    if _tg_task and not _tg_task.done(): _tg_task.cancel()
    asyncio.create_task(tg_send(tg_alert("🔴", "IPTV Tuner Stopped", "Container is shutting down")))
    # Phase 11 Item 5: close both HTTP clients on teardown
    if state._admin_http: await state._admin_http.aclose()
    if state._stream_http: await state._stream_http.aclose()

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="IPTV Tuner", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[], allow_methods=["*"], allow_headers=["*"])

class XtreamLegacyURLMiddleware(BaseHTTPMiddleware):
    """Rewrite the legacy Xtream short URL `/{user}/{password}/{stream_id}[.ts|.m3u8]`
    to the canonical `/live/{user}/{password}/{stream_id}.ts` form so the existing
    `_xtream_live_handler` can serve it.

    9xtream (and a few older clients) build live URLs with this 3-segment form
    instead of the `/live/...` prefix. The canonical form is what the Xtream
    Codes API spec advertises in `server_info`, but real-world clients use both.

    To avoid clobbering unrelated 3-segment paths (e.g. `/api/users/123`) the
    rewrite is gated on the first segment matching a real Xtream username —
    URL-decoded, case-insensitive — that exists in `state.users` right now.
    Runs BEFORE StreamPortFilterMiddleware so the rewritten `/live/...` path
    matches the stream-port allow-list."""
    PATTERN = re.compile(r"^/([^/]+)/([^/]+)/(\d+)(?:\.(ts|m3u8))?$")
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        m = self.PATTERN.match(path)
        if m:
            u_raw = m.group(1)
            try:
                u_decoded = unquote(u_raw)
            except Exception:
                u_decoded = u_raw
            if any(usr.username.lower() == u_decoded.lower() for usr in state.users):
                p_raw = m.group(2)
                sid   = m.group(3)
                ext   = m.group(4) or "ts"
                new_path = f"/live/{u_raw}/{p_raw}/{sid}.{ext}"
                request.scope["path"]      = new_path
                request.scope["raw_path"]  = new_path.encode()
        try:
            return await call_next(request)
        except RuntimeError as exc:
            if "No response" in str(exc) or "disconnect" in str(exc).lower():
                return Response(status_code=499)
            raise

class StreamPortFilterMiddleware(BaseHTTPMiddleware):
    """Phase 11.5: when the request hits the public stream listener
    (`server.stream_port`, default 34343), only Xtream-credentialed endpoints
    plus `/logo/` are served. Everything else (admin dashboard, `/api/*`,
    HDHomeRun discovery, the unauthenticated `/stream/{key}`, `/epg`) is 404'd
    so a browser landing on the public hostname sees nothing to enumerate.
    Plex stays on the LAN admin port for HDHR discovery. Decision uses
    `scope["server"][1]` (real socket port from the bound listener) — NOT the
    Host header, so it cannot be spoofed.

    Phase 13 hardening (2026-04-09): trimmed `/discover.json`, `/lineup.json`,
    `/lineup_status.json`, `/device.xml`, `/stream/`, `/epg` from the public
    allow-list — they leaked the channel inventory unauthenticated. They are
    still served on the LAN admin port (5004) where Plex can reach them."""
    STREAM_ALLOWED = (
        "/player_api.php", "/xmltv.php", "/get.php", "/live/", "/logo/",
    )
    async def dispatch(self, request: Request, call_next):
        srv = request.scope.get("server") or (None, None)
        socket_port = srv[1] if isinstance(srv, (tuple, list)) and len(srv) > 1 else None
        stream_port = (load_config().get("server") or {}).get("stream_port") or 0
        if stream_port and socket_port == int(stream_port):
            path = request.url.path
            if not any(path == p or path.startswith(p) for p in self.STREAM_ALLOWED):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Not Found"}, status_code=404)
        try:
            return await call_next(request)
        except RuntimeError as exc:
            if "No response" in str(exc) or "disconnect" in str(exc).lower():
                return Response(status_code=499)
            raise

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject browser security headers on admin-port responses. Skipped on
    the stream port — VLC/ffplay/Kodi can choke on unexpected headers."""
    _HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "()",
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'"
        ),
    }
    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
        except RuntimeError as exc:
            if "No response" in str(exc) or "disconnect" in str(exc).lower():
                return Response(status_code=499)
            raise
        srv = request.scope.get("server") or (None, None)
        socket_port = srv[1] if isinstance(srv, (tuple, list)) and len(srv) > 1 else None
        stream_port = (load_config().get("server") or {}).get("stream_port") or 0
        if not (stream_port and socket_port == int(stream_port)):
            for k, v in self._HEADERS.items():
                response.headers[k] = v
        return response

class AuthMiddleware(BaseHTTPMiddleware):
    # Phase 10: /player_api.php, /xmltv.php, /get.php, /live/ are Xtream Codes
    # endpoints that authenticate themselves via xtream_auth (Depends) and must
    # not be gated by the dashboard cookie session.
    OPEN = ("/discover.json", "/device.xml", "/lineup", "/stream/",
            "/logo/", "/epg", "/login", "/logout", "/reload", "/health",
            "/app/manifest.json", "/app/sw.js", "/app/icon-", "/app/icon.svg",
            "/player_api.php", "/xmltv.php", "/get.php", "/live/")
    async def dispatch(self, request: Request, call_next):
        # Step 4: reject blocked IPs before anything else
        ip = _client_ip(request)
        if _is_blocked(ip):
            return Response("Forbidden", status_code=403)
        path = request.url.path
        if any(path.startswith(p) or path == p for p in self.OPEN):
            try:
                return await call_next(request)
            except RuntimeError as exc:
                if "No response" in str(exc) or "disconnect" in str(exc).lower():
                    return Response(status_code=499)
                raise
        if path.startswith("/api/") or path == "/":
            if not _check_session(request):
                if path.startswith("/api/"):
                    from fastapi.responses import JSONResponse
                    return JSONResponse({"detail": "Unauthorized"}, status_code=401)
                return Response(status_code=302, headers={"Location": "/login"})
        try:
            return await call_next(request)
        except RuntimeError as exc:
            if "No response" in str(exc) or "disconnect" in str(exc).lower():
                return Response(status_code=499)
            raise

app.add_middleware(AuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(StreamPortFilterMiddleware)
app.add_middleware(XtreamLegacyURLMiddleware)

# ── HDHomeRun endpoints ───────────────────────────────────────────────────────
@app.get("/discover.json")
async def discover():
    return {"FriendlyName": "IPTV HDHomeRun Tuner", "Manufacturer": "Silicondust",
            "ModelNumber": "HDTC-900", "FirmwareName": "bin_hdhomerun_hd_tc",
            "TunerCount": state.total_tuners, "FirmwareVersion": "20150826",
            "DeviceID": state.device_id, "DeviceAuth": "",
            "BaseURL": state.base_url, "LineupURL": f"{state.base_url}/lineup.json", "GuideURL": f"{state.base_url}/epg.xml"}

@app.get("/device.xml")
async def device_xml():
    x = f'''<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <URLBase>{state.base_url}</URLBase>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>IPTV HDHomeRun Tuner</friendlyName>
    <manufacturer>Silicondust</manufacturer>
    <modelName>HDTC-900</modelName>
    <serialNumber>{state.device_id}</serialNumber>
    <UDN>uuid:{state.device_id}</UDN>
  </device>
</root>'''
    return Response(content=x, media_type="application/xml")

@app.get("/lineup_status.json")
async def lineup_status():
    return {"ScanInProgress": 0, "ScanPossible": 1, "Source": "Cable", "SourceList": ["Cable"]}

@app.get("/lineup.json")
async def lineup():
    return [{"GuideName": ch.name, "GuideNumber": ch.number,
             "URL": f"{state.base_url}/stream/{k}",
             "HD": 1 if ch.quality in ("HD","FHD","4K") else 0,
             "Favorite": 0,
             "ImageURL": _logo_url(state.base_url, k, ch.logo),
             "Tags": ch.quality,
             "VariantCount": len(ch.variants) if ch.variants else 1,
             "Variants": [v.quality for v in (ch.variants or [])],
             "VariantSubs": [v.sub_name for v in (ch.variants or [])]}
            for k, ch in _live_channels().items()]

# ── Phase 11: Stream Dispatcher ──────────────────────────────────────────────
# Picks the best ChannelCandidate per (user, channel) using a composite score
# of SubHealth, current load, configured priority, dispatch_weight, and recent
# failure penalty. Caches the decision within `dispatch.sticky_seconds` to
# avoid score-thrashing under rapid client churn.
def _dispatch_cfg() -> dict:
    raw = load_config().get("dispatch")
    raw = raw if isinstance(raw, dict) else {}
    return {
        "enabled":                       bool(raw.get("enabled", True)),
        "sticky_seconds":                int(raw.get("sticky_seconds", 30)),
        "max_failovers_per_session":     int(raw.get("max_failovers_per_session", 3)),
        "score_recent_fail_penalty_60s":  int(raw.get("score_recent_fail_penalty_60s", 50)),
        "score_recent_fail_penalty_300s": int(raw.get("score_recent_fail_penalty_300s", 20)),
        "null_packet_fill_during_swap":   bool(raw.get("null_packet_fill_during_swap", True)),
        "failover_midstream":             bool(raw.get("failover_midstream", True)),
    }

@dataclass
class Decision:
    """The dispatcher's per-request answer: which candidates, in what order,
    why, and how many mid-stream failovers we've burned. Cached briefly per
    (user, channel_key) and consumed by ChannelMux._fetch."""
    channel_key:    str
    user:           Optional[str]                  # None for HDHR / Plex path
    ordered:        List[ChannelCandidate]         # best → worst
    picked_at:      float
    reason:         str                            # "score:87 (IGate:87 vs ...)"
    failover_count: int = 0

class StreamDispatcher:
    """Decides which upstream candidate is used for which (user, channel),
    keeps a sticky cache so rapid re-tunes don't ping-pong between two
    near-tied candidates, and tracks failures so the next pick avoids them.

    Pure logic — does not open any HTTP connections itself. ChannelMux owns
    the actual streaming and calls back via `mark_failure` when a candidate
    fails so the cached decision can be invalidated or re-ordered.
    """
    def __init__(self):
        self._cache: Dict[str, Decision] = {}     # key: f"{user or '_'}:{channel_key}"
        self._lock  = asyncio.Lock()

    @staticmethod
    def _ck(channel_key: str, username: Optional[str]) -> str:
        return f"{username or '_'}:{channel_key}"

    def _score_candidate(self, cand: ChannelCandidate, sub: "Subscription",
                         dispatch_weight: float = 1.0) -> Tuple[float, str]:
        """Composite score 0–~120ish. Returns (score, breakdown_str).
        Components (per spec):
          - SubHealth.score                      0-100
          - Current load: (max-active)/max × 20   0-20
          - Priority inverse: (10-priority)/10×10 0-10
          - dispatch_weight multiplier            ×N
          - Recent fail penalty: -50 (60s), -20 (300s)
        """
        health = _get_sub_health(sub.name).score(sub) if sub else 0
        load_pts = 0.0
        if sub.max_streams:
            load_pts = max(0.0, (sub.max_streams - sub.active_streams) / sub.max_streams) * 20
        prio_pts = max(0.0, (10 - sub.priority)) / 10 * 10
        base = health + load_pts + prio_pts
        base *= max(0.1, float(dispatch_weight))
        cfg = _dispatch_cfg()
        now = time.time()
        if cand.last_fail_ts and (now - cand.last_fail_ts) < 60:
            base -= cfg["score_recent_fail_penalty_60s"]
        elif cand.last_fail_ts and (now - cand.last_fail_ts) < 300:
            base -= cfg["score_recent_fail_penalty_300s"]
        return base, f"{sub.name}:{int(round(base))}"

    def pick(self, channel: "Channel",
             user: Optional["User"] = None) -> Optional[Decision]:
        """Return an ordered Decision. Reuses cached decision if still warm.
        Returns None if no candidates remain after user filtering / sub
        availability. Caller is responsible for raising the appropriate
        HTTPException."""
        if not channel.candidates:
            return None
        cfg_d = _dispatch_cfg()
        sticky = max(0, cfg_d["sticky_seconds"])
        username = user.username if user else None
        ck = self._ck(channel.id or channel.name, username)
        cached = self._cache.get(ck)
        if (cached and sticky > 0
                and (time.time() - cached.picked_at) < sticky
                and cached.ordered):
            return cached

        # Per-sub config (dispatch_weight, reserved_for_users)
        sub_cfg_by_name: Dict[str, dict] = {}
        for sd in load_config().get("subscriptions", []):
            sub_cfg_by_name[sd.get("name", "")] = sd

        # User restriction set (Phase 11 + Phase 10 user model extension)
        allowed_subs: Optional[Set[str]] = None
        if user is not None:
            raw_allowed = getattr(user, "allowed_subscriptions", None) or []
            if raw_allowed:
                allowed_subs = {s for s in raw_allowed}

        scored: List[Tuple[float, str, ChannelCandidate, "Subscription"]] = []
        for cand in channel.candidates:
            sub = state.get_subscription(cand.sub_name)
            if sub is None or not sub.enabled or not sub.available:
                continue
            # Phase 12: skip subs that the admin has drained from dispatch.
            # Active streams using this sub keep running; only new picks are
            # blocked. The set is ephemeral so a process restart releases it.
            if cand.sub_name in _DRAINED_SUBS:
                continue
            sd = sub_cfg_by_name.get(sub.name, {}) or {}
            reserved = sd.get("reserved_for_users") or []
            if reserved and (username is None or username not in reserved):
                continue
            if allowed_subs is not None and sub.name not in allowed_subs:
                continue
            score, breakdown = self._score_candidate(
                cand, sub, dispatch_weight=float(sd.get("dispatch_weight", 1.0)))
            scored.append((score, breakdown, cand, sub))
        if not scored:
            return None
        scored.sort(key=lambda x: x[0], reverse=True)
        ordered     = [t[2] for t in scored]
        breakdown   = " vs ".join(t[1] for t in scored)
        top_score   = scored[0][0]
        reason      = f"score:{int(round(top_score))} ({breakdown})"
        decision = Decision(
            channel_key=channel.id or channel.name,
            user=username,
            ordered=ordered,
            picked_at=time.time(),
            reason=reason,
        )
        self._cache[ck] = decision
        return decision

    def mark_failure(self, decision: Decision, cand_idx: int, reason: str) -> None:
        """ChannelMux callback when a candidate fails (pre- or mid-stream).
        Bumps the candidate's sliding failure counter so future picks score
        it lower, and invalidates the cached decision when every candidate
        is exhausted so the next viewer gets a fresh pick from refresh data.
        """
        if 0 <= cand_idx < len(decision.ordered):
            cand = decision.ordered[cand_idx]
            cand.last_fail_ts = time.time()
            cand.fail_count  += 1
        # Track per-sub failover stats for the observability endpoints
        try:
            sub_name = decision.ordered[cand_idx].sub_name if 0 <= cand_idx < len(decision.ordered) else ""
            _DISPATCH_STATS.setdefault(sub_name, {"streams": 0, "failovers_from": 0,
                                                  "failovers_to": 0, "rejections": 0})
            _DISPATCH_STATS[sub_name]["failovers_from"] += 1
            if cand_idx + 1 < len(decision.ordered):
                next_sub = decision.ordered[cand_idx + 1].sub_name
                _DISPATCH_STATS.setdefault(next_sub, {"streams": 0, "failovers_from": 0,
                                                      "failovers_to": 0, "rejections": 0})
                _DISPATCH_STATS[next_sub]["failovers_to"] += 1
        except Exception:
            pass
        if cand_idx >= len(decision.ordered) - 1:
            # All candidates exhausted — drop the cached decision so the next
            # pick recomputes against potentially recovered subs.
            ck = self._ck(decision.channel_key, decision.user)
            self._cache.pop(ck, None)
        log.debug("Dispatch failure %s idx=%d reason=%s",
                  decision.channel_key, cand_idx, reason)

    def invalidate(self, channel_key: str, user: Optional[str] = None) -> int:
        """Drop cached decision(s). If `user` is None, wipe all decisions for
        the channel across every user (called on refresh / config change /
        manual admin action). Returns the number of cache entries dropped."""
        if user is not None:
            return 1 if self._cache.pop(self._ck(channel_key, user), None) else 0
        n = 0
        for k in list(self._cache.keys()):
            if k.endswith(":" + channel_key):
                self._cache.pop(k, None); n += 1
        return n

    def invalidate_all(self) -> int:
        """Wipe the entire cache (called from refresh_channels)."""
        n = len(self._cache)
        self._cache.clear()
        return n

    def active_decisions(self) -> List[Decision]:
        """Snapshot of currently-cached decisions for /api/dispatch/decisions."""
        return list(self._cache.values())

    def cache_size(self) -> int:
        return len(self._cache)

# Module-level singleton + per-sub stats keyed by sub.name (24h rolling — we
# rely on caller to reset if they want a strict window; for now this is a
# lifetime counter since process start, which matches the spec's "since
# startup" intent for /api/dispatch/stats).
dispatcher = StreamDispatcher()
_DISPATCH_STATS: Dict[str, Dict[str, int]] = {}
_DISPATCH_NO_CANDIDATE_EVENTS: int = 0
_DISPATCH_FAILOVER_LIMIT_HITS: int = 0

# Phase 12: drained subscriptions — ephemeral, runtime-only set. When a sub is
# in this set, the StreamDispatcher refuses to route NEW streams through it,
# but active streams continue to flow until they end naturally. Cleared on
# process restart by design (admin must re-drain after a redeploy).
_DRAINED_SUBS: Set[str] = set()

def _is_sub_drained(name: str) -> bool:
    return name in _DRAINED_SUBS

# ── Stream proxy ──────────────────────────────────────────────────────────────
# ── Failover Logic ───────────────────────────────────────────────────────────
async def _acquire_subscription_with_failover(channel_key: str, ch) -> Optional["Subscription"]:
    """Legacy fallback path used when the dispatcher returns no Decision
    (e.g., the channel was loaded by old code without populating
    `candidates`). Picks the highest-priority sub by load."""
    sub = state.pick_subscription()
    if sub is None:
        log.warning("No subscriptions available for %s", ch.name)
        return None
    sub.fail_count = 0
    return sub

# Phase 10/11: shared mux acquisition. Both /stream/{key} (HDHR) and
# /live/{u}/{p}/{id}.{ext} (Xtream) call this so they hit the exact same
# pool. Phase 11: now invokes the StreamDispatcher to pick a Decision and
# constructs the ChannelMux candidate-aware. Pre-Phase-11 channels (no
# candidates list — shouldn't happen after refresh, but defensive) fall
# back to the legacy single-sub path.
async def _get_or_start_mux(channel_key: str, ch: "Channel",
                            user: Optional["User"] = None) -> "ChannelMux":
    mux = state.mux.get(channel_key)
    if mux is None or not mux._running:
        decision: Optional[Decision] = None
        if ch.candidates:
            decision = dispatcher.pick(ch, user)
        if decision is None:
            # Fallback path — no dispatcher decision possible (no candidates
            # or all filtered out). Try the legacy picker so the request
            # still has a fighting chance.
            sub = await _acquire_subscription_with_failover(channel_key, ch)
            if sub is None:
                raise HTTPException(503, "All subscription streams in use — try again later")
        else:
            primary_cand = decision.ordered[0]
            sub = state.get_subscription(primary_cand.sub_name)
            if sub is None:
                raise HTTPException(503, "Dispatcher picked an unknown subscription")
        mux = ChannelMux(channel_key, ch, sub, decision=decision)
        state.mux[channel_key] = mux
        await mux.start()
    else:
        if mux.viewer_count >= ChannelMux.MAX_VIEWERS:
            raise HTTPException(503, "Channel at max viewers")
    return mux


@app.get("/stream/{channel_key}")
async def stream(channel_key: str, request: Request):
    # Phase 9: refuse new streams while we're draining. Plex honours Retry-After
    # and will re-tune in a few seconds, making redeploys invisible to viewers.
    if state.shutting_down:
        return Response(content="Tuner shutting down, retry shortly",
                        status_code=503,
                        headers={"Retry-After": "5"})
    ch = state.channels.get(channel_key)
    if not ch: raise HTTPException(404, "Channel not found")
    viewer_ip = request.client.host if request.client else "unknown"
    mux = await _get_or_start_mux(channel_key, ch)

    queue = await mux.subscribe(viewer_ip)
    # Fire-and-forget Telegram notification: who opened what on which provider,
    # and did the upstream actually deliver bytes.
    asyncio.create_task(_notify_stream_start(
        channel_key, ch.name, ch.quality, mux.sub.name, viewer_ip))
    async def viewer_stream():
        try:
            while True:
                try: chunk = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError: break
                if chunk is None: break
                yield chunk
                if await request.is_disconnected(): break
        except asyncio.CancelledError: pass
        except Exception as exc: log.warning("Viewer error [%s]: %s", ch.name, exc)
        finally: await mux.unsubscribe(queue, viewer_ip)
    return StreamingResponse(viewer_stream(), media_type="video/MP2T",
                             headers={"Cache-Control": "no-cache", "X-Channel": ch.name})

# ── Logo disk cache (Phase 6) ────────────────────────────────────────────────
# Logos live on the persistent /config volume so they survive restarts and
# are served without hitting upstream on every Plex / IPTV client request.
LOGO_DIR = os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)) or ".", "logos")
LOGO_TTL = 30 * 86400  # refresh after ~30 days so upstream logo changes propagate monthly

_LOGO_EXT_BY_CTYPE = {
    "image/png":     "png",
    "image/jpeg":    "jpg",
    "image/jpg":     "jpg",
    "image/gif":     "gif",
    "image/webp":    "webp",
    "image/svg+xml": "svg",
}
_CTYPE_BY_EXT = {
    "png": "image/png",  "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif",  "webp": "image/webp", "svg": "image/svg+xml",
}

def _logo_safe_key(key: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]', '_', key)

def _logo_url(base: str, key: str, ch_logo: str) -> str:
    """Return full logo URL with ?v=mtime so clients re-fetch after a custom upload.
    Falls back to the AQ default icon for channels with no provider logo and no custom upload."""
    logo_path, logo_mtime = _find_cached_logo(key)
    if not (ch_logo or logo_path):
        # No provider logo and no custom upload — use AQ default icon
        return f"{base}/app/icon-192.png"
    v = int(logo_mtime) if logo_mtime else 0
    return f"{base}/logo/{key}?v={v}" if v else f"{base}/logo/{key}"

def _find_cached_logo(key: str):
    """Return (path, mtime) of the first on-disk logo for this channel key,
    or (None, 0.0) if nothing is cached.
    Custom-uploaded logos (custom_{safe}.*) are checked before auto-downloaded ones."""
    safe = _logo_safe_key(key)
    custom_prefix = f"custom_{safe}."
    auto_prefix = f"{safe}."
    # Two-pass: prefer custom uploads over auto-fetched logos
    try:
        entries = os.listdir(LOGO_DIR)
    except (FileNotFoundError, OSError):
        return None, 0.0
    for prefix in (custom_prefix, auto_prefix):
        for entry in entries:
            if entry.startswith(prefix):
                p = os.path.join(LOGO_DIR, entry)
                try:
                    return p, os.path.getmtime(p)
                except OSError:
                    return None, 0.0
    return None, 0.0

async def _download_logo(ch, key: str):
    """Fetch ch.logo, validate it's an image, store atomically under LOGO_DIR.
    Returns the final path on success, or None on any failure."""
    if not ch.logo or not _is_safe_public_url(ch.logo):
        return None
    try:
        r = await state.admin_http.get(ch.logo, timeout=10)
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            return None
        ext = _LOGO_EXT_BY_CTYPE.get(ctype, "png")
        os.makedirs(LOGO_DIR, exist_ok=True)
        safe = _logo_safe_key(key)
        # Remove any stale variants with a different extension so only one file per key.
        for e in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
            if e == ext: continue
            old = os.path.join(LOGO_DIR, f"{safe}.{e}")
            if os.path.exists(old):
                try: os.remove(old)
                except OSError: pass
        path = os.path.join(LOGO_DIR, f"{safe}.{ext}")
        fd, tmp = tempfile.mkstemp(prefix=".logo.", suffix=f".{ext}", dir=LOGO_DIR)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(r.content)
            os.replace(tmp, path)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise
        return path
    except Exception as exc:
        log.debug("logo download failed for %s: %s", key, _redact(exc))
        return None

def _serve_logo_file(path: str):
    ext   = path.rsplit(".", 1)[-1].lower()
    ctype = _CTYPE_BY_EXT.get(ext, "image/png")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        raise HTTPException(502)
    return Response(content=data, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400"})

# ── Logo endpoint (disk-cached, upstream fallback) ──────────────────────────
@app.get("/logo/{channel_key}")
async def logo(channel_key: str):
    ch = state.channels.get(channel_key)
    if not ch:
        raise HTTPException(404)

    # Check for custom-uploaded logo first — works even when ch.logo is empty
    path, mtime = _find_cached_logo(channel_key)
    if path and path.split("/")[-1].startswith("custom_"):
        return _serve_logo_file(path)

    if not ch.logo:
        raise HTTPException(404)
    # A5: tvg-logo is attacker-controllable via M3U — block SSRF to internal hosts
    if not _is_safe_public_url(ch.logo):
        raise HTTPException(400, "Unsafe logo URL")

    # Fresh cache hit → serve immediately
    if path and (time.time() - mtime) < LOGO_TTL:
        return _serve_logo_file(path)
    # Cache miss or stale → download a fresh copy
    new_path = await _download_logo(ch, channel_key)
    if new_path:
        return _serve_logo_file(new_path)
    # Last resort: serve stale cache if upstream is currently unreachable
    if path:
        return _serve_logo_file(path)
    raise HTTPException(502)

_LOGO_UPLOAD_ALLOWED_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}
_LOGO_UPLOAD_MAX_BYTES = 2 * 1024 * 1024  # 2 MB

@app.post("/api/channels/{key}/logo")
async def upload_channel_logo(key: str, file: UploadFile = File(...)):
    """Upload a custom logo for a channel. Saved as custom_{safe_key}.{ext} in LOGO_DIR.
    Custom logos take priority over auto-downloaded ones in _find_cached_logo."""
    if key not in state.channels:
        raise HTTPException(404, "Channel not found")
    ctype = (file.content_type or "").split(";")[0].strip().lower()
    if ctype not in _LOGO_UPLOAD_ALLOWED_TYPES:
        raise HTTPException(400, f"Unsupported image type: {ctype!r}")
    data = await file.read(_LOGO_UPLOAD_MAX_BYTES + 1)
    if len(data) > _LOGO_UPLOAD_MAX_BYTES:
        raise HTTPException(413, "Logo file too large (max 2 MB)")
    ext = _LOGO_EXT_BY_CTYPE.get(ctype, "png")
    safe = _logo_safe_key(key)
    os.makedirs(LOGO_DIR, exist_ok=True)
    # Remove any previous custom logo for this key (different extension)
    for e in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
        old = os.path.join(LOGO_DIR, f"custom_{safe}.{e}")
        if os.path.exists(old):
            try: os.remove(old)
            except OSError: pass
    dest = os.path.join(LOGO_DIR, f"custom_{safe}.{ext}")
    tmp = dest + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)
    state.log_event("channel", "LOGO UPLOADED", f"{state.channels[key].name} → custom_{safe}.{ext}")
    return {"status": "ok", "key": key, "file": f"custom_{safe}.{ext}"}

@app.delete("/api/channels/{key}/logo")
async def delete_channel_logo(key: str):
    """Remove a custom-uploaded logo, falling back to auto-downloaded logo."""
    safe = _logo_safe_key(key)
    removed = False
    for e in ("png", "jpg", "jpeg", "gif", "webp", "svg"):
        p = os.path.join(LOGO_DIR, f"custom_{safe}.{e}")
        if os.path.exists(p):
            try: os.remove(p); removed = True
            except OSError: pass
    if not removed:
        raise HTTPException(404, "No custom logo found")
    return {"status": "ok", "key": key}

# ── Phase 8: Health & reliability ────────────────────────────────────────────
# Rich per-subscription health tracking, dead-channel auto-pruning, rolling
# health score, and log filtering. All additions are behind the existing auth
# middleware and do not touch the streaming path. Defaults in _health_cfg()
# are conservative — nothing gets pruned unless a channel fails repeatedly.
HEALTH_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)) or ".", "health.json")

def _health_cfg() -> dict:
    """Resolve health config with defaults. Callers get a plain dict they can index."""
    raw = (load_config().get("health") or {}) if isinstance(load_config().get("health"), dict) else {}
    return {
        "stall_seconds":            int(raw.get("stall_seconds", 15)),
        "stall_grace_seconds":      int(raw.get("stall_grace_seconds", 20)),
        "dead_channel_threshold":   int(raw.get("dead_channel_threshold", 5)),
        "dead_window_seconds":      int(raw.get("dead_window_seconds", 3600)),
        "probe_timeout_seconds":    int(raw.get("probe_timeout_seconds", 10)),
        # Phase 9: seconds to wait for active streams to drain naturally before
        # force-killing on shutdown. Keep short enough that redeploys stay snappy.
        "shutdown_drain_seconds":   int(raw.get("shutdown_drain_seconds", 10)),
    }

# Phase 10: Xtream Codes settings — defaults applied here so config.json can omit
# the section entirely and the API still serves usable defaults.
def _xtream_cfg() -> dict:
    raw = load_config().get("xtream")
    raw = raw if isinstance(raw, dict) else {}
    tg  = load_config().get("telegram") or {}
    return {
        "enabled":                       bool(raw.get("enabled", True)),
        "server_name":                   str(raw.get("server_name", "AQ-IPTV")),
        "server_url":                    str(raw.get("server_url", "")),  # falls back to state.base_url
        "default_max_connections":       int(raw.get("default_max_connections", 1)),
        "default_user_expiry_days":      int(raw.get("default_user_expiry_days", 30)),
        "session_idle_seconds":          int(raw.get("session_idle_seconds", 60)),
        "hls_extension_enabled":         bool(raw.get("hls_extension_enabled", True)),
        "auth_failure_threshold":        int(raw.get("auth_failure_threshold", 10)),
        "auth_failure_window_seconds":   int(raw.get("auth_failure_window_seconds", 60)),
        # Convenience mirror so user-lifecycle alerts stay in one settings dict.
        "user_alerts_enabled":           bool(tg.get("user_alerts_enabled", True)),
    }

# Phase 10: Xtream Codes user accounts. Stored in /config/users.json — kept
# separate from config.json so the Phase 9 config snapshot stays clean and
# user mutations don't bloat config backups. Atomic save uses the same
# tempfile + os.replace pattern as Phase 8's health.json.
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)) or ".", "users.json")

@dataclass
class User:
    """Xtream Codes user account. Distinct from cfg['auth'] (dashboard admin).

    The password field stores a bcrypt hash. Plaintext passwords that predate
    the bcrypt migration are auto-hashed on first boot. Persisted to
    /config/users.json which must be chmod 600.
    """
    username:         str
    password:         str
    enabled:          bool = True
    created_at:       float = 0.0
    expires_at:       float = 0.0          # 0 = never expires
    max_connections:  int   = 1
    allowed_groups:   List[str] = field(default_factory=list)   # [] = all groups
    allowed_channels: List[str] = field(default_factory=list)   # channel_keys; [] = all in allowed_groups
    # Phase 11: per-user dispatcher constraints. allowed_subscriptions, when
    # non-empty, restricts the StreamDispatcher to only these sub names. Empty
    # list = all subs are eligible. dispatch_preference is a hint string
    # ("fastest" / "stable" / "cheapest") reserved for future scoring tweaks.
    allowed_subscriptions: List[str] = field(default_factory=list)
    dispatch_preference:   str = ""
    notes:            str = ""
    # ── runtime-only (not persisted) ──
    active_connections: int   = 0
    total_streams:      int   = 0
    last_seen_ts:       float = 0.0
    last_seen_ip:       str   = ""
    bytes_served:       int   = 0          # lifetime, runtime-only

    def is_expired(self) -> bool:
        return bool(self.expires_at) and time.time() > self.expires_at

    def to_dict(self, *, include_password: bool = False) -> dict:
        d = {
            "username":              self.username,
            "enabled":               bool(self.enabled),
            "expired":               self.is_expired(),
            "created_at":            int(self.created_at),
            "expires_at":            int(self.expires_at),
            "max_connections":       int(self.max_connections),
            "allowed_groups":        list(self.allowed_groups),
            "allowed_channels":      list(self.allowed_channels),
            "allowed_subscriptions": list(self.allowed_subscriptions),
            "dispatch_preference":   self.dispatch_preference,
            "notes":                 self.notes,
            "active_connections":    int(self.active_connections),
            "total_streams":         int(self.total_streams),
            "last_seen_ts":          int(self.last_seen_ts),
            "last_seen_ip":          self.last_seen_ip,
            "bytes_served":          int(self.bytes_served),
        }
        if include_password:
            d["password"] = self.password
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "User":
        return cls(
            username              = str(d.get("username", "")),
            password              = str(d.get("password", "")),
            enabled               = bool(d.get("enabled", True)),
            created_at            = float(d.get("created_at", 0) or 0),
            expires_at            = float(d.get("expires_at", 0) or 0),
            max_connections       = int(d.get("max_connections", 1) or 1),
            allowed_groups        = list(d.get("allowed_groups")        or []),
            allowed_channels      = list(d.get("allowed_channels")      or []),
            allowed_subscriptions = list(d.get("allowed_subscriptions") or []),
            dispatch_preference   = str(d.get("dispatch_preference", "")),
            notes                 = str(d.get("notes", "")),
        )

def _hash_password(plaintext: str) -> str:
    """Hash a plaintext password with bcrypt (cost factor 12)."""
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt(rounds=12)).decode()

def _verify_password(plaintext: str, stored: str) -> bool:
    """Verify a password against a bcrypt hash. Falls back to direct comparison
    for legacy plaintext passwords that haven't been migrated yet."""
    if stored.startswith(("$2b$", "$2a$")):
        return bcrypt.checkpw(plaintext.encode(), stored.encode())
    return secrets.compare_digest(plaintext, stored)

def _migrate_plaintext_passwords(users: List[User]) -> bool:
    """One-time migration: detect plaintext passwords (not bcrypt-prefixed),
    hash them in place, return True if any were migrated."""
    migrated = False
    for u in users:
        if u.password and not u.password.startswith(("$2b$", "$2a$")):
            u.password = _hash_password(u.password)
            migrated = True
    return migrated

def _load_users() -> List[User]:
    """Read /config/users.json. Missing file = empty list (first run)."""
    try:
        if not os.path.exists(USERS_FILE):
            return []
        with open(USERS_FILE) as f:
            payload = json.load(f)
        raw = payload.get("users") if isinstance(payload, dict) else payload
        return [User.from_dict(u) for u in (raw or []) if isinstance(u, dict)]
    except Exception as exc:
        log.warning("users.json load failed: %s", _redact(exc))
        return []

def _save_users(users: List[User]) -> None:
    """Atomic write of the user list. Persisted fields only — runtime fields
    (active_connections, last_seen_ts, etc.) are intentionally excluded.
    Best-effort — failures are logged not raised."""
    try:
        payload = {
            "saved_at": time.time(),
            "users": [{
                "username":              u.username,
                "password":              u.password,
                "enabled":               bool(u.enabled),
                "created_at":            float(u.created_at),
                "expires_at":            float(u.expires_at),
                "max_connections":       int(u.max_connections),
                "allowed_groups":        list(u.allowed_groups),
                "allowed_channels":      list(u.allowed_channels),
                "allowed_subscriptions": list(u.allowed_subscriptions),
                "dispatch_preference":   u.dispatch_preference,
                "notes":                 u.notes,
            } for u in users],
        }
        d = os.path.dirname(USERS_FILE) or "."
        fd, tmp = tempfile.mkstemp(prefix=".users.", suffix=".json", dir=d)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, USERS_FILE)
        # chmod 600 — bcrypt hashes live here (sensitive even if not plaintext).
        try: os.chmod(USERS_FILE, 0o600)
        except OSError: pass
    except Exception as exc:
        log.error("users.json save failed: %s", _redact(exc))

def _find_user(username: str) -> Optional[User]:
    """Case-insensitive lookup against state.users."""
    if not username: return None
    u_lower = username.lower()
    for u in state.users:
        if u.username.lower() == u_lower:
            return u
    return None

def _authenticate_user(username: str, password: str) -> Optional[User]:
    """Verify Xtream credentials. Returns the User on success, None on any
    failure (unknown user, wrong password, disabled, expired). Uses bcrypt
    for hashed passwords, falls back to constant-time compare for any
    legacy plaintext entries that haven't been migrated yet."""
    if not username or not password:
        return None
    user = _find_user(username)
    if user is None:
        return None
    if not _verify_password(password, user.password):
        return None
    if not user.enabled:
        return None
    if user.is_expired():
        return None
    return user

# Phase 10: per-client auth-failure spike detector. Sliding window keyed by
# remote IP — when an IP exceeds xtream.auth_failure_threshold inside the
# window, fire one Telegram alert and back off (no spam) until it goes quiet.
_AUTH_FAIL_LOG: Dict[str, List[float]] = {}     # ip -> [ts, ts, ...]
_AUTH_FAIL_ALERTED: Dict[str, float] = {}       # ip -> last_alert_ts (cooldown)
_AUTH_FAIL_LOCK = asyncio.Lock()                # serialize fail-log updates

def _client_ip(request: Request) -> str:
    """Best-effort client IP. Trusts X-Forwarded-For only on the admin port
    (where requests transit DSM nginx, which sets XFF). On the public stream
    port, XFF is attacker-controlled so we use the raw socket peer instead."""
    srv = request.scope.get("server") or (None, None)
    socket_port = srv[1] if isinstance(srv, (tuple, list)) and len(srv) > 1 else None
    stream_port = (load_config().get("server") or {}).get("stream_port") or 0
    is_stream = stream_port and socket_port == int(stream_port)
    if not is_stream:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",", 1)[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"

async def _record_auth_failure(ip: str, username: str, reason: str) -> None:
    """Append a failure timestamp and trip the alert if the threshold is reached
    inside the sliding window. Cooldown prevents Telegram spam from a stuck client."""
    cfg = _xtream_cfg()
    threshold = max(1, cfg["auth_failure_threshold"])
    window    = max(1, cfg["auth_failure_window_seconds"])
    now = time.time()
    async with _AUTH_FAIL_LOCK:
        log_list = _AUTH_FAIL_LOG.setdefault(ip, [])
        log_list.append(now)
        # Trim old entries outside the sliding window
        cutoff = now - window
        log_list[:] = [t for t in log_list if t >= cutoff]
        count = len(log_list)
        last_alert = _AUTH_FAIL_ALERTED.get(ip, 0)
        # 5-minute cooldown so a stuck client doesn't spam Telegram
        cooled_down = (now - last_alert) > 300
        should_alert = count >= threshold and cooled_down
        if should_alert:
            _AUTH_FAIL_ALERTED[ip] = now
    state.log_event("auth", "XTREAM AUTH FAIL",
                    f"ip={ip} user={_redact(username) or '<empty>'} reason={reason}")
    if should_alert:
        state.add_alert("warning",
                        f"Xtream auth failures: {count} from {ip} in {window}s")
        asyncio.create_task(tg_send(tg_alert(
            "🛡️", "Xtream Auth Spike",
            f"<b>{count}</b> failed Xtream logins from <code>{html.escape(ip)}</code> "
            f"in {window}s\nLast user tried: <code>{html.escape(_redact(username) or '?')}</code>"
            f"\nReason: {html.escape(reason)}")))
    # Step 4: feed into blocklist tracker (separate sliding window)
    await _maybe_block_ip(ip)

def _record_auth_success(ip: str) -> None:
    """Clear the failure log for an IP after a successful auth — keeps the
    sliding window honest for legitimate clients that just typo'd once."""
    _AUTH_FAIL_LOG.pop(ip, None)
    _AUTH_FAIL_ALERTED.pop(ip, None)

# ── Step 4: Brute-force rate limit + IP blocklist ────────────────────────────
# Two layers:
#   1. Login rate limit — 5 POST /login per 60s per IP → 429 (applied in do_login)
#   2. IP blocklist — 20 auth failures (any endpoint) in 5 min → blocked 1 hour + Telegram alert
# Blocklist persisted to /config/blocklist.json; loaded on startup.

# ── Channel cache (avoids 6-7 min KSA4You startup download) ─────────────────
# Each subscription's filtered channel list is saved to /config/m3u_cache_<name>.json
# after every successful fetch. On startup, if the cache is younger than TTL
# the HTTP download is skipped entirely. If the live fetch fails, the stale
# cache is used as a fallback so the server starts with known-good data.
CHANNEL_CACHE_TTL = 6 * 3600   # seconds — re-fetch if older than this

def _channel_cache_path(sub_name: str) -> str:
    safe = re.sub(r'[^\w]', '_', sub_name)
    return os.path.join(
        os.path.dirname(os.path.abspath(CONFIG_FILE)) or ".", f"m3u_cache_{safe}.json")

def _load_channel_cache(sub_name: str, max_age: Optional[int] = CHANNEL_CACHE_TTL) -> Optional[List]:
    """Load cached channels. max_age=None ignores TTL (stale fallback use)."""
    try:
        path = _channel_cache_path(sub_name)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            data = json.load(f)
        if max_age is not None and (time.time() - data.get("fetched_at", 0)) > max_age:
            return None
        channels = []
        for d in data.get("channels", []):
            vlist = [QualityVariant(
                stream_id=v["stream_id"], quality=v["quality"],
                priority=v["priority"],   orig_name=v["orig_name"],
                sub_name=v.get("sub_name", ""),
            ) for v in (d.get("variants") or [])]
            channels.append(Channel(
                id=d["id"],   name=d["name"],   number=d["number"],
                logo=d.get("logo", ""),          group=d["group"],
                stream_id=d["stream_id"],         quality=d.get("quality", "HD"),
                tvg_id=d.get("tvg_id", ""),      variants=vlist or None,
            ))
        return channels or None
    except Exception as exc:
        log.warning("Channel cache load failed [%s]: %s", sub_name, exc)
        return None

def _save_channel_cache(sub_name: str, channels: list) -> None:
    """Persist filtered channel list to disk (atomic write)."""
    try:
        path = _channel_cache_path(sub_name)
        data = {
            "fetched_at": time.time(),
            "sub_name":   sub_name,
            "channels": [{
                "id":        ch.id,
                "name":      ch.name,
                "number":    ch.number,
                "logo":      ch.logo,
                "group":     ch.group,
                "stream_id": ch.stream_id,
                "quality":   ch.quality,
                "tvg_id":    ch.tvg_id or "",
                "variants": [{
                    "stream_id": v.stream_id, "quality": v.quality,
                    "priority":  v.priority,  "orig_name": v.orig_name,
                    "sub_name":  v.sub_name,
                } for v in (ch.variants or [])],
            } for ch in channels],
        }
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
        log.info("Channel cache saved: %d channels for %s", len(channels), sub_name)
    except Exception as exc:
        log.warning("Channel cache save failed [%s]: %s", sub_name, exc)

# ── Raw groups cache (per subscription) ──────────────────────────────────────
# Stores ALL raw M3U group names from the last live fetch — independent of the
# channel filter. Used by /api/groups to show the full provider catalog so the
# user can select which groups to import, even after a restart from channel cache.
def _raw_groups_cache_path(sub_name: str) -> str:
    safe = re.sub(r'[^\w]', '_', sub_name)
    return os.path.join(
        os.path.dirname(os.path.abspath(CONFIG_FILE)) or ".", f"raw_groups_{safe}.json")

def _load_raw_groups_cache(sub_name: str) -> List[str]:
    """Load raw group names from disk. Returns [] if not found."""
    try:
        path = _raw_groups_cache_path(sub_name)
        if not os.path.exists(path):
            return []
        with open(path) as f:
            data = json.load(f)
        return data.get("groups", [])
    except Exception as exc:
        log.warning("Raw groups cache load failed [%s]: %s", sub_name, exc)
        return []

def _save_raw_groups_cache(sub_name: str, groups: List[str]) -> None:
    """Persist raw group names to disk (atomic write)."""
    try:
        path = _raw_groups_cache_path(sub_name)
        data = {"sub_name": sub_name, "saved_at": time.time(), "groups": groups}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
        log.info("Raw groups cache saved: %d groups for %s", len(groups), sub_name)
    except Exception as exc:
        log.warning("Raw groups cache save failed [%s]: %s", sub_name, exc)

BLOCKLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(CONFIG_FILE)) or ".", "blocklist.json")
_IP_BLOCKLIST: Dict[str, float] = {}           # ip -> expiry_timestamp
_BLOCKLIST_LOCK = asyncio.Lock()
_BLOCK_FAIL_LOG: Dict[str, List[float]] = {}   # ip -> [ts, ...] (5-min window for blocklist)
_BLOCKLIST_THRESHOLD = 20   # failures to trigger a block
_BLOCKLIST_WINDOW    = 300  # 5-minute sliding window
_BLOCKLIST_DURATION  = 3600 # 1-hour block
# IPs that must never be blocked — Docker bridge gateway appears as the source
# IP for ALL clients due to NAT; blocking it cuts off every viewer at once.
_BLOCKLIST_EXEMPT: frozenset = frozenset({"172.17.0.1", "127.0.0.1"})

# Login rate limit (separate from blocklist — counts all attempts, not just failures)
_LOGIN_ATTEMPTS: Dict[str, List[float]] = {}   # ip -> [ts, ...]
_LOGIN_RATE_LIMIT  = 5
_LOGIN_RATE_WINDOW = 60  # seconds

def _load_blocklist() -> None:
    """Load persisted blocklist on startup. Expired entries are discarded."""
    try:
        if not os.path.exists(BLOCKLIST_FILE):
            return
        with open(BLOCKLIST_FILE) as f:
            data = json.load(f)
        now = time.time()
        for ip, expiry in data.items():
            if ip in _BLOCKLIST_EXEMPT:
                continue
            if isinstance(expiry, (int, float)) and expiry > now:
                _IP_BLOCKLIST[ip] = expiry
        if _IP_BLOCKLIST:
            log.info("Loaded %d active IP block(s) from %s", len(_IP_BLOCKLIST), BLOCKLIST_FILE)
    except Exception as exc:
        log.warning("Failed to load blocklist: %s", exc)

def _save_blocklist() -> None:
    """Persist current blocklist to disk (atomic write)."""
    try:
        now = time.time()
        active = {ip: exp for ip, exp in _IP_BLOCKLIST.items() if exp > now}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(BLOCKLIST_FILE) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(active, f)
            os.replace(tmp, BLOCKLIST_FILE)
        except BaseException:
            os.unlink(tmp)
            raise
    except Exception as exc:
        log.warning("Failed to save blocklist: %s", exc)

def _is_blocked(ip: str) -> bool:
    """Check if an IP is currently blocked."""
    if ip in _BLOCKLIST_EXEMPT:
        return False
    expiry = _IP_BLOCKLIST.get(ip)
    if expiry is None:
        return False
    if time.time() >= expiry:
        _IP_BLOCKLIST.pop(ip, None)
        return False
    return True

def _check_login_rate(ip: str) -> Optional[int]:
    """Return Retry-After seconds if IP exceeds login rate limit, else None."""
    now = time.time()
    attempts = _LOGIN_ATTEMPTS.setdefault(ip, [])
    cutoff = now - _LOGIN_RATE_WINDOW
    attempts[:] = [t for t in attempts if t >= cutoff]
    if len(attempts) >= _LOGIN_RATE_LIMIT:
        oldest = min(attempts)
        return int(oldest + _LOGIN_RATE_WINDOW - now) + 1
    attempts.append(now)
    return None

async def _maybe_block_ip(ip: str) -> None:
    """Called after every auth failure. If the IP exceeds the blocklist threshold
    in the sliding window, block it and fire a Telegram alert."""
    if ip in _BLOCKLIST_EXEMPT:
        return   # Docker gateway / loopback — never block, would cut off all clients
    now = time.time()
    async with _BLOCKLIST_LOCK:
        log_list = _BLOCK_FAIL_LOG.setdefault(ip, [])
        log_list.append(now)
        cutoff = now - _BLOCKLIST_WINDOW
        log_list[:] = [t for t in log_list if t >= cutoff]
        count = len(log_list)
        if count >= _BLOCKLIST_THRESHOLD and not _is_blocked(ip):
            _IP_BLOCKLIST[ip] = now + _BLOCKLIST_DURATION
            _BLOCK_FAIL_LOG.pop(ip, None)  # reset counter
            _save_blocklist()
            blocked = True
        else:
            blocked = False
    if blocked:
        mins = _BLOCKLIST_DURATION // 60
        state.log_event("auth", "IP BLOCKED",
                        f"ip={ip} reason=brute_force ({count} fails in {_BLOCKLIST_WINDOW}s)")
        state.add_alert("danger", f"IP {ip} blocked for {mins}m — {count} auth failures")
        asyncio.create_task(tg_send(tg_alert(
            "🚫", "IP Blocked — Brute Force",
            f"<code>{html.escape(ip)}</code> blocked for <b>{mins} min</b>\n"
            f"{count} auth failures in {_BLOCKLIST_WINDOW}s")))

# Phase 10: live Xtream session tracker. One entry per active /live/ stream.
# Lock-protected so concurrent client connect attempts can't race past the
# per-user max_connections cap. Sessions are added on connect and removed
# in the StreamingResponse `finally` clause when the client disconnects.
@dataclass
class UserSession:
    id:           str          # opaque short id, used for kick endpoints
    username:     str          # case-preserved as on the User
    channel_key:  str          # state.channels key (mux key)
    client_ip:    str
    user_agent:   str
    started_at:   float
    last_chunk_at: float
    bytes_sent:   int  = 0
    kicked:       bool = False  # set by admin kick — viewer loop checks this
    queue:        object = field(default=None, repr=False)  # asyncio.Queue ref for GC cleanup

    def to_dict(self) -> dict:
        ch = state.channels.get(self.channel_key)
        return {
            "id":            self.id,
            "username":      self.username,
            "channel_key":   self.channel_key,
            "channel_name":  ch.name if ch else self.channel_key,
            "client_ip":     self.client_ip,
            "user_agent":    self.user_agent,
            "started_at":    int(self.started_at),
            "last_chunk_at": int(self.last_chunk_at),
            "bytes_sent":    int(self.bytes_sent),
            "duration_s":    int(time.time() - self.started_at),
            "kicked":        self.kicked,
        }

_USER_SESSIONS: Dict[str, UserSession] = {}     # id -> session
_USER_SESSIONS_LOCK = asyncio.Lock()

def _publish_user_event(event: str, username: str, **extra) -> None:
    """Phase 12: SSE `user` topic emitter. The Users tab listens for this so
    create/update/delete + session start/end land in the table without polling.
    Frequency is naturally low (one per CRUD call, one per stream open/close)
    so no throttling is needed."""
    payload = {"event": event, "username": username, "ts": time.time()}
    payload.update(extra)
    try: bus.publish("user", payload)
    except Exception: pass

def _user_session_count(username: str) -> int:
    """Active live streams for a user (case-insensitive). Caller may hold the lock."""
    u_lower = username.lower()
    return sum(1 for s in _USER_SESSIONS.values()
               if s.username.lower() == u_lower and not s.kicked)

async def _register_user_session(user: User, channel_key: str,
                                 client_ip: str, user_agent: str
                                 ) -> Optional[UserSession]:
    """Atomically check the user's max_connections cap and register a new session.
    Returns the UserSession on success, or None if the user is at the cap.
    Bumps user.active_connections + total_streams + last_seen.
    """
    async with _USER_SESSIONS_LOCK:
        if _user_session_count(user.username) >= max(1, user.max_connections):
            return None
        sid = secrets.token_hex(6)
        sess = UserSession(
            id=sid, username=user.username, channel_key=channel_key,
            client_ip=client_ip, user_agent=user_agent,
            started_at=time.time(), last_chunk_at=time.time())
        _USER_SESSIONS[sid] = sess
        user.active_connections = _user_session_count(user.username)
        user.total_streams     += 1
        user.last_seen_ts       = time.time()
        first_time_this_ip      = (user.last_seen_ip != client_ip)
        user.last_seen_ip       = client_ip
    # New-IP alert removed — too noisy for dynamic IPs (mobile clients, etc.)
    _publish_user_event("session_start", user.username,
                        sid=sess.id, channel_key=channel_key,
                        client_ip=client_ip,
                        active_connections=user.active_connections)
    return sess

async def _unregister_user_session(sid: str, bytes_sent: int = 0) -> None:
    """Remove a session, decrement the user's active counter, and accumulate
    lifetime bytes served."""
    async with _USER_SESSIONS_LOCK:
        sess = _USER_SESSIONS.pop(sid, None)
    if sess is None:
        return
    user = _find_user(sess.username)
    final_bytes = int(bytes_sent or sess.bytes_sent)
    if user is not None:
        user.bytes_served    += final_bytes
        user.last_seen_ts     = time.time()
        user.active_connections = _user_session_count(user.username)
    _publish_user_event("session_end", sess.username,
                        sid=sid, channel_key=sess.channel_key,
                        bytes_sent=final_bytes,
                        active_connections=user.active_connections if user else 0)

async def _kick_user_session(sid: str) -> bool:
    """Admin action — flag a session as kicked. The viewer streaming loop polls
    `kicked` and tears its own connection down on the next chunk."""
    async with _USER_SESSIONS_LOCK:
        sess = _USER_SESSIONS.get(sid)
        if sess is None:
            return False
        sess.kicked = True
    _publish_user_event("kicked", sess.username, sid=sid)
    return True

async def _kick_all_user_sessions(username: str) -> int:
    """Flag every session for a username as kicked. Returns count."""
    n = 0
    async with _USER_SESSIONS_LOCK:
        for sess in _USER_SESSIONS.values():
            if sess.username.lower() == username.lower() and not sess.kicked:
                sess.kicked = True
                n += 1
    if n:
        _publish_user_event("kicked_all", username, count=n)
    return n

def _stream_id_to_key(stream_id: int) -> Optional[str]:
    """Resolve an Xtream stream_id (== ch.number) back to the channel_key the
    rest of the system uses. Number lookups are cheap; channel count is small."""
    sid_str = str(stream_id)
    for k, c in _live_channels().items():
        if str(c.number) == sid_str:
            return k
    return None

def _check_channel_allowed(user: User, channel_key: str) -> bool:
    """User-level visibility gate. Per spec:
    - If user.allowed_channels is non-empty, channel_key must be in it.
    - Else if user.allowed_groups is non-empty, the channel's group must be in it.
    - Else everything is allowed (still subject to dead-channel mask).
    """
    ch = state.channels.get(channel_key)
    if ch is None:
        return False
    if _is_channel_dead(channel_key):
        return False
    if user.allowed_channels:
        return channel_key in user.allowed_channels
    if user.allowed_groups:
        allowed = {g.lower() for g in user.allowed_groups}
        return (ch.group or "").lower() in allowed
    return True

def _user_filtered_channels(user: User) -> Dict[str, "Channel"]:
    """Live channels visible to a specific Xtream user. Intersects the Phase 8
    `_live_channels()` set with `_check_channel_allowed`."""
    return {k: c for k, c in _live_channels().items()
            if _check_channel_allowed(user, k)}

@dataclass
class SubHealth:
    """Rich per-subscription metrics. Populated by refresh_channels + health_monitor,
    persisted to HEALTH_STATE_FILE so a restart keeps recent history."""
    name:             str
    last_fetch_ts:    float = 0.0    # epoch seconds of most recent M3U fetch attempt
    last_fetch_ms:    int   = 0      # wall-clock ms for the successful or final attempt
    last_fetch_ok:    bool  = False
    last_error_class: str   = ""     # "", "auth", "http_4xx", "http_5xx", "network", "unknown"
    last_error_msg:   str   = ""
    channels_loaded:  int   = 0      # channels contributed by this sub on last successful fetch
    tcp_probe_ms:     int   = 0      # most recent TCP connect latency (from health_monitor)
    tcp_probe_ok:     bool  = False
    tcp_probe_ts:     float = 0.0
    fetch_history:    List[bool] = None  # rolling window of fetch outcomes (newest last)
    probe_history:    List[bool] = None  # rolling window of tcp probe outcomes
    def __post_init__(self):
        if self.fetch_history is None: self.fetch_history = []
        if self.probe_history is None: self.probe_history = []

    def record_fetch(self, ok: bool, ms: int, err_class: str = "", err_msg: str = "", channels: int = 0):
        self.last_fetch_ts   = time.time()
        self.last_fetch_ms   = int(ms)
        self.last_fetch_ok   = ok
        self.last_error_class = err_class if not ok else ""
        self.last_error_msg   = err_msg if not ok else ""
        if ok: self.channels_loaded = int(channels)
        self.fetch_history.append(ok)
        if len(self.fetch_history) > 48: self.fetch_history = self.fetch_history[-48:]

    def record_tcp(self, ok: bool, ms: int):
        self.tcp_probe_ts = time.time()
        self.tcp_probe_ms = int(ms)
        self.tcp_probe_ok = ok
        self.probe_history.append(ok)
        if len(self.probe_history) > 240: self.probe_history = self.probe_history[-240:]

    def success_rate(self, window: int = 12) -> float:
        hist = self.fetch_history[-window:]
        return (sum(1 for x in hist if x) / len(hist)) if hist else 0.0

    def uptime_pct(self, window: int = 240) -> float:
        hist = self.probe_history[-window:]
        return (sum(1 for x in hist if x) / len(hist) * 100) if hist else 0.0

    def score(self, sub: "Subscription") -> int:
        """0-100 composite. See scope table in Phase 8 plan."""
        pts = 0.0
        # (a) Recent fetch success rate — 40 pts
        pts += self.success_rate(12) * 40
        # (b) TCP probe currently OK — 20 pts
        pts += 20 if self.tcp_probe_ok else 0
        # (c) Had a successful fetch in last 24h — 20 pts
        if self.last_fetch_ok and (time.time() - self.last_fetch_ts) < 86400:
            pts += 20
        # (d) Account not expiring in <7 days — 10 pts
        acct = state.account_info.get(sub.name, {}) or {}
        days = acct.get("days_left")
        if isinstance(days, (int, float)) and days > 7: pts += 10
        elif isinstance(days, (int, float)) and days > 0: pts += 5
        elif not acct: pts += 5  # unknown account state — don't penalize fully
        # (e) Load below 90% — 10 pts
        if sub.max_streams and (sub.active_streams / sub.max_streams) < 0.9: pts += 10
        return max(0, min(100, int(round(pts))))

    def to_dict(self, sub: "Subscription") -> dict:
        return {
            "name":             self.name,
            "last_fetch_ts":    int(self.last_fetch_ts),
            "last_fetch_age_s": int(time.time() - self.last_fetch_ts) if self.last_fetch_ts else None,
            "last_fetch_ms":    self.last_fetch_ms,
            "last_fetch_ok":    self.last_fetch_ok,
            "last_error_class": self.last_error_class,
            "last_error_msg":   self.last_error_msg,
            "channels_loaded":  self.channels_loaded,
            "tcp_probe_ms":     self.tcp_probe_ms,
            "tcp_probe_ok":     self.tcp_probe_ok,
            "tcp_probe_ts":     int(self.tcp_probe_ts),
            "success_rate_1h":  round(self.success_rate(12), 3),
            "uptime_pct_24h":   round(self.uptime_pct(240), 1),
            "score":            self.score(sub),
        }

# State store — one SubHealth per subscription name. Lazily created on first use.
_SUB_HEALTH: Dict[str, SubHealth] = {}

def _get_sub_health(name: str) -> SubHealth:
    h = _SUB_HEALTH.get(name)
    if h is None:
        h = SubHealth(name=name)
        _SUB_HEALTH[name] = h
    return h

def _save_sub_health_snapshot() -> None:
    """Atomic write of the current _SUB_HEALTH + _DEAD_CHANNELS map to health.json.
    Called from health_monitor on each cycle, best-effort — failures are logged not raised."""
    try:
        payload = {
            "saved_at": time.time(),
            "subs": {
                name: {
                    "name":             h.name,
                    "last_fetch_ts":    h.last_fetch_ts,
                    "last_fetch_ms":    h.last_fetch_ms,
                    "last_fetch_ok":    h.last_fetch_ok,
                    "last_error_class": h.last_error_class,
                    "last_error_msg":   h.last_error_msg,
                    "channels_loaded":  h.channels_loaded,
                    "tcp_probe_ms":     h.tcp_probe_ms,
                    "tcp_probe_ok":     h.tcp_probe_ok,
                    "tcp_probe_ts":     h.tcp_probe_ts,
                    "fetch_history":    h.fetch_history[-48:],
                    "probe_history":    h.probe_history[-240:],
                } for name, h in _SUB_HEALTH.items()
            },
            "dead_channels": {
                k: {"count": v["count"], "last_ts": v["last_ts"]}
                for k, v in _DEAD_CHANNELS.items()
            },
        }
        d = os.path.dirname(HEALTH_STATE_FILE) or "."
        fd, tmp = tempfile.mkstemp(prefix=".health.", suffix=".json", dir=d)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, HEALTH_STATE_FILE)
    except Exception as exc:
        log.debug("health snapshot write failed: %s", _redact(exc))

def _load_sub_health_snapshot() -> None:
    """Restore _SUB_HEALTH + _DEAD_CHANNELS from disk at startup. Best-effort."""
    try:
        if not os.path.exists(HEALTH_STATE_FILE): return
        with open(HEALTH_STATE_FILE) as f:
            payload = json.load(f)
        for name, d in (payload.get("subs") or {}).items():
            h = SubHealth(name=name)
            for k, v in d.items():
                if hasattr(h, k): setattr(h, k, v)
            _SUB_HEALTH[name] = h
        for k, v in (payload.get("dead_channels") or {}).items():
            _DEAD_CHANNELS[k] = {"count": int(v.get("count", 0)),
                                 "last_ts": float(v.get("last_ts", 0))}
        log.info("Health snapshot restored: %d subs, %d dead-channel records",
                 len(_SUB_HEALTH), len(_DEAD_CHANNELS))
    except Exception as exc:
        log.warning("Health snapshot load failed: %s", _redact(exc))

# ── Dead channel tracking ────────────────────────────────────────────────────
# Per-channel rolling failure counter. When a channel fails to open ≥N times
# inside a sliding window (both from health.dead_channel_threshold and
# dead_window_seconds), it's marked dead and excluded from all output surfaces
# (lineup.json, /api/channels, M3U, EPG) until the next refresh_channels clears
# the record. Recovery is cheap: one successful stream start clears the counter.
_DEAD_CHANNELS: Dict[str, dict] = {}  # channel_key -> {"count": int, "last_ts": float}

def _record_channel_failure(channel_key: str) -> None:
    cfg = _health_cfg()
    now = time.time()
    rec = _DEAD_CHANNELS.get(channel_key) or {"count": 0, "last_ts": 0}
    # If the last failure was outside the window, reset the counter.
    if now - rec["last_ts"] > cfg["dead_window_seconds"]:
        rec = {"count": 0, "last_ts": 0}
    rec["count"]   += 1
    rec["last_ts"]  = now
    _DEAD_CHANNELS[channel_key] = rec
    if rec["count"] == cfg["dead_channel_threshold"]:
        ch = state.channels.get(channel_key)
        ch_name = ch.name if ch else channel_key
        state.log_event("health", "CHANNEL DEAD",
                        f"{ch_name} failed {rec['count']} times in "
                        f"{cfg['dead_window_seconds']//60}m — excluded until next refresh")
        state.add_alert("warning", f"Channel auto-pruned: {ch_name}")

def _record_channel_success(channel_key: str) -> None:
    if channel_key in _DEAD_CHANNELS:
        _DEAD_CHANNELS.pop(channel_key, None)

def _is_channel_dead(channel_key: str) -> bool:
    rec = _DEAD_CHANNELS.get(channel_key)
    if not rec: return False
    cfg = _health_cfg()
    if time.time() - rec["last_ts"] > cfg["dead_window_seconds"]:
        _DEAD_CHANNELS.pop(channel_key, None)
        return False
    return rec["count"] >= cfg["dead_channel_threshold"]

def _live_channels() -> Dict[str, "Channel"]:
    """Return state.channels filtered to non-dead entries.
    All output surfaces (lineup, /api/channels, M3U, EPG) use this."""
    return {k: ch for k, ch in state.channels.items() if not _is_channel_dead(k)}

# ── Probe-on-add ─────────────────────────────────────────────────────────────
async def _probe_subscription(name: str, url: str, username: str, password: str) -> dict:
    """Synchronous validation of a new subscription before persisting it.
    Returns {"ok": bool, ...details}. Stays under health.probe_timeout_seconds."""
    cfg = _health_cfg()
    budget = cfg["probe_timeout_seconds"]
    result: Dict[str, object] = {
        "ok": False, "tcp": False, "auth": False, "m3u_reachable": False,
        "channels_head": 0, "latency_ms": 0, "error": "",
    }
    base = url.rstrip("/")
    t0 = time.time()

    # 1) TCP connect
    try:
        parsed = urlparse(base if "://" in base else f"http://{base}")
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3.0)
        w.close()
        result["tcp"] = True
    except Exception as exc:
        result["error"] = f"tcp connect failed: {_redact(exc) or type(exc).__name__}"
        result["latency_ms"] = int((time.time() - t0) * 1000)
        return result

    # 2) player_api.php auth check
    try:
        api_url = f"{base}/player_api.php?username={username}&password={password}"
        r = await state.admin_http.get(api_url, timeout=budget / 2)
        if r.status_code == 200:
            try:
                j = r.json()
                if isinstance(j, dict) and j.get("user_info"):
                    result["auth"] = True
            except Exception: pass
        if not result["auth"]:
            result["error"] = f"auth failed: HTTP {r.status_code}"
            result["latency_ms"] = int((time.time() - t0) * 1000)
            return result
    except Exception as exc:
        result["error"] = f"auth request failed: {_redact(exc) or type(exc).__name__}"
        result["latency_ms"] = int((time.time() - t0) * 1000)
        return result

    # 3) M3U partial GET — just count first handful of #EXTINF lines
    try:
        m3u_url = f"{base}/get.php?username={username}&password={password}&type=m3u_plus&output=ts"
        channels = 0
        async with state.admin_http.stream("GET", m3u_url, timeout=budget) as resp:
            if resp.status_code != 200:
                result["error"] = f"m3u HTTP {resp.status_code}"
                result["latency_ms"] = int((time.time() - t0) * 1000)
                return result
            result["m3u_reachable"] = True
            buf = ""
            async for chunk in resp.aiter_bytes(32768):
                buf += chunk.decode("utf-8", errors="replace")
                channels += buf.count("#EXTINF")
                buf = buf[-256:]  # keep tail for partial lines
                if channels >= 50: break
        result["channels_head"] = channels
    except Exception as exc:
        result["error"] = f"m3u stream failed: {_redact(exc) or type(exc).__name__}"
        result["latency_ms"] = int((time.time() - t0) * 1000)
        return result

    result["ok"] = result["tcp"] and result["auth"] and result["m3u_reachable"]
    result["latency_ms"] = int((time.time() - t0) * 1000)
    return result

# ── EPG (cached + single builder, A18/A19) ──────────────────────────────────
_EPG_CACHE: Dict[str, object] = {"hour": 0, "channels_hash": "", "xml": b"", "gz": b""}

def _epg_id(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", name).strip("_").lower()

def _epg_prog_title(ch_name: str, hour: int, day_offset: int = 0) -> str:
    """Return a realistic-looking programme title based on channel type and time."""
    n = ch_name.lower()
    seed = (day_offset * 13 + hour) % 7  # varies by day so same hour ≠ same title every day

    is_sports = any(x in n for x in ("sport", "kass", "afc", "xtra", "thmanayh",
                                      "yas sports", "ad sport", "max "))
    is_movies = any(x in n for x in ("movie", "cinema", "film", "aflam"))
    is_series = any(x in n for x in ("drama", "series", "mosalsal", "turkey"))
    is_kids   = any(x in n for x in ("kids", "junior", "baby", "jeem", "cartoon",
                                      "nick", "animat", "carton", "moonbug", "cbeebies"))
    is_news   = any(x in n for x in ("news", "bloomberg", "fox news"))

    if is_sports:
        _s = [
            ["Sports Briefing",    "Morning Analysis",  "Training Session",
             "Pre-Match Preview",  "Academy Highlights","Sports Roundup", "Warm-Up Show"],
            ["LIVE: League Match", "LIVE: Cup Coverage","Live Championship",
             "LIVE: Stadium Show", "Live Sports Event", "LIVE: Group Stage","LIVE: Feature Match"],
            ["Afternoon Highlights","Live Coverage",     "Sports Magazine",
             "Match Rundown",       "Post-Match Show",  "Tactical Review", "Game Day Live"],
            ["Prime Time Sport",   "Match of the Day",  "Evening Kick-Off",
             "Sports Special",     "Stars of the Game","Night Sport",      "Champions Tonight"],
            ["Match Replay",       "Goals Galore",      "Late Night Sport",
             "Overnight Highlights","Classic Match",    "Legends Tonight", "Sport After Dark"],
        ]
        if   6 <= hour < 10: return _s[0][seed]
        elif 10 <= hour < 14: return _s[1][seed]
        elif 14 <= hour < 17: return _s[2][seed]
        elif 17 <= hour < 22: return _s[3][seed]
        else:                 return _s[4][seed]

    elif is_movies:
        genres = ["Action","Drama","Thriller","Comedy","Adventure","Romance","Mystery"]
        g = genres[(day_offset * 3 + hour // 2) % len(genres)]
        if   6 <= hour < 12: return f"Morning Movie — {g}"
        elif 12 <= hour < 17: return f"Afternoon Film — {g}"
        elif 17 <= hour < 22: return f"Feature Film — {g}"
        else:                 return f"Late Night Movie — {g}"

    elif is_series:
        _s = ["Drama Series", "Evening Drama", "Prime Series", "Family Drama",
              "Weekend Drama", "Classic Series", "New Season"]
        if   6 <= hour < 12: return ["Morning Drama", "Classic Episode"][seed % 2]
        elif 17 <= hour < 22: return ["Prime Time Drama", "Featured Series", "Evening Drama"][seed % 3]
        else:                 return _s[seed]

    elif is_kids:
        shows = ["Cartoon Adventure", "Kids' World", "Fun Learning", "Story Time",
                 "Animation Hour", "Sing Along", "Children's Corner"]
        return shows[(hour + day_offset) % len(shows)]

    elif is_news:
        if   6 <= hour < 10: return "Morning News"
        elif 10 <= hour < 14: return "Midday Report"
        elif 14 <= hour < 18: return "Afternoon Headlines"
        elif 18 <= hour < 22: return "Evening News Bulletin"
        else:                 return "Late News Update"

    else:
        _s = ["Entertainment Tonight", "Variety Show", "Talk Show", "Cultural Programme",
              "Prime Time Special", "Evening Show", "Weekend Magazine"]
        if   6 <= hour < 12: return ["Morning Show", "Wake Up Programme"][seed % 2]
        elif 12 <= hour < 17: return _s[(seed + 2) % len(_s)]
        elif 17 <= hour < 22: return _s[seed]
        else:                 return ["Night Show", "Midnight Broadcast"][seed % 2]

def _epg_category(ch_name: str, group: str) -> str:
    n = (ch_name or "").lower()
    g = (group or "").lower()
    if any(k in n for k in ("sport", "kass", "afc", "xtra", "thmanayh")): return "Sports"
    if any(k in n for k in ("movie", "cinema", "film", "aflam")):         return "Movie"
    if any(k in n for k in ("drama", "series", "mosalsal")):               return "Drama"
    if any(k in n for k in ("kids", "junior", "baby", "jeem", "cartoon",
                             "nick", "animat")):                            return "Children"
    if any(k in n for k in ("news", "bloomberg")):                         return "News"
    if "sport" in g:      return "Sports"
    if "entertain" in g:  return "Entertainment"
    return "General"

def _build_epg_xml() -> bytes:
    """Emit a synthetic XMLTV guide: one <channel> per tuner channel, with a
    week of 2-hour <programme> slots when config.epg.fallback_synthetic is true
    (default), or no programmes at all when disabled."""
    import datetime as dt
    now          = int(time.time())
    slot_secs    = 2 * 3600           # 2-hour blocks look more realistic
    start_slot   = now - (now % slot_secs)
    slots        = 7 * 12             # 7 days × 12 slots/day
    def fmt(ts): return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y%m%d%H%M%S") + " +0000"

    cfg = load_config()
    fallback_synthetic = bool(cfg.get("epg", {}).get("fallback_synthetic", True))

    # Phase 8: dead channels are excluded from the guide too.
    live = _live_channels()
    root = ET.Element("tv", attrib={"generator-info-name": "AQ-IPTV-Tuner",
                                    "source-info-name":    "AQ-IPTV"})
    # <channel> elements — always one per tuner channel. ID matches what
    # _xtream_streams advertises as `epg_channel_id` and what _build_user_xmltv
    # filters on, so per-user filtering keeps every visible channel's slots.
    def _eid(ch): return ch.tvg_id or _epg_id(ch.name)
    for key, ch in live.items():
        eid = _eid(ch)
        cel = ET.SubElement(root, "channel", id=eid)
        ET.SubElement(cel, "display-name", lang="ar").text = ch.name
        ET.SubElement(cel, "display-name", lang="en").text = ch.name
        if ch.logo:
            ET.SubElement(cel, "icon", src=f"{state.base_url}/logo/{key}")

    synth_count = empty_count = 0
    for key, ch in live.items():
        eid = _eid(ch)
        if fallback_synthetic:
            cat = _epg_category(ch.name, ch.group)
            for i in range(slots):
                s = start_slot + i * slot_secs
                prog = ET.SubElement(root, "programme",
                    attrib={"start": fmt(s), "stop": fmt(s + slot_secs), "channel": eid})
                ET.SubElement(prog, "title",    lang="en").text = ch.name
                ET.SubElement(prog, "category", lang="en").text = cat
            synth_count += 1
        else:
            empty_count += 1

    log.info("EPG built: %d synthetic / %d empty (%d channels)",
             synth_count, empty_count, len(state.channels))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)

def _get_epg_bytes():
    """Return (xml_bytes, gz_bytes), rebuilding only when the hour rolls over
    or the channel set has changed."""
    now_hour = int(time.time()) // 3600
    live = _live_channels()
    ch_hash  = hashlib.md5(
        "|".join(f"{k}={ch.name}:{ch.tvg_id}"
                 for k, ch in sorted(live.items())).encode()
    ).hexdigest()
    if (_EPG_CACHE["hour"] != now_hour
            or _EPG_CACHE["channels_hash"] != ch_hash
            or not _EPG_CACHE["xml"]):
        xml_bytes = _build_epg_xml()
        _EPG_CACHE["xml"]           = xml_bytes
        _EPG_CACHE["gz"]            = gzip.compress(xml_bytes)
        _EPG_CACHE["hour"]          = now_hour
        _EPG_CACHE["channels_hash"] = ch_hash
    return _EPG_CACHE["xml"], _EPG_CACHE["gz"]

@app.get("/epg")
async def epg():
    xml_bytes, _ = _get_epg_bytes()
    return Response(content=xml_bytes, media_type="application/xml",
                    headers={"Content-Disposition": "inline; filename=epg.xml",
                             "Cache-Control": "public, max-age=1800"})

@app.get("/epg.xml")
async def epg_xml():
    return await epg()

@app.get("/epg.xml.gz")
async def epg_gz():
    _, gz_bytes = _get_epg_bytes()
    return Response(content=gz_bytes, media_type="application/gzip",
                    headers={"Content-Encoding": "gzip",
                             "Content-Disposition": "inline; filename=epg.xml.gz",
                             "Cache-Control": "public, max-age=1800"})

# ── Phase 10: Xtream Codes API ────────────────────────────────────────────────
# Single backend, two front doors. /stream/{key} (HDHR/Plex) and
# /live/{u}/{p}/{id}.{ext} (Xtream Codes) both resolve to the same ChannelMux
# pool via _get_or_start_mux. Channels aren't duplicated — only the entry path
# differs. VOD/Series return [] — this is a live-only service.

# Per-user filtered XMLTV cache. Key = (username, ""). Invalidates whenever
# refresh_channels rebuilds state.channels (which clears _EPG_CACHE too).
_USER_EPG_CACHE: Dict[Tuple[str, str], Tuple[bytes, bytes]] = {}

def _category_id_for(group: str) -> str:
    """Spec: deterministic integer IDs from sorted(state.all_groups) position.
    Stable within a session; rebuilt on every refresh_channels call."""
    if not group: return "0"
    try:
        idx = state.all_groups.index(group)
        return str(idx + 1)
    except ValueError:
        return "0"

def _xtream_user_info(user: User) -> dict:
    """`user_info` block. Echo username/password back as plaintext (Xtream
    spec — clients display these directly in their settings UI)."""
    cfg = _xtream_cfg()
    active = _user_session_count(user.username)
    if not user.enabled:
        status_str = "Disabled"
    elif user.is_expired():
        status_str = "Expired"
    else:
        status_str = "Active"
    return {
        "username":          user.username,
        "password":          "",
        "message":           cfg["server_name"],
        "auth":              1,
        "status":            status_str,
        "exp_date":          str(int(user.expires_at)) if user.expires_at else None,
        "is_trial":          "0",
        "active_cons":       str(active),
        "created_at":        str(int(user.created_at)) if user.created_at else "",
        "max_connections":   str(int(user.max_connections)),
        "allowed_output_formats": ["ts", "m3u8"] if cfg["hls_extension_enabled"] else ["ts"],
    }

def _xtream_server_info() -> dict:
    """`server_info` block. URL/port advertised so clients store the right base."""
    cfg = _xtream_cfg()
    base = (cfg["server_url"] or state.base_url or "").rstrip("/")
    parsed = urlparse(base) if base else None
    host = parsed.hostname if parsed else ""
    port = parsed.port if parsed and parsed.port else (443 if (parsed and parsed.scheme == "https") else 80)
    https_port = 443 if (parsed and parsed.scheme == "https") else port
    now = datetime.now()
    return {
        "url":             host or "",
        "port":            str(port),
        "https_port":      str(https_port),
        "server_protocol": parsed.scheme if parsed else "http",
        "rtmp_port":       "0",
        "timezone":        time.strftime("%Z") or "UTC",
        "timestamp_now":   int(time.time()),
        "time_now":        now.strftime("%Y-%m-%d %H:%M:%S"),
        "process":         True,
    }

def _xtream_categories(user: User) -> List[dict]:
    """get_live_categories. Filtered to groups visible to this user."""
    seen: Set[str] = set()
    for ch in _user_filtered_channels(user).values():
        if ch.group: seen.add(ch.group)
    out = []
    for grp in sorted(seen, key=str.lower):
        out.append({
            "category_id":   _category_id_for(grp),
            "category_name": grp or "Uncategorized",
            "parent_id":     0,
        })
    return out

def _xtream_streams(user: User, category_id: Optional[str] = None) -> List[dict]:
    """get_live_streams. stream_id = ch.number (already unique per spec)."""
    rows = []
    for key, ch in _user_filtered_channels(user).items():
        cid = _category_id_for(ch.group)
        if category_id and cid != category_id:
            continue
        try: sid_int = int(ch.number)
        except (TypeError, ValueError): continue   # malformed number — skip
        rows.append({
            "num":                 sid_int,
            "name":                ch.name,
            "stream_type":         "live",
            "stream_id":           sid_int,
            "stream_icon":         _logo_url(state.base_url, key, ch.logo),
            "epg_channel_id":      ch.tvg_id or _epg_id(ch.name),
            "added":               str(int(state.started_at)),
            "category_id":         cid,
            "custom_sid":          "",
            "tv_archive":          0,
            "direct_source":       "",
            "tv_archive_duration": 0,
        })
    rows.sort(key=lambda r: r["name"].lower())
    return rows

def _xtream_short_epg(user: User, stream_id: int, limit: int = 4) -> dict:
    """get_short_epg — always returns empty since real XMLTV ingestion was
    removed. Synthetic guide data is only exposed via the global /epg endpoint."""
    return {"epg_listings": []}

def _xtream_simple_data_table(user: User, stream_id: int) -> dict:
    """get_simple_data_table — always returns empty (see _xtream_short_epg)."""
    return {"epg_listings": []}

@app.get("/player_api.php")
async def xtream_player_api(request: Request,
                            username: str = "",
                            password: str = "",
                            action:   str = "",
                            category_id: str = "",
                            stream_id: int = 0,
                            limit: int = 4):
    """Xtream Codes JSON dispatcher. Bare call (no action) returns the login
    envelope; subsequent calls dispatch on `action`. All catalog responses
    are filtered through `_user_filtered_channels(user)`."""
    ip = _client_ip(request)
    # Phase 15c: blocklist check (rate limiting via _record_auth_failure on fail)
    if _is_blocked(ip):
        return JSONResponse({"user_info": {"auth": 0}}, status_code=403)
    if not _xtream_cfg()["enabled"]:
        await _record_auth_failure(ip, username, "xtream_disabled")
        return JSONResponse({"user_info": {"auth": 0}}, status_code=503)
    user = _authenticate_user(username, password)
    if not user:
        await _record_auth_failure(ip, username, "bad_credentials")
        return JSONResponse({"user_info": {"auth": 0}}, status_code=401)
    _record_auth_success(ip)

    a = (action or "").lower()
    if not a or a == "get_account_info":
        return JSONResponse({
            "user_info":   _xtream_user_info(user),
            "server_info": _xtream_server_info(),
        })
    if a == "get_live_categories":
        return JSONResponse(_xtream_categories(user))
    if a == "get_live_streams":
        return JSONResponse(_xtream_streams(user, category_id or None))
    if a == "get_short_epg":
        return JSONResponse(_xtream_short_epg(user, stream_id, limit))
    if a == "get_simple_data_table":
        return JSONResponse(_xtream_simple_data_table(user, stream_id))
    if a in ("get_vod_categories", "get_vod_streams", "get_vod_info",
             "get_series_categories", "get_series", "get_series_info"):
        return JSONResponse([])
    # Unknown action — return the login envelope (matches Xtream behaviour).
    return JSONResponse({
        "user_info":   _xtream_user_info(user),
        "server_info": _xtream_server_info(),
    })

def _build_user_xmltv(user: User) -> Tuple[bytes, bytes]:
    """Reuse the global XMLTV bytes and strip out <channel> / <programme>
    nodes the user can't see. Cached per username; refresh_channels clears
    the cache when the channel set changes."""
    cache_key = (user.username.lower(), "")
    hit = _USER_EPG_CACHE.get(cache_key)
    if hit is not None:
        return hit
    full_xml, _ = _get_epg_bytes()
    visible = _user_filtered_channels(user)
    allowed_epg_ids = {(ch.tvg_id or _epg_id(ch.name)) for ch in visible.values()}
    try:
        root = ET.fromstring(full_xml)
    except ET.ParseError:
        return (full_xml, gzip.compress(full_xml))
    for elem in list(root):
        eid = elem.get("id") if elem.tag == "channel" else elem.get("channel")
        if elem.tag in ("channel", "programme") and eid not in allowed_epg_ids:
            root.remove(elem)
    out_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    out_gz  = gzip.compress(out_xml)
    # Bound the cache: 16 entries max, FIFO eviction.
    if len(_USER_EPG_CACHE) >= 16:
        _USER_EPG_CACHE.pop(next(iter(_USER_EPG_CACHE)))
    _USER_EPG_CACHE[cache_key] = (out_xml, out_gz)
    return (out_xml, out_gz)

@app.get("/xmltv.php")
async def xtream_xmltv(request: Request, username: str = "", password: str = ""):
    """Per-user XMLTV. Filtered against the user's allowed groups/channels so
    clients only see EPG for channels they can actually open."""
    ip = _client_ip(request)
    if _is_blocked(ip):
        raise HTTPException(403, "Forbidden")
    if not _xtream_cfg()["enabled"]:
        await _record_auth_failure(ip, username, "xtream_disabled")
        raise HTTPException(503, "Xtream API is disabled")
    user = _authenticate_user(username, password)
    if not user:
        await _record_auth_failure(ip, username, "bad_credentials")
        raise HTTPException(401, "invalid credentials")
    _record_auth_success(ip)
    xml_bytes, _gz = _build_user_xmltv(user)
    return Response(content=xml_bytes, media_type="application/xml",
                    headers={"Content-Disposition": "inline; filename=xmltv.xml",
                             "Cache-Control": "private, max-age=1800"})

@app.get("/get.php")
async def xtream_get_php(request: Request,
                         username: str = "",
                         password: str = "",
                         type: str = "m3u_plus",
                         output: str = "ts"):
    """User-scoped M3U builder. Stream URLs embed the user's creds path-style
    so the /live/ endpoint can re-authorize on every chunk request."""
    ip = _client_ip(request)
    if _is_blocked(ip):
        raise HTTPException(403, "Forbidden")
    if not _xtream_cfg()["enabled"]:
        await _record_auth_failure(ip, username, "xtream_disabled")
        raise HTTPException(503, "Xtream API is disabled")
    user = _authenticate_user(username, password)
    if not user:
        await _record_auth_failure(ip, username, "bad_credentials")
        raise HTTPException(401, "invalid credentials")
    _record_auth_success(ip)

    cfg = _xtream_cfg()
    base = (state.base_url or "").rstrip("/")
    out_ext = "m3u8" if (output or "ts").lower() == "m3u8" and cfg["hls_extension_enabled"] else "ts"
    lines = ["#EXTM3U"]
    for key, ch in _user_filtered_channels(user).items():
        try: sid = int(ch.number)
        except (TypeError, ValueError): continue
        epg_id = ch.tvg_id or _epg_id(ch.name)
        logo   = f"{base}/logo/{key}" if (ch.logo or _find_cached_logo(key)[0]) else ""
        lines.append(
            f'#EXTINF:-1 tvg-id="{html.escape(epg_id, quote=True)}" '
            f'tvg-name="{html.escape(ch.name, quote=True)}" '
            f'tvg-logo="{html.escape(logo, quote=True)}" '
            f'group-title="{html.escape(ch.group or "", quote=True)}",'
            f'{ch.name}')
        lines.append(f"{base}/live/{user.username}/{user.password}/{sid}.{out_ext}")
    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="audio/mpegurl",
                    headers={"Content-Disposition": "inline; filename=playlist.m3u",
                             "Cache-Control": "no-cache"})

async def _xtream_live_handler(u: str, p: str, stream_id: int, request: Request,
                               *, hls: bool = False) -> Response:
    """Shared body for /live/{u}/{p}/{id}.ts and .m3u8. Re-authorizes on every
    request, enforces user.max_connections atomically, then proxies via the
    shared ChannelMux pool through _get_or_start_mux."""
    if state.shutting_down:
        return Response(content="Tuner shutting down, retry shortly",
                        status_code=503,
                        headers={"Retry-After": "5"})
    cfg = _xtream_cfg()
    if hls and not cfg["hls_extension_enabled"]:
        raise HTTPException(404, "HLS extension disabled")
    ip = _client_ip(request)
    # Phase 15c: blocklist check (rate limiting via _record_auth_failure on fail)
    if _is_blocked(ip):
        raise HTTPException(403, "Forbidden")
    if not cfg["enabled"]:
        await _record_auth_failure(ip, u, "xtream_disabled")
        raise HTTPException(503, "Xtream API is disabled")
    user = _authenticate_user(u, p)
    if not user:
        await _record_auth_failure(ip, u, "bad_credentials")
        raise HTTPException(401, "invalid credentials")
    _record_auth_success(ip)

    channel_key = _stream_id_to_key(stream_id)
    if channel_key is None or not _check_channel_allowed(user, channel_key):
        raise HTTPException(404, "Channel not found")
    ch = state.channels[channel_key]
    user_agent = request.headers.get("user-agent", "")[:200]

    sess = await _register_user_session(user, channel_key, ip, user_agent)
    if sess is None:
        state.log_event("stream", "USER LIMIT",
                        f"{user.username} hit {user.max_connections}",
                        sub=user.username)
        if cfg["user_alerts_enabled"]:
            asyncio.create_task(tg_send(tg_alert(
                "⛔", "Xtream Max Connections",
                f"<b>{html.escape(user.username)}</b> blocked — "
                f"{user.max_connections} connection limit hit "
                f"(client {html.escape(ip)})")))
        raise HTTPException(429, "Max connections reached")

    try:
        mux = await _get_or_start_mux(channel_key, ch, user=user)
    except HTTPException:
        await _unregister_user_session(sess.id)
        raise

    queue = await mux.subscribe(ip, xtream_user=user.username)
    sess.queue = queue  # store ref so GC can unsubscribe zombie viewers
    asyncio.create_task(_notify_stream_start(
        channel_key, ch.name, ch.quality, mux.sub.name, ip,
        xtream_user=user.username))
    state.log_event("stream", "DISPATCH",
                    f"{ch.name} → {user.username}@{ip}",
                    sub=user.username)

    async def viewer_stream():
        bytes_sent_local = 0
        try:
            while True:
                if sess.kicked:
                    state.log_event("stream", "STREAM KICKED",
                                    f"{user.username} kicked from {ch.name}",
                                    sub=user.username)
                    break
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    break
                if chunk is None:
                    break
                bytes_sent_local += len(chunk)
                sess.bytes_sent    = bytes_sent_local
                yield chunk
                sess.last_chunk_at = time.time()  # after yield: only refreshed on successful delivery
                if await request.is_disconnected():
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning("Xtream viewer error [%s]: %s", ch.name, exc)
        finally:
            await mux.unsubscribe(queue, ip)
            await _unregister_user_session(sess.id, bytes_sent_local)

    return StreamingResponse(viewer_stream(), media_type="video/MP2T",
                             headers={"Cache-Control": "no-cache",
                                      "X-Channel": ch.name,
                                      "X-Xtream-User": user.username})

@app.get("/live/{u}/{p}/{stream_id}.ts")
async def xtream_live_ts(u: str, p: str, stream_id: int, request: Request):
    return await _xtream_live_handler(u, p, stream_id, request, hls=False)

@app.get("/live/{u}/{p}/{stream_id}.m3u8")
async def xtream_live_m3u8(u: str, p: str, stream_id: int, request: Request):
    """HLS extension — TiviMate sometimes appends .m3u8 even when output=ts.
    We serve the same MPEG-TS body; HLS clients tolerate the mismatch."""
    return await _xtream_live_handler(u, p, stream_id, request, hls=True)

# ── Static HTML loaded once at import (A17) ──────────────────────────────────
def _load_static(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""

def _load_static_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""

LOGIN_HTML     = _load_static(os.path.join(APP_DIR, "login.html"))
UI_HTML        = _load_static(os.path.join(APP_DIR, "ui", "index.html"))
UI_STYLES      = _load_static(os.path.join(APP_DIR, "ui", "styles.css"))
UI_APP_JS      = _load_static(os.path.join(APP_DIR, "ui", "app.js"))
PWA_MANIFEST   = _load_static(os.path.join(APP_DIR, "pwa", "manifest.json"))
_PWA_SW_TEMPLATE = _load_static(os.path.join(APP_DIR, "pwa", "sw.js"))

# Cache-busting: bake content hashes into asset URLs so every deploy
# automatically invalidates browser caches without manual reloads.
_CSS_HASH = hashlib.md5(UI_STYLES.encode()).hexdigest()[:8]
_JS_HASH  = hashlib.md5(UI_APP_JS.encode()).hexdigest()[:8]
UI_HTML = (UI_HTML
           .replace('/ui/styles.css',  f'/ui/styles.css?v={_CSS_HASH}')
           .replace('/ui/app.js',      f'/ui/app.js?v={_JS_HASH}'))

# Service worker cache version: hash of all cacheable assets so any
# code/style change triggers a new SW install → old cache is purged.
_SW_CACHE_VER = "iptv-pwa-" + hashlib.md5(
    (UI_HTML + UI_STYLES + UI_APP_JS + PWA_MANIFEST).encode()
).hexdigest()[:10]
PWA_SW = _PWA_SW_TEMPLATE.replace("__CACHE_VERSION__", _SW_CACHE_VER)

# ── Auth routes ───────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if _check_session(request):
        return HTMLResponse('<meta http-equiv="refresh" content="0;url=/">')
    return HTMLResponse(LOGIN_HTML)

@app.post("/login")
async def do_login(request: Request):
    ip = _client_ip(request)
    # Step 4: blocklist check
    if _is_blocked(ip):
        return Response("Forbidden", status_code=403)
    # Step 4: rate limit — 5 attempts / 60s per IP
    retry_after = _check_login_rate(ip)
    if retry_after is not None:
        return Response("Too Many Requests", status_code=429,
                        headers={"Retry-After": str(retry_after)})
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    cfg = _auth_cfg()
    if not cfg:
        return Response(status_code=302, headers={"Location": "/"})
    stored_hash = cfg.get("password_hash", "")
    ok = username == cfg.get("username") and _verify_admin_password(password, stored_hash)
    if ok and not stored_hash.startswith(("$2b$", "$2a$")):
        # Auto-migrate legacy SHA256 hash to bcrypt on successful login
        full_cfg = load_config()
        full_cfg["auth"]["password_hash"] = _hash_password(password)
        save_config(full_cfg)
        log.info("Migrated admin password from SHA256 to bcrypt")
    if not ok:
        state.log_event("auth", "LOGIN FAILED", f"Failed attempt for '{username}' from {ip}")
        asyncio.create_task(tg_send(tg_alert("🚨", "Failed Login Attempt",
            f"Username tried: <code>{html.escape(username)}</code>\nIP: <code>{html.escape(ip)}</code>")))
        await _record_auth_failure(ip, username, "login_bad_credentials")
        err_html = LOGIN_HTML.replace(
            "<!--ERROR-->",
            '<p style="color:#ff4560;text-align:center;font-size:12px;margin-top:8px;border:1px solid #ff4560;padding:8px">Invalid username or password</p>')
        return HTMLResponse(err_html)
    _record_auth_success(ip)
    token = secrets.token_hex(32)
    hours = cfg.get("session_hours", 24)
    _sessions[token] = time.time() + hours * 3600
    state.log_event("auth", "LOGIN", f"Successful login for '{username}' from {ip}")
    resp = Response(status_code=302, headers={"Location": "/"})
    resp.set_cookie("iptv_session", token, max_age=hours * 3600, httponly=True, samesite="strict")
    return resp

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("iptv_session")
    if token: _sessions.pop(token, None)
    resp = Response(status_code=302, headers={"Location": "/login"})
    resp.delete_cookie("iptv_session")
    return resp

# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return HTMLResponse(UI_HTML, headers={"Cache-Control": "no-store"})

# ── API: Status ───────────────────────────────────────────────────────────────
def _status_dict() -> dict:
    sys = state.health.get("system", {})
    return {
        "device_id": state.device_id, "base_url": state.base_url,
        "total_tuners": state.total_tuners, "total_channels": len(state.channels),
        "total_groups": len(state.all_groups),
        "active_streams": sum(1 for m in state.mux.values() if m._running),
        "total_viewers": sum(m.viewer_count for m in state.mux.values()),
        "uptime_s": int(time.time() - state.started_at),
        "system": sys,
        "filter": {"active": bool(state.allowed_groups), "count": len(state.allowed_groups)},
        "subscriptions": [{"name": s.name, "url": s.url, "max_streams": s.max_streams,
            "active_streams": sum(1 for m in state.mux.values() if m.sub is s and m._running), "load": round(s.load, 2),
            "available": s.available, "priority": s.priority, "enabled": s.enabled,
            "fail_count": s.fail_count,
            "healthy": s.fail_count == 0,
            "account": state.account_info.get(s.name, {})} for s in state.subscriptions],
        "active_channels": [m.to_dict() for m in state.mux.values() if m._running],
    }

@app.get("/api/status")
async def api_status(): return _status_dict()

# ── SSE: live event stream ────────────────────────────────────────────────────
def _health_dict() -> dict:
    return {"system": state.health.get("system", {}),
            "servers": {k: v for k, v in state.health.items() if k.startswith("srv_")},
            "streams": [{"channel": m.ch.name, "stalled": m.is_stalled,
                         "bitrate_kbps": m.bitrate_kbps, "viewers": m.viewer_count}
                        for m in state.mux.values()],
            "alerts": [a.to_dict() for a in reversed(state.alerts[-50:])],
            "channel_count": len(state.channels)}

@app.get("/api/events")
async def sse_events(request: Request):
    """Server-Sent Events stream. Topics: status, health, alert, log, user."""
    async def gen():
        q = await bus.subscribe()
        try:
            # Initial snapshot so the client doesn't have to poll once on connect
            yield f"event: status\ndata: {json.dumps(_status_dict())}\n\n"
            yield f"event: health\ndata: {json.dumps(_health_dict())}\n\n"
            while True:
                if await request.is_disconnected(): break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: {msg['topic']}\ndata: {json.dumps(msg['data'])}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # comment line keeps the connection open
        finally:
            bus.unsubscribe(q)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })

# ── API: Subscriptions ────────────────────────────────────────────────────────
_DEFAULT_PROVIDER_COLORS = ["#00e5ff", "#fbbf24", "#a78bfa", "#34d399", "#fb7185", "#f97316"]

@app.get("/api/subscriptions")
async def get_subs():
    cfg = load_config()
    subs = cfg.get("subscriptions", [])
    colors = cfg.get("provider_colors", {})
    # Merge color into each subscription entry (add default by index if not set)
    result = []
    for i, s in enumerate(subs):
        entry = dict(s)
        entry["color"] = colors.get(s["name"]) or _DEFAULT_PROVIDER_COLORS[i % len(_DEFAULT_PROVIDER_COLORS)]
        result.append(entry)
    return result

@app.post("/api/subscriptions")
async def add_sub(body: SubModel, probe: bool = True):
    cfg = load_config(); subs = cfg.setdefault("subscriptions", [])
    if any(s["name"] == body.name for s in subs): raise HTTPException(409, "Already exists")
    d = body.model_dump()

    # Phase 8: probe the provider before we accept the subscription so Plex
    # never sees a dead source. Pass ?probe=false to skip (bulk imports).
    if probe:
        result = await _probe_subscription(body.name, d["url"], d["username"], d["password"])
        if not result.get("ok"):
            state.log_event("error", "SUB REJECTED",
                            f"{body.name}: probe failed — {result.get('error','unknown')}",
                            sub=body.name)
            raise HTTPException(400, {"error": "probe_failed", "detail": result})

    # Auto-assign priority: first sub = 1, subsequent = 2, 3, etc.
    if not subs:
        d["priority"] = 1
    else:
        d["priority"] = max(s.get("priority", 1) for s in subs) + 1
    d["enabled"] = True
    subs.append(d); save_config(cfg)
    state.reload_from_config(); await state.refresh_channels()
    state.log_event("channel", "SUB ADDED",
                    f"Subscription added: {body.name} (priority {d['priority']})",
                    sub=body.name)
    return {"status": "ok", "priority": d["priority"]}

@app.patch("/api/subscriptions/{name}")
async def patch_subscription(name: str, request: Request):
    body = await request.json()
    cfg = load_config(); subs = cfg.get("subscriptions", [])
    # Credential-touching fields trigger a channel refresh after save.
    CRED_FIELDS = {"url", "username", "password"}
    for s in subs:
        if s["name"] == name:
            if "enabled"    in body: s["enabled"]    = body["enabled"]
            if "priority"   in body: s["priority"]   = int(body["priority"])
            if "max_streams"in body: s["max_streams"] = int(body["max_streams"])
            if "url"        in body: s["url"]        = str(body["url"]).strip()
            if "username"   in body: s["username"]   = str(body["username"]).strip()
            if "password"   in body: s["password"]   = str(body["password"])
            save_config(cfg); state.reload_from_config()
            # Mask secrets from the log message.
            logged = {k: ("***" if k == "password" else v) for k, v in body.items()}
            state.log_event("channel", "SUB UPDATED",
                f"{name}: {', '.join(f'{k}={v}' for k,v in logged.items())}")
            if CRED_FIELDS & body.keys():
                asyncio.create_task(state.refresh_channels())
            return {"status": "ok"}
    raise HTTPException(404, "Not found")

@app.delete("/api/subscriptions/{name}")
async def del_sub(name: str):
    cfg = load_config(); before = len(cfg.get("subscriptions", []))
    cfg["subscriptions"] = [s for s in cfg.get("subscriptions", []) if s["name"] != name]
    if len(cfg["subscriptions"]) == before: raise HTTPException(404, "Not found")
    save_config(cfg); state.reload_from_config()
    # Phase 8: also drop any stored health/probe history for this sub.
    _SUB_HEALTH.pop(name, None)
    _save_sub_health_snapshot()
    state.log_event("channel", "SUB REMOVED",
                    f"Subscription removed: {name}", sub=name)
    return {"status": "ok"}

@app.post("/api/provider-colors")
async def set_provider_colors(body: _ProviderColorBody):
    """Save per-provider hex colors. body.colors = {sub_name: "#rrggbb"}"""
    # Validate: each value must look like a hex color
    for name, color in body.colors.items():
        if not re.match(r'^#[0-9a-fA-F]{3,8}$', color):
            raise HTTPException(400, f"Invalid color for {name!r}: {color!r}")
    cfg = load_config()
    cfg["provider_colors"] = body.colors
    save_config(cfg)
    state.log_event("channel", "PROVIDER COLORS UPDATED", f"{len(body.colors)} colors saved")
    return {"status": "ok", "colors": body.colors}

@app.get("/api/subscriptions/health")
async def subs_health():
    """Phase 8: rich per-subscription health metrics for the dashboard.

    Returns one entry per configured subscription, whether or not it has
    been probed yet. Unprobed subs surface with zeroed counters so the UI
    can still list them."""
    out = []
    # Phase 11: total dispatch volume across all subs (for share %)
    total_streams = sum(s.get("streams", 0) for s in _DISPATCH_STATS.values()) or 1
    for sub in state.subscriptions:
        h = _get_sub_health(sub.name)
        row = h.to_dict(sub)
        row["enabled"]        = sub.enabled
        row["priority"]       = sub.priority
        row["max_streams"]    = sub.max_streams
        row["active_streams"] = sub.active_streams
        # Reuse the account expiration info the dashboard already shows.
        acct = state.account_info.get(sub.name, {}) or {}
        row["account_days_left"] = acct.get("days_left")
        row["account_status"]    = acct.get("status", "")
        # Phase 11: dispatch share + failover counters since process start
        ds = _DISPATCH_STATS.get(sub.name, {}) or {}
        sub_streams = int(ds.get("streams", 0))
        row["dispatch_share"]    = round((sub_streams / total_streams) * 100, 1)
        row["dispatch_streams"]  = sub_streams
        row["failovers_from"]    = int(ds.get("failovers_from", 0))
        row["failovers_to"]      = int(ds.get("failovers_to", 0))
        # Phase 12: drain status — Dispatch tab toggle reads this.
        row["drained"]           = _is_sub_drained(sub.name)
        out.append(row)
    out.sort(key=lambda r: r["score"])  # worst first so issues are obvious
    return {
        "subscriptions": out,
        "dead_channels": len(_DEAD_CHANNELS),
        "live_channels": len(_live_channels()),
        "total_channels": len(state.channels),
    }

@app.post("/api/subscriptions/{name}/probe")
async def probe_sub(name: str):
    """On-demand re-probe of a subscription — useful after the owner rotates
    credentials, so the dashboard can reflect the new state without waiting
    for the next health_monitor cycle."""
    sub = next((s for s in state.subscriptions if s.name == name), None)
    if not sub: raise HTTPException(404, "Not found")
    result = await _probe_subscription(sub.name, sub.url, sub.username, sub.password)
    return {"status": "ok", "probe": result,
            "health": _get_sub_health(name).to_dict(sub)}

# ── API: Groups ───────────────────────────────────────────────────────────────
@app.get("/api/groups")
async def get_groups():
    """Return per-provider raw group catalog with whitelist + new-group flags.
    Falls back to flat state.all_groups when no raw groups cache exists yet."""
    whitelisted = state.allowed_groups
    providers: Dict[str, List[Dict]] = {}
    for sub in state.subscriptions:
        raw = state.raw_groups_by_sub.get(sub.name, [])
        if not raw:
            continue
        new_set = state.new_groups_by_sub.get(sub.name, set())
        providers[sub.name] = [
            {
                "name":       g,
                "whitelisted": not whitelisted or g in whitelisted,
                "is_new":     g in new_set,
            }
            for g in sorted(raw, key=str.lower)
        ]
    # Fallback: if no raw groups caches exist yet (first boot before live fetch),
    # return a single synthetic provider built from state.all_groups.
    if not providers:
        display = state.raw_groups if state.raw_groups else state.all_groups
        providers["(loading — refresh channels to populate)"] = [
            {"name": g, "whitelisted": not whitelisted or g in whitelisted, "is_new": False}
            for g in sorted(display, key=str.lower)
        ]
    return {
        "filter_active": bool(whitelisted),
        "whitelisted":   sorted(whitelisted),
        "providers":     providers,
    }

@app.post("/api/groups")
async def set_groups(body: GroupModel):
    cfg = load_config(); cfg.setdefault("filters", {})["groups"] = body.groups
    save_config(cfg); state.allowed_groups = set(body.groups)
    # Saving the filter is an explicit "I've seen everything" action — clear new flags
    state.new_groups_by_sub.clear()
    await state.refresh_channels()
    state.log_event("channel", "FILTER UPDATED", f"{len(body.groups)} groups selected, {len(state.channels)} channels loaded")
    return {"status": "ok", "groups": len(body.groups), "channels": len(state.channels)}

@app.post("/api/groups/seen")
async def mark_groups_seen():
    """Clear new-group badges — called when user opens the Groups page."""
    state.new_groups_by_sub.clear()
    return {"status": "ok"}

# ── API: Channel Groups (logical group management) ────────────────────────────
@app.get("/api/channel-groups")
async def get_channel_groups():
    """Return logical groups with their channels, in display order."""
    cfg = load_config()
    group_order = cfg.get("group_order") or [
        "AD Sport","BeIN Sports","BeIN AFC / Al-Kass","BeIN Xtra",
        "Thmanayh","BeIN Entertainment","OSN","Alwan","Shahid","STC",
    ]
    assignments = cfg.get("channel_assignments", {})  # key -> group_name

    # Build per-group channel lists from live state
    groups: Dict[str, List[Dict]] = {g: [] for g in group_order}
    unassigned: List[Dict] = []

    # Determine which subscription each channel's primary source belongs to
    sub_color_map = {s["name"]: s.get("color","") for s in cfg.get("subscriptions",[])}

    for key, ch in sorted(state.channels.items(), key=lambda kv: kv[1].name):
        group = assignments.get(key) or ch.group
        entry = {
            "key":       key,
            "name":      ch.name,
            "group":     group,
            "quality":   ch.quality,
            "sub_name":  ch.sub_name or "",
            "color":     sub_color_map.get(ch.sub_name or "", ""),
            "logo":      _logo_url("", key, ch.logo),
            "variant_count": len(ch.variants) if ch.variants else 1,
        }
        if group in groups:
            groups[group].append(entry)
        else:
            unassigned.append(entry)

    return {
        "group_order": group_order,
        "groups": groups,
        "unassigned": unassigned,
    }

@app.post("/api/channel-groups")
async def set_channel_groups(body: _ChannelGroupsBody):
    """Save logical group order and per-channel assignments. Triggers refresh."""
    cfg = load_config()
    cfg["group_order"]        = body.group_order
    cfg["channel_assignments"] = body.assignments
    save_config(cfg)
    # Rebuild channel_groups regex rules from direct assignments so the
    # existing refresh_channels regex engine picks them up correctly.
    existing_regex = [r for r in cfg.get("channel_groups", [])
                      if not r[0].startswith("^__direct__")]
    direct_rules = [[f"^__direct__{re.escape(key)}$", grp]
                    for key, grp in body.assignments.items()]
    cfg["channel_groups"] = direct_rules + existing_regex
    save_config(cfg)
    await state.refresh_channels()
    state.log_event("channel", "GROUPS UPDATED",
                    f"{len(body.group_order)} groups, {len(body.assignments)} direct assignments")
    return {"status": "ok", "groups": len(body.group_order), "channels": len(state.channels)}

# ── API: Blocklist ────────────────────────────────────────────────────────────
@app.get("/api/blocklist")
async def get_blocklist():
    """List all currently active blocked IPs with expiry info."""
    now = time.time()
    return {
        "blocked": [
            {
                "ip": ip,
                "expires_at": int(exp),
                "expires_in_s": max(0, int(exp - now)),
            }
            for ip, exp in sorted(_IP_BLOCKLIST.items())
            if exp > now
        ]
    }

@app.delete("/api/blocklist/{ip}")
async def delete_blocklist_ip(ip: str):
    """Manually unblock an IP address."""
    from fastapi import HTTPException
    if not _is_blocked(ip):
        raise HTTPException(status_code=404, detail=f"{ip} is not in the blocklist")
    async with _BLOCKLIST_LOCK:
        _IP_BLOCKLIST.pop(ip, None)
    _save_blocklist()
    state.log_event("auth", "IP UNBLOCKED", f"ip={ip} reason=admin_api")
    return {"status": "ok", "ip": ip}

# ── API: Streams ──────────────────────────────────────────────────────────────
@app.get("/api/streams")
async def get_streams(): return [m.to_dict() for m in state.mux.values() if m._running]

@app.delete("/api/streams/{key}")
async def kill_stream(key: str):
    mux = state.mux.get(key)
    if not mux: raise HTTPException(404, "No active stream")
    name = mux.ch.name; await mux.kill_all()
    state.add_alert("info", f"Stream killed: {name}")
    state.log_event("stream", "STREAM KILLED", f"{name} manually killed via admin")
    return {"status": "ok", "channel": name}

@app.delete("/api/streams/{key}/sessions/{sid}")
async def kill_stream_viewer(key: str, sid: str):
    """Kill a single viewer session on a channel without affecting other viewers.
    Tries Xtream session first (works for Xtream clients), then queue_id sentinel
    (works for HDHR/Plex viewers who have no Xtream session)."""
    mux = state.mux.get(key)
    if not mux: raise HTTPException(404, "No active stream")
    ok = await _kick_user_session(sid)          # Xtream path
    if not ok:
        ok = await mux.kick_queue_by_id(sid)    # HDHR/Plex path
    if not ok: raise HTTPException(404, "Session not found")
    state.log_event("stream", "VIEWER KICKED",
                    f"Session {sid} kicked from {mux.ch.name} via admin")
    return {"status": "ok", "channel": mux.ch.name, "sid": sid}

# ── Phase 11: Dispatcher observability ───────────────────────────────────────
# These endpoints sit behind the same dashboard auth as the rest of /api/.
# They expose what the StreamDispatcher is currently doing so the admin can
# see *why* a particular sub is being chosen and how often failovers happen.
@app.get("/api/dispatch/decisions")
async def api_dispatch_decisions():
    """Snapshot of currently-cached dispatch decisions. One row per
    (user, channel) pair, including which candidates the dispatcher
    considered, the picked sub, and how many mid-stream failovers have
    happened on this decision."""
    rows = []
    for d in dispatcher.active_decisions():
        ch = state.channels.get(d.channel_key)
        cands = []
        for idx, c in enumerate(d.ordered):
            sub = state.get_subscription(c.sub_name)
            cands.append({
                "sub":          c.sub_name,
                "stream_id":    c.stream_id,
                "quality":      c.quality,
                "fail_count":   c.fail_count,
                "last_fail_ts": int(c.last_fail_ts),
                "active":       idx == 0,
                "available":    bool(sub and sub.enabled and sub.available),
                "drained":      _is_sub_drained(c.sub_name),
                "score":        _get_sub_health(c.sub_name).score(sub) if sub else 0,
            })
        rows.append({
            "channel_key":     d.channel_key,
            "channel_name":    ch.name if ch else d.channel_key,
            "user":            d.user,
            "picked_sub":      d.ordered[0].sub_name if d.ordered else "",
            "picked_at":       int(d.picked_at),
            "reason":          d.reason,
            "candidate_count": len(d.ordered),
            "failover_count":  d.failover_count,
            "candidates":      cands,
        })
    cfg = _dispatch_cfg()
    return {
        "active_decisions": rows,
        "cache_size":       dispatcher.cache_size(),
        "sticky_seconds":   cfg["sticky_seconds"],
        "enabled":          cfg["enabled"],
        # Phase 12: surface currently drained subs alongside live decisions
        # so the Dispatch tab can render badges in one round-trip.
        "drained_subs":     sorted(_DRAINED_SUBS),
    }

@app.get("/api/dispatch/stats")
async def api_dispatch_stats():
    """Lifetime per-sub dispatch counters (since process start)."""
    return {
        "per_sub":              _DISPATCH_STATS,
        "no_candidate_events":  _DISPATCH_NO_CANDIDATE_EVENTS,
        "failover_limit_hits":  _DISPATCH_FAILOVER_LIMIT_HITS,
    }

@app.post("/api/dispatch/invalidate/{channel_key}")
async def api_dispatch_invalidate(channel_key: str):
    """Force the next viewer of `channel_key` to recompute their decision
    instead of using the cached one. Useful after manually fixing a sub."""
    n = dispatcher.invalidate(channel_key)
    state.log_event("stream", "DISPATCH INVALIDATED",
                    f"channel_key={channel_key} cleared={n}")
    return {"status": "ok", "channel_key": channel_key, "cleared": n}

# ── Phase 12: drain a subscription from dispatch ─────────────────────────────
# Ephemeral runtime flag — not persisted. Active streams using a drained sub
# keep running; only new dispatcher picks skip it. Cleared on container restart.
@app.get("/api/dispatch/drained")
async def api_dispatch_drained_list():
    """List subscription names currently drained from dispatch."""
    return {"drained": sorted(_DRAINED_SUBS)}

@app.post("/api/dispatch/drain/{sub_name}")
async def api_dispatch_drain(sub_name: str):
    """Stop the dispatcher from picking this subscription for new streams.
    Existing streams using it continue. Invalidates dispatcher cache so
    waiting viewers re-pick immediately rather than waiting `sticky_seconds`."""
    sub = next((s for s in state.subscriptions if s.name == sub_name), None)
    if sub is None:
        raise HTTPException(404, "subscription not found")
    _DRAINED_SUBS.add(sub_name)
    # Drop every cached decision so the next viewer recomputes — otherwise the
    # sticky cache could keep handing out decisions pointing at the drained sub.
    cleared = 0
    try: cleared = dispatcher.invalidate_all()
    except Exception: pass
    state.log_event("stream", "DISPATCH DRAIN",
                    f"sub={sub_name} cache_cleared={cleared}", sub=sub_name)
    return {"status": "ok", "sub": sub_name,
            "drained": sorted(_DRAINED_SUBS), "cache_cleared": cleared}

@app.post("/api/dispatch/undrain/{sub_name}")
async def api_dispatch_undrain(sub_name: str):
    """Re-allow this subscription for new dispatch decisions. Cache is
    invalidated so waiting viewers can immediately benefit."""
    if sub_name not in _DRAINED_SUBS:
        return {"status": "noop", "sub": sub_name, "drained": sorted(_DRAINED_SUBS)}
    _DRAINED_SUBS.discard(sub_name)
    cleared = 0
    try: cleared = dispatcher.invalidate_all()
    except Exception: pass
    state.log_event("stream", "DISPATCH UNDRAIN",
                    f"sub={sub_name} cache_cleared={cleared}", sub=sub_name)
    return {"status": "ok", "sub": sub_name,
            "drained": sorted(_DRAINED_SUBS), "cache_cleared": cleared}

# ── Phase 10: API: Xtream Codes Users ────────────────────────────────────────
# Dashboard-protected admin surface for managing Xtream user accounts. Auth is
# enforced by AuthMiddleware (_check_session) since all paths are under /api/.
# Passwords are stored and returned in plaintext — Xtream clients send creds
# in URL query params, so server-side hashing would break the protocol.
class _UserCreateBody(BaseModel):
    username:              str
    password:              Optional[str]      = None  # auto-generated if omitted
    enabled:               Optional[bool]     = True
    max_connections:       Optional[int]      = None
    expires_at:            Optional[float]    = None  # epoch seconds; 0 = never
    expiry_days:           Optional[int]      = None  # alternative to expires_at
    allowed_groups:        Optional[List[str]] = None
    allowed_channels:      Optional[List[str]] = None
    allowed_subscriptions: Optional[List[str]] = None
    dispatch_preference:   Optional[str]      = None
    notes:                 Optional[str]      = ""

class _UserPatchBody(BaseModel):
    password:              Optional[str]      = None
    enabled:               Optional[bool]     = None
    max_connections:       Optional[int]      = None
    expires_at:            Optional[float]    = None
    expiry_days:           Optional[int]      = None
    allowed_groups:        Optional[List[str]] = None
    allowed_channels:      Optional[List[str]] = None
    allowed_subscriptions: Optional[List[str]] = None
    dispatch_preference:   Optional[str]      = None
    notes:                 Optional[str]      = None

def _gen_random_password(length: int = 12) -> str:
    """URL-safe random password for the regenerate endpoint and auto-create."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

@app.get("/api/users")
async def api_users_list():
    """List all Xtream Codes users (passwords included — admin-only surface
    behind dashboard auth, and clients need them to build M3U URLs)."""
    return {
        "total":   len(state.users),
        "users":   [u.to_dict(include_password=True) for u in state.users],
        "active_sessions": len(_USER_SESSIONS),
    }

@app.post("/api/users")
async def api_users_create(body: _UserCreateBody):
    """Create a new Xtream user. Username must be unique (case-insensitive).
    Password is generated if not supplied."""
    username = (body.username or "").strip()
    if not username:
        raise HTTPException(400, "username is required")
    if _find_user(username) is not None:
        raise HTTPException(409, f"user '{username}' already exists")
    cfg = _xtream_cfg()
    plaintext_password = (body.password or "").strip() or _gen_random_password()
    password = _hash_password(plaintext_password)

    expires_at = 0.0
    if body.expires_at:
        expires_at = float(body.expires_at)
    elif body.expiry_days is not None:
        if body.expiry_days > 0:
            expires_at = time.time() + (body.expiry_days * 86400)
    else:
        days = cfg["default_user_expiry_days"]
        if days > 0:
            expires_at = time.time() + (days * 86400)

    user = User(
        username              = username,
        password              = password,
        enabled               = bool(True if body.enabled is None else body.enabled),
        created_at            = time.time(),
        expires_at            = expires_at,
        max_connections       = int(body.max_connections or cfg["default_max_connections"]),
        allowed_groups        = list(body.allowed_groups        or []),
        allowed_channels      = list(body.allowed_channels      or []),
        allowed_subscriptions = list(body.allowed_subscriptions or []),
        dispatch_preference   = (body.dispatch_preference or "").strip(),
        notes                 = (body.notes or "").strip(),
    )
    state.users.append(user)
    _save_users(state.users)
    state.log_event("xtream", "USER CREATED",
                    f"username={user.username} max_conn={user.max_connections}")
    # Optional Item 5: Telegram alert on new user provisioning. Built inline
    # (not via tg_alert) so the admin can copy/paste the credentials block
    # straight to the customer — no extra footer line.
    if cfg.get("user_alerts_enabled", True):
        exp_str = (time.strftime('%Y-%m-%d', time.localtime(expires_at))
                   if expires_at else "no expiry")
        url_value = (cfg["server_url"] or state.base_url or "").rstrip("/") or "—"
        sep = "─" * 24
        msg = (
            f"👤 <b>Xtream User Created</b>\n"
            f"{sep}\n"
            f"<b>Username:</b>  <code>{html.escape(user.username)}</code>\n"
            f"<b>Password:</b>  <code>{html.escape(plaintext_password)}</code>\n"
            f"<b>URL:</b>       <code>{html.escape(url_value)}</code>\n"
            f"<b>Expiry:</b>    {html.escape(exp_str)}\n"
            f"{sep}"
        )
        asyncio.create_task(tg_send(msg))
    _publish_user_event("created", user.username,
                        max_connections=user.max_connections,
                        expires_at=int(expires_at))
    resp = user.to_dict(include_password=False)
    resp["password"] = plaintext_password
    return {"status": "ok", "user": resp}

@app.patch("/api/users/{username}")
async def api_users_patch(username: str, body: _UserPatchBody):
    """Mutate an existing user. Any provided field replaces the current value."""
    user = _find_user(username)
    if user is None:
        raise HTTPException(404, "user not found")
    changed: List[str] = []
    kicked  = 0
    if body.password is not None and body.password.strip():
        user.password = _hash_password(body.password.strip())
        changed.append("password")
        # Password change kicks active sessions — old creds must stop working.
        kicked += await _kick_all_user_sessions(user.username)
    if body.enabled is not None:
        new_enabled = bool(body.enabled)
        if user.enabled and not new_enabled:
            kicked += await _kick_all_user_sessions(user.username)
        user.enabled = new_enabled
        changed.append("enabled")
    if body.max_connections is not None:
        user.max_connections = max(1, int(body.max_connections))
        changed.append("max_connections")
    if body.expires_at is not None:
        user.expires_at = float(body.expires_at)
        changed.append("expires_at")
    elif body.expiry_days is not None:
        user.expires_at = (time.time() + body.expiry_days * 86400) if body.expiry_days > 0 else 0.0
        changed.append("expires_at")
    if body.allowed_groups is not None:
        user.allowed_groups = list(body.allowed_groups)
        changed.append("allowed_groups")
    if body.allowed_channels is not None:
        user.allowed_channels = list(body.allowed_channels)
        changed.append("allowed_channels")
    if body.allowed_subscriptions is not None:
        user.allowed_subscriptions = list(body.allowed_subscriptions)
        changed.append("allowed_subscriptions")
        # Dispatcher decisions are now stale for this user
        try: dispatcher.invalidate_all()
        except Exception: pass
    if body.dispatch_preference is not None:
        user.dispatch_preference = body.dispatch_preference.strip()
        changed.append("dispatch_preference")
    if body.notes is not None:
        user.notes = body.notes
        changed.append("notes")
    if changed:
        _save_users(state.users)
        state.log_event("xtream", "USER UPDATED",
                        f"username={user.username} fields={','.join(changed)}"
                        + (f" kicked={kicked}" if kicked else ""))
        _publish_user_event("updated", user.username,
                            fields=changed, kicked=kicked)
    return {"status": "ok",
            "user": user.to_dict(include_password=True),
            "changed": changed,
            "kicked_sessions": kicked}

@app.post("/api/users/{username}/regenerate_password")
async def api_users_regen_password(username: str):
    """Generate a fresh random password for a user and kick any open sessions
    so the old credentials stop working immediately."""
    user = _find_user(username)
    if user is None:
        raise HTTPException(404, "user not found")
    plaintext = _gen_random_password()
    user.password = _hash_password(plaintext)
    kicked = await _kick_all_user_sessions(user.username)
    _save_users(state.users)
    state.log_event("xtream", "USER PASSWORD RESET",
                    f"username={user.username} kicked={kicked}")
    _publish_user_event("password_reset", user.username, kicked=kicked)
    return {"status": "ok",
            "username": user.username,
            "password": plaintext,
            "kicked_sessions": kicked}

@app.delete("/api/users/{username}")
async def api_users_delete(username: str):
    """Remove a user and immediately kick all of their active sessions."""
    user = _find_user(username)
    if user is None:
        raise HTTPException(404, "user not found")
    kicked = await _kick_all_user_sessions(user.username)
    state.users = [u for u in state.users if u.username.lower() != username.lower()]
    _save_users(state.users)
    state.log_event("xtream", "USER DELETED",
                    f"username={user.username} sessions_kicked={kicked}")
    _publish_user_event("deleted", user.username, kicked=kicked)
    return {"status": "ok", "kicked_sessions": kicked}

@app.get("/api/users/{username}")
async def api_users_get(username: str):
    """Phase 12: singular user fetch for the admin Users drawer.

    Returns the persisted user record + lifetime runtime counters
    (`total_streams`, `bytes_served`, `last_seen_*`) + the current active
    session list. Note: 24h-windowed aggregates aren't available for Xtream
    users — the analytics SQLite tracks Plex viewers, and Xtream session
    history only lives in `/config/app.log`. The drawer surfaces lifetime
    counters since process start; precise 24h windowing would require either
    log file parsing or a new analytics table (deferred).
    """
    user = _find_user(username)
    if user is None:
        raise HTTPException(404, "user not found")
    u_lower = user.username.lower()
    sessions = [s.to_dict() for s in _USER_SESSIONS.values()
                if s.username.lower() == u_lower]
    return {
        "user":     user.to_dict(include_password=True),
        "sessions": sessions,
        "session_count": len(sessions),
        # Lifetime runtime counters (in-memory; reset on container restart)
        "total_streams":  int(user.total_streams),
        "bytes_served":   int(user.bytes_served),
        "last_seen_ts":   int(user.last_seen_ts),
        "last_seen_ip":   user.last_seen_ip,
    }

@app.get("/api/users/{username}/sessions")
async def api_users_sessions(username: str):
    """Active live streams for a single user."""
    user = _find_user(username)
    if user is None:
        raise HTTPException(404, "user not found")
    u_lower = user.username.lower()
    sessions = [s.to_dict() for s in _USER_SESSIONS.values()
                if s.username.lower() == u_lower]
    return {"username": user.username, "count": len(sessions), "sessions": sessions}

@app.get("/api/sessions")
async def api_sessions_all():
    """Every active Xtream session across all users — for the admin overview."""
    return {
        "total":    len(_USER_SESSIONS),
        "sessions": [s.to_dict() for s in _USER_SESSIONS.values()],
    }

@app.post("/api/users/{username}/kick/{sid}")
async def api_users_kick(username: str, sid: str):
    """Kick a single session by sid. The streaming loop tears down on next chunk."""
    sess = _USER_SESSIONS.get(sid)
    if sess is None or sess.username.lower() != username.lower():
        raise HTTPException(404, "session not found")
    ok = await _kick_user_session(sid)
    state.log_event("xtream", "SESSION KICKED",
                    f"username={username} sid={sid}")
    return {"status": "ok" if ok else "noop", "sid": sid}

@app.post("/api/users/{username}/kick")
async def api_users_kick_all(username: str):
    """Kick every active session for a user in one shot."""
    user = _find_user(username)
    if user is None:
        raise HTTPException(404, "user not found")
    n = await _kick_all_user_sessions(user.username)
    state.log_event("xtream", "USER KICK ALL",
                    f"username={user.username} count={n}")
    return {"status": "ok", "kicked_sessions": n}

# ── Phase 12: bulk user actions ──────────────────────────────────────────────
# All bulk endpoints accept {"usernames": [...]} and return per-user
# success/failure rows so the UI can highlight partial completions. Each
# endpoint emits a single BULK USER log event for the audit trail.
class _UserBulkBody(BaseModel):
    usernames: List[str]
    days:      Optional[int] = None  # only used by /bulk/extend

def _bulk_resolve(usernames: List[str]) -> Tuple[List["User"], List[str]]:
    found:   List["User"] = []
    missing: List[str]    = []
    for u in usernames or []:
        user = _find_user(u)
        if user is None: missing.append(u)
        else:            found.append(user)
    return found, missing

@app.post("/api/users/bulk/disable")
async def api_users_bulk_disable(body: _UserBulkBody):
    found, missing = _bulk_resolve(body.usernames)
    kicked_total = 0
    for u in found:
        if u.enabled:
            kicked_total += await _kick_all_user_sessions(u.username)
        u.enabled = False
    if found: _save_users(state.users)
    state.log_event("xtream", "BULK USER DISABLE",
                    f"count={len(found)} kicked={kicked_total} missing={len(missing)}")
    _publish_user_event("bulk", "", action="disable",
                        usernames=[u.username for u in found],
                        kicked=kicked_total)
    return {"status": "ok", "updated": [u.username for u in found],
            "missing": missing, "kicked_sessions": kicked_total}

@app.post("/api/users/bulk/enable")
async def api_users_bulk_enable(body: _UserBulkBody):
    found, missing = _bulk_resolve(body.usernames)
    for u in found: u.enabled = True
    if found: _save_users(state.users)
    state.log_event("xtream", "BULK USER ENABLE",
                    f"count={len(found)} missing={len(missing)}")
    _publish_user_event("bulk", "", action="enable",
                        usernames=[u.username for u in found])
    return {"status": "ok", "updated": [u.username for u in found], "missing": missing}

@app.post("/api/users/bulk/extend")
async def api_users_bulk_extend(body: _UserBulkBody):
    if not body.days or body.days <= 0:
        raise HTTPException(400, "days must be a positive integer")
    found, missing = _bulk_resolve(body.usernames)
    delta = body.days * 86400
    now = time.time()
    for u in found:
        # If the user has no expiry, "extend" means start a fresh expiry
        # window from now. If they're already expired, also restart from now
        # (matches the way the admin would expect "+30d" to work).
        base = max(u.expires_at, now) if u.expires_at else now
        u.expires_at = base + delta
    if found: _save_users(state.users)
    state.log_event("xtream", "BULK USER EXTEND",
                    f"count={len(found)} days={body.days} missing={len(missing)}")
    _publish_user_event("bulk", "", action="extend",
                        usernames=[u.username for u in found], days=body.days)
    return {"status": "ok", "updated": [u.username for u in found],
            "missing": missing, "days": body.days}

@app.post("/api/users/bulk/delete")
async def api_users_bulk_delete(body: _UserBulkBody):
    found, missing = _bulk_resolve(body.usernames)
    kicked_total = 0
    for u in found:
        kicked_total += await _kick_all_user_sessions(u.username)
    keep_lower = {u.username.lower() for u in found}
    state.users = [u for u in state.users if u.username.lower() not in keep_lower]
    if found: _save_users(state.users)
    state.log_event("xtream", "BULK USER DELETE",
                    f"count={len(found)} kicked={kicked_total} missing={len(missing)}")
    state.add_alert("warning", f"Bulk delete: {len(found)} user(s) removed")
    _publish_user_event("bulk", "", action="delete",
                        usernames=[u.username for u in found],
                        kicked=kicked_total)
    return {"status": "ok", "deleted": [u.username for u in found],
            "missing": missing, "kicked_sessions": kicked_total}

@app.post("/api/users/bulk/kick")
async def api_users_bulk_kick(body: _UserBulkBody):
    found, missing = _bulk_resolve(body.usernames)
    total = 0
    for u in found:
        total += await _kick_all_user_sessions(u.username)
    state.log_event("xtream", "BULK USER KICK",
                    f"count={len(found)} kicked={total} missing={len(missing)}")
    _publish_user_event("bulk", "", action="kick",
                        usernames=[u.username for u in found], kicked=total)
    return {"status": "ok", "kicked_sessions": total,
            "missing": missing, "users": [u.username for u in found]}

# ── API: Health ───────────────────────────────────────────────────────────────
@app.get("/api/health")
async def api_health(): return _health_dict()

@app.get("/health")
async def health():
    """E2: unauthenticated liveness probe for Docker HEALTHCHECK and deploy.sh.
    Returns 200 when the app is up and has at least one channel loaded.
    Replaces /discover.json which only tests the HDHR emulator layer."""
    ok = bool(state.channels)
    return JSONResponse({"ok": ok, "channels": len(state.channels)},
                        status_code=200 if ok else 503)

@app.delete("/api/alerts")
async def clear_alerts(): state.alerts.clear(); return {"status": "ok"}

# ── API: Logs ─────────────────────────────────────────────────────────────────
@app.get("/api/logs")
async def api_logs(level: str = "", limit: int = 200,
                   subscription: str = "", event: str = "",
                   q: str = ""):
    """Recent log entries with optional filters.

    level        — exact level match (info/warn/error/stream/channel/...)
    subscription — exact sub name match (Phase 8 sub tag)
    event        — substring match against the event code
    q            — free-text substring match against detail
    """
    entries = list(reversed(state.logs[-500:]))
    if level:        entries = [e for e in entries if e.level == level]
    if subscription: entries = [e for e in entries if e.sub == subscription]
    if event:
        el = event.lower()
        entries = [e for e in entries if el in e.event.lower()]
    if q:
        ql = q.lower()
        entries = [e for e in entries
                   if ql in e.detail.lower() or ql in e.event.lower()]
    return [e.to_dict() for e in entries[:limit]]

@app.delete("/api/logs")
async def clear_logs(): state.logs.clear(); return {"status": "ok"}

# ── Reload ────────────────────────────────────────────────────────────────────
@app.post("/api/telegram/test")
async def telegram_test():
    await tg_send(tg_alert("🔔", "Test Message", "Telegram integration is working correctly!"))
    return {"status": "ok", "message": "Test message sent"}

@app.get("/api/telegram")
async def get_telegram():
    cfg = load_config().get("telegram", {})
    return {"enabled":          cfg.get("enabled", False),
            "chat_id":           cfg.get("chat_id", ""),
            "daily_summary_hour":cfg.get("daily_summary_hour", 8),
            "cpu_alert_pct":     cfg.get("cpu_alert_pct", 85),
            "cpu_critical_pct":  cfg.get("cpu_critical_pct", 95),
            "mem_alert_pct":     cfg.get("mem_alert_pct", 90),
            "has_token":         bool(cfg.get("bot_token"))}

@app.post("/api/telegram")
async def update_telegram(request: Request):
    body = await request.json()
    cfg = load_config()
    tg = cfg.setdefault("telegram", {})
    conn_changed = False
    if "enabled" in body: tg["enabled"] = body["enabled"]
    if "daily_summary_hour" in body: tg["daily_summary_hour"] = int(body["daily_summary_hour"])
    if "bot_token" in body and body["bot_token"]:
        conn_changed = conn_changed or (tg.get("bot_token") != body["bot_token"])
        tg["bot_token"] = body["bot_token"]
    if "chat_id" in body and body["chat_id"]:
        conn_changed = conn_changed or (tg.get("chat_id") != body["chat_id"])
        tg["chat_id"] = body["chat_id"]
    if "cpu_alert_pct"    in body: tg["cpu_alert_pct"]    = int(body["cpu_alert_pct"])
    if "cpu_critical_pct" in body: tg["cpu_critical_pct"] = int(body["cpu_critical_pct"])
    if "mem_alert_pct"    in body: tg["mem_alert_pct"]    = int(body["mem_alert_pct"])
    save_config(cfg)
    if conn_changed:
        asyncio.create_task(_restart_tg_loop())
    state.log_event("system", "TELEGRAM UPDATED", "Telegram settings updated via admin GUI")
    return {"status": "ok", "restarted": conn_changed}

# ── API: Admin credentials ────────────────────────────────────────────────────
@app.post("/api/admin/password")
async def change_admin_password(request: Request):
    """Change the dashboard admin password. Requires current password for verification.
    Forces all active sessions to re-login after a successful change."""
    body = await request.json()
    current  = body.get("current", "")
    new_pw   = body.get("new_password", "")
    if not new_pw or len(new_pw) < 4:
        raise HTTPException(400, "New password must be at least 4 characters")
    cfg = load_config()
    stored = cfg.get("auth", {}).get("password_hash", "")
    if not _verify_admin_password(current, stored):
        raise HTTPException(403, "Current password is incorrect")
    cfg["auth"]["password_hash"] = _hash_password(new_pw)
    save_config(cfg)  # triggers session clear via save_config's _LAST_ADMIN_PW_HASH check
    state.log_event("system", "ADMIN PASSWORD CHANGED", "Admin password changed via web UI")
    return {"status": "ok"}

@app.post("/api/admin/restart")
async def admin_restart(background_tasks: BackgroundTasks):
    """Gracefully restart the container. Blocks new streams immediately, waits up to
    shutdown_drain_seconds for active streams to finish, then sends SIGTERM so Docker
    restarts the container via its restart policy (unless-stopped)."""
    active = sum(1 for m in state.mux.values() if m._running)
    drain  = _health_cfg()["shutdown_drain_seconds"]
    state.log_event("system", "RESTART REQUESTED",
                    f"Admin triggered restart — {active} active stream(s), drain up to {drain}s")
    asyncio.create_task(tg_send(tg_alert("🔄", "IPTV Tuner Restarting",
        f"Admin triggered restart via web UI\nActive streams: {active} (drain up to {drain}s)")))
    def _do_restart():
        time.sleep(0.3)          # let the HTTP response reach the browser first
        os.kill(os.getpid(), signal.SIGTERM)
    background_tasks.add_task(_do_restart)
    return {"ok": True, "active_streams": active, "drain_seconds": drain}

@app.post("/api/admin/username")
async def change_admin_username(request: Request):
    """Change the dashboard admin username. Forces all active sessions to re-login."""
    body = await request.json()
    new_username = body.get("username", "").strip()
    if not new_username:
        raise HTTPException(400, "Username is required")
    cfg = load_config()
    cfg.setdefault("auth", {})["username"] = new_username
    save_config(cfg)
    _sessions.clear()
    state.log_event("system", "ADMIN USERNAME CHANGED",
                    f"Admin username changed to '{new_username}' via web UI")
    return {"status": "ok"}

# ── API: Config backups (Phase 9) ────────────────────────────────────────────
@app.get("/api/config/backups")
async def list_config_backups():
    """List all timestamped config snapshots in BACKUP_DIR, newest first."""
    entries = []
    try:
        for name in os.listdir(BACKUP_DIR):
            if name.startswith("config-") and name.endswith(".json"):
                p = os.path.join(BACKUP_DIR, name)
                try:
                    st = os.stat(p)
                    entries.append({
                        "filename": name,
                        "size":     st.st_size,
                        "mtime":    int(st.st_mtime),
                        "time":     datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    })
                except OSError:
                    pass
    except FileNotFoundError:
        pass
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return {"backups": entries, "count": len(entries),
            "keep": _ops_cfg()["backup_count"], "dir": BACKUP_DIR}

@app.post("/api/config/restore/{filename}")
async def restore_config_backup(filename: str):
    """Atomically restore a config snapshot. A pre-restore backup of the current
    config is taken first so the user can undo. Triggers a channel refresh."""
    safe_name = os.path.basename(filename)
    if (safe_name != filename
            or not safe_name.startswith("config-")
            or not safe_name.endswith(".json")):
        raise HTTPException(400, "Invalid backup filename")
    src = os.path.join(BACKUP_DIR, safe_name)
    if not os.path.exists(src):
        raise HTTPException(404, "Backup not found")
    # Snapshot the current config first — lets the user undo the restore.
    try:
        with open(CONFIG_FILE) as f:
            current = json.load(f)
        _backup_config_snapshot(current)
    except Exception as exc:
        log.warning("pre-restore backup failed: %s", _redact(exc))
    try:
        with open(src) as f:
            restored = json.load(f)
    except Exception as exc:
        raise HTTPException(400, f"Backup unreadable: {exc}")
    save_config(restored)  # atomic write + auto-backup of the newly-restored state
    state.reload_from_config()
    await state.refresh_channels()
    state.log_event("system", "CONFIG RESTORED", f"Restored from {safe_name}")
    return {"status": "ok", "restored": safe_name}

@app.get("/api/account-info")
async def api_account_info():
    return state.account_info

@app.post("/api/account-info/refresh")
async def refresh_account_info():
    await state.fetch_account_info()
    return {"status": "ok", "accounts": len(state.account_info)}

# ── PWA Routes ───────────────────────────────────────────────────────────────
@app.get("/app", response_class=HTMLResponse)
async def pwa_app(request: Request):
    if not _check_session(request):
        return HTMLResponse('<meta http-equiv="refresh" content="0;url=/login">')
    return HTMLResponse(UI_HTML, headers={"Cache-Control": "no-store"})

@app.get("/app/manifest.json")
async def pwa_manifest():
    return Response(content=PWA_MANIFEST,
                    media_type="application/manifest+json",
                    headers={"Cache-Control": "public, max-age=3600"})

@app.get("/app/sw.js")
async def pwa_sw():
    return Response(content=PWA_SW,
                    media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})

# PWA icons — cached bytes at import time
_PWA_ICONS: Dict[str, bytes] = {}
for _sz in ("192", "512"):
    _b = _load_static_bytes(os.path.join(APP_DIR, "pwa", f"icon-{_sz}.png"))
    if _b: _PWA_ICONS[_sz] = _b

@app.get("/app/icon-{size}.png")
async def pwa_icon(size: str):
    data = _PWA_ICONS.get(size)
    if not data: raise HTTPException(404)
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})

_PWA_ICON_SVG = _load_static(os.path.join(APP_DIR, "pwa", "icon.svg"))

@app.get("/app/icon.svg")
async def pwa_icon_svg():
    if not _PWA_ICON_SVG: raise HTTPException(404)
    return Response(content=_PWA_ICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})

# UI fonts — cached bytes at import time (self-hosted Clash Display, Cabinet Grotesk, DM Mono)
_UI_FONTS: Dict[str, bytes] = {}
_FONT_DIR = os.path.join(APP_DIR, "ui", "fonts")
if os.path.isdir(_FONT_DIR):
    for _fn in os.listdir(_FONT_DIR):
        if _fn.endswith(".woff2"):
            _fb = _load_static_bytes(os.path.join(_FONT_DIR, _fn))
            if _fb: _UI_FONTS[_fn] = _fb

@app.get("/ui/fonts/{name}")
async def ui_font(name: str):
    # Hard allowlist: only known self-hosted font filenames (blocks path traversal)
    data = _UI_FONTS.get(name)
    if not data: raise HTTPException(404)
    return Response(content=data, media_type="font/woff2",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})

@app.get("/ui/styles.css")
async def ui_styles():
    return Response(content=UI_STYLES, media_type="text/css",
                    headers={"Cache-Control": "public, max-age=3600"})

@app.get("/ui/app.js")
async def ui_app_js():
    return Response(content=UI_APP_JS, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=3600"})

# ── Analytics API ────────────────────────────────────────────────────────────
@app.get("/api/analytics/summary")
async def analytics_summary(days: int = 7):
    cutoff = time.time() - days * 86400
    try:
        with db_connect() as conn:
            # Top channels
            top_channels = conn.execute("""
                SELECT channel, COUNT(*) as sessions,
                       SUM(COALESCE(duration_s,0)) as total_s,
                       COUNT(DISTINCT viewer_ip) as unique_viewers
                FROM watch_sessions WHERE started_at > ? AND ended_at IS NOT NULL
                GROUP BY channel ORDER BY total_s DESC LIMIT 10
            """, (cutoff,)).fetchall()

            # Top users
            top_users = conn.execute("""
                SELECT COALESCE(plex_user, viewer_ip) as user,
                       COUNT(*) as sessions,
                       SUM(COALESCE(duration_s,0)) as total_s,
                       COUNT(DISTINCT channel) as channels_watched
                FROM watch_sessions WHERE started_at > ? AND ended_at IS NOT NULL
                GROUP BY plex_user ORDER BY total_s DESC LIMIT 10
            """, (cutoff,)).fetchall()

            # Hourly heatmap (0-23)
            heatmap = conn.execute("""
                SELECT CAST(strftime('%H', datetime(started_at,'unixepoch')) AS INTEGER) as hour,
                       COUNT(*) as sessions
                FROM watch_sessions WHERE started_at > ?
                GROUP BY hour ORDER BY hour
            """, (cutoff,)).fetchall()

            # Recent sessions
            recent = conn.execute("""
                SELECT channel, COALESCE(plex_user, viewer_ip) as user,
                       viewer_ip, started_at, ended_at,
                       COALESCE(duration_s,0) as duration_s,
                       COALESCE(bytes_total,0) as bytes_total
                FROM watch_sessions WHERE started_at > ?
                ORDER BY started_at DESC LIMIT 50
            """, (cutoff,)).fetchall()

            # Totals
            totals = conn.execute("""
                SELECT COUNT(*) as total_sessions,
                       SUM(COALESCE(duration_s,0)) as total_watch_s,
                       COUNT(DISTINCT viewer_ip) as unique_ips,
                       COUNT(DISTINCT channel) as unique_channels
                FROM watch_sessions WHERE started_at > ? AND ended_at IS NOT NULL
            """, (cutoff,)).fetchone()

            hour_map = {r["hour"]: r["sessions"] for r in heatmap}

            return {
                "days": days,
                "totals": dict(totals) if totals else {},
                "top_channels": [dict(r) for r in top_channels],
                "top_users":    [dict(r) for r in top_users],
                "heatmap":      [{"hour": h, "sessions": hour_map.get(h, 0)} for h in range(24)],
                "recent":       [dict(r) for r in recent],
                "ip_user_map":  dict(conn.execute("SELECT ip, plex_user FROM ip_user_map").fetchall()),
            }
    except Exception as exc:
        log.error("Analytics query failed: %s", exc)
        raise HTTPException(500, str(exc))

@app.delete("/api/analytics")
async def clear_analytics():
    try:
        with db_connect() as conn:
            conn.execute("DELETE FROM watch_sessions")
            conn.execute("DELETE FROM ip_user_map")
            conn.execute("DELETE FROM daily_bandwidth")  # A12
        _active_sessions.clear()
        _ip_user_cache.clear()
        state.log_event("system", "ANALYTICS CLEARED", "Watch history + bandwidth cleared via admin")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(500, str(exc))

@app.get("/api/quality")
async def get_quality():
    cfg = load_config()
    return {"preference": cfg.get("quality_preference", ["HD","FHD","4K","SD","HEVC"])}

@app.post("/api/quality")
async def set_quality(request: Request):
    body = await request.json()
    pref = body.get("preference", ["HD","FHD","4K","SD","HEVC"])
    cfg = load_config(); cfg["quality_preference"] = pref; save_config(cfg)
    asyncio.create_task(state.refresh_channels())
    state.log_event("channel", "QUALITY UPDATED", f"Preference: {' > '.join(pref)}")
    return {"status": "ok", "preference": pref}

@app.post("/api/quality/merges")
async def save_quality_merges(request: Request):
    """Save manual channel merges and renames, apply to channel list."""
    body = await request.json()
    merges  = body.get("merges", {})   # {target_name: [absorbed_names]}
    renames = body.get("renames", {})  # {old_name: new_name}

    cfg = load_config()
    cfg.setdefault("channel_merges", {}).update(merges)
    cfg.setdefault("channel_renames", {}).update(renames)
    save_config(cfg)

    # Apply renames + merges to live channel state
    changed = 0
    # Apply renames
    for key, ch in list(state.channels.items()):
        if ch.name in renames:
            ch.name = renames[ch.name]
            changed += 1
    # Apply manual merges: move variants + candidates from absorbed → target
    qpref = cfg.get("quality_preference", ["HD","FHD","4K","SD","HEVC"])
    for target_name, absorbed_names in merges.items():
        target_ch = next((c for c in state.channels.values() if c.name == target_name), None)
        if not target_ch: continue
        for abs_name in absorbed_names:
            abs_key = next((k for k,c in state.channels.items() if c.name == abs_name), None)
            if not abs_key: continue
            abs_ch = state.channels[abs_key]
            if target_ch.variants is None: target_ch.variants = []
            for v in (abs_ch.variants or [QualityVariant(
                    stream_id=abs_ch.stream_id, quality=abs_ch.quality,
                    priority=QPRIO.get(abs_ch.quality, 20), orig_name=abs_ch.name,
                    sub_name=abs_ch.sub_name or "")]):
                if not any(ev.stream_id == v.stream_id for ev in target_ch.variants):
                    target_ch.variants.append(v)
            # Merge candidates so dispatcher sees all stream sources
            for c in abs_ch.candidates:
                if not any(tc.sub_name == c.sub_name and tc.stream_id == c.stream_id
                           for tc in target_ch.candidates):
                    target_ch.candidates.append(c)
            # Re-elect best stream per quality preference
            def _pref(q, _qp=qpref): return _qp.index(q) if q in _qp else 99
            best = min(target_ch.variants,
                       key=lambda v: (_pref(v.quality), -v.priority))
            target_ch.stream_id = best.stream_id
            target_ch.quality   = best.quality
            del state.channels[abs_key]
            changed += 1

    state.log_event("channel", "MERGES SAVED",
        f"{len(merges)} merges, {len(renames)} renames applied")
    return {"status": "ok", "changes": changed}

@app.post("/api/quality/unmerge")
async def unmerge_channel(request: Request):
    """Split a merged channel back into individual quality streams."""
    body = await request.json()
    name = body.get("channel", "")
    ch_key = next((k for k,c in state.channels.items() if c.name == name), None)
    if not ch_key:
        raise HTTPException(404, f"Channel not found: {name}")
    ch = state.channels[ch_key]
    if not ch.variants or len(ch.variants) <= 1:
        return {"status": "ok", "message": "Nothing to unmerge"}
    # Re-create individual channels from variants
    for v in ch.variants:
        new_key = hashlib.md5(v.orig_name.lower().encode()).hexdigest()[:10]
        state.channels[new_key] = Channel(
            id=new_key, name=v.orig_name, number=ch.number,
            logo=ch.logo, group=ch.group, stream_id=v.stream_id,
            quality=v.quality, variants=[v])
    del state.channels[ch_key]
    state.log_event("channel", "UNMERGED", f"{name} split into {len(ch.variants)} channels")
    return {"status": "ok", "split_into": len(ch.variants)}

@app.get("/api/quality/overrides")
async def get_quality_overrides():
    """Get per-channel quality overrides."""
    cfg = load_config()
    return cfg.get("quality_overrides", {})

@app.post("/api/quality/overrides")
async def set_quality_overrides(body: _QualityOverridesBody):
    """Set per-channel quality override. channel_name -> quality label (or null to clear)."""
    overrides = body.overrides   # {channel_name: "HD"|"FHD"|"4K"|"SD"|"HEVC"|null}

    cfg = load_config()
    current = cfg.get("quality_overrides", {})

    for ch_name, quality in overrides.items():
        if quality is None:
            current.pop(ch_name, None)
        else:
            current[ch_name] = quality

    cfg["quality_overrides"] = current
    save_config(cfg)

    # Apply immediately to live channels. Newly applied overrides update the
    # Channel config so the next fresh tune uses the new variant; existing
    # running muxes keep playing until Plex re-opens the stream on its own.
    applied = 0
    for key, ch in state.channels.items():
        if ch.name in current:
            wanted = current[ch.name]
            if not ch.variants: continue
            match = next((v for v in ch.variants if v.quality == wanted), None)
            if not match: continue
            already_configured = (ch.stream_id == match.stream_id)
            ch.stream_id = match.stream_id
            ch.quality   = match.quality
            if not already_configured:
                applied += 1

    state.log_event("channel", "QUALITY OVERRIDES",
        f"{len(current)} overrides, {applied} applied live")
    return {"status": "ok", "overrides": current, "applied": applied}

@app.get("/api/epg-info")
async def epg_info():
    """EPG status + URLs for dashboard display. The guide is fully synthetic —
    real XMLTV ingestion was removed in Item 8a cleanup."""
    return {
        "epg_url":       f"{state.base_url}/epg",
        "epg_xml_url":   f"{state.base_url}/epg.xml",
        "epg_gz_url":    f"{state.base_url}/epg.xml.gz",
        "channels":      len(state.channels),
        "fallback_synthetic": bool(load_config().get("epg", {}).get("fallback_synthetic", True)),
        "days":          7,
        "type":          "synthetic",
        "note":          "Synthetic guide (1-hour slots, 7 days). Set EPG URL in Plex: Settings → Live TV → Edit Guide",
    }

@app.get("/api/channels")
async def get_channels(search: str = "", group: str = "", include_dead: bool = False):
    """Full channel list with quality info for Channel Browser.

    Dead channels are excluded by default; pass include_dead=true to inspect
    recently-pruned entries (useful for the Diagnostics panel)."""
    source = state.channels if include_dead else _live_channels()
    channels = []
    for key, ch in source.items():
        if search and search.lower() not in ch.name.lower(): continue
        if group and group != ch.group: continue
        override = load_config().get("quality_overrides", {}).get(ch.name)
        channels.append({
            "key":          key,
            "name":         ch.name,
            "group":        ch.group,
            "logo":         _logo_url(state.base_url, key, ch.logo),
            "quality":      ch.quality,
            "serving":      override or ch.quality,
            "pinned":       bool(override),
            "variant_count": len(ch.variants) if ch.variants else 1,
            "variants":     [v.quality for v in (ch.variants or [])],
            "number":       ch.number,
            "stream_url":   f"{state.base_url}/stream/{key}",
            "dead":         _is_channel_dead(key),
            # Phase 11: surface multi-candidate dispatcher info
            "candidate_count": len(ch.candidates),
            "current_sub":     ch.sub_name,
            "candidate_subs":  [c.sub_name for c in ch.candidates],
        })
    channels.sort(key=lambda c: c["name"].lower())
    return {
        "total":    len(state.channels),
        "live":     len(_live_channels()),
        "dead":     len(_DEAD_CHANNELS),
        "filtered": len(channels),
        "groups":   sorted(set(c.group for c in state.channels.values())),
        "channels": channels,
    }

# Phase 12 Checkpoint D: Per-channel candidate detail for the Channels tab
# "Sources" sub-panel. Lighter than /api/dispatch/decisions because it works
# even when the channel has no active mux — admins can inspect upstream
# coverage before the first viewer arrives.
@app.get("/api/channels/{channel_key}/sources")
async def get_channel_sources(channel_key: str):
    ch = state.channels.get(channel_key)
    if not ch:
        raise HTTPException(404, "channel not found")
    cands = []
    for idx, c in enumerate(ch.candidates):
        sub = state.get_subscription(c.sub_name)
        cands.append({
            "sub":          c.sub_name,
            "stream_id":    c.stream_id,
            "quality":      c.quality,
            "tvg_id":       c.tvg_id,
            "priority":     c.priority,
            "fail_count":   c.fail_count,
            "last_fail_ts": int(c.last_fail_ts),
            "active":       c.sub_name == ch.sub_name and idx == 0,
            "available":    bool(sub and sub.enabled and sub.available),
            "drained":      _is_sub_drained(c.sub_name),
            "score":        _get_sub_health(c.sub_name).score(sub) if sub else 0,
        })
    return {
        "channel_key":  channel_key,
        "channel_name": ch.name,
        "current_sub":  ch.sub_name,
        "candidates":   cands,
    }

@app.get("/api/bandwidth")
async def get_bandwidth(days: int = 7):
    """Daily bandwidth usage for the last N days."""
    cutoff = time.strftime("%Y-%m-%d",
                           time.gmtime(time.time() - days * 86400))
    try:
        with db_connect() as conn:
            rows = conn.execute("""
                SELECT date, bytes_total, sessions, peak_kbps
                FROM daily_bandwidth
                WHERE date >= ?
                ORDER BY date
            """, (cutoff,)).fetchall()
            # Fill missing days with zeros
            import datetime as _dt
            today = _dt.date.today()
            result = []
            data   = {r["date"]: r for r in rows}
            for i in range(days):
                d = (today - _dt.timedelta(days=days-1-i)).isoformat()
                row = data.get(d)
                result.append({
                    "date":      d,
                    "bytes":     row["bytes_total"] if row else 0,
                    "gb":        round((row["bytes_total"] if row else 0) / 1e9, 2),
                    "sessions":  row["sessions"]   if row else 0,
                    "peak_kbps": row["peak_kbps"]  if row else 0,
                })
            total_gb = sum(r["gb"] for r in result)
            return {
                "days":     result,
                "total_gb": round(total_gb, 2),
                "avg_gb":   round(total_gb / days, 2),
            }
    except Exception as e:
        return {"days": [], "total_gb": 0, "avg_gb": 0, "error": str(e)}

async def _do_reload():
    state.reload_from_config()
    await state.refresh_channels()
    await state.fetch_account_info()
    state.log_event("channel", "MANUAL REFRESH", f"{len(state.channels)} channels loaded")

@app.post("/reload")
async def reload(request: Request, background_tasks: BackgroundTasks):
    # A2: loopback-only — only NAS host processes (cron, nginx) can trigger
    # a reload. Raw socket peer is used (not XFF) so this can't be spoofed
    # by an internet caller who sets a forged X-Forwarded-For header.
    # 127.0.0.1 = from inside container; 172.17.0.1 = Docker gateway (NAS host).
    peer = request.client.host if request.client else ""
    if peer not in ("127.0.0.1", "::1", "172.17.0.1"):
        raise HTTPException(403, "Forbidden")
    # A6: accept either a logged-in dashboard/PWA session OR the shared
    # secret header (cron, DSM Task Scheduler).  If RELOAD_TOKEN is unset,
    # fall back to requiring a session so the endpoint is never open.
    if not _check_session(request):
        supplied = request.headers.get("X-Reload-Token", "")
        if not RELOAD_TOKEN or not secrets.compare_digest(supplied, RELOAD_TOKEN):
            raise HTTPException(401, "Unauthorized")
    # A13: kick work to background so cron doesn't block for 30s+
    background_tasks.add_task(_do_reload)
    return {"status": "ok", "channels": len(state.channels), "queued": True}

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    cfg = load_config(); srv = cfg.get("server", {})
    host        = srv.get("host", "0.0.0.0")
    admin_port  = int(srv.get("port", 5004))
    stream_port = int(srv.get("stream_port") or 0)

    common = dict(
        loop="uvloop",        # faster event loop
        http="httptools",     # faster HTTP parser
        access_log=False,     # disable per-request logging (big CPU saving)
        log_level="info",
    )

    if stream_port and stream_port != admin_port:
        # Phase 11.5: two listeners on the same FastAPI app — admin (5004,
        # full route set, lifespan-on) and stream (34343, lifespan-off so the
        # startup hook does not double-fire). The stream socket is not opened
        # until admin lifespan startup completes, so background state is fully
        # initialized before any client request can land. StreamPortFilter
        # middleware uses the bound socket port (scope["server"][1]) to gate
        # admin paths off the stream listener — Host header spoofing cannot
        # bypass it.
        async def _serve_both():
            cfg_admin  = uvicorn.Config(app, host=host, port=admin_port,
                                        lifespan="on",  **common)
            cfg_stream = uvicorn.Config(app, host=host, port=stream_port,
                                        lifespan="off", **common)
            srv_admin  = uvicorn.Server(cfg_admin)
            srv_stream = uvicorn.Server(cfg_stream)
            admin_task = asyncio.create_task(srv_admin.serve())
            # Wait for the admin server's lifespan startup to finish before we
            # bind the stream socket so the stream listener never serves a
            # request before state.channels / state.users are loaded.
            while not srv_admin.started and not admin_task.done():
                await asyncio.sleep(0.05)
            if admin_task.done():
                # Admin failed during startup — propagate the exception so
                # the container exits and Docker restarts us.
                admin_task.result()
                return
            stream_task = asyncio.create_task(srv_stream.serve())
            await asyncio.gather(admin_task, stream_task)
        asyncio.run(_serve_both())
    else:
        uvicorn.run("main:app",
                    host=host, port=admin_port, reload=False, **common)
