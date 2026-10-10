/* TubeNotes customer Web Push and PWA service worker. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(clients.claim()));

self.addEventListener("push", (event) => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (_) {}
  event.waitUntil(self.registration.showNotification(String(payload.title || "TubeNotes"), {
    body: String(payload.body || "You have a new TubeNotes update."),
    tag: String(payload.tag || "tubenotes-update"),
    renotify: true,
    icon: String(payload.icon || "/static/push-icon-v1.png"),
    badge: String(payload.badge || "/static/push-icon-v1.png"),
    image: String(payload.image || "/static/push-banner-v1.png"),
    actions: Array.isArray(payload.actions) ? payload.actions.slice(0, 2) : [],
    data: { url: String(payload.url || "/") },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.action === "subscribe" ? "/?account=1" : ((event.notification.data && event.notification.data.url) || "/");
  const target = new URL(url, self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if (client.url.startsWith(self.location.origin) && "focus" in client) { await client.focus(); return; }
    }
    if (clients.openWindow) await clients.openWindow(target);
  })());
});
