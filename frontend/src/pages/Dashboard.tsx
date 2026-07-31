import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { DashboardSummary, DriftFleetSummary, Alert } from "../lib/types";
import StatCard from "../components/StatCard";
import { useAuth } from "../lib/auth";

const SEVERITY_ICON: Record<string, string> = { critical: "🚨", warning: "⚠️", info: "ℹ️" };
const SEVERITY_COLOR: Record<string, string> = { critical: "text-riskcrit", warning: "text-riskmed", info: "text-brandblue" };

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function Dashboard() {
  const { user } = useAuth();
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [driftSummary, setDriftSummary] = useState<DriftFleetSummary | null>(null);
  const [recentAlerts, setRecentAlerts] = useState<Alert[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState<"live" | "polling" | "connecting">("connecting");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let mounted = true;
    const fetchSummary = () => {
      api
        .get<DashboardSummary>("/dashboard/summary")
        .then((res) => {
          if (!mounted) return;
          setSummary(res.data);
          setError(null);
          setLastUpdated(new Date());
        })
        .catch(() => mounted && setError("Could not reach the NetGuard API."));
    };
    const fetchAlerts = () => {
      api
        .get<Alert[]>("/alerts?status=active&limit=5")
        .then((res) => mounted && setRecentAlerts(res.data))
        .catch(() => {});
    };
    fetchSummary();
    fetchAlerts();
    api
      .get<DriftFleetSummary>("/drift/summary")
      .then((res) => mounted && setDriftSummary(res.data))
      .catch(() => {});

    const base = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1";
    const wsUrl = base.replace(/^http/, "ws") + "/dashboard/ws";
    let ws: WebSocket | null = null;
    let pollInterval: ReturnType<typeof setInterval> | null = null;

    try {
      ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      ws.onopen = () => mounted && setConnection("live");
      ws.onmessage = (evt) => {
        if (!mounted) return;
        try {
          setSummary(JSON.parse(evt.data));
          setError(null);
          setLastUpdated(new Date());
          fetchAlerts();
        } catch {
          /* ignore malformed frame */
        }
      };
      ws.onerror = () => {
        if (!mounted) return;
        setConnection("polling");
        if (!pollInterval) pollInterval = setInterval(() => { fetchSummary(); fetchAlerts(); }, 5000);
      };
      ws.onclose = () => {
        if (!mounted) return;
        setConnection((c) => (c === "live" ? "polling" : c));
        if (!pollInterval) pollInterval = setInterval(() => { fetchSummary(); fetchAlerts(); }, 5000);
      };
    } catch {
      setConnection("polling");
      pollInterval = setInterval(() => { fetchSummary(); fetchAlerts(); }, 5000);
    }

    return () => {
      mounted = false;
      ws?.close();
      if (pollInterval) clearInterval(pollInterval);
    };
  }, []);

  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  return (
    <div>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy">Live Deployment Dashboard</h1>
          <p className="text-sm text-slate-500 mt-1">
            {greeting}
            {user ? `, ${user.full_name.split(" ")[0]}` : ""}. Real-time overview of devices, deployments, and rollbacks.
          </p>
        </div>
        <div className="text-right text-xs text-slate-400">
          <span
            className={`inline-flex items-center gap-1.5 font-medium mr-1 ${
              connection === "live" ? "text-risklow" : connection === "polling" ? "text-riskmed" : "text-slate-400"
            }`}
          >
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                connection === "live" ? "bg-risklow animate-pulse" : connection === "polling" ? "bg-riskmed" : "bg-slate-300"
              }`}
            />
            {connection === "live" ? "Live" : connection === "polling" ? "Polling" : "Connecting…"}
          </span>
          {lastUpdated && <span className="block">Updated {lastUpdated.toLocaleTimeString()}</span>}
        </div>
      </div>

      {error && (
        <div className="mt-4 bg-red-50 border border-riskcrit/30 text-riskcrit text-sm rounded-lg px-4 py-3">
          {error} Make sure the backend is running at{" "}
          <code>{import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1"}</code>.
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-4 mt-6">
        <StatCard
          label="Devices Online"
          value={summary ? `${summary.devices_online}/${summary.devices_total}` : "–"}
          accent="blue"
        />
        <StatCard label="Active Deployments" value={summary?.active_deployments ?? "–"} accent="blue" />
        <StatCard label="Pending Approvals" value={summary?.pending_change_requests ?? "–"} accent="amber" />
        <StatCard label="Failed Deployments" value={summary?.failed_deployments ?? "–"} accent="red" />
        <StatCard label="Rollbacks" value={summary?.rollbacks ?? "–"} accent="red" />
        <StatCard label="Critical Alerts" value={summary?.critical_alerts ?? "–"} accent="red" />
        <StatCard label="Active Warnings" value={summary?.warning_alerts ?? "–"} accent="amber" />
        <StatCard label="Platform Status" value={error ? "Degraded" : "Healthy"} accent={error ? "red" : "green"} />
      </div>

      {summary && summary.devices_total > 0 && (
        <div className="mt-6 bg-white border border-slate-200 rounded-xl p-6">
          <h2 className="font-semibold text-navy mb-3">Fleet Health</h2>
          <div className="w-full h-3 rounded-full bg-slate-100 overflow-hidden flex">
            <div
              className="bg-risklow h-full"
              style={{ width: `${(summary.devices_online / Math.max(summary.devices_total, 1)) * 100}%` }}
              title={`${summary.devices_online} online`}
            />
          </div>
          <p className="text-xs text-slate-500 mt-2">
            {summary.devices_online} of {summary.devices_total} managed devices reporting online.
          </p>
        </div>
      )}

      {/* Recent Alerts Widget */}
      <div className="mt-6 bg-white border border-slate-200 rounded-xl p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="font-semibold text-navy">Recent Alerts</h2>
          <Link
            to="/alerts"
            className="text-xs font-semibold text-brandblue hover:text-navy transition-colors"
          >
            View All →
          </Link>
        </div>
        {recentAlerts.length === 0 ? (
          <div className="text-center py-6">
            <span className="text-3xl">🛡️</span>
            <p className="text-sm text-slate-500 mt-2">No active alerts — network is healthy.</p>
          </div>
        ) : (
          <div className="space-y-2.5">
            {recentAlerts.map((alert) => (
              <div key={alert.id} className="flex items-start gap-3 group">
                <span className="text-base mt-0.5 shrink-0">{SEVERITY_ICON[alert.severity] || "ℹ️"}</span>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className={`text-sm font-medium ${SEVERITY_COLOR[alert.severity] || "text-slate-600"}`}>
                      {alert.category}
                    </span>
                    {alert.acknowledged && (
                      <span className="text-[10px] font-medium text-brandblue bg-blue-50 px-1.5 py-0.5 rounded">ACK</span>
                    )}
                  </div>
                  <p className="text-xs text-slate-500 truncate">{alert.message}</p>
                </div>
                <span className="text-[11px] text-slate-400 shrink-0 mt-0.5">{timeAgo(alert.created_at)}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {driftSummary && driftSummary.total_open_drifts > 0 && (
        <div className="mt-6 bg-white border border-slate-200 rounded-xl p-6">
          <div className="flex items-start justify-between gap-4 flex-wrap">
            <div>
              <h2 className="font-semibold text-navy mb-1">Configuration Drift Posture</h2>
              <p className="text-sm text-slate-500">
                {driftSummary.total_open_drifts} open drift record(s) across {driftSummary.devices_drifted} device(s)
                · average compliance {driftSummary.average_compliance_score}/100.
                {driftSummary.rollback_recommended_count > 0 &&
                  ` ${driftSummary.rollback_recommended_count} recommend rollback.`}
              </p>
            </div>
            <Link
              to="/drift"
              className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors shrink-0"
            >
              Review Drift
            </Link>
          </div>
        </div>
      )}

      <div className="mt-6 bg-white border border-slate-200 rounded-xl p-6">
        <h2 className="font-semibold text-navy mb-2">Getting Started</h2>
        <ol className="list-decimal list-inside text-sm text-slate-600 space-y-1">
          <li>
            Add a device under <span className="font-medium">Devices</span>.
          </li>
          <li>Submit a change request with a proposed configuration.</li>
          <li>Review the AI risk score and configuration diff, then approve.</li>
          <li>Track deployment health and rollbacks from this dashboard.</li>
        </ol>
      </div>
    </div>
  );
}