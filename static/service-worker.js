// Carrac - Service Worker (every network fetch is bounded so respondWith always settles)
//
// __APP_VERSION__ below is a token, not a version: the /service-worker.js route
// substitutes APP_VERSION from carrac/version.py when it serves this file, so a
// new release always gets fresh cache names. Bump the version there, not here.
const CACHE_NAME = 'carrac-v__APP_VERSION__';
const STATIC_CACHE = 'carrac-static-v__APP_VERSION__';
const DATA_CACHE = 'carrac-data-v__APP_VERSION__';

// Hard timeout for the SW's own network fetch. A request can stall forever on a
// half-open ("zombie") TCP connection — common in the seconds right after a tab
// or device wakes from sleep. Without a bound, the fetch below neither resolves
// nor rejects, so respondWith() never settles and the PAGE's fetch hangs forever.
// Critically, the page's own AbortController CANNOT cancel a request that the SW
// has taken over via respondWith() — so the client-side 30s chart/asset
// watchdogs are useless here. The spinner stays stuck until a manual reload.
// This timeout is the real fix: it guarantees the SW always settles (with a
// cached response or a 503), which lets the page clear its spinner.
const API_NETWORK_TIMEOUT_MS = 20000;
const STATIC_NETWORK_TIMEOUT_MS = 15000;

// fetch() with a hard timeout. Aborts the in-flight request and rejects so the
// caller's .catch (cache fallback) runs instead of hanging.
function fetchWithTimeout(request, timeoutMs) {
  return new Promise((resolve, reject) => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort();
      reject(new Error('SW network timeout'));
    }, timeoutMs);
    fetch(request, { signal: controller.signal }).then(
      (response) => { clearTimeout(timer); resolve(response); },
      (err) => { clearTimeout(timer); reject(err); }
    );
  });
}

// Static assets to cache immediately
// Only pre-cache truly static assets — JS/CSS are cache-busted via ?v= in the HTML
const STATIC_ASSETS = [
  '/static/manifest.json',
  '/static/favicon.svg',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/js/details-drawer.js',
  '/static/css/carrac-details.css',
  '/static/js/desk.js',
  '/static/js/desk-grid.js',
  '/static/js/desk-stream.js',
  '/static/css/desk.css'
];

// Install event - cache static assets
self.addEventListener('install', (event) => {
  console.log('[SW] Installing service worker...');
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => {
        console.log('[SW] Caching static assets');
        return cache.addAll(STATIC_ASSETS);
      })
      .then(() => self.skipWaiting())
  );
});

// Activate event - clean up old caches
self.addEventListener('activate', (event) => {
  console.log('[SW] Activating service worker...');
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (cacheName !== STATIC_CACHE && cacheName !== DATA_CACHE) {
            console.log('[SW] Removing old cache:', cacheName);
            return caches.delete(cacheName);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// Fetch event - network first for API, cache first for static
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Skip non-GET requests
  if (event.request.method !== 'GET') {
    return;
  }

  // Skip chrome-extension and other non-http requests
  if (!url.protocol.startsWith('http')) {
    return;
  }

  // Skip cross-origin requests (CDNs, analytics, fonts) — let browser handle directly
  if (url.origin !== self.location.origin) {
    return;
  }

  // Never cache report pages (dynamic content, must always be fresh)
  if (url.pathname.startsWith('/report/')) {
    return;
  }

  // Never cache sync poll requests (real-time, must always hit server)
  if (url.pathname.startsWith('/api/sync/')) {
    return;
  }

  // API requests - network first, cache fallback
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetchWithTimeout(event.request, API_NETWORK_TIMEOUT_MS)
        .then((response) => {
          // Clone and cache successful responses
          if (response.ok) {
            // Stamp the cached copy with its write time so a later stale-serve can
            // report data age. Headers are immutable — reconstruct via new Response.
            const responseClone = response.clone();
            const headers = new Headers(responseClone.headers);
            headers.set('X-Carrac-Cached-At', String(Date.now()));
            const forCache = new Response(responseClone.body, {
              status: responseClone.status,
              statusText: responseClone.statusText,
              headers: headers
            });
            caches.open(DATA_CACHE).then((cache) => {
              cache.put(event.request, forCache);
            });
          }
          return response;
        })
        .catch(() => {
          // Network failed, try cache
          return caches.match(event.request).then((cachedResponse) => {
            if (cachedResponse) {
              const cachedAt = cachedResponse.headers.get('X-Carrac-Cached-At');
              const ageS = cachedAt ? Math.round((Date.now() - Number(cachedAt)) / 1000) : null;
              console.log('[SW] Serving stale cached API response, age=' + ageS + 's');
              const h = new Headers(cachedResponse.headers);
              h.set('X-Carrac-Stale', '1');
              if (ageS !== null) h.set('X-Carrac-Age', String(ageS));
              return new Response(cachedResponse.clone().body, {
                status: cachedResponse.status,
                statusText: cachedResponse.statusText,
                headers: h
              });
            }
            // Return offline response
            return new Response(
              JSON.stringify({
                error: 'You are offline. Cached data not available.',
                offline: true
              }),
              {
                status: 503,
                headers: { 'Content-Type': 'application/json' }
              }
            );
          });
        })
    );
    return;
  }

  // Navigation requests (full-page document loads / reloads) — network-first.
  // Navigations must not reach the catch-all static branch below: that branch
  // is stale-while-revalidate and returns any cached hit instantly. Cache
  // Storage ignores the server's Cache-Control: no-store, so a navigation
  // response stored in STATIC_CACHE would be served on the NEXT navigation —
  // including the one right after a deploy — as a stale document (old asset
  // hashes, missing pre-paint script, stale CSP nonce, stale window._userId
  // embed) instead of a fresh Jinja render.
  //
  // Fix: always try the network first for navigations. On success, refresh
  // the STATIC_CACHE entry (keeps the offline-PWA fallback working) and
  // return the fresh response. Only fall back to the cached document if the
  // network is down or too slow; if there's no cached copy either, let the
  // failure propagate so the browser shows its own offline interstitial.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetchWithTimeout(event.request, API_NETWORK_TIMEOUT_MS)
        .then(function(response) {
          if (response.ok) {
            const responseClone = response.clone();
            caches.open(STATIC_CACHE).then(function(cache) {
              cache.put(event.request, responseClone);
            });
          }
          return response;
        })
        .catch(function(err) {
          return caches.match(event.request).then(function(cachedResponse) {
            if (cachedResponse) return cachedResponse;
            throw err;
          });
        })
    );
    return;
  }

  // Static assets — stale-while-revalidate for version-busted URLs (?v=<hash>).
  // Every ?v= URL is content-addressed: the cached copy IS the correct code for
  // that URL, so serving it instantly is safe. New code ships with a new URL,
  // which misses the cache and falls through to the network fetch below.
  // Non-versioned assets (e.g. /static/manifest.json) also benefit: they are
  // pre-cached on install and served instantly; the background revalidation
  // keeps them fresh for the next visit.
  //
  // Executable assets must never get a synthesized 503. A fake
  // `new Response('Offline', {status: 503})` is harmless for an image (a
  // broken <img>), but for a <script> or CSS <link> the browser parses that
  // body AS the asset's source. A script then declares nothing — every
  // top-level global/function it would have defined never exists, silently,
  // with no error of its own. For example, if kline-drawings.js were served a
  // fake 503, `klineContextMenu` would never exist, and the next tap anywhere
  // would hit chart-events.js's bare `klineContextMenu` check and throw a
  // ReferenceError, killing every statement after it in that click handler
  // (chart-events.js guards that read with a `typeof` check). A late script
  // is better than a fake 503, so script/style requests below skip the
  // timeout-bounded fetch entirely — cache-hit revalidation AND cache-miss
  // fetch both use a bare fetch() that only resolves on a real network
  // outcome, never on an internal clock. Non-executable static assets (images,
  // fonts, manifest.json, …) use the bounded fetchWithTimeout + fake-503
  // fallback — a hung image is tolerable, and bounding it guarantees
  // respondWith() always settles for those.
  var isExecutableAsset = event.request.destination === 'script' ||
    event.request.destination === 'style' ||
    // Fallback for browsers that don't populate request.destination reliably.
    /\.(js|css)$/i.test(url.pathname);

  event.respondWith(
    caches.open(STATIC_CACHE).then(function(cache) {
      return cache.match(event.request).then(function(cachedResponse) {
        if (isExecutableAsset) {
          if (cachedResponse) {
            // Cache hit: return instantly. Revalidate in the background with a
            // plain fetch — no timeout wrapper on this path (see comment above).
            fetch(event.request).then(function(response) {
              if (response.ok) cache.put(event.request, response.clone());
            }).catch(function() {
              // Background revalidation failed — the cached copy already
              // shipped, nothing else to do here.
            });
            return cachedResponse;
          }
          // Cache miss: wait for the real network outcome — no timeout, no
          // synthetic 503. If fetch() itself rejects (genuinely offline), let
          // that rejection propagate to respondWith() so the browser reports
          // its own real load failure instead of us fabricating one.
          return fetch(event.request).then(function(response) {
            if (response.ok) cache.put(event.request, response.clone());
            return response;
          });
        }

        // Non-executable static assets — kick off a background revalidation
        // regardless of cache hit, bounded by fetchWithTimeout so
        // respondWith() always settles.
        var networkFetch = fetchWithTimeout(event.request, STATIC_NETWORK_TIMEOUT_MS)
          .then(function(response) {
            if (response.ok) {
              cache.put(event.request, response.clone());
            }
            return response;
          })
          .catch(function() {
            // Network failed — if we have a cached copy it was already returned;
            // if not, surface the offline 503 by letting the Promise settle.
            return null;
          });

        if (cachedResponse) {
          // Cache hit: return instantly, revalidation runs in the background.
          return cachedResponse;
        }
        // Cache miss: wait for the network (first visit or after cache purge).
        return networkFetch.then(function(response) {
          if (response) return response;
          return new Response('Offline', { status: 503 });
        });
      });
    })
  );
});

// Listen for messages from the app
self.addEventListener('message', (event) => {
  if (event.data === 'skipWaiting') {
    self.skipWaiting();
  }

  if (event.data === 'clearCache') {
    caches.keys().then((cacheNames) => {
      cacheNames.forEach((cacheName) => {
        caches.delete(cacheName);
      });
    });
  }
});

// Background sync for offline actions (future use)
self.addEventListener('sync', (event) => {
  if (event.tag === 'sync-watchlist') {
    console.log('[SW] Syncing watchlist...');
    // Could sync watchlist changes when back online
  }
});

// Push notification handler
self.addEventListener('push', (event) => {
  console.log('[SW] Push notification received');

  let data = { title: 'Market Alert', body: 'Price alert triggered', icon: '/static/icons/icon-192.png' };

  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data.body = event.data.text();
    }
  }

  const options = {
    body: data.body,
    icon: data.icon || '/static/icons/icon-192.png',
    badge: '/static/icons/icon-72.png',
    vibrate: [200, 100, 200],
    data: {
      url: data.url || '/',
      symbol: data.symbol
    },
    actions: data.actions || [
      { action: 'view', title: 'View Chart' },
      { action: 'dismiss', title: 'Dismiss' }
    ],
    tag: data.tag || 'price-alert',
    renotify: true,
    requireInteraction: data.requireInteraction || false
  };

  event.waitUntil(
    self.registration.showNotification(data.title, options).then(() => {
      // Also notify any focused clients so they can show an in-page toast
      return self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(clients => {
        clients.forEach(client => client.postMessage({type: 'push-received', title: data.title, body: data.body, symbol: data.symbol}));
      });
    })
  );
});

// Notification click handler
self.addEventListener('notificationclick', (event) => {
  console.log('[SW] Notification clicked:', event.action);
  event.notification.close();

  if (event.action === 'dismiss') {
    return;
  }

  // Open or focus the app. URL must be same-origin — we never open a window
  // for an arbitrary URL from the push payload, even though the push endpoint
  // is auth'd.
  function _safeUrl(raw) {
    if (!raw) return '/';
    try {
      const parsed = new URL(raw, self.location.origin);
      if (parsed.origin === self.location.origin) {
        return parsed.pathname + parsed.search + parsed.hash;
      }
    } catch (_) { /* fallthrough */ }
    return '/';
  }
  const urlToOpen = _safeUrl(event.notification.data?.url);

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((windowClients) => {
        // Check if app is already open
        for (const client of windowClients) {
          if (client.url.includes(self.location.origin) && 'focus' in client) {
            return client.focus().then((focusedClient) => {
              // Navigate to symbol if specified
              if (event.notification.data?.symbol) {
                focusedClient.postMessage({
                  type: 'NAVIGATE_TO_SYMBOL',
                  symbol: event.notification.data.symbol
                });
              }
              return focusedClient;
            });
          }
        }
        // Open new window if not already open
        return clients.openWindow(urlToOpen);
      })
  );
});
