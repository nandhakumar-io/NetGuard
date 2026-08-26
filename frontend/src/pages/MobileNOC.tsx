import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Alert, AlertSeverity, Device } from "../lib/types";

// Purpose-built mobile view, deliberately NOT wrapped in the desktop
// Layout (sidebar/drawer/topbar) -- an on-call engineer pulling this up
// on a phone at 2am wants one thing (active alerts, big tap targets to
// ack/resolve) as fast as possible, not the full 25-page nav. Routed at
// /noc, outside <Layout> in App.tsx but still behind ProtectedRoute.
//
// Polls GET /alerts (unresolved only) every 12s -- short enough to feel
// live without needing a websocket reconnect/backoff story on a page
// that's meant to survive being backgrounded and reopened repeatedly on
// a phone.
const POLL_MS = 12_000;

const SEVERITY_STYLE: Record<AlertSeverity, { bar: string; chip: string; label: string }> = {
  critical: { bar: "bg-red-600", chip: "bg-red-100 text-red-700", label: "Critical" },
  warning: { bar: "bg-amber-500", chip: "bg-amber-100 text-amber-700", label: "Warning" },
  info: { bar: "bg-blue-500", chip: "bg-blue-100 text-blue-700", label: "Info" },
};

type SeverityFilter = "all" | AlertSeverity;

function timeAgo(iso: string): string {
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export default function MobileNOC() {
  const { user, logout } = useAuth();

  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [devicesById, setDevicesById] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<SeverityFilter>("all");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

  // Avoids a jarring full-page spinner on every 12s poll -- only the
  // first load shows the big loading state, refreshes are silent unless
  // they fail.
  const hasLoadedOnce = useRef(false);

  const fetchAlerts = useCallback(() => {
    api
      .get<Alert[]>("/alerts", { params: { status: "active", limit: 200 } })
      .then((res) => {
        setAlerts(res.data);
        setError(null);
        setLastRefreshed(new Date());
      })
      .catch(() => setError("Couldn't refresh alerts — showing the last known state."))
      .finally(() => {
        hasLoadedOnce.current = true;
        setLoading(false);
      });
  }, []);

  useEffect(() => {
    fetchAlerts();
    api
      .get<Device[]>("/devices")
      .then((res) => {
        const map: Record<string, string> = {};
        for (const d of res.data) map[d.id] = d.hostname;
        setDevicesById(map);
      })
      .catch(() => {});
    const t = setInterval(fetchAlerts, POLL_MS);
    return () => clearInterval(t);
  }, [fetchAlerts]);

  const acknowledge = async (id: string) => {
    setBusyId(id);
    // Optimistic update -- on a flaky mobile connection, waiting for the
    // round trip before reflecting the tap makes the UI feel broken.
    setAlerts((prev) => prev.map((a) => (a.id === id ? { ...a, acknowledged: true } : a)));
    try {
      await api.patch(`/alerts/${id}/acknowledge`);
    } catch {
      fetchAlerts(); // roll back to server truth on failure
    } finally {
      setBusyId(null);
    }
  };

  const resolve = async (id: string) => {
    setBusyId(id);
    setAlerts((prev) => prev.filter((a) => a.id !== id));
    try {
      await api.patch(`/alerts/${id}/resolve`);
    } catch {
      fetchAlerts();
    } finally {
      setBusyId(null);
    }
  };

  const escalate = async (id: string) => {
    setBusyId(id);
    setAlerts((prev) => prev.map((a) => (a.id === id ? { ...a, escalated: true } : a)));
    try {
      await api.patch(`/alerts/${id}/escalate`);
    } catch {
      fetchAlerts();
    } finally {
      setBusyId(null);
    }
  };

  const executeRunbook = async (alert: Alert) => {
    if (!alert.runbook?.id || !alert.device_id) return;
    setBusyId(alert.id);
    try {
      await api.post(`/alert-runbooks/${alert.runbook.id}/execute`, {
        device_id: alert.device_id,
        alert_id: alert.id,
      });
      alert.resolved ? null : await api.patch(`/alerts/${alert.id}/resolve`);
      setAlerts((prev) => prev.filter((a) => a.id !== alert.id));
    } catch (err: unknown) {
      const e = err as { response?: { data?: { detail?: string } } };
      window.alert(e?.response?.data?.detail ?? "Failed to run remediation");
    } finally {
      setBusyId(null);
    }
  };

  const visible = alerts
    .filter((a) => !a.resolved)
    .filter((a) => filter === "all" || a.severity === filter)
    .sort((a, b) => {
      const order: Record<AlertSeverity, number> = { critical: 0, warning: 1, info: 2 };
      const sevDiff = order[a.severity] - order[b.severity];
      if (sevDiff !== 0) return sevDiff;
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    });

  const counts = {
    critical: alerts.filter((a) => !a.resolved && a.severity === "critical").length,
    warning: alerts.filter((a) => !a.resolved && a.severity === "warning").length,
    info: alerts.filter((a) => !a.resolved && a.severity === "info").length,
  };

  return (
    <div className="min-h-screen bg-slate-100 flex flex-col">
      {/* Compact sticky header -- fits in one thumb-reach zone */}
      <div className="sticky top-0 z-10 bg-navy text-white px-4 pt-[env(safe-area-inset-top)] pb-3 shadow">
        <div className="flex items-center justify-between pt-2">
          <div>
            <p className="text-sm font-bold tracking-wide">NOC Mode</p>
            <p className="text-[11px] text-white/60">{user?.full_name || user?.email}</p>
          </div>
          <button
            onClick={() => logout()}
            className="text-[11px] text-white/60 hover:text-white px-2 py-1 rounded-md hover:bg-white/10"
          >
            Sign out
          </button>
        </div>

        <div className="flex gap-2 mt-3 overflow-x-auto pb-1 -mx-1 px-1">
          <FilterChip label="All" active={filter === "all"} onClick={() => setFilter("all")} count={counts.critical + counts.warning + counts.info} />
          <FilterChip label="Critical" active={filter === "critical"} onClick={() => setFilter("critical")} count={counts.critical} tone="critical" />
          <FilterChip label="Warning" active={filter === "warning"} onClick={() => setFilter("warning")} count={counts.warning} tone="warning" />
          <FilterChip label="Info" active={filter === "info"} onClick={() => setFilter("info")} count={counts.info} tone="info" />
        </div>
      </div>

      {error && (
        <div className="mx-4 mt-3 px-3 py-2 rounded-lg bg-amber-50 text-amber-700 text-xs">{error}</div>
      )}

      {/* Alert list -- one big tappable card per alert, no nested detail
          panels: acknowledge/resolve are the only two actions this view
          offers. Anything deeper (runbooks, full history, snooze) is a
          "open in Alert Center" tap away, not crammed in here. */}
      <div className="flex-1 px-3 py-3 space-y-2.5 pb-24">
        {loading && !hasLoadedOnce.current && (
          <div className="flex justify-center py-16">
            <div className="w-6 h-6 border-2 border-slate-300 border-t-navy rounded-full animate-spin" />
          </div>
        )}

        {!loading && visible.length === 0 && (
          <div className="text-center py-16">
            <p className="text-3xl mb-2">✅</p>
            <p className="text-sm font-semibold text-emerald-700">All clear</p>
            <p className="text-xs text-slate-400 mt-1">No active alerts{filter !== "all" ? ` at ${filter} severity` : ""}.</p>
          </div>
        )}

        {visible.map((alert) => {
          const style = SEVERITY_STYLE[alert.severity] || SEVERITY_STYLE.info;
          const hostname = alert.device_id ? devicesById[alert.device_id] : null;
          return (
            <div key={alert.id} className="bg-white rounded-xl shadow-sm overflow-hidden flex">
              <div className={`w-1.5 shrink-0 ${style.bar}`} />
              <div className="flex-1 p-3 min-w-0">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${style.chip}`}>{style.label}</span>
                      {alert.acknowledged && (
                        <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-slate-100 text-slate-500">Acked</span>
                      )}
                      {alert.escalated && (
                        <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-purple-100 text-purple-700">Escalated</span>
                      )}
                    </div>
                    <p className="text-sm font-semibold text-navy mt-1 truncate">{alert.category}</p>
                    {hostname && <p className="text-xs text-slate-500 truncate">{hostname}</p>}
                    <p className="text-xs text-slate-400 mt-0.5 line-clamp-2">{alert.message}</p>
                  </div>
                  <span className="text-[10px] text-slate-400 shrink-0 whitespace-nowrap">{timeAgo(alert.created_at)}</span>
                </div>

                <div className="grid grid-cols-2 gap-2 mt-4">
                  <button
                    onClick={() => acknowledge(alert.id)}
                    disabled={busyId === alert.id || alert.acknowledged}
                    className="w-full py-3.5 rounded-lg bg-slate-100 text-slate-700 text-[15px] font-semibold active:bg-slate-200 disabled:opacity-50 disabled:bg-slate-50 transition-colors"
                  >
                    {alert.acknowledged ? "Acknowledged" : "Acknowledge"}
                  </button>
                  <button
                    onClick={() => resolve(alert.id)}
                    disabled={busyId === alert.id}
                    className="w-full py-3.5 rounded-lg bg-emerald-600 text-white text-[15px] font-semibold active:bg-emerald-700 disabled:opacity-50 transition-colors"
                  >
                    Resolve
                  </button>
                  <button
                    onClick={() => escalate(alert.id)}
                    disabled={busyId === alert.id || alert.escalated}
                    className="w-full py-3.5 rounded-lg border-2 border-purple-200 text-purple-700 text-[15px] font-semibold active:bg-purple-100 disabled:opacity-50 transition-colors"
                  >
                    {alert.escalated ? "Escalated" : "Escalate"}
                  </button>
                  {alert.runbook?.remediation_enabled && (
                    <button
                      onClick={() => executeRunbook(alert)}
                      disabled={busyId === alert.id}
                      className="w-full py-3.5 rounded-lg bg-brandblue text-white text-[15px] font-semibold active:bg-blue-700 disabled:opacity-50 transition-colors shadow-sm"
                    >
                      Run Runbook
                    </button>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Fixed footer: refresh state + link out to the full Alert Center
          for anything this stripped-down view doesn't cover. */}
      <div className="fixed bottom-0 inset-x-0 bg-white border-t border-slate-200 px-4 py-2.5 pb-[calc(env(safe-area-inset-bottom)+0.625rem)] flex items-center justify-between">
        <p className="text-[11px] text-slate-400">
          {lastRefreshed ? `Updated ${timeAgo(lastRefreshed.toISOString())}` : "Loading…"}
        </p>
        <a href="/alerts" className="text-[11px] font-semibold text-brandblue">
          Full Alert Center →
        </a>
      </div>
    </div>
  );
}

function FilterChip({
  label,
  active,
  onClick,
  count,
  tone,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  count: number;
  tone?: "critical" | "warning" | "info";
}) {
  const toneActive: Record<string, string> = {
    critical: "bg-red-600 text-white",
    warning: "bg-amber-500 text-white",
    info: "bg-blue-500 text-white",
  };
  return (
    <button
      onClick={onClick}
      className={`shrink-0 px-3 py-1.5 rounded-full text-xs font-semibold whitespace-nowrap transition-colors ${
        active ? toneActive[tone || ""] || "bg-white text-navy" : "bg-white/10 text-white/70"
      }`}
    >
      {label} {count > 0 && <span className="opacity-80">({count})</span>}
    </button>
  );
}