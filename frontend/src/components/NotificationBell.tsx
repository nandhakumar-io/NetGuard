import { useCallback, useEffect, useRef, useState } from "react";
import { api, getAccessToken } from "../lib/api";
import { AppNotification, NotificationSummary } from "../lib/types";

const SEVERITY_DOT: Record<string, string> = {
  critical: "bg-riskcrit",
  warning: "bg-riskmed",
  info: "bg-brandblue",
};

const EVENT_ICON: Record<string, string> = {
  deployment_succeeded: "✅",
  deployment_failed: "❌",
  rollback_triggered: "↺",
  drift_high: "⚠️",
  drift_critical: "🚨",
  generic: "🔔",
};

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

export default function NotificationBell() {
  const [open, setOpen] = useState(false);
  const [notifications, setNotifications] = useState<AppNotification[]>([]);
  const [summary, setSummary] = useState<NotificationSummary>({ unread_count: 0, total: 0 });
  const [connection, setConnection] = useState<"live" | "polling" | "connecting">("connecting");
  const containerRef = useRef<HTMLDivElement | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const [pushEnabled, setPushEnabled] = useState(false);
  const lastNoteIdRef = useRef<string | null>(null);

  useEffect(() => {
    if ("Notification" in window) {
      setPushEnabled(Notification.permission === "granted");
    }
  }, []);

  const requestPushPermission = async () => {
    if (!("Notification" in window)) return;
    const permission = await Notification.requestPermission();
    setPushEnabled(permission === "granted");
  };

  const deliverBrowserPush = useCallback((n: AppNotification) => {
    if (Notification.permission === "granted") {
      new Notification(`NetGuard: ${n.title}`, {
        body: n.message,
      });
    }
  }, []);

  const fetchNotifications = useCallback(() => {
    api
      .get<AppNotification[]>("/notifications?limit=20")
      .then((res) => {
        setNotifications(res.data);
        if (res.data.length > 0) {
          lastNoteIdRef.current = res.data[0].id;
        }
      })
      .catch(() => {});
  }, []);

  const fetchSummary = useCallback(() => {
    api
      .get<NotificationSummary>("/notifications/summary")
      .then((res) => setSummary(res.data))
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetchNotifications();
    fetchSummary();
  }, [fetchNotifications, fetchSummary]);

  // Realtime via WebSocket, falling back to polling if the socket can't connect.
  useEffect(() => {
    let mounted = true;
    const base = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1";
    const wsUrl = base.replace(/^http/, "ws") + "/notifications/ws?token=" + encodeURIComponent(getAccessToken() || "");
    let ws: WebSocket | null = null;
    let pollInterval: ReturnType<typeof setInterval> | null = null;

    try {
      ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      ws.onopen = () => mounted && setConnection("live");
      ws.onmessage = (evt) => {
        if (!mounted) return;
        try {
          const data = JSON.parse(evt.data);
          if (data.notifications && data.notifications.length > 0) {
            const newTopId = data.notifications[0].id;
            if (lastNoteIdRef.current && lastNoteIdRef.current !== newTopId) {
              const newNotes = [];
              for (const n of data.notifications) {
                if (n.id === lastNoteIdRef.current) break;
                newNotes.push(n);
              }
              if (Notification.permission === "granted") {
                newNotes.reverse().forEach((n) => deliverBrowserPush(n));
              }
            }
            lastNoteIdRef.current = newTopId;
            setNotifications(data.notifications);
          }
          if (data.summary) setSummary(data.summary);
        } catch {
          /* ignore malformed frame, next push will correct state */
        }
      };
      ws.onerror = () => {
        if (!mounted) return;
        setConnection("polling");
        if (!pollInterval) {
          pollInterval = setInterval(() => {
            fetchNotifications();
            fetchSummary();
          }, 10000);
        }
      };
      ws.onclose = () => {
        if (!mounted) return;
        setConnection((prev) => (prev === "live" ? "polling" : prev));
        if (!pollInterval) {
          pollInterval = setInterval(() => {
            fetchNotifications();
            fetchSummary();
          }, 10000);
        }
      };
    } catch {
      setConnection("polling");
      pollInterval = setInterval(() => {
        fetchNotifications();
        fetchSummary();
      }, 10000);
    }

    return () => {
      mounted = false;
      ws?.close();
      if (pollInterval) clearInterval(pollInterval);
    };
  }, [fetchNotifications, fetchSummary, deliverBrowserPush]);

  // Close dropdown on outside click.
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  const markRead = async (id: string) => {
    setNotifications((prev) => prev.map((n) => (n.id === id ? { ...n, read: true } : n)));
    setSummary((prev) => ({ ...prev, unread_count: Math.max(0, prev.unread_count - 1) }));
    try {
      await api.patch(`/notifications/${id}/read`);
    } catch {
      fetchNotifications();
      fetchSummary();
    }
  };

  const markAllRead = async () => {
    setNotifications((prev) => prev.map((n) => ({ ...n, read: true })));
    setSummary((prev) => ({ ...prev, unread_count: 0 }));
    try {
      await api.patch("/notifications/read-all");
    } catch {
      fetchNotifications();
      fetchSummary();
    }
  };

  return (
    <div className="relative" ref={containerRef}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="relative w-9 h-9 flex items-center justify-center rounded-lg text-slate-500 hover:text-navy hover:bg-slate-100 transition-colors"
        title="Notifications"
      >
        <span className="text-lg">🔔</span>
        {summary.unread_count > 0 && (
          <span className="absolute -top-0.5 -right-0.5 bg-riskcrit text-white text-[10px] font-bold rounded-full min-w-[16px] h-4 px-1 flex items-center justify-center">
            {summary.unread_count > 99 ? "99+" : summary.unread_count}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 mt-2 w-96 max-w-[90vw] bg-white border border-slate-200 rounded-xl shadow-xl z-50 overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3 border-b border-slate-100">
            <p className="text-sm font-semibold text-navy">Notifications</p>
            <div className="flex items-center gap-3">
              {!pushEnabled && "Notification" in window && (
                <button onClick={requestPushPermission} className="text-[10px] bg-brandblue/10 hover:bg-brandblue/20 text-brandblue px-2 py-0.5 rounded font-medium" title="Enable browser push notifications for alerts">
                  Enable Push
                </button>
              )}
              <span
                className={`text-[10px] uppercase font-medium ${
                  connection === "live" ? "text-risklow" : "text-slate-400"
                }`}
              >
                {connection === "live" ? "● live" : connection === "polling" ? "polling" : "connecting…"}
              </span>
              {summary.unread_count > 0 && (
                <button onClick={markAllRead} className="text-xs text-brandblue font-medium hover:text-navy">
                  Mark all read
                </button>
              )}
            </div>
          </div>

          <div className="max-h-96 overflow-y-auto">
            {notifications.length === 0 && (
              <p className="text-xs text-slate-400 italic text-center py-8">No notifications yet.</p>
            )}
            {notifications.map((n) => (
              <button
                key={n.id}
                onClick={() => !n.read && markRead(n.id)}
                className={`w-full text-left px-4 py-3 border-b border-slate-50 last:border-0 hover:bg-slate-50 transition-colors flex gap-3 ${
                  n.read ? "opacity-60" : ""
                }`}
              >
                <span className="text-base leading-none mt-0.5">{EVENT_ICON[n.event_type] || "🔔"}</span>
                <span className="flex-1 min-w-0">
                  <span className="flex items-center gap-1.5">
                    <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${SEVERITY_DOT[n.severity] || "bg-slate-300"}`} />
                    <span className="text-xs font-semibold text-navy truncate">{n.title}</span>
                    {!n.read && <span className="w-1.5 h-1.5 rounded-full bg-brandblue shrink-0 ml-auto" />}
                  </span>
                  <span className="block text-xs text-slate-500 mt-0.5 line-clamp-2">{n.message}</span>
                  <span className="block text-[10px] text-slate-400 mt-1">
                    {n.device_hostname ? `${n.device_hostname} · ` : ""}
                    {timeAgo(n.created_at)}
                  </span>
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}