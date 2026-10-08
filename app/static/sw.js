/* TubeNotes customer Web Push service worker. */
self.addEventListener("push", (event) => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (_) {}
  event.waitUntil(self.registration.showNotification(String(payload.title || "TubeNotes"), {
    body: String(payload.body || "You have a new TubeNotes update."),
    tag: String(payload.tag || "tubenotes-update"),
    renotify: true,
    data: { url: String(payload.url || "/") },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if (client.url.startsWith(self.location.origin) && "focus" in client) { await client.focus(); return; }
    }
    if (clients.openWindow) await clients.openWindow(target);
  })());
});
