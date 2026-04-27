const CACHE = '__CACHE_VERSION__';
const PRECACHE = [
  '/',
  '/login',
  '/ui/styles.css',
  '/ui/app.js'
];
const FONT_CACHE = 'iptv-fonts-v1';

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(PRECACHE))
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys
        .filter(k => k !== CACHE && k !== FONT_CACHE)
        .map(k => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Manifest + icons: always network-only (never cache — so installs always get fresh icon/name)
  if (url.pathname === '/app/manifest.json' || url.pathname.startsWith('/app/icon')) {
    e.respondWith(fetch(e.request));
    return;
  }

  // API calls: network-only, offline returns structured JSON
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response(JSON.stringify({error: 'offline', offline: true}),
          {status: 503, headers: {'Content-Type': 'application/json'}}))
    );
    return;
  }

  // Fonts: cache-first (immutable, long-lived)
  if (url.pathname.startsWith('/ui/fonts/')) {
    e.respondWith(
      caches.open(FONT_CACHE).then(c =>
        c.match(e.request).then(hit =>
          hit || fetch(e.request).then(r => { c.put(e.request, r.clone()); return r; })
        ))
    );
    return;
  }

  // Everything else: network-first, fall back to cache
  e.respondWith(
    fetch(e.request).then(r => {
      if (r.ok) {
        const clone = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
      }
      return r;
    }).catch(() => caches.match(e.request))
  );
});
