/* Sovereign Fleet Service Worker - Shell Caching for PWA */
const CACHE_NAME = 'sovereign-fleet-v3';
const SHELL_URLS = [
  '/',
  '/manifest.json',
  '/static/icon.svg'
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS)).catch(() => {})
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      );
      await self.clients.claim();
    })()
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Network-first for all API endpoints to guarantee fresh fleet telemetry
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/health')) {
    event.respondWith(
      fetch(req).catch(() => new Response(JSON.stringify({ error: 'offline' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' }
      }))
    );
    return;
  }

  // Network-first for the dashboard and its shell: a fix shows on the next load, not the one
  // after. The cached copy only answers when the network does not (offline).
  event.respondWith(
    fetch(req).then((resp) => {
      if (resp && resp.status === 200 && req.method === 'GET') {
        const clone = resp.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(req, clone));
      }
      return resp;
    }).catch(() => caches.match(req).then((cached) => cached || Response.error()))
  );
});
