/*
 * BThinkX Business OS — service worker.
 * Served at /crm/service-worker.js (see crm/modules/common/views.py:service_worker) so its
 * default scope is /crm/ — the whole app — with no extra header needed.
 *
 * Responsibilities: makes the app installable, gives it an offline fallback instead of the
 * browser's own "no internet" page, and shows Web Push notifications. It deliberately does
 * NOT try to cache or replay API/form POSTs — this is a business app with money and
 * approvals in it; a "worked offline, synced later" write is the wrong trade-off here.
 *
 * Bump CACHE_VERSION when shell assets change so old caches are dropped on activate.
 */
const CACHE_VERSION = 'v5';
const SHELL_CACHE = `crm-shell-${CACHE_VERSION}`;
const RUNTIME_CACHE = `crm-runtime-${CACHE_VERSION}`;
const OFFLINE_URL = '/static/crm/offline.html';

const SHELL_ASSETS = [
  OFFLINE_URL,
  '/crm/manifest.webmanifest',
  '/static/crm/images/icon-192.png',
  '/static/crm/images/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== SHELL_CACHE && k !== RUNTIME_CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return; // never intercept writes (POST forms, actions, uploads)

  const url = new URL(req.url);

  // Page navigations: always prefer the network (data must be fresh); fall back to the
  // cached offline page only when the network is genuinely unreachable.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match(OFFLINE_URL))
    );
    return;
  }

  // Static assets (css/js/images/fonts): stale-while-revalidate — instant from cache,
  // refreshed in the background for next time.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.open(RUNTIME_CACHE).then((cache) => cache.match(req).then((cached) => {
        const network = fetch(req).then((resp) => {
          if (resp && resp.ok) cache.put(req, resp.clone());
          return resp;
        }).catch(() => cached);
        return cached || network;
      }))
    );
    return;
  }

  // Everything else (pages' own data/API calls): leave untouched, straight to the network.
});

// ---- Notifications. The server sends a path inside the app; the click handler decides which window shows it.
const APP_SCOPE = '/crm/';

// Only ever a path under /crm/ on this site. Anything else (another address, a missing value, a bad string) opens the app home
// instead of an unknown page.
function safeTarget(raw) {
  try {
    const u = new URL(raw || APP_SCOPE, self.location.origin);
    if (u.origin !== self.location.origin || !u.pathname.startsWith(APP_SCOPE)) return APP_SCOPE;
    return u.pathname + u.search + u.hash;
  } catch (e) {
    return APP_SCOPE;
  }
}

function inApp(w) {
  try {
    const u = new URL(w.url);
    return u.origin === self.location.origin && u.pathname.startsWith(APP_SCOPE);
  } catch (e) {
    return false;
  }
}

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { /* non-JSON payload, ignore */ }
  const title = data.title || 'BThinkX Business OS';
  const options = {
    body: data.body || '',
    icon: '/static/crm/images/icon-192.png',
    badge: '/static/crm/images/icon-192.png',
    tag: data.tag || 'crm',          // one per type and destination, so two photos never replace each other
    renotify: true,
    data: { url: safeTarget(data.url), type: data.type || '' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = safeTarget(event.notification.data && event.notification.data.url);
  const absolute = new URL(target, self.location.origin).href;
  event.waitUntil((async () => {
    const windows = (await self.clients.matchAll({ type: 'window', includeUncontrolled: true })).filter(inApp);
    // 1. The right page is already open: show it.
    for (const w of windows) {
      if (w.url === absolute && 'focus' in w) return w.focus();
    }
    // 2. The app is open on another page (installed app or a browser tab): bring it forward and move it to the right page.
    for (const w of windows) {
      try {
        const focused = (await w.focus()) || w;
        if ('navigate' in focused) {
          const moved = await focused.navigate(absolute);
          if (moved) return moved;
        }
      } catch (e) { /* this window cannot be moved; try the next, then open a new one */ }
    }
    // 3. The app is closed: open it straight on the right page. If the person is signed out, the sign-in page returns them here.
    if (self.clients.openWindow) return self.clients.openWindow(absolute);
  })());
});
