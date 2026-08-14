// NetGuard push notification service worker.
//
// Registered from src/pages/PushSettings.tsx when a user adds a "Browser"
// push device. Two jobs: turn an incoming Web Push message into an
// OS-level notification (`push`), and focus/open the app when the user
// taps it (`notificationclick`). Kept dependency-free and outside the
// Vite build (served as a static file from /sw.js) since service workers
// must be same-origin, top-level scripts -- see PushSettings.tsx's
// registration call for why it points here specifically.

self.addEventListener("push", (event) => {
  let data = { title: "NetGuard alert", body: "", severity: "info", url: null };
  if (event.data) {
    try {
      data = { ...data, ...event.data.json() };
    } catch {
      data.body = event.data.text();
    }
  }

  const severity = data.severity || "info";
  const icon = "/netguard.svg";

  const options = {
    body: data.body || "",
    icon,
    badge: icon,
    tag: data.url || data.title, // collapse repeat pushes about the same thing into one notification
    requireInteraction: severity === "critical", // a P1 shouldn't auto-dismiss before it's seen
    data: { url: data.url || "/" },
  };

  event.waitUntil(self.registration.showNotification(data.title || "NetGuard alert", options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = event.notification.data?.url || "/";

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        // Reuse an already-open NetGuard tab rather than piling up new
        // ones every time an alert fires.
        if ("focus" in client) {
          client.navigate(targetUrl);
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })
  );
});