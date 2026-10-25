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
const CACHE_VERSION = 'v1';
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

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { /* non-JSON payload, ignore */ }
  const title = data.title || 'BThinkX Business OS';
  const options = {
    body: data.body || '',
    icon: '/static/crm/images/icon-192.png',
    badge: '/static/crm/images/icon-192.png',
    tag: data.tag || 'crm',
    renotify: true,
    data: { url: data.url || '/crm/' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/crm/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
      for (const w of windows) {
        if (w.url === target && 'focus' in w) return w.focus();
      }
      for (const w of windows) {
        if ('focus' in w) { w.focus(); if ('navigate' in w) return w.navigate(target); }
      }
      if (self.clients.openWindow) return self.clients.openWindow(target);
    })
  );
});
