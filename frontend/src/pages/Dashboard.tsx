import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../lib/api";
import { DashboardSummary, DriftFleetSummary, Alert } from "../lib/types";
import Sparkline from "../components/Sparkline";
import { useAuth } from "../lib/auth";

function formatBps(bps: number | null): string {
  if (bps === null || Number.isNaN(bps)) return "—";
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(2)} Gbps`;
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(2)} Mbps`;
  if (bps >= 1e3) return `${(bps / 1e3).toFixed(1)} Kbps`;
  return `${bps.toFixed(0)} bps`;
}

const SEV_DOT: Record<string, string> = { critical: "bg-noc-crit", warning: "bg-noc-warn", info: "bg-noc-cyan" };
const SEV_TEXT: Record<string, string> = { critical: "text-noc-crit", warning: "text-noc-warn", info: "text-noc-cyan" };

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.floor(hrs / 24)}d`;
}

// Bracket-corner instrument panel -- the recurring visual unit of this
// console. Kept as a tiny wrapper so every widget below gets identical
// framing without repeating the corner-tick markup.
function Panel({ children, className = "", lit = false }: { children: React.ReactNode; className?: string; lit?: boolean }) {
  return <div className={`noc-panel ${lit ? "lit" : ""} p-5 ${className}`}>{children}</div>;
}

function PanelHead({ title, right }: { title: string; right?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-4 pb-3 border-b border-noc-border">
      <h2 className="noc-label text-[13px] text-noc-muted uppercase">{title}</h2>
      {right}
    </div>
  );
}

export default function Dashboard() {
  const { user } = useAuth();
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [, setDriftSummary] = useState<DriftFleetSummary | null>(null);
  const [recentAlerts, setRecentAlerts] = useState<Alert[]>([]);
  const [activeAlerts, setActiveAlerts] = useState<Alert[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [connection, setConnection] = useState<"live" | "polling" | "connecting">("connecting");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [clock, setClock] = useState(new Date());
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
      api.get<Alert[]>("/alerts?status=active&limit=8").then((res) => mounted && setRecentAlerts(res.data)).catch(() => {});
      api.get<Alert[]>("/alerts?status=active&limit=200").then((res) => mounted && setActiveAlerts(res.data)).catch(() => {});
    };
    fetchSummary();
    fetchAlerts();
    api.get<DriftFleetSummary>("/drift/summary").then((res) => mounted && setDriftSummary(res.data)).catch(() => {});

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

    const clockTimer = setInterval(() => setClock(new Date()), 1000);

    return () => {
      mounted = false;
      ws?.close();
      if (pollInterval) clearInterval(pollInterval);
      clearInterval(clockTimer);
    };
  }, []);

  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  const downPortAlerts = activeAlerts.filter((a) => a.category.startsWith("Interface Down"));
  const unreachableAlerts = activeAlerts.filter((a) => a.category === "Device Unreachable");
  const restartAlerts = activeAlerts.filter((a) => a.category === "Device Restart");
  const noIssues = downPortAlerts.length === 0 && unreachableAlerts.length === 0 && restartAlerts.length === 0
    && (summary?.down_ports?.length ?? 0) === 0 && (summary?.recent_reboots?.length ?? 0) === 0;

  const scoreColor = (score: number) => (score >= 90 ? "text-noc-good" : score >= 70 ? "text-noc-warn" : "text-noc-crit");
  const utilBar = (val: number) => (val >= 90 ? "bg-noc-crit" : val >= 65 ? "bg-noc-warn" : "bg-noc-cyan");

  const online = summary?.devices_online ?? 0;
  const total = summary?.devices_total ?? 0;
  const onlinePct = total > 0 ? (online / total) * 100 : 100;
  const offline = Math.max(total - online, 0);

  // Ticker: live-feed of the most urgent/recent things happening on the
  // fleet right now -- the console's signature marquee.
  const tickerItems = [
    ...(summary?.flagged_unstable_devices ?? []).map((d) => ({ text: `UNSTABLE — ${d.hostname} (${d.ip_address})`, tone: "text-noc-crit" })),
    ...recentAlerts.map((a) => ({ text: `${a.category.toUpperCase()} — ${a.message}`, tone: SEV_TEXT[a.severity] || "text-noc-cyan" })),
    ...(summary?.down_ports ?? []).slice(0, 6).map((p) => ({ text: `PORT DOWN — ${p.hostname} / ${p.interface}`, tone: "text-noc-warn" })),
    ...(summary?.recent_reboots ?? []).slice(0, 4).map((r) => ({ text: `REBOOT — ${r.hostname} up ${Math.floor(r.uptime_seconds / 60)}m`, tone: "text-noc-cyan" })),
  ];
  const tickerLoop = tickerItems.length > 0 ? [...tickerItems, ...tickerItems] : [];

  return (
    <div className="noc-root -m-8 p-6 font-sans text-noc-text">
      {/* ---- Console header ------------------------------------------------ */}
      <div className="flex items-start justify-between gap-4 flex-wrap mb-4">
        <div>
          <div className="flex items-center gap-2.5">
            <span className={`w-2 h-2 rounded-full ${connection === "live" ? "bg-noc-good noc-live-dot" : connection === "polling" ? "bg-noc-warn" : "bg-noc-faint"}`} />
            <h1 className="noc-label text-2xl uppercase tracking-widest text-noc-text">Fleet Operations</h1>
          </div>
          <p className="text-[13px] text-noc-muted mt-1">
            {greeting}{user ? `, ${user.full_name.split(" ")[0]}` : ""} — {total} devices under management
          </p>
        </div>
        <div className="text-right noc-num text-noc-muted text-xs">
          <div className={`uppercase font-semibold tracking-wider ${connection === "live" ? "text-noc-good" : connection === "polling" ? "text-noc-warn" : "text-noc-faint"}`}>
            {connection === "live" ? "● LIVE" : connection === "polling" ? "● POLLING" : "○ CONNECTING"}
          </div>
          <div className="text-lg text-noc-text mt-0.5">{clock.toLocaleTimeString()}</div>
          <div>{lastUpdated ? `synced ${timeAgo(lastUpdated.toISOString())} ago` : "—"}</div>
        </div>
      </div>

      {error && !summary && (
        <div className="noc-panel border-noc-crit/40 p-4 mb-4 text-sm text-noc-crit">
          {error} Make sure the backend is running at{" "}
          <code className="bg-noc-panel2 px-2 py-0.5 rounded border border-noc-border ml-1">{import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1"}</code>.
        </div>
      )}

      {/* ---- Ticker marquee ------------------------------------------------ */}
      <div className="noc-panel mb-5 overflow-hidden h-9 flex items-center">
        <span className="noc-label text-[11px] px-3 shrink-0 text-noc-bg bg-noc-cyan h-full flex items-center uppercase">Live Feed</span>
        {tickerLoop.length > 0 ? (
          <div className="noc-ticker-track">
            {tickerLoop.map((t, i) => (
              <span key={i} className={`noc-num text-[12px] px-6 whitespace-nowrap ${t.tone}`}>{t.text}</span>
            ))}
          </div>
        ) : (
          <span className="noc-num text-[12px] px-4 text-noc-good">All systems nominal — no active incidents.</span>
        )}
      </div>

      {/* ---- KPI strip ------------------------------------------------------ */}
      <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-7 gap-3 mb-5">
        <Panel className="!p-4">
          <p className="noc-label text-[10px] text-noc-muted uppercase mb-1.5">Health Score</p>
          <p className={`noc-num text-3xl font-bold ${scoreColor(summary?.global_health_score ?? 100)}`}>{summary?.global_health_score ?? 100}</p>
        </Panel>
        <Panel className="!p-4">
          <p className="noc-label text-[10px] text-noc-muted uppercase mb-1.5">Online</p>
          <p className="noc-num text-3xl font-bold text-noc-good">{online}<span className="text-sm text-noc-faint">/{total}</span></p>
        </Panel>
        <Panel className="!p-4" lit={offline > 0}>
          <p className="noc-label text-[10px] text-noc-muted uppercase mb-1.5">Offline</p>
          <p className={`noc-num text-3xl font-bold ${offline > 0 ? "text-noc-crit" : "text-noc-faint"}`}>{offline}</p>
        </Panel>
        <Panel className="!p-4" lit={(summary?.critical_alerts ?? 0) > 0}>
          <p className="noc-label text-[10px] text-noc-muted uppercase mb-1.5">Critical Alerts</p>
          <p className={`noc-num text-3xl font-bold ${(summary?.critical_alerts ?? 0) > 0 ? "text-noc-crit" : "text-noc-faint"}`}>{summary?.critical_alerts ?? 0}</p>
        </Panel>
        <Panel className="!p-4">
          <p className="noc-label text-[10px] text-noc-muted uppercase mb-1.5">Warnings</p>
          <p className={`noc-num text-3xl font-bold ${(summary?.warning_alerts ?? 0) > 0 ? "text-noc-warn" : "text-noc-faint"}`}>{summary?.warning_alerts ?? 0}</p>
        </Panel>
        <Panel className="!p-4">
          <p className="noc-label text-[10px] text-noc-muted uppercase mb-1.5">Open Drifts</p>
          <p className="noc-num text-3xl font-bold text-noc-violet">{summary?.open_drifts ?? 0}</p>
        </Panel>
        <Panel className="!p-4">
          <p className="noc-label text-[10px] text-noc-muted uppercase mb-1.5">Deploy Success</p>
          <p className="noc-num text-3xl font-bold text-noc-cyan">{summary?.deployment_success_rate ?? 100}<span className="text-sm text-noc-faint">%</span></p>
        </Panel>
      </div>

      {/* ---- Main grid -------------------------------------------------------- */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-5">
        {/* Left / main column */}
        <div className="xl:col-span-2 space-y-5">

          {/* Fleet connectivity bar + history chart */}
          <Panel>
            <PanelHead
              title="Fleet Connectivity — 24h"
              right={<span className="noc-num text-xs text-noc-muted">{online}/{total} online</span>}
            />
            <div className="w-full h-2.5 rounded-full bg-noc-panel2 overflow-hidden flex mb-5 border border-noc-border">
              <div className="h-full bg-noc-good" style={{ width: `${onlinePct}%` }} />
              <div className="h-full bg-noc-crit" style={{ width: `${100 - onlinePct}%` }} />
            </div>
            {(summary?.fleet_health_history?.length ?? 0) === 0 ? (
              <p className="text-sm text-noc-faint py-10 text-center">Not enough polling history yet.</p>
            ) : (
              <div className="h-56">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={summary?.fleet_health_history}>
                    <defs>
                      <linearGradient id="cpuFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#22D3EE" stopOpacity={0.35} />
                        <stop offset="95%" stopColor="#22D3EE" stopOpacity={0.02} />
                      </linearGradient>
                      <linearGradient id="memFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#A78BFA" stopOpacity={0.3} />
                        <stop offset="95%" stopColor="#A78BFA" stopOpacity={0.02} />
                      </linearGradient>
                      <linearGradient id="bwFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#FBBF24" stopOpacity={0.3} />
                        <stop offset="95%" stopColor="#FBBF24" stopOpacity={0.02} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1D2532" vertical={false} />
                    <XAxis dataKey="timestamp" tickFormatter={(v) => (v ? new Date(v).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "")} tick={{ fontSize: 10, fill: "#7C8697" }} minTickGap={40} />
                    <YAxis tickFormatter={(v) => `${v}%`} tick={{ fontSize: 10, fill: "#7C8697" }} width={36} domain={[0, 100]} />
                    <Tooltip
                      contentStyle={{ background: "#0E131C", border: "1px solid #1D2532", borderRadius: 6, fontSize: 12 }}
                      labelStyle={{ color: "#7C8697" }}
                      formatter={(v: number, name: string) => [`${v?.toFixed?.(1) ?? v}%`, name]}
                      labelFormatter={(v) => (v ? new Date(v).toLocaleString() : "")}
                    />
                    <Area type="monotone" dataKey="avg_cpu" name="CPU" stroke="#22D3EE" fill="url(#cpuFill)" strokeWidth={2} connectNulls />
                    <Area type="monotone" dataKey="avg_memory" name="Memory" stroke="#A78BFA" fill="url(#memFill)" strokeWidth={2} connectNulls />
                    <Area type="monotone" dataKey="avg_bandwidth" name="Bandwidth" stroke="#FBBF24" fill="url(#bwFill)" strokeWidth={2} connectNulls />
                  </AreaChart>
                </ResponsiveContainer>
                <div className="flex gap-4 justify-center mt-2 text-[11px] noc-num">
                  <span className="text-noc-cyan">■ CPU</span>
                  <span className="text-noc-violet">■ Memory</span>
                  <span className="text-noc-warn">■ Bandwidth</span>
                </div>
              </div>
            )}
          </Panel>

          {/* Uplinks */}
          <Panel>
            <PanelHead title="Uplinks & WAN Links" right={<Link to="/devices" className="text-[11px] text-noc-cyan hover:underline noc-label">ALL DEVICES →</Link>} />
            {(summary?.uplinks?.length ?? 0) === 0 ? (
              <p className="text-xs text-noc-faint italic">No devices tagged as WAN/uplink/core/edge yet. Set a device's Role (e.g. "wan-edge", "core", "uplink") to surface it here.</p>
            ) : (
              <div className="space-y-4">
                {summary?.uplinks?.map((link, i) => (
                  <div key={i} className="flex flex-col gap-1.5">
                    <div className="flex justify-between items-center gap-2 text-[13px]">
                      <span className="flex items-center gap-2 font-semibold text-noc-text truncate">
                        <span className={`w-2 h-2 rounded-full shrink-0 ${link.status === "online" ? "bg-noc-good" : link.status === "offline" ? "bg-noc-crit" : "bg-noc-faint"}`} />
                        {link.hostname}
                        <span className="text-noc-muted font-normal noc-num text-[11px]">{link.ip_address}</span>
                        {link.role && <span className="text-[9px] uppercase noc-label text-noc-cyan bg-noc-panel2 px-1.5 py-0.5 rounded border border-noc-border">{link.role}</span>}
                      </span>
                      <span className="flex items-center gap-2 shrink-0 noc-num font-semibold text-noc-text">
                        <Sparkline values={link.history} color={link.utilization_pct >= 85 ? "#F87171" : link.utilization_pct >= 65 ? "#FBBF24" : "#22D3EE"} />
                        {formatBps(link.throughput_bps)}
                        <span className="text-noc-muted font-normal">({link.utilization_pct.toFixed(1)}%)</span>
                      </span>
                    </div>
                    <div className="w-full h-1.5 rounded-full bg-noc-panel2 overflow-hidden border border-noc-border">
                      <div className={`h-full ${utilBar(link.utilization_pct)}`} style={{ width: `${Math.min(link.utilization_pct, 100)}%` }} />
                    </div>
                    {(link.errors ?? 0) > 0 && <p className="text-[11px] font-medium text-noc-warn">{link.errors} interface error(s) since last poll</p>}
                  </div>
                ))}
              </div>
            )}
          </Panel>

          {/* Top-N metric widgets */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
            {[
              { title: "Top CPU", data: summary?.top_cpu_devices, key: "cpu" as const, unit: "%" },
              { title: "Top Memory", data: summary?.top_memory_devices, key: "memory" as const, unit: "%" },
              { title: "Top Bandwidth", data: summary?.top_bandwidth_devices, key: "bandwidth" as const, unit: "" },
            ].map((col) => (
              <Panel key={col.title}>
                <h3 className="noc-label text-[11px] text-noc-muted uppercase mb-3">{col.title}</h3>
                {(col.data?.length ?? 0) === 0 ? (
                  <p className="text-xs text-noc-faint italic">No telemetry yet.</p>
                ) : (
                  <div className="space-y-3">
                    {col.data?.slice(0, 5).map((dev: any, i: number) => (
                      <div key={i}>
                        <div className="flex justify-between items-center gap-2 text-[12px] font-medium text-noc-text mb-1">
                          <span className="truncate">{dev.hostname}</span>
                          <span className="noc-num text-noc-muted shrink-0">{col.unit ? `${dev[col.key].toFixed(0)}${col.unit}` : formatBps(dev[col.key])}</span>
                        </div>
                        <div className="w-full h-1.5 rounded-full bg-noc-panel2 overflow-hidden border border-noc-border">
                          <div className={`h-full ${utilBar(col.unit ? dev[col.key] : Math.min((dev[col.key] / 1e9) * 100, 100))}`} style={{ width: `${col.unit ? Math.min(dev[col.key], 100) : Math.min((dev[col.key] / 1e9) * 100, 100)}%` }} />
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </Panel>
            ))}
          </div>
        </div>

        {/* Right column: live incident rail */}
        <div className="space-y-5">
          {/* NOC live status */}
          <Panel lit={!noIssues}>
            <PanelHead title="Live Status" right={<span className="noc-num text-[10px] text-noc-muted">last 200 alerts</span>} />
            {!noIssues ? (
              <ul className="space-y-1.5 max-h-72 overflow-y-auto pr-1">
                {downPortAlerts.slice(0, 6).map((a) => (
                  <li key={a.id} className="flex items-center justify-between gap-2 text-[11px] bg-noc-panel2 border border-noc-border rounded px-2 py-1.5">
                    <span className="font-semibold text-noc-warn truncate noc-num">⚠ {a.message}</span>
                    <span className="text-noc-faint shrink-0">{timeAgo(a.created_at)}</span>
                  </li>
                ))}
                {unreachableAlerts.slice(0, 6).map((a) => (
                  <li key={a.id} className="flex items-center justify-between gap-2 text-[11px] bg-noc-panel2 border border-noc-border rounded px-2 py-1.5">
                    <span className="font-semibold text-noc-crit truncate noc-num">✕ {a.message}</span>
                    <span className="text-noc-faint shrink-0">{timeAgo(a.created_at)}</span>
                  </li>
                ))}
                {(summary?.down_ports || []).slice(0, 6).map((p, i) => (
                  <li key={`dp-${i}`} className="flex items-center justify-between gap-2 text-[11px] bg-noc-panel2 border border-noc-border rounded px-2 py-1.5">
                    <span className="font-semibold text-noc-warn truncate noc-num">⚠ {p.hostname} — {p.interface}</span>
                    <span className="text-noc-faint shrink-0">{p.down_since ? timeAgo(p.down_since) : "—"}</span>
                  </li>
                ))}
                {(summary?.recent_reboots || []).slice(0, 4).map((r, i) => (
                  <li key={`rb-${i}`} className="flex items-center justify-between gap-2 text-[11px] bg-noc-panel2 border border-noc-border rounded px-2 py-1.5">
                    <span className="font-semibold text-noc-cyan truncate noc-num">↻ {r.hostname}</span>
                    <span className="text-noc-faint shrink-0">up {Math.floor(r.uptime_seconds / 60)}m</span>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="text-center py-6">
                <p className="text-sm font-semibold text-noc-good noc-label uppercase tracking-wide">All Clear</p>
                <p className="text-[11px] text-noc-faint mt-1">No down ports, unreachable devices, or recent reboots.</p>
              </div>
            )}
          </Panel>

          {/* Active alerts feed */}
          <Panel>
            <PanelHead title="Active Alerts" right={<Link to="/alerts" className="text-[11px] text-noc-cyan hover:underline noc-label">VIEW ALL →</Link>} />
            {recentAlerts.length === 0 ? (
              <div className="text-center py-6">
                <p className="text-sm font-semibold text-noc-good noc-label uppercase">No Active Alerts</p>
              </div>
            ) : (
              <div className="space-y-2 max-h-80 overflow-y-auto pr-1">
                {recentAlerts.map((alert) => (
                  <div key={alert.id} className="flex gap-2.5 bg-noc-panel2 rounded px-3 py-2 border border-noc-border">
                    <span className={`w-1.5 h-1.5 rounded-full mt-1.5 shrink-0 ${SEV_DOT[alert.severity] || "bg-noc-cyan"}`} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-0.5">
                        <span className={`text-[10px] uppercase noc-label ${SEV_TEXT[alert.severity] || "text-noc-muted"}`}>{alert.category}</span>
                        {alert.acknowledged && <span className="text-[9px] font-bold uppercase text-noc-cyan bg-noc-cyan/10 px-1 py-0.5 rounded">ACK</span>}
                      </div>
                      <p className="text-[12px] text-noc-text truncate">{alert.message}</p>
                      <p className="text-[10px] text-noc-faint mt-0.5 noc-num">{timeAgo(alert.created_at)} ago</p>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Panel>

          {/* Unstable devices */}
          {(summary?.flagged_unstable_devices?.length ?? 0) > 0 && (
            <Panel lit>
              <h2 className="noc-label text-[13px] text-noc-crit uppercase mb-3">Flagged Unstable</h2>
              <div className="space-y-1.5">
                {summary?.flagged_unstable_devices.map((d) => (
                  <div key={d.id} className="flex items-center justify-between text-[11px] bg-noc-crit/10 border border-noc-crit/30 rounded px-2.5 py-1.5">
                    <span className="font-semibold text-noc-text truncate">{d.hostname}</span>
                    <span className="text-noc-muted noc-num shrink-0">{d.ip_address}</span>
                    <span className="text-noc-faint shrink-0">{d.unstable_since ? timeAgo(d.unstable_since) : ""}</span>
                  </div>
                ))}
              </div>
            </Panel>
          )}

          {/* Compact status row */}
          <div className="grid grid-cols-3 gap-2.5">
            <Panel className="!p-3 text-center">
              <p className="noc-label text-[9px] text-noc-muted uppercase">Pending</p>
              <p className="noc-num text-lg font-bold text-noc-warn mt-0.5">{summary?.pending_change_requests ?? 0}</p>
            </Panel>
            <Panel className="!p-3 text-center">
              <p className="noc-label text-[9px] text-noc-muted uppercase">Rollbacks</p>
              <p className="noc-num text-lg font-bold text-noc-crit mt-0.5">{summary?.rollbacks ?? 0}</p>
            </Panel>
            <Panel className="!p-3 text-center">
              <p className="noc-label text-[9px] text-noc-muted uppercase">EOL/EOS</p>
              <p className="noc-num text-lg font-bold text-noc-violet mt-0.5">{summary?.eos_device_count ?? 0}</p>
            </Panel>
          </div>
        </div>
      </div>
    </div>
  );
}