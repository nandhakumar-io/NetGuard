import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../lib/api";
import { DeviceOverview, DeviceTimelineEvent } from "../lib/types";
import { useToast, errorMessage } from "../lib/toast";
import StatCard from "../components/StatCard";

// Health/alerts/drift/syslog/deployments used to live on four separate
// pages, so triaging "why is this device unhealthy" meant tab-hopping
// between them and correlating timestamps by hand. This page is the
// single-panel answer: current health, recent-source counts, and one
// merged timeline (GET /devices/{id}/overview), reachable directly from
// Alert Center / Incidents instead of starting a fresh hunt each time.

const HEALTH_COLOR_STYLE: Record<string, string> = {
  green: "bg-risklow/10 text-risklow border-risklow/20",
  yellow: "bg-riskmed/10 text-riskmed border-riskmed/20",
  red: "bg-riskcrit/10 text-riskcrit border-riskcrit/20",
  gray: "bg-slate-100 text-slate-500 border-slate-200",
  unknown: "bg-slate-100 text-slate-500 border-slate-200",
};

const EVENT_STYLE: Record<DeviceTimelineEvent["kind"], { icon: string; label: string }> = {
  alert_raised: { icon: "🚨", label: "Alert" },
  alert_resolved: { icon: "✅", label: "Alert resolved" },
  config_drift: { icon: "🔀", label: "Config drift" },
  syslog: { icon: "🖥️", label: "Syslog" },
  deployment: { icon: "🚀", label: "Deployment" },
};

const SEVERITY_DOT: Record<DeviceTimelineEvent["severity"], string> = {
  critical: "bg-riskcrit",
  warning: "bg-riskmed",
  info: "bg-brandblue",
};

const WINDOW_OPTIONS = [
  { hours: 24, label: "24h" },
  { hours: 72, label: "3d" },
  { hours: 24 * 7, label: "7d" },
  { hours: 24 * 30, label: "30d" },
];

function timeAgo(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function DeviceDetail() {
  const { deviceId } = useParams<{ deviceId: string }>();
  const navigate = useNavigate();
  const toast = useToast();

  const [overview, setOverview] = useState<DeviceOverview | null>(null);
  const [hours, setHours] = useState(72);
  const [loading, setLoading] = useState(true);
  const [kindFilter, setKindFilter] = useState<DeviceTimelineEvent["kind"] | "all">("all");

  useEffect(() => {
    if (!deviceId) return;
    let cancelled = false;
    setLoading(true);
    api
      .get<DeviceOverview>(`/devices/${deviceId}/overview`, { params: { hours } })
      .then((res) => {
        if (!cancelled) setOverview(res.data);
      })
      .catch((err) => toast.error(errorMessage(err, "Failed to load device overview")))
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId, hours]);

  if (loading && !overview) {
    return <div className="p-8 text-sm text-slate-400">Loading device overview…</div>;
  }
  if (!overview) {
    return <div className="p-8 text-sm text-slate-400">Device not found.</div>;
  }

  const healthColor = overview.health?.health_color || "unknown";
  const events = overview.timeline.filter((e) => kindFilter === "all" || e.kind === kindFilter);

  return (
    <div className="p-6 space-y-6 max-w-5xl mx-auto">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <button
            onClick={() => navigate(-1)}
            className="text-xs font-medium text-slate-400 hover:text-navy dark:hover:text-white mb-2"
          >
            ← Back
          </button>
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="text-2xl font-bold text-navy dark:text-white">{overview.hostname}</h1>
            <span
              className={`text-xs font-bold uppercase tracking-wide px-2.5 py-1 rounded-full border ${HEALTH_COLOR_STYLE[healthColor]}`}
            >
              {healthColor}
              {overview.health?.health_score != null ? ` · ${overview.health.health_score}` : ""}
            </span>
          </div>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            {overview.ip_address} · {overview.vendor} · {overview.status}
          </p>
        </div>

        <div className="flex items-center gap-1 bg-slate-100 dark:bg-slate-800 rounded-lg p-1">
          {WINDOW_OPTIONS.map((w) => (
            <button
              key={w.hours}
              onClick={() => setHours(w.hours)}
              className={`text-xs font-semibold px-3 py-1.5 rounded-md transition-colors ${
                hours === w.hours
                  ? "bg-white dark:bg-slate-700 text-navy dark:text-white shadow-sm"
                  : "text-slate-500 hover:text-navy dark:hover:text-white"
              }`}
            >
              {w.label}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <StatCard label="Active Alerts" value={overview.active_alert_count} accent={overview.active_alert_count > 0 ? "red" : "green"} />
        <StatCard label={`Drift (${hours}h)`} value={overview.drift_count} accent={overview.drift_count > 0 ? "amber" : "green"} />
        <StatCard label={`Notable Syslog (${hours}h)`} value={overview.notable_syslog_count} accent={overview.notable_syslog_count > 0 ? "amber" : "green"} />
        <StatCard label={`Config Changes (${hours}h)`} value={overview.deployment_count} accent="blue" />
      </div>

      <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm">
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100 dark:border-slate-800 flex-wrap gap-3">
          <h2 className="font-semibold text-navy dark:text-white text-sm">Timeline</h2>
          <div className="flex items-center gap-1.5 flex-wrap">
            {(["all", "alert_raised", "config_drift", "syslog", "deployment"] as const).map((k) => (
              <button
                key={k}
                onClick={() => setKindFilter(k)}
                className={`text-[11px] font-semibold px-2.5 py-1 rounded-full transition-colors ${
                  kindFilter === k
                    ? "bg-navy text-white dark:bg-white dark:text-navy"
                    : "bg-slate-100 dark:bg-slate-800 text-slate-500 hover:text-navy dark:hover:text-white"
                }`}
              >
                {k === "all" ? "All" : EVENT_STYLE[k].label}
              </button>
            ))}
          </div>
        </div>

        <div className="divide-y divide-slate-100 dark:divide-slate-800">
          {events.length === 0 && (
            <p className="px-5 py-8 text-center text-sm text-slate-400">
              No {kindFilter === "all" ? "" : `${EVENT_STYLE[kindFilter].label.toLowerCase()} `}events in the last {hours}h.
            </p>
          )}
          {events.map((e) => (
            <div key={`${e.kind}-${e.ref_id}-${e.occurred_at}`} className="flex items-start gap-3 px-5 py-3.5">
              <span className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${SEVERITY_DOT[e.severity]}`} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-sm">{EVENT_STYLE[e.kind].icon}</span>
                  <span className="text-[11px] font-bold uppercase tracking-wide text-slate-400">
                    {EVENT_STYLE[e.kind].label}
                  </span>
                  <span className="text-[11px] text-slate-400">{timeAgo(e.occurred_at)}</span>
                </div>
                <p className="text-sm font-medium text-navy dark:text-white mt-0.5">{e.title}</p>
                <p className="text-sm text-slate-500 dark:text-slate-400 mt-0.5 break-words">{e.detail}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}