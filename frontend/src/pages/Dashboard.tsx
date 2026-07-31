import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { DashboardSummary, DriftFleetSummary } from "../lib/types";
import StatCard from "../components/StatCard";
import { useAuth } from "../lib/auth";

export default function Dashboard() {
  const { user } = useAuth();
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [driftSummary, setDriftSummary] = useState<DriftFleetSummary | null>(null);
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
    fetchSummary();
    api
      .get<DriftFleetSummary>("/drift/summary")
      .then((res) => mounted && setDriftSummary(res.data))
      .catch(() => {
        /* drift widget is supplementary -- fail quietly */
      });

    // Live Deployment Dashboard (SRS 6.9): prefer WebSocket push, fall back to polling.
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
        } catch {
          /* ignore malformed frame */
        }
      };
      ws.onerror = () => {
        if (!mounted) return;
        setConnection("polling");
        if (!pollInterval) pollInterval = setInterval(fetchSummary, 5000);
      };
      ws.onclose = () => {
        if (!mounted) return;
        setConnection((c) => (c === "live" ? "polling" : c));
        if (!pollInterval) pollInterval = setInterval(fetchSummary, 5000);
      };
    } catch {
      setConnection("polling");
      pollInterval = setInterval(fetchSummary, 5000);
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

      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4 mt-6">
        <StatCard
          label="Devices Online"
          value={summary ? `${summary.devices_online}/${summary.devices_total}` : "–"}
          accent="blue"
        />
        <StatCard label="Active Deployments" value={summary?.active_deployments ?? "–"} accent="blue" />
        <StatCard label="Pending Approvals" value={summary?.pending_change_requests ?? "–"} accent="amber" />
        <StatCard label="Failed Deployments" value={summary?.failed_deployments ?? "–"} accent="red" />
        <StatCard label="Rollbacks" value={summary?.rollbacks ?? "–"} accent="red" />
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