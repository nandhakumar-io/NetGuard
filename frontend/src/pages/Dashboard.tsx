import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../lib/api";
import { DashboardSummary, DriftFleetSummary, Alert } from "../lib/types";
import StatCard from "../components/StatCard";
import Sparkline from "../components/Sparkline";
import { useAuth } from "../lib/auth";

function formatBps(bps: number | null): string {
  if (bps === null || Number.isNaN(bps)) return "—";
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(2)} Gbps`;
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(2)} Mbps`;
  if (bps >= 1e3) return `${(bps / 1e3).toFixed(1)} Kbps`;
  return `${bps.toFixed(0)} bps`;
}

const SEVERITY_ICON: Record<string, string> = { critical: "🚨", warning: "⚠️", info: "ℹ️" };
const SEVERITY_COLOR: Record<string, string> = { critical: "text-riskcrit", warning: "text-riskmed", info: "text-brandblue" };

// --- Role-based dashboard views -----------------------------------------
// Three fixed presets rather than per-widget user customization: NOC
// (live operational status -- uplinks, port/device down events, top
// resource consumers), Change Management (deployment/backup/config-drift
// audit trail), and Exec/Compliance (fleet-wide posture: health trend,
// drift, EOL/EOS exposure, unstable-device flags -- no raw ops noise).
// Every widget below already existed; this only changes which ones are
// mounted for a given view, via the `views` set passed to each section.
type DashboardView = "noc" | "change_management" | "exec";

const VIEW_LABELS: Record<DashboardView, string> = {
  noc: "NOC",
  change_management: "Change Management",
  exec: "Exec / Compliance",
};

// A view a given role lands on by default -- still freely switchable via
// the segmented control, this just saves the common case a click.
const ROLE_DEFAULT_VIEW: Record<string, DashboardView> = {
  noc_engineer: "noc",
  network_engineer: "noc",
  network_admin: "change_management",
  security: "exec",
  auditor: "exec",
};

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
  const [activeAlerts, setActiveAlerts] = useState<Alert[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState<"live" | "polling" | "connecting">("connecting");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const [view, setView] = useState<DashboardView>(() => ROLE_DEFAULT_VIEW[user?.role || ""] || "noc");
  const showIn = (...views: DashboardView[]) => views.includes(view);

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
      // Wider pull, used only to compute the NOC live-status widget below
      // (down ports / offline devices / recent restarts) -- kept separate
      // from the 5-item "Active Alerts" list above so that widget isn't
      // capped at whatever happens to be in the top 5 most recent alerts.
      api
        .get<Alert[]>("/alerts?status=active&limit=200")
        .then((res) => mounted && setActiveAlerts(res.data))
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

  // NOC live-status widget: derived from the active-alerts pull above,
  // bucketed by the categories metrics_service/reachability_service raise
  // for port/device down-and-back events. Doesn't need its own endpoint --
  // these are just ordinary Alert rows with recognizable category
  // prefixes/names, same vocabulary the Alert Center already renders.
  const downPortAlerts = activeAlerts.filter((a) => a.category.startsWith("Interface Down"));
  const unreachableAlerts = activeAlerts.filter((a) => a.category === "Device Unreachable");
  const restartAlerts = activeAlerts.filter((a) => a.category === "Device Restart");

  // Helpers for Health Score color
  const getScoreColor = (score: number) => {
    if (score >= 90) return "text-green-500";
    if (score >= 70) return "text-amber-500";
    return "text-red-500";
  };
  
  // Helpers for CPU / Memory progress bar
  const getUtilColor = (val: number) => {
      if (val >= 90) return "bg-red-500";
      if (val >= 70) return "bg-amber-500";
      return "bg-brandblue";
  };

  return (
    <div className="pb-16 max-w-7xl mx-auto flex flex-col gap-6 pt-2">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-bold text-navy dark:text-white">Network Dashboard</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1 font-medium">
            {greeting}
            {user ? `, ${user.full_name.split(" ")[0]}` : ""}. Real-time network telemetry and deployment tracking.
          </p>
        </div>
        <div className="text-right text-xs font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 px-4 py-2 rounded-full shadow-sm">
          <span
            className={`inline-flex items-center gap-2 font-black mr-2 ${
              connection === "live" ? "text-risklow" : connection === "polling" ? "text-riskmed" : "text-slate-400 dark:text-slate-500"
            }`}
          >
            <span
              className={`w-2 h-2 rounded-full ${
                connection === "live" ? "bg-risklow animate-pulse shadow-[0_0_8px_rgba(34,197,94,0.6)]" : connection === "polling" ? "bg-riskmed" : "bg-slate-300 dark:bg-slate-600"
              }`}
            />
            {connection === "live" ? "LIVE" : connection === "polling" ? "POLLING" : "CONNECTING…"}
          </span>
          | {lastUpdated ? <span className="ml-2 font-medium">Updated {lastUpdated.toLocaleTimeString()}</span> : "—"}
        </div>
      </div>

      {/* Role-based dashboard view switcher -- defaults from the logged-in
          user's role (ROLE_DEFAULT_VIEW) but any view is one click away,
          since plenty of people cover more than one hat. */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs font-bold text-slate-400 dark:text-slate-500 uppercase tracking-wider">View:</span>
        <div className="inline-flex rounded-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 shadow-sm overflow-hidden">
          {(Object.keys(VIEW_LABELS) as DashboardView[]).map((v) => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={`px-4 py-1.5 text-xs font-bold transition-colors ${
                view === v ? "bg-brandblue text-white" : "text-slate-500 dark:text-slate-400 hover:bg-slate-50 dark:hover:bg-slate-700"
              }`}
            >
              {VIEW_LABELS[v]}
            </button>
          ))}
        </div>
      </div>

      {error && !summary && (
        <div className="bg-red-50 border border-red-200 text-riskcrit text-sm font-semibold rounded-lg px-4 py-3 shadow-sm">
          {error} Make sure the backend is running at{" "}
          <code className="bg-white dark:bg-slate-800 px-2 py-0.5 rounded border border-red-100 ml-1">{import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1"}</code>.
        </div>
      )}
      {error && summary && (
        <div className="bg-amber-50 border border-amber-200 text-riskmed text-xs font-medium rounded-lg px-4 py-2 shadow-sm">
          Showing the last data we could load — the most recent refresh failed. Retrying automatically…
        </div>
      )}

      {/* Top Value / High-Level KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Overall Health Score Card */}
        <div className="bg-white dark:bg-slate-800 border flex flex-col border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm transform transition hover:-translate-y-1 hover:shadow-md">
            <h3 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-2">Global Health Score</h3>
            <div className="mt-auto flex items-end gap-2">
                <span className={`text-5xl font-black ${getScoreColor(summary?.global_health_score ?? 100)} tracking-tighter`}>
                    {summary?.global_health_score ?? 100}
                </span>
                <span className="text-slate-400 dark:text-slate-500 font-bold mb-1">/100</span>
            </div>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-2 font-medium">Aggregated fleet wellness index.</p>
        </div>

        {/* Deployment Success Rate */}
        <div className="bg-white dark:bg-slate-800 border flex flex-col border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm transform transition hover:-translate-y-1 hover:shadow-md">
            <h3 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-2">Deployment Success</h3>
            <div className="mt-auto flex items-end gap-2">
                <span className={`text-5xl font-black ${summary?.deployment_success_rate && summary.deployment_success_rate < 95 ? "text-amber-500" : "text-risklow"} tracking-tighter`}>
                    {summary?.deployment_success_rate ?? 100}
                </span>
                <span className="text-slate-400 dark:text-slate-500 font-bold mb-1">%</span>
            </div>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-2 font-medium">Automated configuration accuracy.</p>
        </div>

        {/* Network Health Pipeline (Online vs Offline) */}
        <div className="bg-white dark:bg-slate-800 border flex flex-col border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm lg:col-span-2">
           <div className="flex justify-between items-center mb-4">
               <h3 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider">Network Connectivity</h3>
               <span className="text-xs font-bold px-2 py-1 bg-slate-100 dark:bg-slate-700 rounded text-navy dark:text-white">{summary ? `${summary.devices_online}/${summary.devices_total} Online` : "—"}</span>
           </div>
           
           <div className="flex-1 flex flex-col justify-center">
             <div className="w-full h-4 rounded-full bg-slate-100 dark:bg-slate-700 overflow-hidden flex shadow-inner">
                 <div
                 className="bg-risklow h-full transition-all duration-1000"
                 style={{ width: `${((summary?.devices_online ?? 0) / Math.max(summary?.devices_total ?? 1, 1)) * 100}%` }}
                 />
             </div>
             <p className="text-[13px] font-medium text-slate-500 dark:text-slate-400 mt-3 flex justify-between">
                 <span><span className="w-2 h-2 rounded-full inline-block bg-risklow mr-1.5 align-middle"></span>Online</span>
                 <span><span className="w-2 h-2 rounded-full inline-block bg-slate-300 dark:bg-slate-600 mr-1.5 align-middle"></span>Offline</span>
             </p>
           </div>
        </div>
      </div>

      {/* Fleet Health Trend -- 24h fleet-wide avg CPU / memory / bandwidth,
          the graph the dashboard was missing: everything above is a
          point-in-time snapshot, this is the actual trend over time. */}
      {showIn("noc", "exec") && (
      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm">
        <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide mb-3">
          Fleet Health Trend (24h)
        </p>
        {(summary?.fleet_health_history?.length ?? 0) === 0 ? (
          <p className="text-sm text-slate-400 py-10 text-center">Not enough polling history yet.</p>
        ) : (
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={summary?.fleet_health_history}>
                <defs>
                  <linearGradient id="cpuFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#2563eb" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="#2563eb" stopOpacity={0.02} />
                  </linearGradient>
                  <linearGradient id="memFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#7c3aed" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="#7c3aed" stopOpacity={0.02} />
                  </linearGradient>
                  <linearGradient id="bwFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#d97706" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="#d97706" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis
                  dataKey="timestamp"
                  tickFormatter={(v) => (v ? new Date(v).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "")}
                  tick={{ fontSize: 11 }}
                  minTickGap={40}
                />
                <YAxis tickFormatter={(v) => `${v}%`} tick={{ fontSize: 11 }} width={40} domain={[0, 100]} />
                <Tooltip
                  formatter={(v: number, name: string) => [`${v?.toFixed?.(1) ?? v}%`, name]}
                  labelFormatter={(v) => (v ? new Date(v).toLocaleString() : "")}
                />
                <Legend wrapperStyle={{ fontSize: 12, fontWeight: 600 }} />
                <Area type="monotone" dataKey="avg_cpu" name="CPU" stroke="#2563eb" fill="url(#cpuFill)" strokeWidth={2} connectNulls />
                <Area type="monotone" dataKey="avg_memory" name="Memory" stroke="#7c3aed" fill="url(#memFill)" strokeWidth={2} connectNulls />
                <Area type="monotone" dataKey="avg_bandwidth" name="Bandwidth" stroke="#d97706" fill="url(#bwFill)" strokeWidth={2} connectNulls />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Main Column */}
          <div className="space-y-6 lg:col-span-2">

             {/* Uplinks / WAN Links */}
             {showIn("noc") && (
             <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm">
                 <h3 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-4 flex items-center gap-2">
                     <span className="bg-slate-100 dark:bg-slate-700 p-1.5 rounded-lg text-lg">🌐</span> Uplinks &amp; WAN Links
                 </h3>
                 {(summary?.uplinks?.length ?? 0) === 0 ? (
                     <p className="text-xs text-slate-400 dark:text-slate-500 italic">
                         No devices tagged as WAN/uplink/core/edge yet. Set a device's Role (e.g. "wan-edge", "core",
                         "uplink") to surface it here.
                     </p>
                 ) : (
                     <div className="space-y-4">
                         {summary?.uplinks?.map((link, i) => (
                             <div key={i} className="flex flex-col gap-1.5">
                                 <div className="flex justify-between items-center gap-2 text-[13px]">
                                     <span className="flex items-center gap-2 font-bold text-navy dark:text-white truncate">
                                         <span
                                             className={`w-2 h-2 rounded-full shrink-0 ${
                                                 link.status === "online"
                                                     ? "bg-risklow"
                                                     : link.status === "offline"
                                                     ? "bg-riskcrit"
                                                     : "bg-slate-300 dark:bg-slate-600"
                                             }`}
                                         />
                                         {link.hostname}
                                         <span className="text-slate-400 dark:text-slate-500 font-medium">({link.ip_address})</span>
                                         {link.role && (
                                             <span className="text-[10px] uppercase font-bold text-brandblue bg-blue-50 dark:bg-slate-700 px-1.5 py-0.5 rounded">
                                                 {link.role}
                                             </span>
                                         )}
                                     </span>
                                     <span className="flex items-center gap-2 shrink-0 font-bold text-navy dark:text-white">
                                         <Sparkline
                                             values={link.history}
                                             color={link.utilization_pct >= 85 ? "#dc2626" : link.utilization_pct >= 65 ? "#d97706" : "#2563eb"}
                                         />
                                         {formatBps(link.throughput_bps)}
                                         <span className="text-slate-400 dark:text-slate-500 font-medium">
                                             ({link.utilization_pct.toFixed(1)}%)
                                         </span>
                                     </span>
                                 </div>
                                 <div className="w-full h-2 rounded-full bg-slate-100 dark:bg-slate-700 overflow-hidden">
                                     <div className={`h-full ${getUtilColor(link.utilization_pct)}`} style={{ width: `${Math.min(link.utilization_pct, 100)}%` }} />
                                 </div>
                                 {(link.errors ?? 0) > 0 && (
                                     <p className="text-[11px] font-semibold text-riskmed">{link.errors} interface error(s) since last poll</p>
                                 )}
                             </div>
                         ))}
                     </div>
                 )}
             </div>
             )}

             {/* Top-N Metric Widgets: CPU, Memory, Bandwidth */}
             {showIn("noc") && (
             <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                 {/* Top CPU Widget */}
                 <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm">
                     <h3 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-4 flex items-center gap-2">
                         <span className="bg-slate-100 dark:bg-slate-700 p-1.5 rounded-lg text-lg">⚡</span> Top CPU Utilization
                     </h3>
                     {summary?.top_cpu_devices?.length === 0 ? (
                         <p className="text-xs text-slate-400 dark:text-slate-500 italic">No telemetry data available.</p>
                     ) : (
                         <div className="space-y-4">
                             {summary?.top_cpu_devices?.map((dev, i) => (
                                 <div key={i}>
                                     <div className="flex justify-between items-center gap-2 text-[13px] font-bold text-navy dark:text-white mb-1.5">
                                         <span className="truncate">{dev.hostname} <span className="text-slate-400 dark:text-slate-500 font-medium ml-1">({dev.ip_address})</span></span>
                                         <span className="flex items-center gap-2 shrink-0">
                                             <Sparkline values={dev.cpu_history} color={dev.cpu >= 85 ? "#dc2626" : dev.cpu >= 65 ? "#d97706" : "#2563eb"} />
                                             {dev.cpu.toFixed(1)}%
                                         </span>
                                     </div>
                                     <div className="w-full h-2 rounded-full bg-slate-100 dark:bg-slate-700 overflow-hidden">
                                         <div className={`h-full ${getUtilColor(dev.cpu)}`} style={{ width: `${Math.min(dev.cpu, 100)}%` }} />
                                     </div>
                                 </div>
                             ))}
                         </div>
                     )}
                 </div>

                 {/* Top Memory Widget */}
                 <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm">
                     <h3 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-4 flex items-center gap-2">
                         <span className="bg-slate-100 dark:bg-slate-700 p-1.5 rounded-lg text-lg">🧠</span> Top Memory Utilization
                     </h3>
                     {summary?.top_memory_devices?.length === 0 ? (
                         <p className="text-xs text-slate-400 dark:text-slate-500 italic">No telemetry data available.</p>
                     ) : (
                         <div className="space-y-4">
                             {summary?.top_memory_devices?.map((dev, i) => (
                                 <div key={i}>
                                     <div className="flex justify-between items-center gap-2 text-[13px] font-bold text-navy dark:text-white mb-1.5">
                                         <span className="truncate">{dev.hostname} <span className="text-slate-400 dark:text-slate-500 font-medium ml-1">({dev.ip_address})</span></span>
                                         <span className="flex items-center gap-2 shrink-0">
                                             <Sparkline values={dev.memory_history} color={dev.memory >= 85 ? "#dc2626" : dev.memory >= 65 ? "#d97706" : "#2563eb"} />
                                             {dev.memory.toFixed(1)}%
                                         </span>
                                     </div>
                                     <div className="w-full h-2 rounded-full bg-slate-100 dark:bg-slate-700 overflow-hidden">
                                         <div className={`h-full ${getUtilColor(dev.memory)}`} style={{ width: `${Math.min(dev.memory, 100)}%` }} />
                                     </div>
                                 </div>
                             ))}
                         </div>
                     )}
                 </div>

                 {/* Top Bandwidth Widget -- fleet-wide highest interface_utilization_pct,
                     same shape/data source as Top CPU/Memory (see dashboard.py top_bandwidth_devices) */}
                 <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm">
                     <h3 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-4 flex items-center gap-2">
                         <span className="bg-slate-100 dark:bg-slate-700 p-1.5 rounded-lg text-lg">📶</span> Top Bandwidth Utilization
                     </h3>
                     {summary?.top_bandwidth_devices?.length === 0 ? (
                         <p className="text-xs text-slate-400 dark:text-slate-500 italic">No telemetry data available.</p>
                     ) : (
                         <div className="space-y-4">
                             {summary?.top_bandwidth_devices?.map((dev, i) => (
                                 <div key={i}>
                                     <div className="flex justify-between items-center gap-2 text-[13px] font-bold text-navy dark:text-white mb-1.5">
                                         <span className="truncate">{dev.hostname} <span className="text-slate-400 dark:text-slate-500 font-medium ml-1">({dev.ip_address})</span></span>
                                         <span className="flex items-center gap-2 shrink-0">
                                             <Sparkline values={dev.bandwidth_history} color={dev.bandwidth >= 85 ? "#dc2626" : dev.bandwidth >= 65 ? "#d97706" : "#2563eb"} />
                                             {dev.bandwidth.toFixed(1)}%
                                         </span>
                                     </div>
                                     <div className="w-full h-2 rounded-full bg-slate-100 dark:bg-slate-700 overflow-hidden">
                                         <div className={`h-full ${getUtilColor(dev.bandwidth)}`} style={{ width: `${Math.min(dev.bandwidth, 100)}%` }} />
                                     </div>
                                 </div>
                             ))}
                         </div>
                     )}
                 </div>
             </div>
             )}

             {/* Protocol Operations Feed */}
             {showIn("change_management") && (
             <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm">
                 <h3 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-4 border-b border-slate-100 dark:border-slate-800 pb-3 flex justify-between">
                     <span>Protocol Operations (Recent)</span>
                     <span className="text-slate-400 dark:text-slate-500 font-medium">Audit Trail</span>
                 </h3>
                 <div className="w-full overflow-x-auto">
                     <table className="w-full text-left text-sm">
                         <thead>
                             <tr className="text-xs font-bold text-slate-400 dark:text-slate-500 uppercase tracking-wide border-b border-slate-100 dark:border-slate-800">
                                 <th className="pb-2">Protocol</th>
                                 <th className="pb-2">Operation</th>
                                 <th className="pb-2">Target</th>
                                 <th className="pb-2">Status</th>
                                 <th className="pb-2 text-right">Time</th>
                             </tr>
                         </thead>
                         <tbody className="divide-y divide-slate-50">
                             {summary?.recent_protocol_operations?.length === 0 && (
                                 <tr>
                                     <td colSpan={5} className="py-4 text-center text-xs text-slate-400 dark:text-slate-500 italic">No protocol ops recorded yet.</td>
                                 </tr>
                             )}
                             {summary?.recent_protocol_operations?.map((op) => (
                                 <tr key={op.id} className="group hover:bg-slate-50 dark:bg-slate-900/50 transition-colors">
                                     <td className="py-2.5 font-bold text-navy dark:text-white uppercase text-[11px] tracking-wider">{op.protocol}</td>
                                     <td className="py-2.5 font-mono text-xs text-slate-600 dark:text-slate-300">{op.operation}</td>
                                     <td className="py-2.5 font-semibold text-brandblue text-xs">{op.device_hostname}</td>
                                     <td className="py-2.5">
                                         {op.success ? (
                                             <span className="text-[10px] uppercase font-bold text-risklow bg-green-50 text-green-700 px-2 py-0.5 rounded shadow-sm">Success</span>
                                         ) : (
                                             <span className="text-[10px] uppercase font-bold text-riskcrit bg-red-50 text-red-700 px-2 py-0.5 rounded shadow-sm">Failed</span>
                                         )}
                                     </td>
                                     <td className="py-2.5 text-right font-medium text-slate-400 dark:text-slate-500 text-xs">{timeAgo(op.created_at)}</td>
                                 </tr>
                             ))}
                         </tbody>
                     </table>
                 </div>
             </div>
             )}
             
             {/* Devices with Drift Widget */}
             {showIn("change_management", "exec") && driftSummary && driftSummary.total_open_drifts > 0 && (
                 <div className="bg-white dark:bg-slate-800 border-2 border-brandblue/20 rounded-xl p-5 shadow-sm transform transition hover:-translate-y-1">
                 <div className="flex items-start justify-between gap-4 flex-wrap">
                     <div>
                     <h2 className="text-xs font-bold text-brandblue uppercase tracking-wider mb-2 flex items-center gap-2">
                         <span className="text-lg">⚖️</span> Configuration Drift Detected
                     </h2>
                     <p className="text-sm font-medium text-slate-600 dark:text-slate-300 leading-relaxed">
                         <strong className="text-navy dark:text-white">{driftSummary.total_open_drifts} open drift record(s)</strong> across <strong className="text-navy dark:text-white">{driftSummary.devices_drifted} device(s)</strong>
                         . Fleet average compliance is <span className="font-mono bg-slate-100 dark:bg-slate-700 px-1 py-0.5 rounded">{driftSummary.average_compliance_score}/100</span>.
                         {driftSummary.rollback_recommended_count > 0 &&
                         <span className="text-riskcrit font-bold ml-1"> {driftSummary.rollback_recommended_count} recommend rollback.</span>}
                     </p>
                     </div>
                     <Link
                     to="/drift"
                     className="bg-brandblue text-white rounded-full px-5 py-2 text-xs font-bold tracking-widest shadow-md hover:bg-navy dark:bg-slate-950 transition-colors shrink-0 uppercase"
                     >
                     Review Drift
                     </Link>
                 </div>
                 </div>
             )}

             {/* EOL/EOS Firmware Widget */}
             {showIn("change_management", "exec") && summary && summary.eos_device_count > 0 && (
                 <div className="bg-white dark:bg-slate-800 border-2 border-riskcrit/20 rounded-xl p-5 shadow-sm transform transition hover:-translate-y-1">
                 <div className="flex items-start justify-between gap-4 flex-wrap">
                     <div>
                     <h2 className="text-xs font-bold text-riskcrit uppercase tracking-wider mb-2 flex items-center gap-2">
                         <span className="text-lg">🕰️</span> End-of-Support Firmware Detected
                     </h2>
                     <p className="text-sm font-medium text-slate-600 dark:text-slate-300 leading-relaxed">
                         <strong className="text-navy dark:text-white">{summary.eos_device_count} device(s)</strong> are running
                         hardware/software past its vendor End-of-Support date -- no more fixes or support contracts available.
                     </p>
                     </div>
                     <Link
                     to="/devices"
                     className="bg-riskcrit text-white rounded-full px-5 py-2 text-xs font-bold tracking-widest shadow-md hover:bg-red-700 transition-colors shrink-0 uppercase"
                     >
                     Review Devices
                     </Link>
                 </div>
                 </div>
             )}

          </div>

          {/* Sidebar Area */}
          <div className="space-y-6">
             {/* NOC Live Status Widget -- the 3 signals a NOC operator
                  scans for first: devices down, ports down, recent
                  reboots. Counts from alerts, detail lists from the
                  dashboard summary's interface_statuses query. */}
             {showIn("noc") && (
                <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm">
                  <div className="flex items-center justify-between mb-4 border-b border-slate-100 dark:border-slate-800 pb-3">
                    <h2 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider flex items-center gap-2">
                      <span className={`w-2 h-2 rounded-full ${(unreachableAlerts.length > 0 || downPortAlerts.length > 0 || restartAlerts.length > 0) ? "bg-riskcrit animate-pulse" : "bg-risklow"}`} />
                      NOC Live Status
                    </h2>
                    <Link to="/alerts" className="text-xs font-bold uppercase tracking-wider text-brandblue hover:text-navy dark:text-white transition-colors">
                      View All
                    </Link>
                  </div>
                  <div className="grid grid-cols-3 gap-2 mb-3">
                    <div className="text-center bg-red-50 dark:bg-red-950/30 border border-red-100 dark:border-red-900 rounded-lg py-2">
                      <p className="text-lg font-black text-riskcrit leading-none">{unreachableAlerts.length}</p>
                      <p className="text-[10px] font-bold text-slate-500 dark:text-slate-400 uppercase mt-1">Devices Down</p>
                    </div>
                    <div className="text-center bg-amber-50 dark:bg-amber-950/30 border border-amber-100 dark:border-amber-900 rounded-lg py-2">
                      <p className="text-lg font-black text-riskmed leading-none">{summary?.down_ports?.length ?? downPortAlerts.length}</p>
                      <p className="text-[10px] font-bold text-slate-500 dark:text-slate-400 uppercase mt-1">Ports Down</p>
                    </div>
                    <div className="text-center bg-blue-50 dark:bg-blue-950/30 border border-blue-100 dark:border-blue-900 rounded-lg py-2">
                      <p className="text-lg font-black text-brandblue leading-none">{summary?.recent_reboots?.length ?? restartAlerts.length}</p>
                      <p className="text-[10px] font-bold text-slate-500 dark:text-slate-400 uppercase mt-1">Recent Reboots</p>
                    </div>
                  </div>
                  {(unreachableAlerts.length > 0 || (summary?.down_ports?.length ?? 0) > 0 || (summary?.recent_reboots?.length ?? 0) > 0) ? (
                    <ul className="space-y-1.5 max-h-48 overflow-y-auto pr-1">
                      {unreachableAlerts.slice(0, 4).map((a) => (
                        <li key={a.id} className="flex items-center justify-between gap-2 text-[11px] bg-red-50 dark:bg-red-950/20 border border-red-100 dark:border-red-900 rounded-md px-2 py-1.5">
                          <span className="font-semibold text-riskcrit truncate">🔴 {a.category}</span>
                          <span className="text-slate-400 shrink-0">{timeAgo(a.created_at)}</span>
                        </li>
                      ))}
                      {(summary?.down_ports || []).slice(0, 6).map((p, i) => (
                        <li key={`dp-${i}`} className="flex items-center justify-between gap-2 text-[11px] bg-amber-50 dark:bg-amber-950/20 border border-amber-100 dark:border-amber-900 rounded-md px-2 py-1.5">
                          <span className="font-semibold text-riskmed truncate">⚠️ {p.hostname} — {p.interface}</span>
                          <span className="text-slate-400 shrink-0">{p.down_since ? timeAgo(p.down_since) : "—"}</span>
                        </li>
                      ))}
                      {(summary?.recent_reboots || []).slice(0, 4).map((r, i) => (
                        <li key={`rb-${i}`} className="flex items-center justify-between gap-2 text-[11px] bg-blue-50 dark:bg-blue-950/20 border border-blue-100 dark:border-blue-900 rounded-md px-2 py-1.5">
                          <span className="font-semibold text-brandblue truncate">🔄 {r.hostname}</span>
                          <span className="text-slate-400 shrink-0">up {Math.floor(r.uptime_seconds / 60)}m</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <div className="text-center py-4">
                      <span className="text-3xl mb-2 inline-block filter drop-shadow-sm">✅</span>
                      <p className="text-sm font-bold text-risklow">All Clear</p>
                      <p className="text-[11px] text-slate-400 dark:text-slate-500 mt-0.5">No down ports, unreachable devices, or recent reboots.</p>
                    </div>
                  )}
                </div>
             )}

             {/* Recent Alerts Widget */}
             {showIn("noc", "exec") && (
             <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm">
                 <div className="flex items-center justify-between mb-4 border-b border-slate-100 dark:border-slate-800 pb-3">
                 <h2 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider">Active Alerts</h2>
                 <Link
                     to="/alerts"
                     className="text-xs font-bold uppercase tracking-wider text-brandblue hover:text-navy dark:text-white transition-colors"
                 >
                     View All
                 </Link>
                 </div>
                 {recentAlerts.length === 0 ? (
                 <div className="text-center py-8">
                     <span className="text-4xl filter drop-shadow-sm mb-3 inline-block">🛡️</span>
                     <p className="text-sm font-bold text-slate-500 dark:text-slate-400">No active alerts.</p>
                     <p className="text-xs font-medium text-slate-400 dark:text-slate-500 mt-1">Network is running perfectly.</p>
                 </div>
                 ) : (
                 <div className="space-y-3">
                     {recentAlerts.map((alert) => (
                     <div key={alert.id} className="flex gap-3 bg-slate-50 dark:bg-slate-900 rounded-lg p-3 border border-slate-100 dark:border-slate-800 hover:border-slate-200 dark:border-slate-700 transition">
                         <span className="text-lg shrink-0 pt-0.5">{SEVERITY_ICON[alert.severity] || "ℹ️"}</span>
                         <div className="flex-1 min-w-0">
                         <div className="flex items-center gap-2 mb-1">
                             <span className={`text-xs uppercase font-bold tracking-wider ${SEVERITY_COLOR[alert.severity] || "text-slate-600 dark:text-slate-300"}`}>
                             {alert.category}
                             </span>
                             {alert.acknowledged && (
                             <span className="text-[9px] font-black uppercase text-brandblue bg-blue-100 px-1 py-0.5 rounded">ACK</span>
                             )}
                         </div>
                         <p className="text-[13px] font-medium text-navy dark:text-white truncate">{alert.message}</p>
                         <p className="text-[11px] font-semibold text-slate-400 dark:text-slate-500 mt-1">{timeAgo(alert.created_at)}</p>
                         </div>
                     </div>
                     ))}
                 </div>
                 )}
             </div>
             )}

             {/* Recent Backups Widget */}
             {showIn("change_management") && (
             <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm">
                  <div className="flex items-center justify-between mb-4 border-b border-slate-100 dark:border-slate-800 pb-3">
                     <h2 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider flex items-center gap-2">
                         Recent Backups
                     </h2>
                 </div> 
                 {summary?.recent_backups?.length === 0 ? (
                     <p className="text-xs text-slate-400 dark:text-slate-500 italic text-center py-4">No backups on file.</p>
                 ) : (
                     <div className="space-y-3">
                         {summary?.recent_backups?.map((backup) => (
                             <div key={backup.id} className="flex items-center justify-between group">
                                 <div className="flex items-center gap-3">
                                     <div className="bg-slate-100 dark:bg-slate-700 text-slate-500 dark:text-slate-400 text-xs font-mono font-bold px-2 py-1 rounded">
                                         v{backup.version}
                                     </div>
                                     <span className="text-[13px] font-bold text-navy dark:text-white">{backup.hostname}</span>
                                 </div>
                                 <span className="text-[11px] font-semibold text-slate-400 dark:text-slate-500">{timeAgo(backup.created_at)}</span>
                             </div>
                         ))}
                     </div>
                 )}
             </div>
             )}
             
             {/* Small Status Summary -- shown in every view as the compact
                 shared anchor across all three dashboards. */}
             <div className="grid grid-cols-3 gap-3">
                 <div className="bg-slate-50 dark:bg-slate-900 p-4 rounded-xl border border-slate-100 dark:border-slate-800 text-center">
                     <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide">Pending</p>
                     <p className="text-2xl font-black text-amber-500 mt-1">{summary?.pending_change_requests ?? 0}</p>
                 </div>
                 <div className="bg-slate-50 dark:bg-slate-900 p-4 rounded-xl border border-slate-100 dark:border-slate-800 text-center">
                     <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide">Rollbacks</p>
                     <p className="text-2xl font-black text-red-500 mt-1">{summary?.rollbacks ?? 0}</p>
                 </div>
                 <div className="bg-slate-50 dark:bg-slate-900 p-4 rounded-xl border border-slate-100 dark:border-slate-800 text-center">
                     <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide">Open Drifts</p>
                     <p className="text-2xl font-black text-amber-600 mt-1">{summary?.open_drifts ?? 0}</p>
                 </div>
                 <div className="bg-slate-50 dark:bg-slate-900 p-4 rounded-xl border border-slate-100 dark:border-slate-800 text-center">
                     <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide">Critical Alerts</p>
                     <p className="text-2xl font-black text-riskcrit mt-1">{summary?.critical_alerts ?? 0}</p>
                 </div>
                 <div className="bg-slate-50 dark:bg-slate-900 p-4 rounded-xl border border-slate-100 dark:border-slate-800 text-center">
                     <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide">Warning Alerts</p>
                     <p className="text-2xl font-black text-riskmed mt-1">{summary?.warning_alerts ?? 0}</p>
                 </div>
                 <div className="bg-slate-50 dark:bg-slate-900 p-4 rounded-xl border border-slate-100 dark:border-slate-800 text-center">
                     <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide">Unstable Devices</p>
                     <p className="text-2xl font-black text-riskcrit mt-1">{summary?.flagged_unstable_count ?? 0}</p>
                 </div>
             </div>

             {showIn("noc", "exec") && (summary?.flagged_unstable_devices?.length ?? 0) > 0 && (
                 <div className="bg-white dark:bg-slate-800 p-5 rounded-xl border border-red-200 shadow-sm">
                     <h2 className="text-sm font-bold text-navy dark:text-white mb-3 flex items-center gap-2">
                         🚩 Devices Flagged Unstable — Manual Review Required
                     </h2>
                     <div className="space-y-2">
                         {summary?.flagged_unstable_devices.map((d) => (
                             <div key={d.id} className="flex items-center justify-between text-xs bg-red-50 border border-red-100 rounded-lg px-3 py-2">
                                 <span className="font-bold text-navy dark:text-white">{d.hostname}</span>
                                 <span className="text-slate-400 dark:text-slate-500 font-mono">{d.ip_address}</span>
                                 <span className="text-slate-400 dark:text-slate-500">{d.unstable_since ? timeAgo(d.unstable_since) : ""}</span>
                             </div>
                         ))}
                     </div>
                 </div>
             )}
          </div>
      </div>
    </div>
  );
}