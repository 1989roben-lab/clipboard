const CACHE_NAME = "memory-shell-v15";
const APP_SHELL = [
  "/?app=v15",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-512.png",
  "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((name) => name !== CACHE_NAME)
            .map((name) => caches.delete(name)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("message", (event) => {
  if (event.data?.type === "ACTIVATE_UPDATE") {
    self.skipWaiting();
  }
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // User content is always network-only and is never written to PWA caches.
  if (url.pathname.startsWith("/api/") || url.pathname === "/health") {
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches
              .open(CACHE_NAME)
                .then((cache) => cache.put("/?app=v15", copy));
          }
          return response;
        })
        .catch(() => caches.match("/?app=v15")),
    );
    return;
  }

  if (APP_SHELL.includes(url.pathname)) {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ||
          fetch(request).then((response) => {
            if (response.ok) {
              const copy = response.clone();
              caches
                .open(CACHE_NAME)
                .then((cache) => cache.put(request, copy));
            }
            return response;
          }),
      ),
    );
  }
});
