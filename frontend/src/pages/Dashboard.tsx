import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { DashboardSummary } from "../lib/types";
import StatCard from "../components/StatCard";

export default function Dashboard() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    const fetchSummary = () => {
      api
        .get<DashboardSummary>("/dashboard/summary")
        .then((res) => mounted && setSummary(res.data))
        .catch(() => mounted && setError("Could not reach the NetGuard AI API."));
    };
    fetchSummary();

    // Live Deployment Dashboard (SRS 6.9): prefer WebSocket push, fall back to polling.
    const base = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1";
    const wsUrl = base.replace(/^http/, "ws") + "/dashboard/ws";
    let ws: WebSocket | null = null;
    let pollInterval: ReturnType<typeof setInterval> | null = null;

    try {
      ws = new WebSocket(wsUrl);
      ws.onmessage = (evt) => {
        if (!mounted) return;
        try {
          setSummary(JSON.parse(evt.data));
          setError(null);
        } catch {
          /* ignore malformed frame */
        }
      };
      ws.onerror = () => {
        if (!pollInterval) pollInterval = setInterval(fetchSummary, 5000);
      };
    } catch {
      pollInterval = setInterval(fetchSummary, 5000);
    }

    return () => {
      mounted = false;
      ws?.close();
      if (pollInterval) clearInterval(pollInterval);
    };
  }, []);

  return (
    <div>
      <h1 className="text-2xl font-bold text-navy">Live Deployment Dashboard</h1>
      <p className="text-sm text-slate-500 mt-1">
        Real-time overview of devices, deployments, and rollbacks.
      </p>

      {error && (
        <div className="mt-4 bg-red-50 border border-riskcrit/30 text-riskcrit text-sm rounded-lg px-4 py-3">
          {error} Make sure the backend is running at{" "}
          <code>{import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1"}</code>.
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4 mt-6">
        <StatCard label="Devices Online" value={summary ? `${summary.devices_online}/${summary.devices_total}` : "–"} accent="blue" />
        <StatCard label="Active Deployments" value={summary?.active_deployments ?? "–"} accent="blue" />
        <StatCard label="Pending Approvals" value={summary?.pending_change_requests ?? "–"} accent="amber" />
        <StatCard label="Failed Deployments" value={summary?.failed_deployments ?? "–"} accent="red" />
        <StatCard label="Rollbacks" value={summary?.rollbacks ?? "–"} accent="red" />
        <StatCard label="Platform Status" value="Healthy" accent="green" />
      </div>

      <div className="mt-8 bg-white border border-slate-200 rounded-xl p-6">
        <h2 className="font-semibold text-navy mb-2">Getting Started</h2>
        <ol className="list-decimal list-inside text-sm text-slate-600 space-y-1">
          <li>Add a device under <span className="font-medium">Devices</span>.</li>
          <li>Submit a change request with a proposed configuration.</li>
          <li>Review the AI risk score and configuration diff, then approve.</li>
          <li>Track deployment health and rollbacks from this dashboard.</li>
        </ol>
      </div>
    </div>
  );
}
