// Keeps the app shell available offline; demo data always comes live from the server.
const SHELL = "itmam-shell-v1";
self.addEventListener("install", e => e.waitUntil(caches.open(SHELL).then(c => c.addAll(["/", "/icon.svg", "/manifest.webmanifest"]))));
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
