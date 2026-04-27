/* AQ · IPTV Admin — JS (split from index.html) */
/* ════════════════════════════════════════════════════════════════════════════
   AQ · IPTV Admin — unified responsive UI
   ════════════════════════════════════════════════════════════════════════════ */

const B = '';
const Q_COLORS = {HD:'var(--cyan)',FHD:'var(--emerald)','4K':'var(--violet)',SD:'var(--amber)',HEVC:'var(--text3)'};

let _activePage = 'overview';
let _es = null;
let _sse = { status: null, health: null };
let _allGroups = [], _selGroups = new Set();
let _prevVals = {};
let _qPref = ['HD','FHD','4K','SD','HEVC'];
let _qLineup = [], _qSelected = new Set(), _qRenames = {}, _qMergeLog = [], _qManualMerges = {};
let _qOverrides = {};
let _qGroups = [];
let _logoTs  = 0;           // incremented after each upload to bust 24h browser cache
// Phase 12 D: per-channel source/candidate metadata
let _qSrcMap   = {};        // channel name → {key, count, subs, current_sub}
let _qKeyByName = {};       // channel name → channel key (for group assignment)
let _qSrcExp   = new Set(); // expanded channel names
let _qSrcCache = {};        // channel key → full sources payload from /api/channels/{key}/sources
let _epgData = null;
let _editingSubName = null;

/* ── Helpers ──────────────────────────────────────────────────────────────── */
function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

/* Phase 13 Item 7: Error boundary UX.
   api() handles 401 (session expired → redirect to /login?expired=1),
   non-OK responses (toast with status), and network failures (toast unless
   offline — the offline banner already covers that case). Returns null on
   any error so callers can short-circuit with `if (!data)` and render an
   errorPanel for first-load contexts. apiWrite() is the same wrapper for
   write ops (POST/PUT/DELETE) that need the raw Response back. */
async function api(url, opt) {
  try {
    const r = await fetch(B+url, opt);
    if (r.status === 401) { _handle401(); return null; }
    if (!r.ok) {
      toast(`Request failed (${r.status}) — ${url.split('?')[0]}`, 'error');
      return null;
    }
    return await r.json();
  } catch (e) {
    if (navigator.onLine !== false) toast('Network error — ' + url.split('?')[0], 'error');
    return null;
  }
}
async function apiWrite(url, opt) {
  try {
    const r = await fetch(B+url, opt);
    if (r.status === 401) { _handle401(); return null; }
    return r;
  } catch (e) {
    if (navigator.onLine !== false) toast('Network error — ' + url.split('?')[0], 'error');
    return null;
  }
}
let _redirecting = false;
function _handle401() {
  if (_redirecting) return;
  _redirecting = true;
  try { if (typeof _es !== 'undefined' && _es) _es.close(); } catch (_) {}
  location.replace('/login?expired=1');
}
function errorPanel(hostId, label, retryFn) {
  const host = document.getElementById(hostId);
  if (!host) return;
  // Don't clobber real data: only render an error panel on first-load failures
  // (host is empty or still showing skeletons). For background poll failures
  // the existing rendered data stays put — the toast already alerted the user.
  const hasReal = host.children.length > 0 && !host.querySelector('.skeleton-row');
  if (hasReal) return;
  host.innerHTML =
    '<div class="error-panel">' +
      '<div class="ep-icon">!</div>' +
      '<div class="ep-msg">Couldn\u2019t load ' + esc(label) + '. Check your connection and try again.</div>' +
      '<button type="button" class="ep-retry">Retry</button>' +
    '</div>';
  const btn = host.querySelector('.ep-retry');
  if (btn && typeof retryFn === 'function') {
    btn.addEventListener('click', () => {
      _skSeen.delete(hostId);
      host.innerHTML = '';
      try { retryFn(); } catch (e) { console.error(e); }
    });
  }
}
function _initOfflineWatcher() {
  const el = document.getElementById('offline-banner');
  if (!el) return;
  const sync = () => {
    const off = navigator.onLine === false;
    if (off) el.classList.add('show'); else el.classList.remove('show');
    // Disable write-action buttons when offline
    document.querySelectorAll('[data-write]').forEach(b => {
      b.disabled = off;
      b.title = off ? 'Unavailable while offline' : '';
    });
  };
  window.addEventListener('online', sync);
  window.addEventListener('offline', sync);
  sync();
}

/* Phase 13 Item 4: Toast notification system.
   toast(message, variant, dur?) — variant ∈ {info, success, warning, error}.
   Stack max 5 (oldest auto-pruned), click to dismiss, per-variant durations.
   Optional 3rd arg `dur` (ms) overrides the variant default — use it for
   toasts that need to linger (e.g. generated passwords the user must read
   and copy before they vanish). Pass null/undefined to keep the default.
   Backward-compat: passing a number as the 2nd arg is treated as a legacy
   duration override with default variant 'info'. */
const TOAST_DURATION = { info: 3000, success: 3000, warning: 4500, error: 6000 };
const TOAST_MAX = 5;
function toast(msg, variant='info', dur) {
  if (typeof variant === 'number') { dur = variant; variant = 'info'; }
  else if (typeof dur !== 'number') { dur = TOAST_DURATION[variant] || TOAST_DURATION.info; }
  const stack = document.getElementById('toast-stack');
  if (!stack) return;
  while (stack.children.length >= TOAST_MAX) stack.firstElementChild.remove();
  const el = document.createElement('div');
  el.className = 'toast toast--' + variant;
  el.setAttribute('role', variant === 'error' || variant === 'warning' ? 'alert' : 'status');
  el.textContent = msg;
  let removed = false;
  const dismiss = () => {
    if (removed) return; removed = true;
    el.classList.remove('show');
    setTimeout(() => el.remove(), 300);
  };
  el.addEventListener('click', dismiss);
  stack.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(dismiss, dur);
}

/* Phase 13 Item 5: loading-state helpers.
   ────────────────────────────────────────
   - skeletonRows(n, shape): shimmer placeholder HTML for the *first* render of a
     list/table container, before its initial fetch resolves. After data arrives
     the host's innerHTML is overwritten as normal. Tracked-once via _skSeen so
     subsequent reloads (refresh button, SSE pushes) keep current data visible
     instead of flashing skeletons.
   - withButtonBusy(btn, label, fn): disables a button + swaps its label while
     fn() runs. Restores in finally{} so failures don't strand the button.
   - progressStart/Done/Fail(): top-of-page bar. Determinate-ish 0→90% over 25s
     for /reload (channel refresh on this box runs 15–30s), snap to 100% on
     resolve. Use progressIndeterminate(true|false) to toggle the dispatch
     polling sweep.
   - SSE state vars (_sseConnected, _sseStaleTimer) live with the rest of the
     module-level state and are wired in initEventSource(). */
const _skSeen = new Set();
function skeletonRows(host, count, shape) {
  // shape: array of class hints per cell, e.g. ['sk-w-xs','flex','sk-w-md'].
  // Defaults to a single full-width cell when shape omitted.
  const cells = (shape && shape.length ? shape : ['']).map(c => {
    const cls = ['skeleton-cell']; if (c) cls.push(c);
    return `<div class="${cls.join(' ')}"></div>`;
  }).join('');
  let html = '';
  for (let i = 0; i < count; i++) html += `<div class="skeleton-row">${cells}</div>`;
  if (host) host.innerHTML = html;
}
// First-render gate: returns true once per host id, then short-circuits.
function showSkeletonOnce(hostId, count, shape) {
  if (_skSeen.has(hostId)) return false;
  _skSeen.add(hostId);
  const host = document.getElementById(hostId);
  if (!host) return false;
  if (host.innerHTML.trim()) return false;  // already populated by SSE/another path
  skeletonRows(host, count, shape);
  return true;
}

async function withButtonBusy(btn, label, fn) {
  if (!btn) return fn();
  const orig = btn.textContent;
  const wasDisabled = btn.disabled;
  btn.disabled = true;
  btn.textContent = label;
  try { return await fn(); }
  finally { btn.disabled = wasDisabled; btn.textContent = orig; }
}

let _progressTimer = null;
function _progressEl() { return document.getElementById('progress-bar'); }
function progressStart() {
  const el = _progressEl(); if (!el) return;
  if (_progressTimer) { clearInterval(_progressTimer); _progressTimer = null; }
  el.classList.remove('fail', 'indeterminate');
  el.classList.add('active');
  // Use a child element so we can animate width via inline style cleanly.
  el.innerHTML = '<i style="display:block;height:100%;width:0;background:var(--cyan);box-shadow:0 0 10px var(--cyan-glow);transition:width .35s ease-out"></i>';
  const fill = el.firstElementChild;
  let pct = 0;
  // Climb to 90% over ~25s on a curve that decelerates so it doesn't hit 90 too fast.
  // Step every 250ms; ~100 steps; logistic-ish curve.
  _progressTimer = setInterval(() => {
    if (pct < 90) {
      const remaining = 90 - pct;
      pct += Math.max(0.4, remaining * 0.04);
      if (pct > 90) pct = 90;
      fill.style.width = pct + '%';
    }
  }, 250);
}
function progressDone() {
  const el = _progressEl(); if (!el) return;
  if (_progressTimer) { clearInterval(_progressTimer); _progressTimer = null; }
  const fill = el.firstElementChild;
  if (fill) fill.style.width = '100%';
  setTimeout(() => {
    el.classList.remove('active');
    setTimeout(() => { if (!el.classList.contains('active') && !el.classList.contains('indeterminate')) el.innerHTML = ''; }, 300);
  }, 200);
}
function progressFail() {
  const el = _progressEl(); if (!el) return;
  if (_progressTimer) { clearInterval(_progressTimer); _progressTimer = null; }
  el.classList.add('fail');
  const fill = el.firstElementChild;
  if (fill) { fill.style.width = '100%'; fill.style.background = 'var(--rose)'; fill.style.boxShadow = '0 0 10px rgba(251,113,133,0.45)'; }
  setTimeout(() => {
    el.classList.remove('active', 'fail');
    setTimeout(() => { if (!el.classList.contains('active')) el.innerHTML = ''; }, 300);
  }, 600);
}
function progressIndeterminate(on) {
  const el = _progressEl(); if (!el) return;
  if (on) {
    if (_progressTimer) return;  // a determinate run is in progress; don't stomp it
    if (el.classList.contains('active')) return;
    el.classList.add('active', 'indeterminate');
    el.innerHTML = '';  // CSS ::after handles the sweep
  } else {
    if (!el.classList.contains('indeterminate')) return;
    el.classList.remove('active', 'indeterminate');
    el.innerHTML = '';
  }
}

/* SSE connection state — shows the topbar "stale" badge if the browser's
   auto-reconnect doesn't complete within a 5s grace window. */
let _sseConnected = false;
let _sseStaleTimer = null;
function _sseShowStale() {
  const b = document.getElementById('stale-indicator');
  if (b) b.classList.add('show');
}
function _sseHideStale() {
  const b = document.getElementById('stale-indicator');
  if (b) b.classList.remove('show');
  if (_sseStaleTimer) { clearTimeout(_sseStaleTimer); _sseStaleTimer = null; }
}

function fmtUp(s) {
  if (!s && s!==0) return '—';
  const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), sec=s%60;
  return h>0?h+'h '+m+'m':m>0?m+'m '+sec+'s':sec+'s';
}

function setVal(id, val, unit='') {
  const el = document.getElementById(id);
  if (!el) return;
  const prev = _prevVals[id];
  const str = val != null ? val + unit : '—';
  if (prev !== str) {
    el.textContent = str;
    el.classList.remove('pulse');
    void el.offsetWidth;
    el.classList.add('pulse');
    _prevVals[id] = str;
  }
}

function updateBadge(n) {
  // sh-badge: side-nav Monitoring, h-badge: bottom-nav Monitor, sub-h-badge: Health sub-tab
  ['h-badge','sh-badge','sub-h-badge'].forEach(id => {
    const b = document.getElementById(id); if(!b)return;
    b.style.display = n ? '' : 'none'; b.textContent = n;
  });
}

function debounce(fn, ms) {
  let timer = null;
  return function(...args) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => { timer = null; fn.apply(this, args); }, ms);
  };
}

function emptyState(text, icon) {
  const ic = icon ? `<div class="empty-icon">${icon}</div>` : '';
  return `<div class="empty">${ic}${text}</div>`;
}

/* ── Render helpers ──────────────────────────────────────────────────────── */
function renderStatCard(label, value, color, valStyle) {
  const vs = valStyle ? ` style="${valStyle}"` : '';
  return `<div class="stat-card ${color||''}"><div class="stat-l">${label}</div><div class="stat-v"${vs}>${value}</div></div>`;
}

function renderStreamRow(s, opts) {
  opts = opts || {};
  const stalled = !!s.is_stalled;
  const dotC = stalled ? 'var(--rose)' : 'var(--emerald)';
  const key = esc(s.channel_key);
  const chName = esc(s.channel);
  const subName = esc(s.subscription || '');
  const uptime = fmtUp(s.uptime_s);
  const qTag = `<span class="tag tag-info" style="font-size:9px;padding:2px 8px">${esc(s.quality||'—')}</span>`;
  if (opts.simple) {
    const simpleUsers = (s.viewer_users && s.viewer_users.length ? s.viewer_users : (s.viewer_ips || [])).join(', ') || '—';
    return `<div class="stream-row">
      <div class="sdot" style="color:var(--emerald);background:var(--emerald)"></div>
      <div class="flex-1">
        <div class="stream-name row-sm wrap">${chName} ${qTag}</div>
        <div class="stream-meta">${subName} · ${s.viewers} viewer(s) · ${s.bitrate_kbps} kbps · ${uptime}</div>
        <div class="stream-meta" style="color:var(--emerald)">${esc(simpleUsers)}</div>
      </div>
      <button class="btn btn-rose btn-sm" data-key="${key}" data-act="kill">Kill</button>
    </div>`;
  }
  const subUse = (s.sub_active_streams != null && s.sub_max_streams != null)
    ? ` · ${s.sub_active_streams}/${s.sub_max_streams} streams` : '';
  const stalledTag = stalled ? '<span class="tag tag-err" style="font-size:8px">STALLED</span>' : '';
  // Per-viewer rows with individual kill buttons
  const sids   = s.viewer_sids   || [];
  const unames = s.viewer_users  || s.viewer_ips || [];
  const viewerRows = unames.map((u, i) => {
    const sid = sids[i];
    // Viewer type: Xtream session IDs are 12 hex chars (token_hex(6)),
    // HDHR/Plex queue IDs are 8 hex chars (token_hex(4)).
    const vType = sid ? (sid.length >= 12 ? 'xtr' : 'hdhr') : 'hdhr';
    const vBadge = `<span class="viewer-badge viewer-badge--${vType}">${vType.toUpperCase()}</span>`;
    const killBtn = sid
      ? `<button class="btn btn-rose btn-sm" style="font-size:9px;padding:2px 8px" data-key="${key}" data-sid="${esc(sid)}" data-act="kill-viewer" title="Kick this viewer">✕</button>`
      : '';
    return `<div class="stream-viewer-row">${esc(u||'—')}${vBadge}${killBtn}</div>`;
  }).join('');
  return `<div class="stream-row">
    <div class="sdot" style="color:${dotC};background:${dotC}"></div>
    <div class="flex-1">
      <div class="stream-name row-sm wrap">${chName} ${qTag}${stalledTag}</div>
      <div class="stream-meta">${subName}${subUse} · ${s.viewers} viewer(s) · ${s.bitrate_kbps} kbps · ${uptime}</div>
      <div class="stream-viewers">${viewerRows || '<div class="stream-viewer-row" style="color:var(--text3)">—</div>'}</div>
    </div>
    <button class="btn btn-rose btn-sm" data-key="${key}" data-act="kill" title="Kill all viewers">Kill all</button>
  </div>`;
}

function renderHealthRow(label, value, color) {
  const vs = color ? ` style="color:${color}"` : '';
  return `<div class="hrow"><span class="hrow-l">${label}</span><span class="hrow-v"${vs}>${value}</span></div>`;
}

function renderLogRow(e) {
  const cls = {stream:'lb-stream',viewer:'lb-viewer',channel:'lb-channel',health:'lb-health',auth:'lb-auth',system:'lb-system'};
  return `<div class="log-row"><span class="log-time">${esc(e.time)}</span><span class="log-badge ${cls[e.level]||'lb-system'}">${esc((e.level||'').toUpperCase())}</span><span class="log-detail"><b>${esc(e.event)}</b> — ${esc(e.detail)}</span></div>`;
}

function renderAlertRow(a) {
  const c = a.level==='error'?'var(--rose)':a.level==='warning'?'var(--amber)':'var(--cyan)';
  return `<div class="alert-row"><span class="alert-time">${esc(a.time)}</span><span class="tag" style="border-color:${c};color:${c};flex-shrink:0;margin-right:8px;font-size:9px">${esc((a.level||'').slice(0,4).toUpperCase())}</span><span class="alert-msg">${esc(a.message)}</span></div>`;
}

function renderSubCard(s, opts) {
  opts = opts || {};
  const p = Math.round((s.load||0) * 100);
  const lc = p===0?'var(--emerald)':p<70?'var(--cyan)':p<100?'var(--amber)':'var(--rose)';
  const acc = s.account || opts.acc || {};
  const days = acc.days_left;
  const priL = s.priority===1?(opts.full?'🟢 PRIMARY':'PRIMARY'):s.priority===2?(opts.full?'🟡 BACKUP':'BACKUP'):`#${s.priority}`;
  const priC = s.priority===1?'var(--emerald)':'var(--amber)';
  const name = esc(s.name);
  const url  = esc((s.url||'').replace(/https?:\/\//,''));
  // Provider plan vs local cap. Provider's user_info reports max_connections;
  // local max_streams is the dispatcher's hard cap. They should match — if
  // local > provider, flag in red so the admin knows the upstream will reject
  // the extra slot. Provider value of "?" / unset → don't compare, just hide.
  const provMax = parseInt(acc.max_connections, 10);
  const provN   = isFinite(provMax) ? provMax : null;
  const planMismatch = provN != null && provN < (s.max_streams || 0);
  const planCol = planMismatch ? 'var(--rose)' : 'var(--text3)';
  const planTxt = provN != null
    ? `${s.max_streams}<span style="color:${planCol};font-weight:400"> / ${provN} plan</span>`
    : `${s.max_streams}`;
  if (!opts.full) {
    let dayTxt='', dayC='var(--text3)';
    if (days!=null && days>=0) { dayTxt=` · ${days}d left`; dayC=days<=7?'var(--rose)':days<=30?'var(--amber)':'var(--emerald)'; }
    const cacheBadge = _cacheBadge(s.name);
    return `<div class="sub-card">
      <div class="sub-dot" style="color:${lc};background:${lc}"></div>
      <div class="flex-1">
        <div class="sub-name">${name}</div>
        <div class="sub-meta"><span style="color:${priC};font-weight:600">${priL}</span> · ${url} · ${s.active_streams}/${planTxt} streams<span style="color:${dayC}">${esc(dayTxt)}</span></div>
        <div class="load-bar"><div class="load-fill" style="width:${p}%;background:${lc}"></div></div>
      </div>
      ${cacheBadge}
      <span class="tag" style="border-color:${lc};color:${lc};background:rgba(0,0,0,0.2)">${p===0?'IDLE':p<100?'ACTIVE':'FULL'}</span>
    </div>`;
  }
  const stat = opts.stat || {};
  const enabled = s.enabled !== false;
  const failCnt = stat.fail_count || 0;
  const healthy = failCnt === 0;
  const accSt = acc.status==='Active'?'var(--emerald)':acc.status?'var(--rose)':'var(--text3)';
  let dayCol='var(--text3)', dayTxt='—';
  if (days!=null && days>=0) { dayTxt=days+'d'; dayCol=days<=7?'var(--rose)':days<=30?'var(--amber)':'var(--emerald)'; }
  const enc = encodeURIComponent(s.name);
  // Plan chip for the full subscription card. Always show local cap. When
  // provider differs, append "/N plan" in the chip and switch to amber/rose.
  let planChipBorder = 'var(--cyan)', planChipColor = 'var(--cyan)';
  let planChipText = `${s.active_streams||0}/${s.max_streams} slots`;
  if (provN != null) {
    if (planMismatch) {
      planChipBorder = 'var(--rose)'; planChipColor = 'var(--rose)';
      planChipText = `${s.active_streams||0}/${s.max_streams} slots · plan ${provN}`;
    } else if (provN > s.max_streams) {
      planChipBorder = 'var(--amber)'; planChipColor = 'var(--amber)';
      planChipText = `${s.active_streams||0}/${s.max_streams} slots · plan ${provN}`;
    } else {
      planChipText = `${s.active_streams||0}/${s.max_streams} slots`;
    }
  }
  const planChipTitle = provN != null
    ? (planMismatch
        ? `Local cap (${s.max_streams}) exceeds provider plan (${provN}) — upstream will reject the extra stream`
        : `Provider plan: ${provN} concurrent connection${provN!==1?'s':''}`)
    : `Provider plan unknown — using local cap`;
  return `<div class="sub-card" style="opacity:${enabled?1:0.4}">
    <div>
      <div style="color:${priC};font-size:10px;font-weight:700;margin-bottom:6px">${priL}</div>
      <div class="row-sm" style="gap:4px">
        <button class="btn btn-sm btn-ghost" data-sub="${enc}" data-sub-act="prio-up" ${(s.priority||1)<=1?'disabled':''} style="font-size:8px;padding:2px 7px">▲</button>
        <button class="btn btn-sm btn-ghost" data-sub="${enc}" data-sub-act="prio-dn" style="font-size:8px;padding:2px 7px">▼</button>
      </div>
    </div>
    <div class="flex-1">
      <div class="sub-name">${name}</div>
      <div class="sub-meta">${url}</div>
      <div class="row-sm wrap" style="gap:6px;margin-top:6px">
        <span class="tag" style="border-color:${accSt};color:${accSt}">${esc(acc.status||'—')}</span>
        <span class="tag tag-info">${esc(acc.exp_date||'—')}</span>
        <span class="tag" style="border-color:${dayCol};color:${dayCol}">${esc(dayTxt)} left</span>
        <span class="tag" style="border-color:${planChipBorder};color:${planChipColor}" title="${esc(planChipTitle)}">${planChipText}</span>
        <span class="tag ${healthy?'tag-ok':'tag-err'}">${healthy?'OK':failCnt+' fails'}</span>
      </div>
    </div>
    <div class="col" style="gap:6px">
      <button class="btn btn-sm ${enabled?'btn-amber':'btn-green'}" data-sub="${enc}" data-sub-act="${enabled?'disable':'enable'}">${enabled?'Disable':'Enable'}</button>
      <button class="btn btn-sm btn-ghost" data-sub="${enc}" data-sub-act="edit">Edit</button>
      <button class="btn btn-rose btn-sm" data-sub="${enc}" data-sub-act="delete">Delete</button>
    </div>
  </div>`;
}

function renderPrefItem(q, i, total) {
  const up = i > 0 ? `<button class="btn btn-sm btn-ghost" data-pref-move="${i}:-1" style="padding:4px 12px">▲</button>` : '<span style="width:40px"></span>';
  const dn = i < total-1 ? `<button class="btn btn-sm btn-ghost" data-pref-move="${i}:1" style="padding:4px 12px">▼</button>` : '<span style="width:40px"></span>';
  return `<div class="pref-item">
    <span class="text-sm" style="width:16px">${i+1}</span>
    <span style="width:9px;height:9px;border-radius:50%;background:${Q_COLORS[q]||'var(--text3)'}"></span>
    <span class="flex-1" style="font-weight:700;color:#fff;font-size:14px">${q}</span>
    <div class="row-sm" style="gap:6px">${up}${dn}</div>
  </div>`;
}

function renderChannelRow(ch, isSel) {
  const count       = ch.VariantCount || 1;
  const variants    = ch.Variants || [ch.Tags || 'HD'];
  const variantSubs = ch.VariantSubs || [];
  const merged      = count > 1;
  const enc         = encodeURIComponent(ch.GuideName);
  // lineup.json returns ImageURL; strip origin so request goes via nginx on :5005
  // Append _logoTs to bust 24h browser cache after a custom upload
  const _rawLogo    = (ch.ImageURL || ch.Logo || '').replace(/^https?:\/\/[^/]+/, '');
  const logo        = _rawLogo && _logoTs ? `${_rawLogo}?t=${_logoTs}` : _rawLogo;
  const group       = (ch.GroupTitle || '').replace(/★/g,'').replace(/⚽/g,'').trim();
  const src         = _qSrcMap[ch.GuideName];
  const chKey       = _qKeyByName[ch.GuideName] || src?.key || '';
  const srcCount    = src?.count || 0;
  const srcColor    = srcCount >= 2 ? 'var(--emerald)'
                    : srcCount === 1 ? 'var(--amber)'
                    : 'var(--rose)';
  const srcBadge    = src
    ? `<span class="ch-src-badge" data-q-srcs="${enc}" style="border-color:${srcColor};color:${srcColor}" title="${srcCount} upstream source${srcCount!==1?'s':''} — click to expand">${srcCount} src${srcCount!==1?'s':''}</span>`
    : '';
  const expanded = _qSrcExp.has(ch.GuideName);
  const srcPanel = expanded ? renderQSourcesPanel(ch.GuideName) : '';

  const logoEl = logo
    ? `<img class="ch-logo" src="${esc(logo)}" onerror="this.src='/app/icon-192.png'">`
    : `<img class="ch-logo" src="/app/icon-192.png">`;

  const grpDropdown = _cgOrder.length && chKey ? (() => {
    const curGrp = _cgAssignments[chKey] || '';
    const opts = `<option value="">— auto (${esc(group||'ungrouped')}) —</option>` +
      _cgOrder.map(g => `<option value="${esc(g)}" ${g===curGrp?'selected':''}>${esc(g)}</option>`).join('');
    return `<select data-q-grp="${esc(chKey)}" style="margin-top:3px;background:var(--surface2);border:1px solid var(--border);color:var(--text2);padding:2px 6px;font-size:10px;border-radius:5px;max-width:160px;cursor:pointer;outline:none">${opts}</select>`;
  })() : '';

  return `<div class="ch-row ${isSel?'selected':''} ${merged?'merged':''} ${expanded?'src-expanded':''}" data-q-enc="${enc}">
    <input type="checkbox" ${isSel?'checked':''} data-q-toggle="${enc}" style="width:17px;height:17px;accent-color:var(--emerald);flex-shrink:0;cursor:pointer">
    ${logoEl}
    <div class="flex-1">
      <div class="row-sm wrap" style="gap:6px">
        <span class="ch-name" data-q-rename="${enc}" title="Tap to rename">${esc(ch.GuideName)}</span>
        ${srcBadge}
      </div>
      ${grpDropdown}
      <div class="ch-meta">
        ${variants.map((v,i) => {
          const sub = variantSubs[i] || '';
          const provColor = sub && _providerColors[sub] ? _providerColors[sub] : null;
          const color = provColor || Q_COLORS[v] || 'var(--text3)';
          return `<span style="color:${color}">${esc(v)}</span>`;
        }).join(' · ')}
        ${merged?` · <span style="color:var(--emerald)">${count} variants</span>`:''}
      </div>
    </div>
    <div class="col flex-none" style="align-items:center;gap:4px" data-q-noselect>
      ${merged?`<button class="btn btn-sm btn-rose" data-q-split="${enc}" style="font-size:9px;padding:3px 8px">Split</button>`:''}
      <button class="btn btn-sm btn-ghost" data-q-logo="${esc(chKey)}" data-q-logo-name="${enc}" style="font-size:9px;padding:3px 8px" title="Upload logo">📷</button>
      <button class="btn btn-sm btn-rose" data-q-logo-del="${esc(chKey)}" style="font-size:9px;padding:3px 8px" title="Delete custom logo">🗑</button>
    </div>
    ${srcPanel}
  </div>`;
}

function renderQSourcesPanel(name) {
  const meta = _qSrcMap[name];
  if (!meta) return '';
  const cached = _qSrcCache[meta.key];
  if (!cached) {
    return `<div class="ch-src-panel" data-q-noselect><div class="text-sm" style="padding:8px">Loading sources…</div></div>`;
  }
  const cands = cached.candidates || [];
  if (!cands.length) {
    return `<div class="ch-src-panel" data-q-noselect><div class="text-sm" style="padding:8px">No upstream candidates configured</div></div>`;
  }
  const rows = cands.map((c, i) => {
    const status = c.drained ? 'drained'
                  : !c.available ? 'down'
                  : c.active ? 'active'
                  : c.fail_count > 0 ? 'cooldown'
                  : 'standby';
    const statusColor = {
      active:'var(--emerald)', standby:'var(--text3)', cooldown:'var(--amber)',
      down:'var(--rose)',     drained:'var(--rose)',
    }[status];
    return `<div class="ch-src-cand">
      <span class="ch-src-idx">#${i+1}</span>
      <span class="ch-src-sub">${esc(c.sub)}</span>
      <span class="ch-src-status" style="color:${statusColor}">${status.toUpperCase()}</span>
      <span class="ch-src-meta">score ${Math.round(c.score||0)} · q=${esc(c.quality||'?')} · fail=${c.fail_count||0}</span>
    </div>`;
  }).join('');
  const inv = `<button class="btn btn-sm btn-amber" data-q-src-inv="${encodeURIComponent(meta.key)}" title="Force re-pick on next viewer" style="font-size:9px;padding:3px 8px">↻ Invalidate cache</button>`;
  return `<div class="ch-src-panel" data-q-noselect>
    <div class="ch-src-cands">${rows}</div>
    <div class="ch-src-actions">${inv}</div>
  </div>`;
}

async function _qLoadSources(name) {
  const meta = _qSrcMap[name];
  if (!meta) return;
  if (_qSrcCache[meta.key]) return;
  const d = await api('/api/channels/'+encodeURIComponent(meta.key)+'/sources');
  if (d) _qSrcCache[meta.key] = d;
}

/* ── Layout ──────────────────────────────────────────────────────────────── */
function applyLayout() {
  const unfolded = window.innerWidth >= 600;
  const sn = document.getElementById('side-nav');
  if (sn) {
    sn.style.display = unfolded ? 'flex' : 'none';
    if (unfolded) {
      sn.classList.remove('mobile-open');
      const ov = document.getElementById('side-overlay');
      if (ov) ov.classList.remove('open');
    }
  }
}
window.addEventListener('resize', applyLayout);
applyLayout();

if ('serviceWorker' in navigator) navigator.serviceWorker.register('/app/sw.js').catch(()=>{});

/* Phase 15a: PWA install prompt */
let _deferredInstall = null;
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  _deferredInstall = e;
  const btn = document.getElementById('btn-pwa-install');
  if (btn) btn.style.display = '';
});
window.addEventListener('appinstalled', () => {
  _deferredInstall = null;
  const btn = document.getElementById('btn-pwa-install');
  if (btn) btn.style.display = 'none';
});
function pwaInstall() {
  if (!_deferredInstall) return;
  _deferredInstall.prompt();
  _deferredInstall.userChoice.then(() => { _deferredInstall = null; });
}

/* ── Navigation ──────────────────────────────────────────────────────────── */
const PAGES = ['overview','channels','groups','monitoring','providers','users'];

// Sub-tab state per parent. Keys are parent page names, values are the
// active sub-pane name. Used by goSubPane + isPaneOpen so SSE handlers can
// keep refreshing whichever sub is currently visible.
const _subPanes = {
  channels:   'channel-groups', // channel-groups | merges
  monitoring: 'health',         // health | logs | analytics
  providers:  'subscriptions',  // subscriptions | colors | dispatch
};

function goPage(name, fromSide=false) {
  if (!PAGES.includes(name)) name = 'overview';
  PAGES.forEach(p => document.getElementById('page-'+p)?.classList.remove('active'));
  document.querySelectorAll('.nav-btn,.side-nav-item').forEach(b => b.classList.remove('active'));
  document.getElementById('page-'+name)?.classList.add('active');
  document.querySelectorAll('[data-page="'+name+'"]').forEach(e => e.classList.add('active'));
  _activePage = name;
  // Sync hash without triggering hashchange
  if (location.hash !== '#/'+name) history.replaceState(null, '', '#/'+name);
  // Page enter: kick the loader once. Status/health-driven pages reuse the
  // SSE cache when available; everything else fetches normally on entry.
  const map = {
    overview:   () => loadOverview(_sse.status, _sse.health),
    channels:   () => goSubPane('channels', _subPanes.channels),
    groups:     () => loadGroupsPage(),
    monitoring: () => goSubPane('monitoring', _subPanes.monitoring),
    providers:  () => goSubPane('providers',  _subPanes.providers),
    users:      () => loadUsers(),
  };
  map[name]?.();
  if (!fromSide) window.scrollTo(0,0);
  else document.getElementById('fold-content')?.scrollTo(0,0);
  if (fromSide) closeSideNav();
}

/* Sub-tab pane switcher inside a parent page (Monitoring / Providers) or
 * inside the Settings drawer. Updates active classes + fires the loader. */
const _subLoaders = {
  'channel-groups': () => loadChannelGroups(),
  merges:           () => loadQuality(),
  health:           () => loadHealth(_sse.health),
  logs:             () => loadLogs(),
  analytics:        () => loadAnalytics(),
  subscriptions:    () => loadSubs(),
  colors:           () => loadProviderColors(),
  dispatch:         () => loadDispatch(),
  quality:          () => loadQualityPrefs(),
  epg:              () => loadEpg(),
  telegram:         () => loadTg(),
};
function goSubPane(host, sub) {
  const root = host === 'settings'
    ? document.getElementById('settings-drawer')
    : document.getElementById('page-'+host);
  if (!root) return;
  const subAttr = host === 'settings' ? 'data-stab' : 'data-sub';
  const paneAttr = host === 'settings' ? 'data-stab-pane' : 'data-sub-pane';
  root.querySelectorAll('.subtab').forEach(b => b.classList.toggle('active', b.getAttribute(subAttr) === sub));
  root.querySelectorAll('.subtab-pane').forEach(p => p.classList.toggle('active', p.getAttribute(paneAttr) === sub));
  if (host !== 'settings') _subPanes[host] = sub;
  else _settingsSub = sub;
  _subLoaders[sub]?.();
}

/* Returns true when `name` is the user-visible content area — either the
 * page itself, an active sub-tab, or an open Settings drawer pane. */
function isPaneOpen(name) {
  if (_activePage === name) return true;
  if (_activePage === 'channels'   && _subPanes.channels   === name) return true;
  if (_activePage === 'monitoring' && _subPanes.monitoring === name) return true;
  if (_activePage === 'providers'  && _subPanes.providers  === name) return true;
  if (_settingsOpen && _settingsSub === name) return true;
  return false;
}
let _settingsOpen = false;
let _settingsSub = 'quality';

/* ── SSE: live updates from /api/events ──────────────────────────────────── */
function applySSE(topic, data) {
  if (topic === 'status') {
    _sse.status = data;
    if (_activePage === 'overview') loadOverview(data, _sse.health);
  } else if (topic === 'health') {
    _sse.health = data;
    updateBadge(data.alerts.filter(a => a.level === 'error').length);
    if (isPaneOpen('health')) loadHealth(data);
  } else if (topic === 'log') {
    if (isPaneOpen('logs')) {
      const list = document.getElementById('logs-list');
      const empty = list?.querySelector('.empty');
      if (empty) list.innerHTML = '';
      list?.insertAdjacentHTML('afterbegin', renderLogRow(data));
      // Cap at 200 rows in DOM
      const rows = list?.querySelectorAll('.log-row');
      if (rows && rows.length > 200) rows[rows.length-1].remove();
      const lbl = document.getElementById('log-count-lbl');
      if (lbl) lbl.textContent = (rows?.length || 0) + ' entries';
    }
  } else if (topic === 'alert') {
    if (_sse.health) {
      _sse.health.alerts.unshift(data);
      _sse.health.alerts = _sse.health.alerts.slice(0, 50);
      updateBadge(_sse.health.alerts.filter(a => a.level === 'error').length);
    }
    if (isPaneOpen('health')) {
      const list = document.getElementById('h-alerts');
      const empty = list?.querySelector('.empty');
      if (empty) list.innerHTML = '';
      list?.insertAdjacentHTML('afterbegin', renderAlertRow(data));
    }
  } else if (topic === 'user') {
    // Phase 12: Xtream user lifecycle/session events. Re-fetch the table when
    // the Users tab is open so live counts stay honest. The volume is low
    // (CRUD calls + stream open/close) so a full reload is fine.
    if (_activePage === 'users') {
      // Debounced reload to coalesce rapid bulk events
      _usDebouncedReload();
    }
    // If the drawer is open and the event targets the open user, refresh it
    if (_usDrawerUser && data.username && data.username.toLowerCase() === _usDrawerUser.toLowerCase()) {
      _loadUsDrawer(_usDrawerUser, /*reopen*/false);
    }
  }
}

function initEventSource() {
  if (_es) return;
  try {
    _es = new EventSource(B + '/api/events');
    _es.addEventListener('status', e => { try { applySSE('status', JSON.parse(e.data)); } catch(_){} });
    _es.addEventListener('health', e => { try { applySSE('health', JSON.parse(e.data)); } catch(_){} });
    _es.addEventListener('log',    e => { try { applySSE('log',    JSON.parse(e.data)); } catch(_){} });
    _es.addEventListener('alert',  e => { try { applySSE('alert',  JSON.parse(e.data)); } catch(_){} });
    _es.addEventListener('user',   e => { try { applySSE('user',   JSON.parse(e.data)); } catch(_){} });
    // Phase 13 Item 5: stale-data indicator. The browser auto-reconnects on
    // SSE drop, but until now the UI was silent about it — viewers had no
    // idea that "live" updates had paused. We now show an amber pill in the
    // topbar after a 5s grace window (most reconnects complete in <2s, so
    // this avoids flicker on transient hiccups) and clear it on reopen.
    _es.onopen = () => {
      const wasStale = !_sseConnected && _sseStaleTimer == null
                       ? false
                       : document.getElementById('stale-indicator')?.classList.contains('show');
      _sseConnected = true;
      _sseHideStale();
      if (wasStale) toast('Reconnected', 'success');
    };
    _es.onerror = () => {
      _sseConnected = false;
      if (_sseStaleTimer) return;  // grace timer already pending
      _sseStaleTimer = setTimeout(() => {
        if (!_sseConnected) _sseShowStale();
        _sseStaleTimer = null;
      }, 5000);
    };
  } catch (_) { /* no SSE support — fall back to one-shot loaders on goPage */ }
}

/* ── Hash-based router (#/overview, #/monitoring, …) ────────────────────── */
// Legacy hash aliases — old bookmarks like #/health or #/dispatch keep working
// by redirecting to the parent page + selecting the right sub-pane.
const _hashAliases = {
  streams:       { page: 'overview',   sub: null },
  health:        { page: 'monitoring', sub: 'health' },
  logs:          { page: 'monitoring', sub: 'logs' },
  analytics:     { page: 'monitoring', sub: 'analytics' },
  subscriptions: { page: 'providers',  sub: 'subscriptions' },
  dispatch:      { page: 'providers',  sub: 'dispatch' },
  groups:        { page: 'overview',   sub: null, settings: 'groups' },
  telegram:      { page: 'overview',   sub: null, settings: 'telegram' },
};
function getPageFromHash() {
  const h = (location.hash || '').replace(/^#\/?/, '');
  if (PAGES.includes(h)) return h;
  if (_hashAliases[h]) return _hashAliases[h].page;
  return 'overview';
}
window.addEventListener('hashchange', () => {
  const h = (location.hash || '').replace(/^#\/?/, '');
  const alias = _hashAliases[h];
  goPage(getPageFromHash(), false);
  if (alias?.sub) goSubPane(alias.page, alias.sub);
  if (alias?.settings) openSettings(alias.settings);
});

document.querySelectorAll('.side-nav-item[data-page]').forEach(el => {
  el.addEventListener('click', () => goPage(el.dataset.page, true));
});
document.querySelectorAll('.bottom-nav .nav-btn[data-page]').forEach(el => {
  el.addEventListener('click', () => goPage(el.dataset.page, false));
});

// Sub-tab clicks (Monitoring + Providers panes)
document.addEventListener('click', (e) => {
  const stab = e.target.closest('.subtab-strip .subtab');
  if (!stab) return;
  const strip = stab.closest('.subtab-strip');
  const host = strip?.dataset.subtabHost;
  if (!host) return;
  if (host === 'settings') {
    goSubPane('settings', stab.dataset.stab);
  } else {
    goSubPane(host, stab.dataset.sub);
  }
});

/* Clock */
setInterval(() => {
  const el = document.getElementById('clock');
  if (el) el.textContent = new Date().toTimeString().slice(0,5);
}, 1000);

/* ── Overview ────────────────────────────────────────────────────────────── */
async function loadOverview(statusData, healthData) {
  // Phase 13 Item 5: first-load skeletons (subsequent reloads keep prior data).
  showSkeletonOnce('ov-subs',    2, ['sk-tall']);
  showSkeletonOnce('ov-streams', 3, ['sk-w-xs', '', 'sk-w-md']);
  const d = statusData || await api('/api/status');
  if (!d) {
    errorPanel('ov-subs', 'overview data', () => loadOverview());
    errorPanel('ov-streams', 'active streams', () => loadOverview());
    return;
  }
  setVal('p-ch',  d.total_channels.toLocaleString());
  setVal('p-str', d.active_streams);
  setVal('p-vi',  d.total_viewers);
  setVal('p-up',  fmtUp(d.uptime_s));
  const s = d.system || {};
  const cpu = s.cpu_pct;
  setVal('p-cpu', cpu != null ? cpu : null, cpu!=null?'%':'');
  setVal('p-mem', s.mem_mb != null ? s.mem_mb : null, s.mem_mb!=null?' MB':'');
  const cpuCard = document.querySelector('#p-cpu')?.closest('.stat-card');
  if (cpuCard) cpuCard.style.setProperty('--accent', cpu>80?'var(--rose)':cpu>50?'var(--amber)':'var(--cyan)');

  const sorted = (d.subscriptions||[]).slice().sort((a,b)=>(a.priority||1)-(b.priority||1));
  const sh = sorted.map(s => renderSubCard(s)).join('');
  document.getElementById('ov-subs').innerHTML = sh || emptyState('No subscriptions');

  if (!d.active_channels.length) {
    document.getElementById('ov-streams').innerHTML = emptyState('No active streams', '◉');
  } else {
    document.getElementById('ov-streams').innerHTML =
      d.active_channels.map(ch => renderStreamRow(ch)).join('');
  }

  // Phase 12 D: Slot utilization gauges from /api/status (already has each sub)
  renderOvGauges(sorted);

  // Phase 12 D: Top 5 channels/users (24h) + dispatcher sparkline.
  // Fired in parallel and rendered as data arrives so the basic Overview
  // header isn't blocked by these enrichments.
  loadOverviewWidgets();

  const h = healthData || await api('/api/health');
  if (h) updateBadge(h.alerts.filter(a=>a.level==='error').length);
}

function renderOvGauges(subs) {
  const host = document.getElementById('ov-gauges');
  if (!host) return;
  if (!subs.length) { host.innerHTML = emptyState('No subscriptions', '⊞'); return; }
  host.innerHTML = subs.map(s => {
    const max  = s.max_streams || 1;
    const act  = s.active_streams || 0;
    const util = Math.round((act / max) * 100);
    const col  = util >= 100 ? 'var(--rose)'
                : util >= 75  ? 'var(--amber)'
                : 'var(--emerald)';
    return `<div class="ov-gauge">
      <div class="ov-gauge-hdr">
        <span class="ov-gauge-name">${esc(s.name)}</span>
        <span class="ov-gauge-val" style="color:${col}">${act}/${max}</span>
      </div>
      <div class="ov-gauge-bar"><div class="ov-gauge-fill" style="width:${Math.min(100,util)}%;background:${col}"></div></div>
    </div>`;
  }).join('');
}

async function loadOverviewWidgets() {
  // Fan-out — failures on any single source still leave the others visible.
  const [an, dispStats, logs] = await Promise.all([
    api('/api/analytics/summary?days=1'),
    api('/api/dispatch/stats'),
    api('/api/logs?limit=300'),
  ]);
  renderOvTopCh(an?.top_channels || []);
  renderOvTopUs(an?.top_users || []);
  renderOvSparkline(an?.heatmap || [], Array.isArray(logs) ? logs : []);
  // dispStats currently unused for the overview header but reserved for a
  // future "lifetime failovers" badge — keeping the fetch so the cache warms.
  void dispStats;
}

function renderOvTopCh(rows) {
  const host = document.getElementById('ov-top-ch');
  if (!host) return;
  const top = rows.slice(0, 5);
  if (!top.length) { host.innerHTML = emptyState('No streams in last 24h', '◈'); return; }
  const max = top[0].total_s || 1;
  host.innerHTML = top.map((c, i) => {
    const h = Math.floor((c.total_s||0)/3600);
    const m = Math.floor(((c.total_s||0)%3600)/60);
    const pct = Math.round(((c.total_s||0)/max)*100);
    return `<div class="ov-top-row" data-ov-goto="channels">
      <div class="ov-top-rank">${i+1}</div>
      <div class="ov-top-body">
        <div class="ov-top-name">${esc(c.channel||'?')}</div>
        <div class="ov-top-bar"><div class="ov-top-fill" style="width:${pct}%;background:var(--cyan)"></div></div>
        <div class="ov-top-meta">${c.sessions||0} sessions · ${h}h ${m}m</div>
      </div>
    </div>`;
  }).join('');
}

function renderOvTopUs(rows) {
  const host = document.getElementById('ov-top-us');
  if (!host) return;
  const top = rows.slice(0, 5);
  if (!top.length) { host.innerHTML = emptyState('No user activity in last 24h', '👥'); return; }
  const max = top[0].total_s || 1;
  host.innerHTML = top.map((u, i) => {
    const h = Math.floor((u.total_s||0)/3600);
    const m = Math.floor(((u.total_s||0)%3600)/60);
    const pct = Math.round(((u.total_s||0)/max)*100);
    return `<div class="ov-top-row" data-ov-goto="users">
      <div class="ov-top-rank">${i+1}</div>
      <div class="ov-top-body">
        <div class="ov-top-name" style="color:var(--emerald)">${esc(u.user||'?')}</div>
        <div class="ov-top-bar"><div class="ov-top-fill" style="width:${pct}%;background:var(--emerald)"></div></div>
        <div class="ov-top-meta">${u.sessions||0} sessions · ${h}h ${m}m</div>
      </div>
    </div>`;
  }).join('');
}

function renderOvSparkline(heatmap, logs) {
  const host = document.getElementById('ov-spark');
  if (!host) return;
  // Build 24-hour buckets for failover events from logs
  const now = new Date();
  const fobuckets = new Array(24).fill(0);
  for (const e of logs) {
    if (!/FAILOVER|NO CANDIDATES/i.test(e.event||'')) continue;
    const t = new Date((e.ts||0) * 1000);
    const ageHr = Math.floor((now - t) / 3600000);
    if (ageHr < 0 || ageHr >= 24) continue;
    // Bucket index: 0 = 23h ago, 23 = current hour
    fobuckets[23 - ageHr]++;
  }
  // Heatmap from analytics is keyed by hour-of-day (0-23). We want
  // newest-on-the-right, so rotate it so the right edge = current hour.
  const curHour = now.getHours();
  const hmap = new Array(24).fill(0);
  for (const h of heatmap) {
    const off = (h.hour - curHour + 23 + 24) % 24;  // hours-ago bucket index (0..23)
    hmap[23 - off] = h.sessions || 0;
  }
  const maxV = Math.max(...hmap, ...fobuckets, 1);
  let html = '<div class="ov-spark"><div class="ov-spark-bars">';
  for (let i = 0; i < 24; i++) {
    const grayPct = Math.max(Math.round((hmap[i] / maxV) * 100), hmap[i] > 0 ? 4 : 1);
    const redPct  = Math.max(Math.round((fobuckets[i] / maxV) * 100), fobuckets[i] > 0 ? 4 : 0);
    const lbl = (i % 6 === 0) ? `${(curHour - (23 - i) + 24) % 24}` : '';
    html += `<div class="ov-spark-col" title="${24-i}h ago: ${hmap[i]} starts, ${fobuckets[i]} failovers">
      <div class="ov-spark-stack">
        ${redPct  ? `<div class="ov-spark-fo" style="height:${redPct}%"></div>` : ''}
        <div class="ov-spark-bar" style="height:${grayPct}%"></div>
      </div>
      <div class="ov-spark-lbl">${lbl}</div>
    </div>`;
  }
  html += '</div></div>';
  host.innerHTML = html;
}

// Click-through: top rows on Overview navigate to the relevant tab
document.getElementById('fold-content').addEventListener('click', (e) => {
  const r = e.target.closest('[data-ov-goto]');
  if (!r) return;
  goPage(r.dataset.ovGoto);
});

async function refreshAccInfo(btn) {
  const orig=btn.textContent; btn.textContent='⟳ …'; btn.disabled=true;
  await fetch(B+'/api/account-info/refresh',{method:'POST'});
  btn.textContent=orig; btn.disabled=false;
  toast('Account info refreshed', 'success'); loadOverview();
}

async function forceRefresh(btn) {
  // Phase 13 Item 5: top progress bar covers the slow channel reload (~15-30s
  // on this box). Fake-progress curve climbs to 90% over ~25s and snaps to
  // 100% on resolve; failed reload paints red briefly.
  const orig=btn.textContent; btn.textContent='⟳ Refreshing…'; btn.disabled=true;
  progressStart();
  let r;
  try { r = await api('/reload',{method:'POST'}); }
  finally { btn.textContent=orig; btn.disabled=false; }
  if (r) { progressDone(); toast(`${r.channels} channels loaded`, 'success'); }
  else   { progressFail(); }
}

document.getElementById('btn-refresh-acc').addEventListener('click', function(){ refreshAccInfo(this); });
document.getElementById('btn-refresh-ch').addEventListener('click', function(){ forceRefresh(this); });
document.getElementById('btn-kill-all-ov').addEventListener('click', killAll);
document.getElementById('btn-restart').addEventListener('click', restartContainer);

async function restartContainer() {
  const s = await api('/api/status');
  const active = s?.active_streams ?? 0;
  const drain  = 10;
  const msg = active > 0
    ? `${active} active stream(s) will be given up to ${drain}s to finish.\n\nRestart now?`
    : 'No active streams. Restart now?';
  if (!confirm(msg)) return;

  const btn = document.getElementById('btn-restart');
  const orig = btn.textContent;
  btn.textContent = '↺ Restarting…';
  btn.disabled = true;

  try {
    await fetch(B + '/api/admin/restart', { method: 'POST' });
  } catch (_) { /* connection drop is expected */ }

  toast('Restarting — back in ~10s', 'info');

  // Poll /api/status until the container comes back up
  const poll = setInterval(async () => {
    try {
      const r = await fetch(B + '/api/status', { signal: AbortSignal.timeout(2000) });
      if (r.ok) {
        clearInterval(poll);
        btn.textContent = orig;
        btn.disabled = false;
        toast('Container restarted', 'success');
        loadOverview();
      }
    } catch (_) { /* still down, keep polling */ }
  }, 2000);

  // Safety: stop polling after 60s regardless
  setTimeout(() => {
    clearInterval(poll);
    btn.textContent = orig;
    btn.disabled = false;
  }, 60000);
}

/* Event delegation for kill buttons in Overview + Streams lists */
document.getElementById('fold-content').addEventListener('click', (e) => {
  const bv = e.target.closest('[data-act="kill-viewer"]');
  if (bv) { killViewer(bv.dataset.key, bv.dataset.sid); return; }
  const b = e.target.closest('[data-act="kill"]');
  if (!b) return;
  killStream(b.dataset.key);
});

/* ── Stream kill helpers (Phase 13 A.5: Streams page killed; lives in Overview Active Channels) ── */
async function killStream(key) {
  await fetch(B+'/api/streams/'+key,{method:'DELETE'});
  toast('Stream killed', 'success');
  loadOverview(_sse.status, _sse.health);
}
async function killViewer(key, sid) {
  if (!confirm('Kick this viewer?')) return;
  const r = await fetch(B+'/api/streams/'+key+'/sessions/'+sid,{method:'DELETE'});
  toast(r.ok ? 'Viewer kicked' : 'Failed to kick viewer', r.ok ? 'success' : 'error');
  loadOverview(_sse.status, _sse.health);
}
async function killAll() {
  const s = await api('/api/streams')||[];
  if(!s.length){toast('No active streams', 'info');return;}
  if(!confirm(`Kill all ${s.length} stream(s)?`))return;
  for(const x of s) await fetch(B+'/api/streams/'+x.channel_key,{method:'DELETE'});
  toast('All streams killed', 'success');
  loadOverview(_sse.status, _sse.health);
}

/* ── Health ──────────────────────────────────────────────────────────────── */
async function loadHealth(healthData) {
  // Phase 13 Item 5: first-load skeleton for the alerts list.
  showSkeletonOnce('h-alerts', 3, ['sk-w-sm', '', 'sk-w-md']);
  const h = healthData || await api('/api/health');
  if (!h) { errorPanel('h-alerts', 'health alerts', () => loadHealth()); return; }
  const sys = h.system||{};
  const cpu = sys.cpu_pct||0;
  const cpuC = cpu>80?'var(--rose)':cpu>50?'var(--amber)':'var(--emerald)';
  const stalledCnt = h.streams.filter(s=>s.stalled).length;
  document.getElementById('h-sys').innerHTML =
    renderHealthRow('CPU', (sys.cpu_pct??'—')+'%', cpuC) +
    renderHealthRow('Memory', (sys.mem_mb??'—')+' MB') +
    renderHealthRow('Process %', (sys.mem_pct??'—')+'%') +
    `<div class="hrow"><span class="hrow-l">Uptime</span><span style="color:var(--text3);font-size:12px">${fmtUp(sys.uptime_s)}</span></div>` +
    `<div class="hrow"><span class="hrow-l">Last check</span><span style="color:var(--text3);font-size:10px">${sys.checked??'—'}</span></div>`;
  document.getElementById('h-streams').innerHTML =
    renderHealthRow('Channels loaded', h.channel_count, 'var(--cyan)') +
    renderHealthRow('Active streams', h.streams.length, 'var(--emerald)') +
    renderHealthRow('Stalled', stalledCnt, stalledCnt?'var(--rose)':'var(--emerald)') +
    renderHealthRow('Total alerts', h.alerts.length, h.alerts.length?'var(--amber)':'var(--text3)');
  const srvs=Object.values(h.servers||{});
  document.getElementById('h-servers').innerHTML = srvs.length
    ? srvs.map(s=>{const ok=s.status==='ok';return`<div class="hrow"><span class="hrow-l">${esc(s.label)}</span><span><span class="tag ${ok?'tag-ok':'tag-err'}" style="margin-right:6px">${ok?'ONLINE':'OFFLINE'}</span><span style="color:var(--text3);font-size:10px">${esc(s.checked||'')}</span></span></div>`;}).join('')
    : emptyState('No health data yet');
  const ah = h.alerts.slice(0,40).map(renderAlertRow).join('');
  document.getElementById('h-alerts').innerHTML = ah || emptyState('No alerts', '✓');
  updateBadge(h.alerts.filter(a=>a.level==='error').length);
}
document.getElementById('btn-clear-alerts').addEventListener('click', async () => {
  await fetch(B+'/api/alerts',{method:'DELETE'});
  toast('Alerts cleared', 'success'); loadHealth();
});

/* ── Logs ────────────────────────────────────────────────────────────────── */
async function loadLogs(logsData) {
  // Phase 13 Item 5: first-load skeleton (filter changes get a real reload).
  showSkeletonOnce('logs-list', 8, ['sk-w-sm', 'sk-w-md', '']);
  const level=document.getElementById('log-filter')?.value||'';
  const fetched = logsData || await api('/api/logs?limit=200'+(level?'&level='+level:''));
  if (fetched == null) { errorPanel('logs-list', 'logs', () => loadLogs()); return; }
  const data = fetched || [];
  const lbl=document.getElementById('log-count-lbl');if(lbl)lbl.textContent=data.length+' entries';
  const h = data.map(renderLogRow).join('');
  document.getElementById('logs-list').innerHTML = h || emptyState('No log entries yet', '▤');
}
document.getElementById('log-filter').addEventListener('change', loadLogs);
document.getElementById('btn-clear-logs').addEventListener('click', async () => {
  await fetch(B+'/api/logs',{method:'DELETE'});
  toast('Logs cleared', 'success'); loadLogs();
});

/* ── Analytics ───────────────────────────────────────────────────────────── */
async function loadAnalytics() {
  // Phase 13 Item 5: first-load skeletons for the two top-N bars.
  showSkeletonOnce('an-ch', 5, ['sk-w-xs', '', 'sk-w-sm']);
  showSkeletonOnce('an-us', 5, ['sk-w-xs', '', 'sk-w-sm']);
  const days=document.getElementById('an-days')?.value||7;
  const d=await api(`/api/analytics/summary?days=${days}`);
  if (!d) {
    errorPanel('an-ch', 'top channels', () => loadAnalytics());
    errorPanel('an-us', 'top users', () => loadAnalytics());
    return;
  }
  const t=d.totals||{},tw=t.total_watch_s||0;
  const th=Math.floor(tw/3600),tm2=Math.floor((tw%3600)/60);
  document.getElementById('an-totals').innerHTML =
    renderStatCard('Sessions', (t.total_sessions||0).toLocaleString(), 'cyan') +
    renderStatCard('Watch Time', `${th}h ${tm2}m`, 'amber') +
    renderStatCard('Viewers', t.unique_ips||0, 'green') +
    renderStatCard('Channels', t.unique_channels||0, 'violet');

  const hm=d.heatmap||[],maxS=Math.max(...hm.map(h=>h.sessions),1);
  let hmH='<div style="display:flex;gap:3px;align-items:flex-end;height:80px">';
  for(const h of hm){
    const pct=Math.max(Math.round((h.sessions/maxS)*100),4),now=new Date().getHours()===h.hour;
    hmH+=`<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px"><div style="width:100%;background:${now?'var(--cyan)':'rgba(0,229,255,0.2)'};height:${pct}%;border-radius:2px 2px 0 0;box-shadow:${now?'0 0 8px var(--cyan-glow)':'none'}" title="${h.hour}:00 — ${h.sessions}"></div>${h.hour%3===0?`<span style="font-size:8px;color:var(--text3)">${h.hour}</span>`:'<span style="font-size:8px"> </span>'}</div>`;
  }
  hmH+='</div>';
  document.getElementById('an-heat').innerHTML=hmH;

  let ch='';
  for(const[i,c]of(d.top_channels||[]).entries()){
    const h2=Math.floor(c.total_s/3600),m2=Math.floor((c.total_s%3600)/60);
    const pct=Math.round((c.total_s/(d.top_channels[0]?.total_s||1))*100);
    ch+=`<div style="padding:11px 14px;border-bottom:1px solid var(--border)"><div style="display:flex;justify-content:space-between;margin-bottom:5px"><span style="color:#fff;font-size:12px;font-family:var(--head);font-weight:600">${i+1}. ${esc(c.channel)}</span><span style="color:var(--text3);font-size:10px">${h2}h ${m2}m</span></div><div style="background:var(--border);height:3px;border-radius:3px"><div style="width:${pct}%;height:100%;background:var(--cyan);border-radius:3px"></div></div><div style="color:var(--text3);font-size:10px;margin-top:4px">${c.sessions} sessions</div></div>`;
  }
  document.getElementById('an-ch').innerHTML=ch||emptyState('No data yet');

  let us='';
  for(const[i,u]of(d.top_users||[]).entries()){
    const h2=Math.floor(u.total_s/3600),m2=Math.floor((u.total_s%3600)/60);
    const pct=Math.round((u.total_s/(d.top_users[0]?.total_s||1))*100);
    us+=`<div style="padding:11px 14px;border-bottom:1px solid var(--border)"><div style="display:flex;justify-content:space-between;margin-bottom:5px"><span style="color:var(--emerald);font-size:12px;font-family:var(--head);font-weight:600">${i+1}. ${esc(u.user)}</span><span style="color:var(--text3);font-size:10px">${h2}h ${m2}m</span></div><div style="background:var(--border);height:3px;border-radius:3px"><div style="width:${pct}%;height:100%;background:var(--emerald);border-radius:3px"></div></div><div style="color:var(--text3);font-size:10px;margin-top:4px">${u.sessions} sessions</div></div>`;
  }
  document.getElementById('an-us').innerHTML=us||emptyState('No data yet');

  let rs='';
  for(const s of(d.recent||[])){
    const dt=new Date(s.started_at*1000).toLocaleString();
    const dur=s.duration_s?fmtUp(s.duration_s):'<span style="color:var(--rose)">●Live</span>';
    const kb=s.bytes_total>0?Math.round(s.bytes_total/1024).toLocaleString()+' KB':'—';
    rs+=`<div style="padding:11px 14px;border-bottom:1px solid var(--border)"><div style="display:flex;justify-content:space-between"><span style="color:#fff;font-family:var(--head);font-size:13px;font-weight:600">${esc(s.channel)}</span><span style="color:var(--text3);font-size:11px">${dur}</span></div><div style="color:var(--emerald);font-size:11px;margin-top:2px">${esc(s.user)} <span style="color:var(--text3)">(${esc(s.viewer_ip)})</span></div><div style="color:var(--text3);font-size:10px;margin-top:2px">${esc(dt)} · ${kb}</div></div>`;
  }
  document.getElementById('an-rec').innerHTML = rs || emptyState('No sessions yet');

  loadBandwidth(parseInt(days));
}

async function loadBandwidth(days) {
  const d = await api('/api/bandwidth?days='+days);
  const bwEl = document.getElementById('an-bandwidth-sec');
  if (!d || !d.days || !d.days.length) { if (bwEl) bwEl.style.display='none'; return; }
  if (bwEl) bwEl.style.display = '';
  const maxGb = Math.max(...d.days.map(r=>r.gb), 0.1);
  const bars = d.days.map(r => {
    const h = Math.max(Math.round((r.gb/maxGb)*100), r.gb>0?4:1);
    const isToday = r.date === new Date().toISOString().slice(0,10);
    const label = new Date(r.date+'T12:00:00').toLocaleDateString('en',{weekday:'short'});
    return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:4px"><div style="font-size:9px;color:var(--text3)">${r.gb>0?r.gb+'GB':''}</div><div style="width:100%;background:${isToday?'var(--cyan)':'rgba(0,229,255,0.3)'};height:${h}px;border-radius:3px 3px 0 0;min-height:${r.gb>0?4:1}px" title="${r.date}: ${r.gb} GB"></div><div style="font-size:9px;color:var(--text3)">${label}</div></div>`;
  }).join('');
  bwEl.innerHTML =
    `<div class="sec-hdr"><span class="sec-title">Bandwidth (${days} days)</span><span style="font-size:10px;color:var(--text3)">Total: ${d.total_gb} GB · Avg: ${d.avg_gb} GB/day</span></div>` +
    `<div style="padding:16px"><div style="display:flex;gap:6px;align-items:flex-end;height:100px">${bars}</div></div>`;
}

document.getElementById('an-days').addEventListener('change', loadAnalytics);
document.getElementById('btn-clear-analytics').addEventListener('click', async () => {
  if(!confirm('Clear ALL watch history? This cannot be undone.'))return;
  await fetch(B+'/api/analytics',{method:'DELETE'});
  toast('Watch history cleared', 'success'); loadAnalytics();
});

/* ── Subscriptions ───────────────────────────────────────────────────────── */
async function loadSubs() {
  showSkeletonOnce('subs-detail', 2, ['sk-tall']);
  const [data,accs,status] = await Promise.all([api('/api/subscriptions'),api('/api/account-info'),api('/api/status')]);
  if(!data){errorPanel('subs-detail','subscriptions',()=>loadSubs());return;}
  const accounts=accs||{};
  const statusSubs=(status?.subscriptions||[]).reduce((m,s)=>{m[s.name]=s;return m;},{});
  const sorted=[...data].sort((a,b)=>(a.priority||1)-(b.priority||1));
  const h = sorted.map(s => renderSubCard(s, {
    full: true,
    acc:  accounts[s.name] || {},
    stat: statusSubs[s.name] || {},
  })).join('');
  document.getElementById('subs-detail').innerHTML = data.length ? h : emptyState('No subscriptions configured');
}

function resetSubForm() {
  _editingSubName = null;
  document.getElementById('a-name').value = '';
  document.getElementById('a-name').readOnly = false;
  document.getElementById('a-url').value = '';
  document.getElementById('a-user').value = '';
  document.getElementById('a-pass').value = '';
  document.getElementById('a-pass').placeholder = '';
  document.getElementById('a-max').value = 1;
  document.getElementById('sub-form-title').textContent = 'New Subscription';
  document.getElementById('btn-add-sub').textContent = 'Save';
}
function openSubFormForEdit(rec) {
  _editingSubName = rec.name;
  document.getElementById('a-name').value = rec.name;
  document.getElementById('a-name').readOnly = true;
  document.getElementById('a-url').value = rec.url || '';
  document.getElementById('a-user').value = rec.username || '';
  document.getElementById('a-pass').value = '';
  document.getElementById('a-pass').placeholder = '(unchanged)';
  document.getElementById('a-max').value = rec.max_streams || 1;
  document.getElementById('sub-form-title').textContent = 'Edit Subscription';
  document.getElementById('btn-add-sub').textContent = 'Update';
  document.getElementById('add-form-sec').style.display = '';
  document.getElementById('add-form-sec').scrollIntoView({behavior:'smooth', block:'start'});
}

document.getElementById('subs-detail').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-sub-act]'); if (!b) return;
  const enc = b.dataset.sub, act = b.dataset.subAct;
  const name = decodeURIComponent(enc);
  const cur = (await api('/api/subscriptions'))||[];
  const rec = cur.find(x=>x.name===name);
  if (!rec) return;
  if (act==='prio-up' || act==='prio-dn') {
    const p = (rec.priority||1) + (act==='prio-up'?-1:1);
    if (p<1) return;
    await fetch(B+'/api/subscriptions/'+enc,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({priority:p})});
    loadSubs();
  } else if (act==='disable' || act==='enable') {
    await fetch(B+'/api/subscriptions/'+enc,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled: act==='enable'})});
    loadSubs();
  } else if (act==='edit') {
    openSubFormForEdit(rec);
  } else if (act==='delete') {
    if (!confirm('Delete "'+name+'"?')) return;
    await fetch(B+'/api/subscriptions/'+enc,{method:'DELETE'});
    toast('Deleted', 'success'); loadSubs();
  }
});

document.getElementById('btn-toggle-add').addEventListener('click', () => {
  const f = document.getElementById('add-form-sec');
  const opening = f.style.display === 'none';
  if (opening) resetSubForm();
  f.style.display = opening ? '' : 'none';
});
document.getElementById('btn-cancel-add').addEventListener('click', () => {
  document.getElementById('add-form-sec').style.display = 'none';
  resetSubForm();
});
document.getElementById('btn-add-sub').addEventListener('click', async function () {
  // Phase 13 Item 5: button busy-state during save/edit network round-trip.
  const btn = this;
  if (_editingSubName) {
    const body = {
      url:  document.getElementById('a-url').value.trim(),
      username: document.getElementById('a-user').value.trim(),
      max_streams: parseInt(document.getElementById('a-max').value)||1,
    };
    const pw = document.getElementById('a-pass').value;
    if (pw) body.password = pw;
    if(!body.url||!body.username){toast('URL and username required', 'warning');return;}
    const enc = encodeURIComponent(_editingSubName);
    await withButtonBusy(btn, 'Saving…', async () => {
      const r = await fetch(B+'/api/subscriptions/'+enc,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if (r.ok) {
        document.getElementById('add-form-sec').style.display='none';
        resetSubForm(); loadSubs(); toast('Subscription updated!', 'success');
      } else { const e=await r.json(); toast(e.detail||'Error', 'error'); }
    });
    return;
  }
  const body = {
    name: document.getElementById('a-name').value.trim(),
    url:  document.getElementById('a-url').value.trim(),
    username: document.getElementById('a-user').value.trim(),
    password: document.getElementById('a-pass').value,
    max_streams: parseInt(document.getElementById('a-max').value)||1,
  };
  if(!body.name||!body.url||!body.username||!body.password){toast('All fields required', 'warning');return;}
  await withButtonBusy(btn, 'Saving…', async () => {
    const r = await fetch(B+'/api/subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if (r.ok) {
      document.getElementById('add-form-sec').style.display='none';
      resetSubForm(); loadSubs(); toast('Subscription added!', 'success');
    }
    else { const e=await r.json(); toast(e.detail, 'error'); }
  });
});

/* ── Users (Phase 12) ────────────────────────────────────────────────────── */
let _usList         = [];          // raw /api/users response.users
let _usSelected     = new Set();   // selected usernames for bulk actions
let _usDrawerUser   = null;        // username of the user currently open in drawer
let _usDrawerData   = null;        // last GET /api/users/{u} payload
let _usAllSubs      = [];          // all subscription names (for allowed_subscriptions picker)
let _usDrawerDirty  = false;       // tracks unsaved edits in the drawer

const _usDebouncedReload = debounce(() => loadUsers(/*silent*/true), 350);

function _usStatus(u) {
  // Returns {key, label, color, days} for a user row.
  if (!u.enabled)        return {key:'disabled', label:'DISABLED', color:'var(--text3)', days:null};
  if (u.expired)         return {key:'expired',  label:'EXPIRED',  color:'#000',          days:null};
  if (!u.expires_at)     return {key:'active',   label:'ACTIVE',   color:'var(--emerald)', days:null};
  const secs = u.expires_at - (Date.now()/1000);
  const days = Math.floor(secs/86400);
  if (secs <= 86400)     return {key:'critical', label:'CRITICAL', color:'var(--rose)',   days};
  if (secs <= 7*86400)   return {key:'expiring', label:'EXPIRING', color:'var(--amber)',  days};
  return {key:'active', label:'ACTIVE', color:'var(--emerald)', days};
}

function _usFmtExpiry(u) {
  if (!u.expires_at) return '—';
  const d = new Date(u.expires_at * 1000);
  return d.toLocaleDateString('en', {year:'numeric', month:'short', day:'numeric'});
}

function _usFmtSeen(ts) {
  if (!ts) return '—';
  const delta = Math.floor(Date.now()/1000) - ts;
  if (delta < 60)        return delta + 's ago';
  if (delta < 3600)      return Math.floor(delta/60) + 'm ago';
  if (delta < 86400)     return Math.floor(delta/3600) + 'h ago';
  return Math.floor(delta/86400) + 'd ago';
}

function _usFmtBytes(n) {
  if (!n) return '0 B';
  if (n < 1024)         return n + ' B';
  if (n < 1024*1024)    return (n/1024).toFixed(1) + ' KB';
  if (n < 1024*1024*1024) return (n/1024/1024).toFixed(1) + ' MB';
  return (n/1024/1024/1024).toFixed(2) + ' GB';
}

async function loadUsers(silent=false) {
  // Phase 13 Item 5: first-load skeleton (refresh button silently re-renders).
  showSkeletonOnce('us-table', 6, ['sk-w-xs', '', 'sk-w-md', '', 'sk-w-sm', '', 'sk-w-lg']);
  const d = await api('/api/users');
  if (!d) { errorPanel('us-table', 'users', () => loadUsers()); return; }
  _usList = d.users || [];
  // Render stats strip
  const total    = _usList.length;
  const active   = _usList.filter(u => u.enabled && !u.expired).length;
  const sessions = d.active_sessions || 0;
  const expiring = _usList.filter(u => {
    if (!u.enabled || u.expired || !u.expires_at) return false;
    const days = (u.expires_at - Date.now()/1000) / 86400;
    return days >= 0 && days <= 7;
  }).length;
  const disabled = _usList.filter(u => !u.enabled).length;
  document.getElementById('us-stats').innerHTML =
    renderStatCard('Total',     total,    'cyan')   +
    renderStatCard('Active',    active,   'green')  +
    renderStatCard('Sessions',  sessions, 'violet') +
    renderStatCard('Expiring',  expiring, 'amber')  +
    renderStatCard('Disabled',  disabled, 'rose');
  document.getElementById('us-count-lbl').textContent = total + ' user' + (total!==1?'s':'');
  // Drop selections that no longer exist
  const live = new Set(_usList.map(u => u.username));
  for (const sel of [..._usSelected]) if (!live.has(sel)) _usSelected.delete(sel);
  renderUsersTable();
  if (silent !== true) {
    // First load: also fetch subs list for the drawer's allowed_subscriptions picker (cached)
    if (!_usAllSubs.length) {
      const s = await api('/api/subscriptions');
      if (Array.isArray(s)) _usAllSubs = s.map(x => x.name);
    }
  }
}

function renderUsersTable() {
  const search = (document.getElementById('us-search')?.value || '').toLowerCase().trim();
  const sFilt  = document.getElementById('us-status-filter')?.value || '';
  const sort   = document.getElementById('us-sort')?.value || 'username';

  let rows = _usList.slice();
  if (search) {
    rows = rows.filter(u =>
      (u.username||'').toLowerCase().includes(search) ||
      (u.last_seen_ip||'').toLowerCase().includes(search) ||
      (u.notes||'').toLowerCase().includes(search));
  }
  if (sFilt) {
    rows = rows.filter(u => _usStatus(u).key === sFilt);
  }
  rows.sort((a,b) => {
    if (sort === 'status')    return _usStatus(a).key.localeCompare(_usStatus(b).key);
    if (sort === 'expires')   return (a.expires_at||9e15) - (b.expires_at||9e15);
    if (sort === 'active')    return (b.active_connections||0) - (a.active_connections||0);
    if (sort === 'last_seen') return (b.last_seen_ts||0) - (a.last_seen_ts||0);
    return (a.username||'').localeCompare(b.username||'');
  });

  const summary = document.getElementById('us-summary');
  if (summary) summary.textContent = `${rows.length}/${_usList.length} shown`;

  const tbl = document.getElementById('us-table');
  if (!rows.length) { tbl.innerHTML = emptyState('No users match', '👥'); _usUpdateSelBar(); return; }
  tbl.innerHTML = rows.map(renderUserRow).join('');
  _usUpdateSelBar();
}

function renderUserRow(u) {
  const st  = _usStatus(u);
  const sel = _usSelected.has(u.username);
  const enc = encodeURIComponent(u.username);
  const exp = _usFmtExpiry(u);
  const expHint = (st.days != null && u.enabled && !u.expired) ? `<div class="us-cell-sub">${st.days}d left</div>` : '';
  const conn = `${u.active_connections||0}/${u.max_connections||1}`;
  const seen = _usFmtSeen(u.last_seen_ts);
  const seenIp = u.last_seen_ip ? `<div class="us-cell-sub">${esc(u.last_seen_ip)}</div>` : '';
  return `<div class="us-row ${sel?'selected':''}" data-us-row="${enc}">
    <div class="us-cell-chk">
      <input type="checkbox" data-us-toggle="${enc}" ${sel?'checked':''}>
    </div>
    <div class="us-cell-name">
      <div class="us-name">${esc(u.username)}</div>
      <div class="us-cell-sub">${u.notes ? esc(u.notes) : '—'}</div>
    </div>
    <div class="us-cell-status">
      <span class="us-badge" style="border-color:${st.color};color:${st.color}">${st.label}</span>
    </div>
    <div class="us-cell-exp">
      ${esc(exp)}${expHint}
    </div>
    <div class="us-cell-conn">
      <span style="color:${(u.active_connections||0)>=(u.max_connections||1)?'var(--amber)':'var(--text)'}">${conn}</span>
    </div>
    <div class="us-cell-seen">
      ${esc(seen)}${seenIp}
    </div>
    <div class="us-cell-act">
      <button class="btn btn-sm btn-cyan"  data-us-edit="${enc}">Edit</button>
      <button class="btn btn-sm btn-rose"  data-us-kick="${enc}" ${u.active_connections?'':'disabled'}>Kick</button>
    </div>
  </div>`;
}

function _usUpdateSelBar() {
  const n = _usSelected.size;
  const bar  = document.getElementById('us-sel-bar');
  const info = document.getElementById('us-sel-info');
  const allChk = document.getElementById('us-chk-all');
  if (bar) bar.style.display = n >= 1 ? 'flex' : 'none';
  if (info) info.textContent = n + ' user' + (n!==1?'s':'') + ' selected';
  if (allChk) {
    const visible = document.querySelectorAll('[data-us-toggle]').length;
    allChk.checked = visible > 0 && n >= visible;
    allChk.indeterminate = n > 0 && n < visible;
  }
}

/* Table interactions */
document.getElementById('us-table').addEventListener('click', (e) => {
  const editBtn = e.target.closest('[data-us-edit]');
  if (editBtn) { openUsDrawer(decodeURIComponent(editBtn.dataset.usEdit)); return; }
  const kickBtn = e.target.closest('[data-us-kick]');
  if (kickBtn) { _usKickOne(decodeURIComponent(kickBtn.dataset.usKick)); return; }
  const tog = e.target.closest('[data-us-toggle]');
  if (tog) {
    const name = decodeURIComponent(tog.dataset.usToggle);
    if (tog.checked) _usSelected.add(name); else _usSelected.delete(name);
    tog.closest('.us-row')?.classList.toggle('selected', tog.checked);
    _usUpdateSelBar();
    return;
  }
});

document.getElementById('us-chk-all').addEventListener('change', (e) => {
  const checked = e.target.checked;
  document.querySelectorAll('[data-us-toggle]').forEach(cb => {
    const name = decodeURIComponent(cb.dataset.usToggle);
    cb.checked = checked;
    if (checked) _usSelected.add(name); else _usSelected.delete(name);
    cb.closest('.us-row')?.classList.toggle('selected', checked);
  });
  _usUpdateSelBar();
});

document.getElementById('us-search').addEventListener('input', debounce(renderUsersTable, 200));
document.getElementById('us-status-filter').addEventListener('change', renderUsersTable);
document.getElementById('us-sort').addEventListener('change', renderUsersTable);
document.getElementById('btn-us-refresh').addEventListener('click', () => loadUsers());
document.getElementById('btn-us-cancel').addEventListener('click', () => {
  _usSelected.clear();
  document.querySelectorAll('[data-us-toggle]').forEach(cb => cb.checked = false);
  document.querySelectorAll('.us-row.selected').forEach(r => r.classList.remove('selected'));
  _usUpdateSelBar();
});
document.getElementById('btn-us-new').addEventListener('click', () => openUsDrawer(null));

/* Bulk actions */
document.getElementById('us-sel-bar').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-us-bulk]'); if (!b) return;
  const action = b.dataset.usBulk;
  const usernames = [..._usSelected];
  if (!usernames.length) return;
  const ulist = '\n - ' + usernames.join('\n - ');
  let body = {usernames};
  let path = '';
  let okMsg = '';
  if (action === 'enable') {
    if (!confirm(`Enable ${usernames.length} user(s)?`+ulist)) return;
    path = '/api/users/bulk/enable'; okMsg = 'Enabled';
  } else if (action === 'disable') {
    if (!confirm(`Disable ${usernames.length} user(s)? Active sessions will be kicked.`+ulist)) return;
    path = '/api/users/bulk/disable'; okMsg = 'Disabled';
  } else if (action === 'extend') {
    const daysStr = prompt('Extend expiry by how many days?', '30');
    if (!daysStr) return;
    const days = parseInt(daysStr, 10);
    if (!days || days <= 0) { toast('Invalid days', 'warning'); return; }
    body.days = days;
    path = '/api/users/bulk/extend'; okMsg = `+${days}d`;
  } else if (action === 'kick') {
    if (!confirm(`Kick all active sessions for ${usernames.length} user(s)?`+ulist)) return;
    path = '/api/users/bulk/kick'; okMsg = 'Kicked';
  } else if (action === 'delete') {
    if (!confirm(`DELETE ${usernames.length} user(s)? This cannot be undone.`+ulist)) return;
    path = '/api/users/bulk/delete'; okMsg = 'Deleted';
  } else { return; }

  // Phase 13 Item 5: busy-state on the clicked bulk-action button.
  await withButtonBusy(b, 'Working…', async () => {
    const r = await fetch(B+path, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    if (r.ok) {
      const j = await r.json();
      const missing = (j.missing||[]).length;
      toast(`${okMsg} — ${missing?missing+' missing':'all ok'}`, 'success');
      _usSelected.clear();
      loadUsers();
    } else {
      const err = await r.json().catch(()=>({}));
      toast(err.detail || 'Error', 'error');
    }
  });
});

async function _usKickOne(username) {
  if (!confirm(`Kick all sessions for "${username}"?`)) return;
  const r = await fetch(B+'/api/users/'+encodeURIComponent(username)+'/kick', {method:'POST'});
  if (r.ok) { toast('Kicked', 'success'); loadUsers(); }
  else      { toast('Error', 'error'); }
}

/* ── User Drawer ─────────────────────────────────────────────────────────── */
function openUsDrawer(username) {
  _usDrawerUser  = username;       // null = create mode
  _usDrawerData  = null;
  _usDrawerDirty = false;
  document.getElementById('us-drawer').classList.add('open');
  document.getElementById('us-drawer-overlay').classList.add('open');
  document.getElementById('us-drawer').setAttribute('aria-hidden', 'false');
  if (username) {
    _loadUsDrawer(username, true);
  } else {
    document.getElementById('us-d-title').textContent = 'New User';
    document.getElementById('us-d-sub').textContent   = 'Create Xtream account';
    document.getElementById('us-d-delete').style.display = 'none';
    renderUsDrawerBody({user:{
      username:'', enabled:true, expires_at:0, max_connections:1,
      allowed_subscriptions:[], notes:'',
      active_connections:0, total_streams:0, last_seen_ts:0,
      last_seen_ip:'', bytes_served:0, password:'',
    }, sessions:[]}, /*isCreate*/true);
  }
}

function closeUsDrawer() {
  if (_usDrawerDirty && !confirm('Discard unsaved changes?')) return;
  document.getElementById('us-drawer').classList.remove('open');
  document.getElementById('us-drawer-overlay').classList.remove('open');
  document.getElementById('us-drawer').setAttribute('aria-hidden', 'true');
  _usDrawerUser  = null;
  _usDrawerData  = null;
  _usDrawerDirty = false;
}

async function _loadUsDrawer(username, reopen) {
  const d = await api('/api/users/'+encodeURIComponent(username));
  if (!d || !d.user) {
    document.getElementById('us-d-body').innerHTML = emptyState('User not found', '✗');
    return;
  }
  _usDrawerData = d;
  document.getElementById('us-d-title').textContent = d.user.username;
  document.getElementById('us-d-sub').textContent   =
    `${d.user.active_connections||0}/${d.user.max_connections||1} active · ` +
    `${d.user.total_streams||0} lifetime streams`;
  document.getElementById('us-d-delete').style.display = '';
  renderUsDrawerBody(d, /*isCreate*/false);
}

function renderUsDrawerBody(d, isCreate) {
  const u = d.user;
  const sessions = d.sessions || [];
  const allowedSubs = new Set(u.allowed_subscriptions || []);

  const expDate = u.expires_at
    ? new Date(u.expires_at*1000).toISOString().slice(0,10) : '';

  const subChips = _usAllSubs.length
    ? _usAllSubs.map(s => {
        const on = allowedSubs.has(s);
        const enc = encodeURIComponent(s);
        return `<label class="us-chip ${on?'on':''}" data-us-sub="${enc}"><input type="checkbox" ${on?'checked':''}>${esc(s)}</label>`;
      }).join('')
    : '<span class="text-sm">No subscriptions loaded</span>';

  const sessRows = sessions.length
    ? sessions.map(s => `<div class="us-sess">
        <div class="flex-1">
          <div class="us-sess-ch">${esc(s.channel_name || s.channel_key)}</div>
          <div class="us-cell-sub">${esc(s.client_ip)} · ${fmtUp(s.duration_s)} · ${_usFmtBytes(s.bytes_sent)}</div>
        </div>
        <button class="btn btn-sm btn-rose" data-us-sess-kick="${esc(s.id)}">Kick</button>
      </div>`).join('')
    : '<div class="empty" style="padding:18px">No active sessions</div>';

  const lifetime = `
    <div class="hrow"><span class="hrow-l">Total streams</span><span class="hrow-v">${u.total_streams||0}</span></div>
    <div class="hrow"><span class="hrow-l">Bytes served</span><span class="hrow-v">${_usFmtBytes(u.bytes_served)}</span></div>
    <div class="hrow"><span class="hrow-l">Last seen</span><span class="hrow-v" style="font-size:12px">${_usFmtSeen(u.last_seen_ts)}</span></div>
    <div class="hrow"><span class="hrow-l">Last IP</span><span class="hrow-v" style="font-size:12px">${esc(u.last_seen_ip||'—')}</span></div>
  `;

  document.getElementById('us-d-body').innerHTML = `
    <div class="sec" style="margin-bottom:14px">
      <div class="sec-hdr"><span class="sec-title">Account</span></div>
      <div class="form-grid">
        <div class="f-field">
          <label class="f-label">Username</label>
          <input class="f-input" id="us-f-username" value="${esc(u.username)}" ${isCreate?'':'readonly'}>
        </div>
        <div class="f-field">
          <label class="f-label">Password</label>
          <div style="display:flex;gap:6px">
            <input class="f-input" id="us-f-password" value="${esc(u.password||'')}" placeholder="${isCreate?'(auto-generate if empty)':'(unchanged)'}" type="text" style="flex:1">
            <button class="btn btn-sm btn-violet" id="us-f-regen" ${isCreate?'style="display:none"':''}>Regen</button>
          </div>
        </div>
        <div class="f-field">
          <label class="f-label">Max Connections</label>
          <input class="f-input" id="us-f-max" type="number" min="1" value="${u.max_connections||1}">
        </div>
        <div class="f-field">
          <label class="f-label">Expires (YYYY-MM-DD, blank = never)</label>
          <input class="f-input" id="us-f-expires" type="date" value="${expDate}">
        </div>
        <div class="f-field">
          <label class="f-label">Status</label>
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;color:var(--text2);font-size:12px;padding:11px 0">
            <input type="checkbox" id="us-f-enabled" ${u.enabled?'checked':''} style="width:16px;height:16px;accent-color:var(--emerald)">
            Enabled
          </label>
        </div>
        <div class="f-field full">
          <label class="f-label">Notes</label>
          <input class="f-input" id="us-f-notes" value="${esc(u.notes||'')}" placeholder="Internal label / contact / device">
        </div>
      </div>
    </div>

    <div class="sec" style="margin-bottom:14px">
      <div class="sec-hdr"><span class="sec-title">Allowed Subscriptions</span><span class="text-xs">${u.allowed_subscriptions?.length || 'all'}</span></div>
      <div class="us-chip-grid" id="us-f-subs">${subChips}</div>
    </div>

    ${isCreate ? '' : `
    <div class="sec" style="margin-bottom:14px">
      <div class="sec-hdr"><span class="sec-title">Active Sessions</span><span class="text-xs">${sessions.length}</span></div>
      ${sessRows}
    </div>
    <div class="sec">
      <div class="sec-hdr"><span class="sec-title">Lifetime Stats</span></div>
      ${lifetime}
    </div>`}
  `;

  // Mark dirty on any input
  document.getElementById('us-d-body').addEventListener('input', () => { _usDrawerDirty = true; }, {once:true});

  // Chip toggles
  document.getElementById('us-f-subs').addEventListener('click', (e) => {
    const lbl = e.target.closest('[data-us-sub]'); if (!lbl) return;
    setTimeout(() => lbl.classList.toggle('on', lbl.querySelector('input').checked), 0);
    _usDrawerDirty = true;
  });

  // Session kick buttons
  if (!isCreate) {
    document.getElementById('us-d-body').addEventListener('click', async (e) => {
      const k = e.target.closest('[data-us-sess-kick]'); if (!k) return;
      const sid = k.dataset.usSessKick;
      const r = await fetch(B+`/api/users/${encodeURIComponent(_usDrawerUser)}/kick/${encodeURIComponent(sid)}`, {method:'POST'});
      if (r.ok) { toast('Session kicked', 'success'); _loadUsDrawer(_usDrawerUser); }
      else      { toast('Error', 'error'); }
    });

    // Regenerate password
    const regen = document.getElementById('us-f-regen');
    if (regen) regen.addEventListener('click', async () => {
      if (!confirm(`Regenerate password for "${_usDrawerUser}"? Active sessions will be kicked.`)) return;
      const r = await fetch(B+'/api/users/'+encodeURIComponent(_usDrawerUser)+'/regenerate_password', {method:'POST'});
      if (r.ok) {
        const j = await r.json();
        document.getElementById('us-f-password').value = j.password;
        toast('New password: '+j.password, 'success', 8000);
        _loadUsDrawer(_usDrawerUser);
      } else { toast('Error', 'error'); }
    });
  }
}

function _usCollectDrawerForm(isCreate) {
  const username = document.getElementById('us-f-username').value.trim();
  const password = document.getElementById('us-f-password').value;
  const max      = parseInt(document.getElementById('us-f-max').value) || 1;
  const expDate  = document.getElementById('us-f-expires').value;
  const enabled  = document.getElementById('us-f-enabled').checked;
  const notes    = document.getElementById('us-f-notes').value.trim();
  const subs = [...document.querySelectorAll('#us-f-subs [data-us-sub] input:checked')]
    .map(cb => decodeURIComponent(cb.parentElement.dataset.usSub));

  const body = {
    enabled,
    max_connections:       max,
    allowed_groups:        [],   // group filtering removed — every user gets all proxy channels
    allowed_subscriptions: subs,
    notes,
  };
  if (expDate) {
    // YYYY-MM-DD → epoch (end of day local)
    const dt = new Date(expDate + 'T23:59:59');
    body.expires_at = Math.floor(dt.getTime()/1000);
  } else {
    body.expires_at = 0;
  }
  if (password) body.password = password;
  if (isCreate) body.username = username;
  return body;
}

document.getElementById('us-d-close').addEventListener('click', closeUsDrawer);
document.getElementById('us-d-cancel').addEventListener('click', closeUsDrawer);
document.getElementById('us-drawer-overlay').addEventListener('click', closeUsDrawer);

document.getElementById('us-d-save').addEventListener('click', async function () {
  // Phase 13 Item 5: busy-state on Save (covers both create + edit paths).
  const btn = this;
  const isCreate = !_usDrawerUser;
  const body = _usCollectDrawerForm(isCreate);
  if (isCreate) {
    if (!body.username) { toast('Username required', 'warning'); return; }
  }
  const url = isCreate ? '/api/users' : '/api/users/'+encodeURIComponent(_usDrawerUser);
  const method = isCreate ? 'POST' : 'PATCH';
  await withButtonBusy(btn, 'Saving…', async () => {
    const r = await fetch(B+url, {
      method, headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    if (r.ok) {
      const j = await r.json();
      _usDrawerDirty = false;
      if (isCreate) {
        const newName = j.user?.username || body.username;
        toast('User created: ' + newName, 'success', 5000);
        _usDrawerUser = newName;
        _loadUsDrawer(newName);
      } else {
        toast('Saved' + (j.kicked_sessions ? ` (${j.kicked_sessions} kicked)` : ''), 'success');
        _loadUsDrawer(_usDrawerUser);
      }
      loadUsers(/*silent*/true);
    } else {
      const err = await r.json().catch(()=>({}));
      toast(err.detail || 'Error', 'error');
    }
  });
});

document.getElementById('us-d-delete').addEventListener('click', async function () {
  // Phase 13 Item 5: busy-state during DELETE round-trip.
  const btn = this;
  if (!_usDrawerUser) return;
  if (!confirm(`DELETE user "${_usDrawerUser}"? This cannot be undone.`)) return;
  await withButtonBusy(btn, 'Deleting…', async () => {
    const r = await fetch(B+'/api/users/'+encodeURIComponent(_usDrawerUser), {method:'DELETE'});
    if (r.ok) {
      const j = await r.json();
      toast('Deleted' + (j.kicked_sessions ? ` (${j.kicked_sessions} kicked)` : ''), 'success');
      _usDrawerDirty = false;
      closeUsDrawer();
      loadUsers();
    } else { toast('Error', 'error'); }
  });
});

/* ── Dispatch (Phase 12 Checkpoint C) ────────────────────────────────────── */
let _dpDecisions = [];        // last /api/dispatch/decisions payload
let _dpStats     = null;      // last /api/dispatch/stats payload
let _dpHealth    = [];        // last /api/subscriptions/health rows
let _dpExpanded  = new Set(); // expanded channel_keys
let _dpPollTimer = null;

function _dpFmtAge(ts) {
  if (!ts) return '—';
  const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if (s < 60)    return s + 's';
  if (s < 3600)  return Math.floor(s/60) + 'm';
  if (s < 86400) return Math.floor(s/3600) + 'h';
  return Math.floor(s/86400) + 'd';
}

async function loadDispatch(silent=false) {
  // Phase 13 Item 5: first-load skeletons for the two list panes (the polling
  // loop hits this fn every 5s; the once-gate keeps it from flashing).
  showSkeletonOnce('dp-decisions',    5, ['', 'sk-w-md', 'sk-w-md', 'sk-w-xs', 'sk-w-xs', 'sk-w-sm']);
  showSkeletonOnce('dp-failover-log', 4, ['sk-w-sm', 'sk-w-md', '']);
  // Pull all four sources in parallel
  const [dec, stats, health, fl] = await Promise.all([
    api('/api/dispatch/decisions'),
    api('/api/dispatch/stats'),
    api('/api/subscriptions/health'),
    api('/api/logs?event=DISPATCH&limit=50'),
  ]);
  if (!dec || !stats || !health) {
    errorPanel('dp-decisions',    'dispatch decisions', () => loadDispatch());
    errorPanel('dp-failover-log', 'dispatch failover log', () => loadDispatch());
    return;
  }
  _dpDecisions = dec.active_decisions || [];
  _dpStats     = stats;
  _dpHealth    = health.subscriptions || [];

  // Header sub-line
  const sub = document.getElementById('dp-sub-lbl');
  if (sub) sub.textContent =
    `${_dpDecisions.length} cached decision${_dpDecisions.length!==1?'s':''} · ` +
    `sticky=${dec.sticky_seconds}s · ` +
    (dec.enabled ? 'dispatcher enabled' : 'dispatcher DISABLED');

  // Stat strip
  const totStreams = Object.values(stats.per_sub||{}).reduce((a,b)=>a+(b.streams||0),0);
  const totFoFrom  = Object.values(stats.per_sub||{}).reduce((a,b)=>a+(b.failovers_from||0),0);
  document.getElementById('dp-stats').innerHTML =
    renderStatCard('Cache size',     dec.cache_size,                'cyan')   +
    renderStatCard('Streams (life)', totStreams,                    'green')  +
    renderStatCard('Failovers',      totFoFrom,                     'amber')  +
    renderStatCard('No-cand events', stats.no_candidate_events||0,  'rose')   +
    renderStatCard('FO limit hits',  stats.failover_limit_hits||0,  'violet') +
    renderStatCard('Drained',        (dec.drained_subs||[]).length, 'rose');

  renderDpSubs();
  renderDpDecisions();
  renderDpFailoverLog(Array.isArray(fl) ? fl : []);

  // Kick off polling on first entry; the timer self-stops when the pane changes.
  if (isPaneOpen('dispatch')) _dpStartPoll();
}

function renderDpSubs() {
  const host = document.getElementById('dp-subs');
  if (!host) return;
  if (!_dpHealth.length) { host.innerHTML = emptyState('No subscriptions configured', '⊞'); return; }
  host.innerHTML = _dpHealth.map(s => {
    const util = s.max_streams ? Math.round((s.active_streams / s.max_streams) * 100) : 0;
    const utilColor = util >= 100 ? 'var(--rose)'
                    : util >= 75  ? 'var(--amber)'
                    : 'var(--emerald)';
    const score = s.score != null ? Math.round(s.score) : '—';
    const drained = !!s.drained;
    const enc = encodeURIComponent(s.name);
    const expHint = (s.account_days_left != null && s.account_days_left >= 0)
      ? ` · acct ${s.account_days_left}d` : '';
    return `<div class="dp-sub-card ${drained?'drained':''}">
      <div class="dp-sub-hdr">
        <div class="dp-sub-name">${esc(s.name)}</div>
        <label class="dp-drain-toggle" title="${drained?'Re-enable for new dispatches':'Stop new dispatches to this sub (active streams continue)'}">
          <input type="checkbox" data-dp-drain="${enc}" ${drained?'checked':''}>
          <span>${drained?'DRAINED':'live'}</span>
        </label>
      </div>
      <div class="dp-sub-meta">score ${score}${expHint}</div>
      <div class="dp-gauge-wrap">
        <div class="dp-gauge-bar">
          <div class="dp-gauge-fill" style="width:${Math.min(100,util)}%;background:${utilColor}"></div>
        </div>
        <div class="dp-gauge-lbl">${s.active_streams||0}/${s.max_streams||0} slots · ${util}%</div>
      </div>
      <div class="dp-sub-row">
        <div><div class="dp-k">Share</div><div class="dp-v">${s.dispatch_share||0}%</div></div>
        <div><div class="dp-k">Streams</div><div class="dp-v">${s.dispatch_streams||0}</div></div>
        <div><div class="dp-k">FO from</div><div class="dp-v">${s.failovers_from||0}</div></div>
        <div><div class="dp-k">FO to</div><div class="dp-v">${s.failovers_to||0}</div></div>
      </div>
    </div>`;
  }).join('');
}

function renderDpDecisions() {
  const host = document.getElementById('dp-decisions');
  if (!host) return;
  if (!_dpDecisions.length) {
    host.innerHTML = emptyState('No active dispatch decisions', '⇄');
    return;
  }
  host.innerHTML = _dpDecisions.map(renderDpDecisionRow).join('');
}

function renderDpDecisionRow(d) {
  const enc      = encodeURIComponent(d.channel_key);
  const expanded = _dpExpanded.has(d.channel_key);
  const fo       = d.failover_count || 0;
  const foColor  = fo >= 2 ? 'var(--rose)' : fo >= 1 ? 'var(--amber)' : 'var(--text3)';
  const cands = (d.candidates || []).map((c, i) => {
    const status = c.drained ? 'drained'
                  : !c.available ? 'down'
                  : c.active ? 'active'
                  : c.fail_count > 0 ? 'cooldown'
                  : 'standby';
    const statusColor = {
      active:'var(--emerald)', standby:'var(--text3)', cooldown:'var(--amber)',
      down:'var(--rose)',     drained:'var(--rose)',
    }[status];
    return `<div class="dp-cand">
      <span class="dp-cand-idx">#${i+1}</span>
      <span class="dp-cand-sub">${esc(c.sub)}</span>
      <span class="dp-cand-status" style="color:${statusColor}">${status.toUpperCase()}</span>
      <span class="dp-cand-meta">score ${Math.round(c.score||0)} · q=${esc(c.quality||'?')} · fail=${c.fail_count||0}</span>
    </div>`;
  }).join('');
  return `<div class="dp-row ${expanded?'expanded':''}" data-dp-row="${enc}">
    <div class="dp-c-ch"><div class="dp-ch-name">${esc(d.channel_name)}</div><div class="dp-ch-sub">${esc(d.reason||'')}</div></div>
    <div class="dp-c-user">${esc(d.user || '—')}</div>
    <div class="dp-c-sub">${esc(d.picked_sub || '—')}</div>
    <div class="dp-c-cands">${d.candidate_count||0}</div>
    <div class="dp-c-fo" style="color:${foColor}">${fo}</div>
    <div class="dp-c-age">${_dpFmtAge(d.picked_at)}</div>
    <div class="dp-c-act">
      <button class="btn btn-sm btn-amber" data-dp-inv="${enc}" title="Force re-pick on next viewer">↻</button>
    </div>
    ${expanded ? `<div class="dp-cands">${cands || '<span class="text-sm">No candidates</span>'}</div>` : ''}
  </div>`;
}

function renderDpFailoverLog(entries) {
  const host = document.getElementById('dp-failover-log');
  if (!host) return;
  // Filter to dispatch-related events only
  const interesting = entries.filter(e =>
    /FAILOVER|NO CANDIDATES|DISPATCH (DRAIN|UNDRAIN|INVALIDATED)/i.test(e.event || ''));
  if (!interesting.length) {
    host.innerHTML = emptyState('No recent failover activity', '✓');
    return;
  }
  host.innerHTML = interesting.map(e => {
    const color = /FAILOVER|NO CANDIDATES/i.test(e.event) ? 'var(--amber)' : 'var(--cyan)';
    return `<div class="dp-fl-row">
      <div class="dp-fl-ts">${new Date((e.ts||0)*1000).toTimeString().slice(0,8)}</div>
      <div class="dp-fl-ev" style="color:${color}">${esc(e.event||'')}</div>
      <div class="dp-fl-detail">${esc(e.detail||'')}${e.sub?` <span class="text-xs">(${esc(e.sub)})</span>`:''}</div>
    </div>`;
  }).join('');
}

/* Dispatch interactions */
document.getElementById('btn-dp-refresh').addEventListener('click', () => loadDispatch());
document.getElementById('btn-dp-clear').addEventListener('click', async function () {
  // Phase 13 Item 5: busy-state — this loop fires N invalidate calls serially,
  // so on a busy box "Clearing…" can stay visible for a noticeable beat.
  const btn = this;
  if (!confirm('Clear ALL cached dispatch decisions? Active streams continue, but next viewers re-pick fresh.')) return;
  await withButtonBusy(btn, 'Clearing…', async () => {
    // No bulk-clear endpoint — invalidate each cached key in turn
    let n = 0;
    for (const d of _dpDecisions) {
      const r = await fetch(B+'/api/dispatch/invalidate/'+encodeURIComponent(d.channel_key), {method:'POST'});
      if (r.ok) n++;
    }
    toast(`Cleared ${n} decision${n!==1?'s':''}`, 'success');
    loadDispatch();
  });
});

document.getElementById('dp-decisions').addEventListener('click', async (e) => {
  // Per-row invalidate button
  const inv = e.target.closest('[data-dp-inv]');
  if (inv) {
    e.stopPropagation();
    const key = decodeURIComponent(inv.dataset.dpInv);
    const r = await fetch(B+'/api/dispatch/invalidate/'+encodeURIComponent(key), {method:'POST'});
    if (r.ok) { toast('Invalidated', 'success'); loadDispatch(); }
    else      { toast('Error', 'error'); }
    return;
  }
  // Row click → toggle expand
  const row = e.target.closest('[data-dp-row]');
  if (!row) return;
  const key = decodeURIComponent(row.dataset.dpRow);
  if (_dpExpanded.has(key)) _dpExpanded.delete(key);
  else                      _dpExpanded.add(key);
  renderDpDecisions();
});

document.getElementById('dp-subs').addEventListener('change', async (e) => {
  const cb = e.target.closest('[data-dp-drain]');
  if (!cb) return;
  const sub = decodeURIComponent(cb.dataset.dpDrain);
  const action = cb.checked ? 'drain' : 'undrain';
  const r = await fetch(B+`/api/dispatch/${action}/${encodeURIComponent(sub)}`, {method:'POST'});
  if (r.ok) {
    toast(sub + ' ' + (cb.checked ? 'drained' : 'undrained'), 'success');
    loadDispatch();
  } else {
    toast('Error', 'error');
    cb.checked = !cb.checked;
  }
});

// Poll while the Dispatch tab is visible. There's no SSE topic for dispatch
// today — failover counts come through the existing log topic, but the
// decision cache + per-sub stats need a periodic refresh to stay live.
function _dpStartPoll() {
  if (_dpPollTimer) return;
  // Phase 13 Item 5: indeterminate sweep stripe while polls are in flight.
  _dpPollTimer = setInterval(async () => {
    if (!isPaneOpen('dispatch')) { _dpStopPoll(); return; }
    progressIndeterminate(true);
    try { await loadDispatch(/*silent*/true); }
    finally { progressIndeterminate(false); }
  }, 5000);
}
function _dpStopPoll() {
  if (_dpPollTimer) { clearInterval(_dpPollTimer); _dpPollTimer = null; }
  // Phase 13 Item 5: clear any lingering sweep when leaving the pane.
  progressIndeterminate(false);
}

/* ── Groups Page ─────────────────────────────────────────────────────────── */
// State: raw groups from backend, keyed by provider name
let _grpfData = null;  // full /api/groups response
let _grpfSel = new Set();  // selected (whitelisted) group names

async function loadGroupsPage() {
  const [d, subs] = await Promise.all([api('/api/groups'), api('/api/subscriptions')]);
  if(!d) return;
  if (subs) subs.forEach(s => { _providerColors[s.name] = s.color; });
  _grpfData = d;
  _grpfSel = new Set(d.whitelisted);
  // Populate provider filter dropdown
  const provSel = document.getElementById('grpf-provider');
  if (provSel) {
    const existing = new Set(Array.from(provSel.options).map(o => o.value));
    Object.keys(d.providers || {}).forEach(name => {
      if (!existing.has(name)) {
        const opt = document.createElement('option');
        opt.value = name; opt.textContent = name;
        provSel.appendChild(opt);
      }
    });
  }
  // Update new-groups badge in side nav
  const allEntries = Object.values(d.providers || {}).flat();
  const newCount = allEntries.filter(g => g.is_new).length;
  const badge = document.getElementById('grp-new-badge');
  if (badge) { badge.style.display = newCount > 0 ? '' : 'none'; badge.textContent = newCount; }
  renderGroupsPage();
}

function _grpfProviderColors() {
  // Pull colors from loaded subs state if available, otherwise fallback palette
  const palette = ['#00e5ff','#fbbf24','#a78bfa','#34d399','#fb7185','#f97316'];
  if (!_grpfData || !_grpfData.providers) return {};
  return Object.keys(_grpfData.providers).reduce((m, name, i) => {
    m[name] = (_providerColors && _providerColors[name]) || palette[i % palette.length];
    return m;
  }, {});
}

function renderGroupsPage() {
  const list = document.getElementById('grpf-list'); if (!list) return;
  if (!_grpfData) { list.innerHTML = emptyState('No data'); return; }
  const search = (document.getElementById('grpf-search')?.value || '').toLowerCase();
  const filterProv = document.getElementById('grpf-provider')?.value || '';
  const colors = _grpfProviderColors();
  let h = '', total = 0, selCount = 0;
  const providers = _grpfData.providers || {};
  const provNames = Object.keys(providers);
  for (const provName of provNames) {
    if (filterProv && provName !== filterProv) continue;
    const groups = providers[provName];
    const color = colors[provName] || '#00e5ff';
    const filtered = groups.filter(g => !search || g.name.toLowerCase().includes(search));
    if (!filtered.length) continue;
    // Provider section header
    h += `<div style="padding:6px 14px 4px;font-size:9px;color:var(--text3);letter-spacing:1.2px;text-transform:uppercase;display:flex;align-items:center;gap:6px;background:var(--surface2);border-bottom:1px solid var(--border)">
      <span style="width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0;display:inline-block"></span>${esc(provName)}
    </div>`;
    for (const g of filtered) {
      const on = _grpfSel.has(g.name);
      const enc = encodeURIComponent(provName + '||' + g.name);
      h += `<div class="grpf-item ${on?'on':''}" data-grpf="${enc}" style="display:flex;align-items:center;gap:10px;padding:9px 14px;border-bottom:1px solid var(--border);cursor:pointer">
        <input type="checkbox" ${on?'checked':''} style="accent-color:${color};width:15px;height:15px;flex-shrink:0;pointer-events:none">
        <span style="width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0;display:inline-block"></span>
        <span style="flex:1;font-size:12px;color:var(--text)">${esc(g.name)}</span>
        ${g.is_new ? `<span style="font-size:9px;color:#0a0f1e;background:var(--amber);padding:2px 6px;border-radius:4px;font-weight:700;letter-spacing:0.5px">NEW</span>` : ''}
      </div>`;
      total++;
      if (on) selCount++;
    }
  }
  list.innerHTML = h || emptyState('No groups match');
  const sub = document.getElementById('grpf-sub-lbl');
  if (sub) sub.textContent = `${selCount} of ${total} selected`;
}

document.getElementById('grpf-list').addEventListener('click', (e) => {
  const el = e.target.closest('[data-grpf]'); if (!el) return;
  const [, grpName] = decodeURIComponent(el.dataset.grpf).split('||');
  if (_grpfSel.has(grpName)) _grpfSel.delete(grpName); else _grpfSel.add(grpName);
  el.classList.toggle('on', _grpfSel.has(grpName));
  el.querySelector('input').checked = _grpfSel.has(grpName);
  // Update sub label count
  const allEntries = Object.values(_grpfData?.providers || {}).flat();
  const filterProv = document.getElementById('grpf-provider')?.value || '';
  const visEntries = filterProv ? allEntries.filter(g => {
    return Object.entries(_grpfData.providers).some(([p, gs]) => p === filterProv && gs.includes(g));
  }) : allEntries;
  const selCount = visEntries.filter(g => _grpfSel.has(g.name)).length;
  const sub = document.getElementById('grpf-sub-lbl');
  if (sub) sub.textContent = `${selCount} of ${visEntries.length} selected`;
});
document.getElementById('grpf-search').addEventListener('input', () => renderGroupsPage());
document.getElementById('grpf-provider').addEventListener('change', () => renderGroupsPage());
document.getElementById('btn-grpf-all').addEventListener('click', () => {
  Object.values(_grpfData?.providers || {}).flat().forEach(g => _grpfSel.add(g.name));
  renderGroupsPage();
});
document.getElementById('btn-grpf-none').addEventListener('click', () => {
  _grpfSel.clear(); renderGroupsPage();
});
document.getElementById('btn-grpf-save').addEventListener('click', async function() {
  if (!confirm('Apply group filter? This will rebuild the channel list.')) return;
  await withButtonBusy(this, 'Saving…', async () => {
    const r = await fetch(B+'/api/groups',{method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify({groups: [..._grpfSel]})});
    if (!r.ok) { toast('Error saving filter', 'error'); return; }
    const d = await r.json();
    // Clear new-group flags
    await fetch(B+'/api/groups/seen', {method:'POST'});
    const badge = document.getElementById('grp-new-badge');
    if (badge) badge.style.display = 'none';
    toast(`Filter applied — ${d.channels} channels`, 'success');
    _grpfData = null; // force reload next open
  });
});

/* ── Channels > Logical Groups subpane ──────────────────────────────────── */
let _cgData = null;  // {group_order, groups: {name:[entries]}, unassigned}
let _cgOrder = [];   // editable copy of group_order
let _cgAssignments = {};  // key → group_name (editable copy)

async function loadChannelGroups() {
  const d = await api('/api/channel-groups'); if (!d) return;
  _cgData = d;
  _cgOrder = [...d.group_order];
  _cgAssignments = {...(Object.fromEntries(
    d.group_order.flatMap(g => (d.groups[g]||[]).map(e => [e.key, g]))
  ))};
  renderChannelGroups();
}

function renderChannelGroups() {
  const list = document.getElementById('cg-list'); if (!list) return;
  const lbl  = document.getElementById('cg-count-lbl');
  if (lbl) lbl.textContent = `${_cgOrder.length} groups`;
  if (!_cgOrder.length) { list.innerHTML = emptyState('No groups yet — add one'); return; }
  list.innerHTML = _cgOrder.map((g, i) => {
    const entries = (_cgData?.groups[g] || []);
    const count = entries.length + (_cgData?.group_order.includes(g) ? 0 : 0);
    // Count from _cgData including current _cgAssignments
    const realCount = Object.values(_cgAssignments).filter(v => v === g).length;
    return `<div class="cg-row" data-cg-idx="${i}" style="display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--border)">
      <div style="display:flex;flex-direction:column;gap:2px;flex-shrink:0">
        <button class="btn btn-ghost btn-sm" data-cg-move="${i}:-1" style="padding:1px 6px;font-size:10px;line-height:1" ${i===0?'disabled':''}>▲</button>
        <button class="btn btn-ghost btn-sm" data-cg-move="${i}:1"  style="padding:1px 6px;font-size:10px;line-height:1" ${i===_cgOrder.length-1?'disabled':''}>▼</button>
      </div>
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:8px">
          <input class="f-input cg-name-input" data-cg-edit="${i}" value="${esc(g)}" style="flex:1;min-width:0;padding:6px 10px;font-size:12px">
          <span style="font-size:10px;color:var(--text3);flex-shrink:0">${realCount} ch</span>
        </div>
      </div>
      <button class="btn btn-rose btn-sm" data-cg-del="${i}" style="padding:5px 10px;font-size:11px;flex-shrink:0">✕</button>
    </div>`;
  }).join('');
}

document.getElementById('cg-list').addEventListener('click', (e) => {
  const moveBtn = e.target.closest('[data-cg-move]');
  const delBtn  = e.target.closest('[data-cg-del]');
  if (moveBtn) {
    const [idx, dir] = moveBtn.dataset.cgMove.split(':').map(Number);
    const n = idx + dir;
    if (n < 0 || n >= _cgOrder.length) return;
    [_cgOrder[idx], _cgOrder[n]] = [_cgOrder[n], _cgOrder[idx]];
    renderChannelGroups();
  } else if (delBtn) {
    const idx = parseInt(delBtn.dataset.cgDel);
    const name = _cgOrder[idx];
    if (!confirm(`Remove group "${name}"? Channels in it become unassigned.`)) return;
    _cgOrder.splice(idx, 1);
    // Remove assignments for this group
    for (const k in _cgAssignments) if (_cgAssignments[k] === name) delete _cgAssignments[k];
    renderChannelGroups();
  }
});

document.getElementById('btn-cg-add').addEventListener('click', () => {
  const f = document.getElementById('cg-add-form');
  if (f) { f.style.display = f.style.display === 'none' || !f.style.display ? 'flex' : 'none'; }
  document.getElementById('cg-new-name')?.focus();
});
document.getElementById('btn-cg-add-confirm').addEventListener('click', async function() {
  const input = document.getElementById('cg-new-name');
  const name = input?.value.trim(); if (!name) return;
  if (_cgOrder.includes(name)) { toast('Group already exists', 'error'); return; }
  _cgOrder.push(name);
  input.value = '';
  document.getElementById('cg-add-form').style.display = 'none';
  renderChannelGroups();
  // Auto-save immediately so switching to Merges reflects the new group
  const r = await fetch(B+'/api/channel-groups', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({group_order: _cgOrder, assignments: _cgAssignments})
  });
  if (r.ok) toast(`Group "${name}" saved`, 'success');
  else toast('Save failed', 'error');
});
document.getElementById('btn-cg-add-cancel').addEventListener('click', () => {
  document.getElementById('cg-add-form').style.display = 'none';
});

document.getElementById('btn-cg-save').addEventListener('click', async function() {
  // Sync any edited names from inputs
  document.querySelectorAll('[data-cg-edit]').forEach(inp => {
    const idx = parseInt(inp.dataset.cgEdit);
    const oldName = _cgOrder[idx];
    const newName = inp.value.trim();
    if (newName && newName !== oldName) {
      _cgOrder[idx] = newName;
      // Update assignments
      for (const k in _cgAssignments) if (_cgAssignments[k] === oldName) _cgAssignments[k] = newName;
    }
  });
  await withButtonBusy(this, 'Saving…', async () => {
    const r = await fetch(B+'/api/channel-groups', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({group_order: _cgOrder, assignments: _cgAssignments})
    });
    if (r.ok) {
      toast('Groups saved', 'success');
      _cgData = null;
      setTimeout(loadChannelGroups, 1500);
    } else toast('Error saving', 'error');
  });
});

/* ── Merge suggestions (Channels > Merges) ───────────────────────────────── */
function renderMergeSuggestions(lineup) {
  const sec = document.getElementById('q-suggest-sec'); if (!sec) return;
  if (!lineup || !lineup.length) { sec.style.display = 'none'; return; }
  // Find groups of channels with the same normalised name (strip quality suffix)
  const norm = name => name.replace(/\s*(HD|FHD|4K|SD|HEVC|\d{3,4}p)\s*$/i,'').trim().toLowerCase();
  const groups = {};
  for (const ch of lineup) {
    if (ch.variants && ch.variants.length > 1) continue; // already merged
    const key = norm(ch.name);
    (groups[key] = groups[key] || []).push(ch);
  }
  const suggestions = Object.entries(groups).filter(([,v]) => v.length > 1);
  const cnt = document.getElementById('q-suggest-count');
  if (!suggestions.length) { sec.style.display = 'none'; return; }
  sec.style.display = '';
  if (cnt) cnt.textContent = `${suggestions.length} suggestion${suggestions.length!==1?'s':''}`;
  const body = document.getElementById('q-suggest-body');
  if (!body) return;
  body.innerHTML = suggestions.map(([,chs]) =>
    `<div style="padding:8px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span style="flex:1;min-width:120px;font-size:12px;color:var(--text)">${esc(chs[0].name.replace(/\s*(HD|FHD|4K|SD|HEVC|\d{3,4}p)\s*$/i,'').trim())}</span>
      <span style="font-size:10px;color:var(--text3)">${chs.map(c=>esc(c.name)).join(' + ')}</span>
      <button class="btn btn-green btn-sm" style="font-size:10px;padding:4px 10px"
        data-suggest-merge="${esc(chs.map(c=>c.name).join('|'))}">Merge</button>
    </div>`
  ).join('');
}

document.getElementById('q-suggest-hdr')?.addEventListener('click', () => {
  const body = document.getElementById('q-suggest-body');
  const chev = document.getElementById('q-suggest-chevron');
  if (!body) return;
  const open = body.style.display !== 'none';
  body.style.display = open ? 'none' : '';
  if (chev) chev.textContent = open ? '▸ Show' : '▾ Hide';
});

document.getElementById('q-suggest-body')?.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-suggest-merge]'); if (!btn) return;
  const names = btn.dataset.suggestMerge.split('|');
  if (names.length < 2) return;
  // Apply merge: add to _qManualMerges as {target: rest[]}
  _qManualMerges[names[0]] = (_qManualMerges[names[0]] || []).concat(names.slice(1));
  toast(`Merged ${names.length} channels`, 'success');
  btn.closest('div').remove();
  const remaining = document.getElementById('q-suggest-body')?.children.length || 0;
  if (!remaining) document.getElementById('q-suggest-sec').style.display = 'none';
  renderQVariants();
});

/* ── Provider Colors (Providers > Colors) ────────────────────────────────── */
let _providerColors = {};  // sub_name → "#hexcolor"

async function loadProviderColors() {
  const subs = await api('/api/subscriptions'); if (!subs) return;
  const list = document.getElementById('pc-list'); if (!list) return;
  // Build color map from returned subs (each has .color injected by backend)
  _providerColors = {};
  subs.forEach(s => { _providerColors[s.name] = s.color; });
  const palette = ['#00e5ff','#fbbf24','#a78bfa','#34d399','#fb7185','#f97316'];
  list.innerHTML = subs.map((s, i) => {
    const color = s.color || palette[i % palette.length];
    return `<div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--border)">
      <label style="position:relative;cursor:pointer;flex-shrink:0">
        <span style="display:block;width:32px;height:32px;border-radius:8px;background:${color};border:2px solid rgba(255,255,255,0.15);transition:transform .15s" onmouseover="this.style.transform='scale(1.1)'" onmouseout="this.style.transform=''"></span>
        <input type="color" value="${color}" data-pc-sub="${esc(s.name)}" style="position:absolute;opacity:0;width:0;height:0" onchange="_pcChange(this)">
      </label>
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;color:var(--text);font-weight:500">${esc(s.name)}</div>
        <div style="font-size:10px;color:var(--text3);margin-top:2px" id="pc-val-${esc(s.name)}">${color}</div>
      </div>
    </div>`;
  }).join('');
}

function _pcChange(input) {
  const name = input.dataset.pcSub;
  const color = input.value;
  _providerColors[name] = color;
  // Update swatch + label
  const swatch = input.previousElementSibling;
  if (swatch) swatch.style.background = color;
  const lbl = document.getElementById(`pc-val-${name}`);
  if (lbl) lbl.textContent = color;
}

document.getElementById('btn-pc-save')?.addEventListener('click', async function() {
  await withButtonBusy(this, 'Saving…', async () => {
    const r = await fetch(B+'/api/provider-colors', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({colors: _providerColors})
    });
    if (r.ok) toast('Provider colors saved', 'success');
    else toast('Error saving', 'error');
  });
});

/* ── Telegram ────────────────────────────────────────────────────────────── */
async function loadTg() {
  const d = await api('/api/telegram'); if(!d) return;
  document.getElementById('tg-enabled').checked = d.enabled;
  document.getElementById('tg-chat').value = d.chat_id||'';
  document.getElementById('tg-hour').value = d.daily_summary_hour??8;
  document.getElementById('tg-cpu-warn').value = d.cpu_alert_pct??85;
  document.getElementById('tg-cpu-crit').value = d.cpu_critical_pct??95;
  document.getElementById('tg-mem-warn').value = d.mem_alert_pct??90;
}
document.getElementById('btn-tg-save').addEventListener('click', async function () {
  // Phase 13 Item 5: busy-state on Telegram settings save.
  const btn = this;
  const body = {
    enabled: document.getElementById('tg-enabled').checked,
    chat_id: document.getElementById('tg-chat').value.trim(),
    daily_summary_hour: parseInt(document.getElementById('tg-hour').value)||8,
    cpu_alert_pct: parseInt(document.getElementById('tg-cpu-warn').value)||85,
    cpu_critical_pct: parseInt(document.getElementById('tg-cpu-crit').value)||95,
    mem_alert_pct: parseInt(document.getElementById('tg-mem-warn').value)||90,
  };
  const token = document.getElementById('tg-token').value.trim();
  if (token) body.bot_token = token;
  await withButtonBusy(btn, 'Saving…', async () => {
    const r = await fetch(B+'/api/telegram',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    toast(r.ok ? 'Settings saved' : 'Error saving', r.ok ? 'success' : 'error');
  });
});
document.getElementById('btn-tg-test').addEventListener('click', async () => {
  const r = await fetch(B+'/api/telegram/test',{method:'POST'});
  toast(r.ok ? 'Test sent to Telegram!' : 'Failed — check settings', r.ok ? 'success' : 'error');
});

/* ── Admin Account ───────────────────────────────────────────────────────── */
document.getElementById('btn-adm-username').addEventListener('click', async function () {
  const username = document.getElementById('adm-username').value.trim();
  if (!username) { toast('Username is required', 'warning'); return; }
  if (!confirm(`Change admin username to "${username}"? You will be logged out.`)) return;
  await withButtonBusy(this, 'Saving…', async () => {
    const r = await fetch(B+'/api/admin/username', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username})
    });
    if (r.ok) {
      toast('Username changed — please log in again', 'success');
      setTimeout(() => location.href = '/login', 1500);
    } else {
      const d = await r.json().catch(()=>({}));
      toast(d.detail || 'Error saving username', 'error');
    }
  });
});

document.getElementById('btn-adm-password').addEventListener('click', async function () {
  const current = document.getElementById('adm-pw-current').value;
  const newPw   = document.getElementById('adm-pw-new').value;
  const confirm2 = document.getElementById('adm-pw-confirm').value;
  if (!current || !newPw) { toast('All fields are required', 'warning'); return; }
  if (newPw !== confirm2) { toast('Passwords do not match', 'warning'); return; }
  if (newPw.length < 4)  { toast('Password must be at least 4 characters', 'warning'); return; }
  await withButtonBusy(this, 'Saving…', async () => {
    const r = await fetch(B+'/api/admin/password', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({current, new_password: newPw})
    });
    if (r.ok) {
      toast('Password changed — please log in again', 'success');
      document.getElementById('adm-pw-current').value = '';
      document.getElementById('adm-pw-new').value = '';
      document.getElementById('adm-pw-confirm').value = '';
      setTimeout(() => location.href = '/login', 1500);
    } else {
      const d = await r.json().catch(()=>({}));
      toast(d.detail || 'Error saving password', 'error');
    }
  });
});

/* ── Channels > Merges subpane ───────────────────────────────────────────── */
async function loadQuality() {
  showSkeletonOnce('q-variants', 6, ['sk-w-xs', '', 'sk-w-md', 'sk-w-sm']);
  const [qd, lineup, overrides, chSrc, subs, cgd] = await Promise.all([
    api('/api/quality'), api('/lineup.json', {cache:'no-store'}), api('/api/quality/overrides'),
    api('/api/channels'), api('/api/subscriptions'), api('/api/channel-groups'),
  ]);
  // Keep _providerColors in sync whenever we load the merges page
  if (subs) subs.forEach(s => { _providerColors[s.name] = s.color; });
  // Keep channel-group assignment data in sync so the group dropdown works
  if (cgd) {
    _cgData = cgd;
    _cgOrder = [...cgd.group_order];
    _cgAssignments = Object.fromEntries(
      cgd.group_order.flatMap(g => (cgd.groups[g]||[]).map(e => [e.key, g]))
    );
  }
  if (!qd || !lineup) { errorPanel('q-variants','channels',()=>loadQuality()); return; }
  _qPref      = qd.preference || ['HD','FHD','4K','SD','HEVC'];
  _qLineup    = lineup;
  _qOverrides = overrides || {};
  // Derive groups from lineup GroupTitle values
  const groupSet = new Set();
  for (const ch of lineup) if (ch.GroupTitle) groupSet.add(ch.GroupTitle.replace(/★/g,'').replace(/⚽/g,'').trim());
  _qGroups = [...groupSet].sort();
  // Build name → sources lookup so renderChannelRow can show the badge
  _qSrcMap = {};
  _qKeyByName = {};
  for (const c of (chSrc?.channels || [])) {
    _qSrcMap[c.name] = {
      key:         c.key,
      count:       c.candidate_count || 0,
      subs:        c.candidate_subs || [],
      current_sub: c.current_sub || '',
    };
    _qKeyByName[c.name] = c.key;
  }
  _qSelected.clear();
  updateQSelUI();
  populateGroupFilter();
  renderQVariants();
  renderMergeSuggestions(lineup);
}

/* ── Settings > Quality pane ─────────────────────────────────────────────── */
async function loadQualityPrefs() {
  const qd = await api('/api/quality'); if (!qd) return;
  _qPref = qd.preference || ['HD','FHD','4K','SD','HEVC'];
  renderQPref();
}

function populateGroupFilter() {
  const sel = document.getElementById('q-group');
  if (!sel) return;
  // Rebuild using logical groups (_cgOrder) so user filters by their own group names
  const groups = _cgOrder.length ? _cgOrder : _qGroups;
  while (sel.options.length > 1) sel.remove(1);
  groups.forEach(g => {
    const o = document.createElement('option');
    o.value = g;
    o.textContent = g.replace(/★/g,'').replace(/⚽/g,'').trim();
    sel.appendChild(o);
  });
}

function renderQPref() {
  document.getElementById('q-pref-list').innerHTML =
    _qPref.map((q, i) => renderPrefItem(q, i, _qPref.length)).join('');
}

document.getElementById('q-pref-list').addEventListener('click', (e) => {
  const b = e.target.closest('[data-pref-move]'); if (!b) return;
  const [idx, dir] = b.dataset.prefMove.split(':').map(Number);
  const n = idx + dir;
  if (n < 0 || n >= _qPref.length) return;
  [_qPref[idx], _qPref[n]] = [_qPref[n], _qPref[idx]];
  renderQPref();
});

function renderQVariants() {
  const mergedOnly = document.getElementById('q-merged-only')?.checked;
  const search = (document.getElementById('q-search')?.value || '').toLowerCase().trim();
  const grp    = document.getElementById('q-group')?.value || '';

  const display = _qLineup.map(ch => ({...ch, GuideName: _qRenames[ch.GuideName] || ch.GuideName}));
  let filtered = mergedOnly ? display.filter(c => (c.VariantCount||1) > 1) : display;
  if (search) filtered = filtered.filter(c => (c.GuideName||'').toLowerCase().includes(search));
  if (grp)    filtered = filtered.filter(c => {
    // Filter by logical group assignment if available, otherwise fall back to broadcast GroupTitle
    const key = _qKeyByName[c.GuideName] || '';
    const logicalGrp = key ? (_cgAssignments[key] || '') : '';
    const broadcastGrp = (c.GroupTitle||'').replace(/★/g,'').replace(/⚽/g,'').trim();
    return (logicalGrp || broadcastGrp) === grp;
  });

  const multi = display.filter(c => (c.VariantCount||1) > 1);
  const qs = document.getElementById('q-summary');
  if (qs) qs.textContent = `${filtered.length}/${display.length} ch · ${multi.length} merged`;

  if (!filtered.length) {
    document.getElementById('q-variants').innerHTML = emptyState('No channels match', '◈');
    return;
  }

  document.getElementById('q-variants').innerHTML =
    filtered.map(ch => renderChannelRow(ch, _qSelected.has(ch.GuideName))).join('');
}

/* Channel-list event delegation (select / rename / override / split / auto / sources) */
document.getElementById('q-variants').addEventListener('click', async (e) => {
  const row = e.target.closest('.ch-row');
  if (!row) return;

  // Sources badge → toggle inline expansion (Phase 12 D)
  const srcBtn = e.target.closest('[data-q-srcs]');
  if (srcBtn) {
    e.stopPropagation();
    const name = decodeURIComponent(srcBtn.dataset.qSrcs);
    if (_qSrcExp.has(name)) {
      _qSrcExp.delete(name);
    } else {
      _qSrcExp.add(name);
      await _qLoadSources(name);  // populates _qSrcCache so re-render shows real data
    }
    renderQVariants();
    return;
  }

  // Per-channel "invalidate cache" inside the sources panel
  const invBtn = e.target.closest('[data-q-src-inv]');
  if (invBtn) {
    e.stopPropagation();
    const key = decodeURIComponent(invBtn.dataset.qSrcInv);
    const r = await fetch(B+'/api/dispatch/invalidate/'+encodeURIComponent(key), {method:'POST'});
    if (r.ok) toast('Invalidated', 'success'); else toast('Error', 'error');
    return;
  }

  const splitBtn = e.target.closest('[data-q-split]');
  if (splitBtn) { e.stopPropagation(); unmergeQChannel(splitBtn.dataset.qSplit); return; }

  const autoBtn = e.target.closest('[data-q-auto]');
  if (autoBtn) { e.stopPropagation(); clearQOverride(autoBtn.dataset.qAuto); return; }

  const logoBtn = e.target.closest('[data-q-logo]');
  if (logoBtn) {
    e.stopPropagation();
    const key = logoBtn.dataset.qLogo;
    if (!key) { toast('Channel key not found', 'error'); return; }
    const inp = document.createElement('input');
    inp.type = 'file'; inp.accept = 'image/*';
    inp.addEventListener('change', async () => {
      const file = inp.files[0]; if (!file) return;
      const fd = new FormData(); fd.append('file', file);
      const r = await fetch(B+'/api/channels/'+encodeURIComponent(key)+'/logo', {method:'POST', body: fd});
      if (r.ok) { toast('Logo uploaded', 'success'); _logoTs = Date.now(); setTimeout(loadQuality, 1000); }
      else { const d = await r.json().catch(()=>{}); toast('Upload failed: '+(d?.detail||r.status), 'error'); }
    });
    inp.click();
    return;
  }

  const logoDelBtn = e.target.closest('[data-q-logo-del]');
  if (logoDelBtn) {
    e.stopPropagation();
    const key = logoDelBtn.dataset.qLogoDel;
    if (!key) return;
    if (!confirm('Remove custom logo? The auto-downloaded logo will be used instead.')) return;
    const r = await fetch(B+'/api/channels/'+encodeURIComponent(key)+'/logo', {method:'DELETE'});
    if (r.ok) { toast('Custom logo removed', 'success'); setTimeout(loadQuality, 1000); }
    else { const d = await r.json().catch(()=>{}); toast('Delete failed: '+(d?.detail||r.status), 'error'); }
    return;
  }

  const renameSpan = e.target.closest('[data-q-rename]');
  if (renameSpan) { e.stopPropagation(); startQRename(renameSpan.dataset.qRename, renameSpan); return; }

  // Clicks on select/option elements or noselect zone don't toggle selection
  if (e.target.tagName === 'SELECT' || e.target.tagName === 'OPTION') return;
  if (e.target.closest('[data-q-noselect]')) return;

  // Checkbox or row body toggles selection
  const enc = row.dataset.qEnc;
  if (!enc) return;
  const name = decodeURIComponent(enc);
  if (_qSelected.has(name)) _qSelected.delete(name); else _qSelected.add(name);
  updateQSelUI();
  renderQVariants();
});

document.getElementById('q-variants').addEventListener('change', async (e) => {
  const sel = e.target.closest('[data-q-override]');
  if (sel) { setQOverride(sel.dataset.qOverride, sel.value); return; }

  const grpSel = e.target.closest('[data-q-grp]');
  if (grpSel) {
    const key = grpSel.dataset.qGrp; if (!key) return;
    const grp = grpSel.value;
    if (grp) _cgAssignments[key] = grp; else delete _cgAssignments[key];
    // Auto-save to backend
    const r = await fetch(B+'/api/channel-groups', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({group_order: _cgOrder, assignments: _cgAssignments})
    });
    if (r.ok) toast('Group saved', 'success');
    else toast('Save failed', 'error');
    return;
  }
});

document.getElementById('q-search').addEventListener('input', debounce(renderQVariants, 200));
document.getElementById('q-group').addEventListener('change', renderQVariants);
document.getElementById('q-merged-only').addEventListener('change', renderQVariants);

function clearQSelection() { _qSelected.clear(); updateQSelUI(); renderQVariants(); }
function updateQSelUI() {
  const n = _qSelected.size;
  const bar   = document.getElementById('q-sel-bar');
  const info  = document.getElementById('q-sel-info');
  const mbtn  = document.getElementById('q-merge-btn');
  const count = document.getElementById('q-merge-count');
  if (bar)   bar.style.display  = n >= 1 ? 'flex' : 'none';
  if (info)  info.textContent   = n + ' channel' + (n!==1?'s':'') + ' selected';
  if (mbtn)  mbtn.style.display = n >= 2 ? '' : 'none';
  if (count) count.textContent  = n;
}
document.getElementById('q-cancel-btn').addEventListener('click', clearQSelection);
document.getElementById('q-merge-btn').addEventListener('click', mergeQSelected);
document.getElementById('q-undo-btn').addEventListener('click', undoQMerge);

async function mergeQSelected() {
  if (_qSelected.size < 2) { toast('Select at least 2 channels', 'warning'); return; }
  const names      = [..._qSelected];
  const targetName = names[0];
  const absorbed   = names.slice(1);
  const msg = 'Merge ' + _qSelected.size + ' channels into "' + targetName + '"?\n\n' + absorbed.map(n => ' - ' + n).join('\n');
  if (!confirm(msg)) return;

  const absData = absorbed.map(n => _qLineup.find(c =>
    ((_qRenames[c.GuideName]||c.GuideName) === n) || c.GuideName === n
  )).filter(Boolean);

  _qMergeLog.push({targetName, absData: absData.map(c => ({...c}))});
  document.getElementById('q-undo-btn').style.display = '';

  const target = _qLineup.find(c =>
    ((_qRenames[c.GuideName]||c.GuideName) === targetName) || c.GuideName === targetName);
  if (!target) return;

  let allV = [...(target.Variants || [target.Tags || 'HD'])];
  for (const absName of absorbed) {
    const abs = _qLineup.find(c =>
      ((_qRenames[c.GuideName]||c.GuideName) === absName) || c.GuideName === absName);
    if (!abs) continue;
    allV = [...new Set([...allV, ...(abs.Variants || [abs.Tags || 'HD'])])];
    if (!_qManualMerges[targetName]) _qManualMerges[targetName] = [];
    _qManualMerges[targetName].push(abs.GuideName);
  }
  const ti = _qLineup.findIndex(c => c === target);
  _qLineup[ti] = {...target, Variants: allV, VariantCount: allV.length};
  _qLineup = _qLineup.filter(c => {
    const dn = _qRenames[c.GuideName] || c.GuideName;
    return !absorbed.includes(dn) && !absorbed.includes(c.GuideName);
  });
  _qSelected.clear(); updateQSelUI(); renderQVariants();
  await saveQManualMerges();
  toast('Merged into "' + targetName + '"', 'success');
}

function undoQMerge() {
  const last = _qMergeLog.pop(); if (!last) return;
  const target = _qLineup.find(c =>
    ((_qRenames[c.GuideName]||c.GuideName) === last.targetName) || c.GuideName === last.targetName);
  if (target) {
    const absVs = last.absData.flatMap(c => c.Variants || [c.Tags || 'HD']);
    target.Variants = (target.Variants || []).filter(v => !absVs.includes(v));
    target.VariantCount = target.Variants.length;
  }
  _qLineup.push(...last.absData);
  if (_qMergeLog.length === 0) document.getElementById('q-undo-btn').style.display = 'none';
  renderQVariants();
}

function startQRename(enc, el) {
  const origName = decodeURIComponent(enc);
  const current  = _qRenames[origName] || origName;
  const input    = document.createElement('input');
  input.value    = current;
  input.style.cssText = 'background:var(--surface2);border:1px solid var(--cyan);color:#fff;padding:4px 10px;font-size:13px;border-radius:6px;outline:none;width:180px';
  el.replaceWith(input); input.focus(); input.select();
  const commit = () => {
    const n = input.value.trim();
    if (n && n !== origName) _qRenames[origName] = n;
    renderQVariants();
  };
  input.onblur    = commit;
  input.onkeydown = e => { if (e.key==='Enter') commit(); if (e.key==='Escape') renderQVariants(); };
}

async function setQOverride(enc, quality) {
  const name = decodeURIComponent(enc);
  const r = await fetch(B+'/api/quality/overrides', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({overrides: {[name]: quality}})
  });
  if (r.ok) { _qOverrides[name] = quality; renderQVariants(); toast(name + ' → ' + quality, 'success'); }
  else toast('Error', 'error');
}

async function clearQOverride(enc) {
  const name = decodeURIComponent(enc);
  const r = await fetch(B+'/api/quality/overrides', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({overrides: {[name]: null}})
  });
  if (r.ok) { delete _qOverrides[name]; renderQVariants(); toast('Auto mode restored', 'success'); }
}

async function unmergeQChannel(enc) {
  const name = decodeURIComponent(enc);
  if (!confirm('Split "' + name + '" back into individual quality channels?')) return;
  const r = await fetch(B+'/api/quality/unmerge', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({channel: name})
  });
  if (r.ok) { await loadQuality(); toast('Split', 'success'); }
  else toast('Error', 'error');
}

async function saveQManualMerges() {
  if (!Object.keys(_qManualMerges).length && !Object.keys(_qRenames).length) return;
  await fetch(B+'/api/quality/merges', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({merges: _qManualMerges, renames: _qRenames})
  });
}

document.getElementById('btn-save-quality').addEventListener('click', async () => {
  // Merges pane: save overrides + merges (no quality pref order here)
  const r2 = await fetch(B+'/api/quality/merges', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({merges: _qManualMerges, renames: _qRenames})
  });
  if (r2.ok) {
    toast('Saved channel merges', 'success');
    _qManualMerges = {}; _qRenames = {}; _qMergeLog = [];
    document.getElementById('q-undo-btn').style.display = 'none';
    setTimeout(loadQuality, 2500);
  } else toast('Error saving', 'error');
});

document.getElementById('btn-save-quality-st')?.addEventListener('click', async () => {
  // Settings quality pane: save preference order only
  const r = await fetch(B+'/api/quality', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({preference: _qPref})
  });
  if (r.ok) toast('Quality order saved: ' + _qPref.join(' → '), 'success');
  else toast('Error saving', 'error');
});

/* ── EPG (Settings pane) ─────────────────────────────────────────────────── */
async function loadEpg() {
  if (!_epgData) _epgData = await api('/api/epg-info');
  if (!_epgData) return;
  const d = _epgData;

  const statsEl = document.getElementById('epg-stats');
  if (statsEl) statsEl.innerHTML =
    renderStatCard('Channels', d.channels, 'cyan') +
    renderStatCard('Total Slots', (d.total_slots||0).toLocaleString(), 'violet') +
    renderStatCard('Days', d.days, 'green') +
    renderStatCard('Type', esc(d.type), 'amber', 'font-size:16px');

  const xmlEl = document.getElementById('epg-xml');
  const gzEl  = document.getElementById('epg-gz');
  if (xmlEl) xmlEl.textContent = d.epg_xml_url;
  if (gzEl)  gzEl.textContent  = d.epg_gz_url;
  window._epgXml = d.epg_xml_url;
  window._epgGz  = d.epg_gz_url;
}

function copyText(text, label) {
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(() => toast('Copied ' + label, 'success')).catch(() => _copyFallback(text, label));
  } else {
    _copyFallback(text, label);
  }
}
function _copyFallback(text, label) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.focus(); ta.select();
  try { document.execCommand('copy'); toast('Copied ' + label, 'success'); }
  catch { toast('Copy failed — select manually', 'error'); }
  document.body.removeChild(ta);
}
document.getElementById('btn-epg-xml').addEventListener('click', () => {
  if (!window._epgXml) return; copyText(window._epgXml, 'XML URL');
});
document.getElementById('btn-epg-gz').addEventListener('click', () => {
  if (!window._epgGz) return; copyText(window._epgGz, 'GZ URL');
});

/* ── Mobile drawer ───────────────────────────────────────────────────────── */
function openSideNav() {
  const sn = document.getElementById('side-nav');
  const ov = document.getElementById('side-overlay');
  if (sn) { sn.style.display = 'flex'; sn.classList.add('mobile-open'); }
  if (ov) ov.classList.add('open');
}
function closeSideNav() {
  const sn = document.getElementById('side-nav');
  const ov = document.getElementById('side-overlay');
  if (sn) {
    sn.classList.remove('mobile-open');
    // Remove inline display style so applyLayout() determines the correct
    // state — avoids stuck-open side nav on phones at exactly 600px width.
    sn.style.display = '';
    applyLayout();
  }
  if (ov) ov.classList.remove('open');
}
document.getElementById('btn-more').addEventListener('click', openSideNav);
document.getElementById('side-overlay').addEventListener('click', closeSideNav);

/* ── Settings drawer (Phase 13 A.5: Groups + Telegram behind gear icon) ── */
function openSettings(sub) {
  const dr = document.getElementById('settings-drawer');
  const ov = document.getElementById('settings-overlay');
  if (!dr || !ov) return;
  _settingsOpen = true;
  dr.classList.add('open');
  dr.setAttribute('aria-hidden', 'false');
  ov.classList.add('open');
  goSubPane('settings', sub || _settingsSub || 'quality');
}
function closeSettings() {
  const dr = document.getElementById('settings-drawer');
  const ov = document.getElementById('settings-overlay');
  if (!dr || !ov) return;
  _settingsOpen = false;
  dr.classList.remove('open');
  dr.setAttribute('aria-hidden', 'true');
  ov.classList.remove('open');
}
/* ── Phase 13 Item 3: Theme toggle ───────────────────────────────────────── */
function _syncThemeIcon(name) {
  const icon = document.getElementById('theme-icon');
  if (icon) icon.textContent = name === 'light' ? '☀' : '☾';
}
function setTheme(name, persist) {
  if (name !== 'light' && name !== 'dark') name = 'dark';
  document.documentElement.setAttribute('data-theme', name);
  if (persist) { try { localStorage.setItem('aq.theme', name); } catch (_) {} }
  _syncThemeIcon(name);
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') || 'dark';
  setTheme(cur === 'light' ? 'dark' : 'light', /*persist*/true);
}
document.getElementById('btn-theme')?.addEventListener('click', toggleTheme);
// Sync icon to whatever the inline <head> script applied — no localStorage write.
_syncThemeIcon(document.documentElement.getAttribute('data-theme') || 'dark');
// Track OS preference until the user picks a theme explicitly.
try {
  const mq = window.matchMedia('(prefers-color-scheme: light)');
  const onChange = (e) => {
    let saved = null;
    try { saved = localStorage.getItem('aq.theme'); } catch (_) {}
    if (saved !== 'light' && saved !== 'dark') setTheme(e.matches ? 'light' : 'dark', /*persist*/false);
  };
  mq.addEventListener ? mq.addEventListener('change', onChange) : mq.addListener(onChange);
} catch (_) {}

document.getElementById('btn-settings')?.addEventListener('click', () => openSettings());
document.getElementById('btn-settings-side')?.addEventListener('click', () => {
  if (window.innerWidth < 600) closeSideNav();
  openSettings();
});
document.getElementById('settings-close')?.addEventListener('click', closeSettings);
document.getElementById('settings-overlay')?.addEventListener('click', closeSettings);
/* Phase 13 Item 7 — Mobile drawer swipe-down-to-close.
   Drag handles (.drawer-drag-handle) appear on mobile only (CSS).
   Touch drag ≥80px downward → close the parent drawer. */
(function initDrawerSwipe() {
  const THRESHOLD = 80;
  document.querySelectorAll('.drawer-drag-handle').forEach(handle => {
    let startY = 0, dragging = false, drawer = null, closeFn = null;
    handle.addEventListener('touchstart', (e) => {
      drawer = handle.closest('.us-drawer, .settings-drawer');
      if (!drawer || !drawer.classList.contains('open')) return;
      startY = e.touches[0].clientY;
      dragging = true;
      closeFn = drawer.classList.contains('us-drawer') ? closeUsDrawer : closeSettings;
      drawer.style.transition = 'none';
    }, { passive: true });
    handle.addEventListener('touchmove', (e) => {
      if (!dragging) return;
      const dy = Math.max(0, e.touches[0].clientY - startY);
      drawer.style.transform = `translateY(${dy}px)`;
    }, { passive: true });
    handle.addEventListener('touchend', (e) => {
      if (!dragging) return;
      dragging = false;
      const dy = e.changedTouches[0].clientY - startY;
      drawer.style.transition = '';
      drawer.style.transform = '';
      if (dy >= THRESHOLD && closeFn) closeFn();
    });
    handle.addEventListener('touchcancel', () => {
      if (!dragging) return;
      dragging = false;
      if (drawer) { drawer.style.transition = ''; drawer.style.transform = ''; }
    });
  });
})();

/* Phase 13 Item 6 — Keyboard shortcuts (lite version).
   Escape closes drawers/modals. `?` opens help. `g` prefix → navigate.
   `r` triggers refresh. All bare-key — skipped when typing in inputs. */
let _kbPending = '';          // prefix buffer for 2-key chords (e.g. 'g')
let _kbTimer   = 0;          // auto-clear after 1s

function _kbHelp() {
  const el = document.getElementById('kb-help');
  if (!el) return;
  el.classList.toggle('open');
}

document.getElementById('kb-help-overlay')?.addEventListener('click', _kbHelp);

document.addEventListener('keydown', (e) => {
  // Esc unwinds the topmost layer: help → settings → user drawer → mobile nav
  if (e.key === 'Escape') {
    const help = document.getElementById('kb-help');
    if (help?.classList.contains('open')) { help.classList.remove('open'); return; }
    if (_settingsOpen) { closeSettings(); return; }
    if (document.getElementById('us-drawer')?.classList.contains('open')) { closeUsDrawer(); return; }
    const sn = document.getElementById('side-nav');
    if (sn?.classList.contains('mobile-open')) { closeSideNav(); return; }
    return;
  }
  // Bare-key shortcuts: skip when typing or holding modifiers
  const t = e.target;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  // ? → toggle shortcut help
  if (e.key === '?') { e.preventDefault(); _kbHelp(); return; }

  // 2-key chord: g → {u,c,d,o,e,m,p,s}
  if (_kbPending === 'g') {
    clearTimeout(_kbTimer); _kbPending = '';
    e.preventDefault();
    const map = { u:'users', c:'channels', d:'providers', o:'overview',
                  e:'epg', m:'monitoring', p:'providers', s:'providers' };
    const page = map[e.key];
    if (page) {
      goPage(page, false);
      // sub-tab shortcuts: d→dispatch, s→subscriptions
      if (e.key === 'd') goSubPane('providers', 'dispatch');
      if (e.key === 's') goSubPane('providers', 'subscriptions');
    }
    return;
  }

  // Start 'g' prefix
  if (e.key === 'g') {
    _kbPending = 'g';
    _kbTimer = setTimeout(() => { _kbPending = ''; }, 1000);
    return;
  }

  _kbPending = '';
  if (e.key === 'r' || e.key === 'R') {
    e.preventDefault();
    forceRefresh();
  }
});

/* ── Init ────────────────────────────────────────────────────────────────── */
initEventSource();
_initOfflineWatcher();
goPage(getPageFromHash(), false);
// Honor legacy hash aliases on first load (#/health → monitoring/health, etc.)
{
  const h = (location.hash || '').replace(/^#\/?/, '');
  const alias = _hashAliases[h];
  if (alias?.sub) goSubPane(alias.page, alias.sub);
  if (alias?.settings) openSettings(alias.settings);
}

/* ══════════════════════════════════════════════════════════════════════════════
   UI v2 additions — cache badge + blocked IPs panel
   ══════════════════════════════════════════════════════════════════════════════ */

// Track when each subscription's data was last fetched (client-side proxy for
// cache age — precise fetched_at requires backend work, done as health check Track C)
const _subLastFetch = {};

function _cacheBadge(subName) {
  const t = _subLastFetch[subName];
  if (!t) return '';
  const ageMin = Math.floor((Date.now() - t) / 60000);
  if (ageMin < 1)  return '<span class="cache-badge cache-badge--live">● LIVE</span>';
  if (ageMin < 180) return `<span class="cache-badge cache-badge--warm">↻ ${ageMin < 60 ? ageMin+'m' : Math.floor(ageMin/60)+'h'} ago</span>`;
  if (ageMin < 360) return `<span class="cache-badge cache-badge--stale">⚠ ${Math.floor(ageMin/60)}h ago</span>`;
  return '<span class="cache-badge cache-badge--offline">✕ OFFLINE</span>';
}

// Stamp fetch time when health data loads
const _origLoadSubs = typeof loadSubs === 'function' ? loadSubs : null;
document.addEventListener('DOMContentLoaded', () => {
  // Populate hostname in brand subtitle from server config
  api('/api/status').then(s => {
    const h = s?.hostname; if (h) { const el = document.getElementById('brand-hostname'); if (el) el.textContent = h; }
  }).catch(() => {});
  // Patch: after each subs/health load, stamp fetch times
  const origFetch = window.fetch;
  window.fetch = function(url, opts) {
    const p = origFetch.apply(this, arguments);
    if (typeof url === 'string' && url.includes('/api/subscriptions/health')) {
      p.then(r => r.clone().json().catch(()=>null)).then(data => {
        if (data && data.subscriptions) {
          data.subscriptions.forEach(s => { _subLastFetch[s.name] = Date.now(); });
        }
      }).catch(()=>{});
    }
    return p;
  };
});

// ── Blocked IPs panel ──────────────────────────────────────────────────────
let _blockedIpsTimer = null;

function renderBlockedIps(data) {
  const list = data && data.blocked ? data.blocked : [];
  const countEl = document.getElementById('blocked-count');
  const bodyEl  = document.getElementById('blocked-ips-body');
  if (!countEl || !bodyEl) return;

  if (list.length === 0) {
    if (countEl) countEl.style.display = 'none';
    bodyEl.innerHTML = `
      <div class="ip-empty">
        <svg class="ip-empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/>
        </svg>
        <span class="ip-empty-text">No blocked IPs</span>
      </div>`;
    return;
  }

  countEl.textContent = list.length;
  countEl.style.display = 'inline-flex';

  const rows = list.map(entry => {
    const ip  = esc(entry.ip || '—');
    const at  = esc(entry.blocked_at || '—');
    const exp = esc(entry.expires_in || '—');
    return `<div class="ip-table-row">
      <span class="ip-address">${ip}</span>
      <span class="ip-time">${at}</span>
      <span class="ip-time">${exp}</span>
      <button class="btn-unblock" data-ip="${ip}" title="Unblock ${ip}">Unblock</button>
    </div>`;
  }).join('');

  bodyEl.innerHTML = `
    <div class="ip-table-head">
      <span>IP Address</span><span>Blocked</span><span>Expires</span><span></span>
    </div>
    ${rows}`;

  bodyEl.querySelectorAll('.btn-unblock').forEach(btn => {
    btn.addEventListener('click', async () => {
      const ip = btn.dataset.ip;
      const r = await apiWrite(`/api/blocklist/${encodeURIComponent(ip)}`, {method:'DELETE'});
      if (r && r.ok) { toast(`Unblocked ${ip}`, 'success'); loadBlockedIps(); }
    });
  });
}

async function loadBlockedIps() {
  // Endpoint added in health check Track A — silently skip if not yet deployed
  try {
    const r = await fetch('/api/blocklist');
    if (r.status === 404) return; // endpoint not deployed yet
    if (!r.ok) return;
    const data = await r.json();
    renderBlockedIps(data);
  } catch (_) {}
}

function startBlockedIpsPoller() {
  loadBlockedIps();
  if (_blockedIpsTimer) clearInterval(_blockedIpsTimer);
  _blockedIpsTimer = setInterval(loadBlockedIps, 30000);
}

// Start poller only when user explicitly opens the health sub-tab
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-sub="health"]').forEach(btn => {
    btn.addEventListener('click', () => { if (!_blockedIpsTimer) startBlockedIpsPoller(); });
  });
  // Do NOT auto-start on page load — the endpoint may not exist yet (Track A pending)
});
