import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { Alert, AlertSeverity, DashboardSummary, SyslogSummary, TopologyResponse } from "../lib/types";

// Fullscreen, kiosk-style rollup for a wall monitor in the NOC -- the
// desktop counterpart to pages/MobileNOC.tsx (also routed outside
// <Layout>, see App.tsx). Where MobileNOC is "one engineer, one phone,
// active alerts only", this is "whole room, big screen, glanceable
// status" -- so it trades interactivity for size: no filters, no click
// targets beyond fullscreen/pause, everything sized to read from across
// a room.
//
// A dense always-visible stat strip stays on screen the whole time (the
// numbers someone glances up for -- fleet availability, uplink health,
// syslog volume, deployment/change activity, not just alert counts);
// below it, four content panels auto-rotate on a timer (the things that
// take more than a glance): top active alerts, fleet topology health
// grid, fleet resource hotspots + uplinks, and ops activity (open
// incidents, in-flight deployments, recent backups/changes).
const ROTATE_MS = 12_000;
const POLL_MS = 20_000;
const CLOCK_MS = 1_000;

type Panel = "alerts" | "topology" | "fleet" | "ops";
const PANELS: { id: Panel; label: string }[] = [
  { id: "alerts", label: "Active Alerts" },
  { id: "topology", label: "Topology Health" },
  { id: "fleet", label: "Fleet & Uplinks" },
  { id: "ops", label: "Ops Activity" },
];

interface Incident {
  id: string;
  title: string;
  severity: string;
  status: string;
  alert_ids: string[];
  detected_at: string | null;
  created_at: string;
}

const SEVERITY_DOT: Record<AlertSeverity, string> = {
  critical: "bg-red-500",
  warning: "bg-amber-400",
  info: "bg-sky-400",
};

const SEVERITY_ROW: Record<AlertSeverity, string> = {
  critical: "border-red-500/40 bg-red-950/30",
  warning: "border-amber-400/30 bg-amber-950/20",
  info: "border-sky-400/20 bg-sky-950/10",
};

const HEALTH_DOT: Record<string, string> = {
  green: "bg-emerald-500",
  yellow: "bg-amber-400",
  red: "bg-red-500",
  gray: "bg-slate-500",
};

const STATUS_DOT: Record<string, string> = {
  online: "bg-emerald-500",
  degraded: "bg-amber-400",
  offline: "bg-red-500",
  unknown: "bg-slate-500",
};

const INCIDENT_SEVERITY_BADGE: Record<string, string> = {
  critical: "bg-red-500/20 text-red-300 border-red-500/40",
  major: "bg-amber-500/20 text-amber-300 border-amber-500/40",
  minor: "bg-slate-500/20 text-slate-300 border-slate-500/40",
};

const OPEN_INCIDENT_STATUSES = new Set(["open", "mitigated", "postmortem_due"]);

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

function formatBps(bps: number | null | undefined): string {
  if (bps === null || bps === undefined) return "—";
  if (bps >= 1_000_000_000) return `${(bps / 1_000_000_000).toFixed(1)}Gbps`;
  if (bps >= 1_000_000) return `${(bps / 1_000_000).toFixed(1)}Mbps`;
  if (bps >= 1_000) return `${(bps / 1_000).toFixed(0)}Kbps`;
  return `${bps}bps`;
}

function StatTile({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="flex-1 min-w-[110px] px-3.5 py-2.5 border-r border-slate-800 last:border-r-0">
      <div className={`text-2xl font-black tabular-nums leading-none ${tone || "text-white"}`}>{value}</div>
      <div className="text-[10px] uppercase tracking-wider text-slate-500 font-bold mt-1.5">{label}</div>
    </div>
  );
}

export default function WallBoard() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [topology, setTopology] = useState<TopologyResponse | null>(null);
  const [syslogSummary, setSyslogSummary] = useState<SyslogSummary | null>(null);
  const [devicesById, setDevicesById] = useState<Record<string, string>>({});
  const [now, setNow] = useState(new Date());
  const [activePanel, setActivePanel] = useState<Panel>("alerts");
  const [paused, setPaused] = useState(false);
  const [connError, setConnError] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);

  const loadAll = useCallback(() => {
    Promise.all([
      api.get<DashboardSummary>("/dashboard/summary"),
      api.get<Alert[]>("/alerts", { params: { status: "active", limit: 100 } }),
      api.get<Incident[]>("/incidents"),
      api.get<TopologyResponse>("/topology"),
      api.get<SyslogSummary>("/syslog/summary", { params: { hours: 1 } }).catch(() => null),
    ])
      .then(([s, a, i, t, sl]) => {
        setSummary(s.data);
        setAlerts(a.data);
        setIncidents(i.data);
        setTopology(t.data);
        if (sl) setSyslogSummary(sl.data);
        const map: Record<string, string> = {};
        for (const n of t.data.nodes) map[n.id] = n.hostname;
        setDevicesById(map);
        setConnError(false);
      })
      .catch(() => setConnError(true));
  }, []);

  useEffect(() => {
    loadAll();
    const t = setInterval(loadAll, POLL_MS);
    return () => clearInterval(t);
  }, [loadAll]);

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), CLOCK_MS);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (paused) return;
    const t = setInterval(() => {
      setActivePanel((p) => {
        const idx = PANELS.findIndex((x) => x.id === p);
        return PANELS[(idx + 1) % PANELS.length].id;
      });
    }, ROTATE_MS);
    return () => clearInterval(t);
  }, [paused]);

  useEffect(() => {
    const onFsChange = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, []);

  const toggleFullscreen = () => {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      containerRef.current?.requestFullscreen().catch(() => {});
    }
  };

  const sortedAlerts = useMemo(() => {
    const rank: Record<AlertSeverity, number> = { critical: 0, warning: 1, info: 2 };
    return [...alerts].sort((a, b) => rank[a.severity] - rank[b.severity]).slice(0, 14);
  }, [alerts]);

  const openIncidents = useMemo(
    () => incidents.filter((i) => OPEN_INCIDENT_STATUSES.has(i.status)).sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [incidents]
  );

  const criticalCount = alerts.filter((a) => a.severity === "critical").length;
  const warningCount = alerts.filter((a) => a.severity === "warning").length;
  const syslogErrorCount = syslogSummary
    ? (syslogSummary.by_severity["error"] || 0) + (syslogSummary.by_severity["critical"] || 0) + (syslogSummary.by_severity["emergency"] || 0) + (syslogSummary.by_severity["alert"] || 0)
    : 0;

  return (
    <div ref={containerRef} className="fixed inset-0 bg-slate-950 text-white flex flex-col overflow-hidden font-sans">
      {/* --- Top bar: identity, clock, controls --- */}
      <div className="flex items-center justify-between px-5 py-2.5 border-b border-slate-800 shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-brandblue flex items-center justify-center">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 2l8 4v6c0 5-3.4 8.4-8 10-4.6-1.6-8-5-8-10V6l8-4z" />
            </svg>
          </div>
          <span className="font-bold text-lg tracking-tight">NetGuard6</span>
          <span className="text-slate-500 text-sm">NOC Wall Board</span>
          {connError && (
            <span className="text-[11px] font-bold px-2 py-0.5 rounded-full bg-red-500/20 text-red-300 border border-red-500/40">
              Connection issue — showing last known state
            </span>
          )}
        </div>
        <div className="flex items-center gap-4">
          <div className="text-right">
            <div className="text-2xl font-black tabular-nums leading-none">
              {now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
            </div>
            <div className="text-[11px] text-slate-500">
              {now.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}
            </div>
          </div>
          <button
            onClick={() => setPaused((p) => !p)}
            title={paused ? "Resume auto-rotation" : "Pause auto-rotation"}
            className="w-9 h-9 rounded-lg flex items-center justify-center text-slate-400 hover:bg-slate-800 hover:text-white"
          >
            {paused ? (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z" /></svg>
            ) : (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="5" width="4" height="14" /><rect x="14" y="5" width="4" height="14" /></svg>
            )}
          </button>
          <button
            onClick={toggleFullscreen}
            title={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
            className="w-9 h-9 rounded-lg flex items-center justify-center text-slate-400 hover:bg-slate-800 hover:text-white"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M8 3H5a2 2 0 00-2 2v3M16 3h3a2 2 0 012 2v3M8 21H5a2 2 0 01-2-2v-3M16 21h3a2 2 0 002-2v-3" />
            </svg>
          </button>
        </div>
      </div>

      {/* --- Always-visible stat strip -- a real NOC needs more than just
          alert counts here: fleet availability, uplink uptime, syslog
          error volume, and change/deployment activity are the numbers an
          on-call engineer actually glances up for. Wraps to a second row
          on smaller wall displays rather than clipping. --- */}
      <div className="flex flex-wrap border-b border-slate-800 shrink-0">
        <StatTile label="Devices Online" value={summary ? `${summary.devices_online}/${summary.devices_total}` : "—"} />
        <StatTile label="Fleet Health" value={summary ? `${summary.global_health_score}` : "—"} tone={healthTone(summary?.global_health_score)} />
        <StatTile
          label="Fleet Availability"
          value={summary?.fleet_health_weighted_pct !== undefined ? `${summary.fleet_health_weighted_pct.toFixed(1)}%` : "—"}
          tone={healthTone(summary?.fleet_health_weighted_pct)}
        />
        <StatTile label="Critical Alerts" value={criticalCount} tone={criticalCount > 0 ? "text-red-400" : "text-white"} />
        <StatTile label="Warning Alerts" value={warningCount} tone={warningCount > 0 ? "text-amber-400" : "text-white"} />
        <StatTile label="Open Incidents" value={openIncidents.length} tone={openIncidents.length > 0 ? "text-red-400" : "text-white"} />
        <StatTile
          label="Uplinks Up"
          value={summary?.uplink_availability ? `${summary.uplink_availability.uplinks_up}/${summary.uplink_availability.uplinks_total}` : "—"}
          tone={
            summary?.uplink_availability && summary.uplink_availability.uplinks_up < summary.uplink_availability.uplinks_total
              ? "text-amber-400"
              : "text-white"
          }
        />
        <StatTile label="Ports Down" value={summary?.down_ports?.length ?? "—"} tone={(summary?.down_ports?.length ?? 0) > 0 ? "text-amber-400" : "text-white"} />
        <StatTile label="Active Deployments" value={summary?.active_deployments ?? "—"} />
        <StatTile label="Failed Deployments" value={summary?.failed_deployments ?? "—"} tone={(summary?.failed_deployments ?? 0) > 0 ? "text-red-400" : "text-white"} />
        <StatTile label="Open Drifts" value={summary?.open_drifts ?? "—"} />
        <StatTile label="Pending Changes" value={summary?.pending_change_requests ?? "—"} />
        <StatTile label="Syslog Errors (1h)" value={syslogSummary ? syslogErrorCount : "—"} tone={syslogErrorCount > 0 ? "text-amber-400" : "text-white"} />
      </div>

      {/* --- Rotating main panel --- */}
      <div className="flex-1 min-h-0 overflow-hidden p-5">
        {activePanel === "alerts" && <AlertsPanel alerts={sortedAlerts} total={alerts.length} />}
        {activePanel === "topology" && <TopologyPanel topology={topology} summary={summary} />}
        {activePanel === "fleet" && <FleetPanel summary={summary} />}
        {activePanel === "ops" && <OpsPanel incidents={openIncidents} devicesById={devicesById} summary={summary} />}
      </div>

      {/* --- Panel indicator / manual switch --- */}
      <div className="flex items-center justify-center gap-2 py-3 border-t border-slate-800 shrink-0">
        {PANELS.map((p) => (
          <button
            key={p.id}
            onClick={() => setActivePanel(p.id)}
            className={`flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold transition-colors ${
              activePanel === p.id ? "bg-brandblue text-white" : "bg-slate-800 text-slate-400 hover:bg-slate-700"
            }`}
          >
            {p.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function healthTone(score: number | undefined): string {
  if (score === undefined) return "text-white";
  if (score >= 90) return "text-emerald-400";
  if (score >= 70) return "text-amber-400";
  return "text-red-400";
}

function AlertsPanel({ alerts, total }: { alerts: Alert[]; total: number }) {
  return (
    <div className="h-full flex flex-col">
      <h2 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-3">
        Top Active Alerts {total > alerts.length && <span className="text-slate-600">(showing {alerts.length} of {total})</span>}
      </h2>
      {alerts.length === 0 ? (
        <div className="flex-1 flex items-center justify-center text-slate-600 text-xl font-bold">No active alerts — fleet is quiet.</div>
      ) : (
        <div className="flex-1 min-h-0 overflow-y-auto grid grid-cols-2 gap-2.5 content-start">
          {alerts.map((a) => (
            <div key={a.id} className={`rounded-xl border px-4 py-3 flex items-start gap-3 ${SEVERITY_ROW[a.severity]}`}>
              <span className={`w-2.5 h-2.5 rounded-full mt-1.5 shrink-0 ${SEVERITY_DOT[a.severity]}`} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <div className="font-bold text-sm truncate">{a.category}</div>
                  <div className="text-[11px] text-slate-500 shrink-0">{timeAgo(a.last_seen_at || null)}</div>
                </div>
                <div className="text-sm text-slate-300 truncate">{a.message}</div>
                {a.occurrence_count && a.occurrence_count > 1 && (
                  <div className="text-[11px] text-slate-500 mt-0.5">×{a.occurrence_count} occurrences</div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function TopologyPanel({ topology, summary }: { topology: TopologyResponse | null; summary: DashboardSummary | null }) {
  if (!topology) {
    return <div className="h-full flex items-center justify-center text-slate-600 text-xl font-bold">Loading topology…</div>;
  }
  // Deliberately not the full interactive canvas from pages/Topology.tsx --
  // a wall board wants "which devices are unhealthy right now, at a
  // glance from across the room", not pan/zoom/click. A dense dot grid,
  // grouped by site, reads better at distance than a force-directed graph
  // would at this size. Nodes/edges/ports here are the exact same live
  // build_topology() output the interactive Topology page renders --
  // real discovered adjacency (LLDP/CDP/GNS3/subnet-inferred), not
  // synthetic wallboard-only data.
  const bySite = new Map<string, typeof topology.nodes>();
  for (const n of topology.nodes) {
    const site = n.site || "Unassigned";
    if (!bySite.has(site)) bySite.set(site, []);
    bySite.get(site)!.push(n);
  }
  const sites = Array.from(bySite.entries()).sort((a, b) => b[1].length - a[1].length);

  const unhealthy = topology.nodes.filter((n) => n.health_color === "red" || n.status === "offline").length;

  return (
    <div className="h-full flex gap-5 min-h-0">
      <div className="flex-1 flex flex-col min-w-0">
        <h2 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-3">
          Topology Health — {topology.nodes.length} devices, {topology.edges.length} links
          {unhealthy > 0 && <span className="text-red-400"> · {unhealthy} unhealthy</span>}
        </h2>
        <div className="flex-1 min-h-0 overflow-y-auto space-y-4">
          {sites.map(([site, nodes]) => (
          <div key={site}>
            <div className="text-xs font-bold text-slate-500 mb-1.5">{site} <span className="text-slate-600">({nodes.length})</span></div>
            <div className="flex flex-wrap gap-1.5">
              {nodes.map((n) => (
                <div
                  key={n.id}
                  title={`${n.hostname} — ${n.status}${n.health_color ? `, health: ${n.health_color}` : ""}`}
                  className={`w-3.5 h-3.5 rounded-sm ${
                    n.status === "offline" ? STATUS_DOT.offline : n.health_color ? HEALTH_DOT[n.health_color] : STATUS_DOT[n.status] || STATUS_DOT.unknown
                  }`}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
      <div className="flex items-center gap-4 pt-3 mt-2 border-t border-slate-800 text-[11px] text-slate-500">
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-sm bg-emerald-500" /> Healthy</span>
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-sm bg-amber-400" /> Degraded</span>
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-sm bg-red-500" /> Offline / Unhealthy</span>
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-sm bg-slate-500" /> No data</span>
      </div>
      </div>
      {(summary?.offline_devices && summary.offline_devices.length > 0) && (
        <div className="w-80 flex flex-col shrink-0 border-l border-slate-800 pl-5 min-h-0">
          <h2 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-3">Offline & Degraded</h2>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
            {summary.offline_devices.map(d => (
              <div key={d.id} className="rounded-lg bg-slate-900/60 border border-slate-800 px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-bold text-sm truncate">{d.hostname}</span>
                  <span className={`text-[10px] uppercase font-bold px-1.5 py-0.5 rounded border shrink-0 ${d.status === 'offline' ? 'bg-red-500/20 text-red-300 border-red-500/40' : 'bg-amber-500/20 text-amber-300 border-amber-500/40'}`}>{d.status}</span>
                </div>
                {d.last_seen && <div className="text-xs text-slate-500 mt-1">Last seen: {timeAgo(d.last_seen)} ago</div>}
                {d.last_error && <div className="text-[10px] text-red-400 truncate mt-0.5" title={d.last_error}>{d.last_error}</div>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// New: fleet resource hotspots (CPU/mem/bandwidth) + uplink health --
// the "what's about to become an incident" view that a pure alerts/
// topology rotation was missing. Same DashboardSummary the main
// Dashboard page already computes, just laid out for glance-reading.
function FleetPanel({ summary }: { summary: DashboardSummary | null }) {
  if (!summary) {
    return <div className="h-full flex items-center justify-center text-slate-600 text-xl font-bold">Loading fleet metrics…</div>;
  }
  return (
    <div className="h-full grid grid-cols-4 gap-5 min-h-0">
      <div className="min-h-0 flex flex-col">
        <h2 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-3">Top CPU</h2>
        <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
          {summary.top_cpu_devices.length === 0 ? (
            <div className="text-slate-600 text-sm">No data.</div>
          ) : (
            summary.top_cpu_devices.slice(0, 8).map((d) => (
              <div key={d.hostname} className="flex items-center justify-between rounded-lg bg-slate-900/60 border border-slate-800 px-3 py-2">
                <span className="text-sm font-bold truncate">{d.hostname}</span>
                <span className={`text-sm font-black tabular-nums ${d.cpu >= 90 ? "text-red-400" : d.cpu >= 75 ? "text-amber-400" : "text-slate-300"}`}>
                  {d.cpu.toFixed(0)}%
                </span>
              </div>
            ))
          )}
        </div>
      </div>
      <div className="min-h-0 flex flex-col">
        <h2 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-3">Top Memory</h2>
        <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
          {summary.top_memory_devices.length === 0 ? (
            <div className="text-slate-600 text-sm">No data.</div>
          ) : (
            summary.top_memory_devices.slice(0, 8).map((d) => (
              <div key={d.hostname} className="flex items-center justify-between rounded-lg bg-slate-900/60 border border-slate-800 px-3 py-2">
                <span className="text-sm font-bold truncate">{d.hostname}</span>
                <span className={`text-sm font-black tabular-nums ${d.memory >= 90 ? "text-red-400" : d.memory >= 75 ? "text-amber-400" : "text-slate-300"}`}>
                  {d.memory.toFixed(0)}%
                </span>
              </div>
            ))
          )}
        </div>
      </div>
      <div className="min-h-0 flex flex-col">
        <h2 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-3">Top Bandwidth</h2>
        <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
          {summary.top_bandwidth_devices.length === 0 ? (
            <div className="text-slate-600 text-sm">No data.</div>
          ) : (
            summary.top_bandwidth_devices.slice(0, 8).map((d) => (
              <div key={d.hostname} className="flex items-center justify-between rounded-lg bg-slate-900/60 border border-slate-800 px-3 py-2">
                <span className="text-sm font-bold truncate">{d.hostname}</span>
                <span className={`text-sm font-black tabular-nums ${d.bandwidth >= 90 ? "text-red-400" : d.bandwidth >= 75 ? "text-amber-400" : "text-slate-300"}`}>
                  {d.bandwidth.toFixed(0)}%
                </span>
              </div>
            ))
          )}
        </div>
      </div>
      <div className="min-h-0 flex flex-col gap-4">
        <div className="min-h-0 flex-1 flex flex-col">
          <h2 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-3">
            Uplinks {summary.uplink_availability && (
              <span className="text-slate-600 normal-case font-medium">
                ({summary.uplink_availability.uplinks_up}/{summary.uplink_availability.uplinks_total} up
                {summary.uplink_availability.uptime_pct !== null ? `, ${summary.uplink_availability.uptime_pct.toFixed(1)}% ${summary.uplink_availability.window_days}d` : ""})
              </span>
            )}
          </h2>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
            {summary.uplinks.length === 0 ? (
              <div className="text-slate-600 text-sm">No uplinks configured.</div>
            ) : (
              summary.uplinks.slice(0, 4).map((u) => (
                <div key={u.hostname + (u.role || "")} className="rounded-lg bg-slate-900/60 border border-slate-800 px-3 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-bold truncate">{u.hostname}</span>
                    <span className={`w-2 h-2 rounded-full shrink-0 ${STATUS_DOT[u.status] || STATUS_DOT.unknown}`} />
                  </div>
                  <div className="flex items-center justify-between text-[11px] text-slate-500 mt-0.5">
                    <span>{u.role || "uplink"}</span>
                    <span className="tabular-nums">{u.utilization_pct.toFixed(0)}% · {formatBps(u.throughput_bps)}</span>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
        <div className="min-h-0 flex-1 flex flex-col">
          <h2 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-3">Top Errors</h2>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
            {summary.top_error_devices.length === 0 ? (
              <div className="text-slate-600 text-sm">No data.</div>
            ) : (
              summary.top_error_devices.slice(0, 4).map((d) => (
                <div key={d.hostname} className="flex items-center justify-between rounded-lg bg-slate-900/60 border border-slate-800 px-3 py-2">
                  <span className="text-sm font-bold truncate">{d.hostname}</span>
                  <span className="text-sm font-black text-amber-400 tabular-nums">
                    {d.interface_errors}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
      {(summary.down_ports.length > 0 || summary.flapping_interfaces.length > 0) && (
        <div className="col-span-4 flex gap-5 pt-1 border-t border-slate-800">
          {summary.down_ports.length > 0 && (
            <div className="flex-1 min-w-0">
              <div className="text-[11px] font-bold uppercase tracking-wider text-amber-400 mt-2 mb-1.5">Ports Down ({summary.down_ports.length})</div>
              <div className="flex flex-wrap gap-1.5">
                {summary.down_ports.slice(0, 12).map((p, idx) => (
                  <span key={idx} className="text-[11px] px-2 py-1 rounded-md bg-amber-500/10 border border-amber-500/30 text-amber-300">
                    {p.hostname} · {p.interface}
                  </span>
                ))}
              </div>
            </div>
          )}
          {summary.flapping_interfaces.length > 0 && (
            <div className="flex-1 min-w-0">
              <div className="text-[11px] font-bold uppercase tracking-wider text-red-400 mt-2 mb-1.5">Flapping ({summary.flapping_interfaces.length})</div>
              <div className="flex flex-wrap gap-1.5">
                {summary.flapping_interfaces.slice(0, 12).map((p, idx) => (
                  <span key={idx} className="text-[11px] px-2 py-1 rounded-md bg-red-500/10 border border-red-500/30 text-red-300">
                    {p.hostname} · {p.interface} ×{p.flap_count}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// New: what NOC/ops actually did/is doing -- open incidents plus
// deployment success rate, recent config backups, and recent
// protocol/automation operations, so the wallboard isn't purely a
// "what's broken" view.
function OpsPanel({
  incidents,
  devicesById,
  summary,
}: {
  incidents: Incident[];
  devicesById: Record<string, string>;
  summary: DashboardSummary | null;
}) {
  return (
    <div className="h-full grid grid-cols-3 gap-5 min-h-0">
      <div className="min-h-0 flex flex-col">
        <h2 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-3">Open Incidents ({incidents.length})</h2>
        {incidents.length === 0 ? (
          <div className="flex-1 flex items-center justify-center text-slate-600 text-lg font-bold">No open incidents.</div>
        ) : (
          <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
            {incidents.map((inc) => (
              <div key={inc.id} className="rounded-xl border border-slate-800 bg-slate-900/60 px-4 py-3 flex items-center gap-4">
                <span className={`text-[11px] font-bold px-2 py-0.5 rounded-full border shrink-0 ${INCIDENT_SEVERITY_BADGE[inc.severity] || INCIDENT_SEVERITY_BADGE.minor}`}>
                  {inc.severity.toUpperCase()}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="font-bold truncate">{inc.title}</div>
                  <div className="text-xs text-slate-500">
                    {inc.alert_ids.length} alert{inc.alert_ids.length === 1 ? "" : "s"} · status: {inc.status.replace(/_/g, " ")}
                  </div>
                </div>
                <div className="text-xs text-slate-500 shrink-0">open {timeAgo(inc.detected_at || inc.created_at)}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="min-h-0 flex flex-col gap-4">
        <div>
          <h2 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-2">Deployment Activity</h2>
          <div className="grid grid-cols-3 gap-2">
            <div className="rounded-lg bg-slate-900/60 border border-slate-800 px-3 py-2">
              <div className="text-lg font-black tabular-nums">{summary?.active_deployments ?? "—"}</div>
              <div className="text-[10px] uppercase tracking-wider text-slate-500 font-bold">In flight</div>
            </div>
            <div className="rounded-lg bg-slate-900/60 border border-slate-800 px-3 py-2">
              <div className={`text-lg font-black tabular-nums ${(summary?.failed_deployments ?? 0) > 0 ? "text-red-400" : ""}`}>{summary?.failed_deployments ?? "—"}</div>
              <div className="text-[10px] uppercase tracking-wider text-slate-500 font-bold">Failed</div>
            </div>
            <div className="rounded-lg bg-slate-900/60 border border-slate-800 px-3 py-2">
              <div className="text-lg font-black tabular-nums">{summary?.deployment_success_rate !== undefined ? `${summary.deployment_success_rate.toFixed(0)}%` : "—"}</div>
              <div className="text-[10px] uppercase tracking-wider text-slate-500 font-bold">Success rate</div>
            </div>
          </div>
        </div>

        <div className="min-h-0 flex-1 flex flex-col">
          <h2 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-2">Recent Config Backups</h2>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-1.5">
            {!summary || summary.recent_backups.length === 0 ? (
              <div className="text-slate-600 text-sm">No recent backups.</div>
            ) : (
              summary.recent_backups.slice(0, 6).map((b) => (
                <div key={b.id} className="flex items-center justify-between text-xs bg-slate-900/60 border border-slate-800 rounded-lg px-3 py-1.5">
                  <span className="font-bold truncate">{b.hostname}</span>
                  <span className="text-slate-500 tabular-nums shrink-0">{timeAgo(b.created_at)} ago</span>
                </div>
              ))
            )}
          </div>
        </div>

      </div>
      <div className="min-h-0 flex flex-col gap-4">
        <div className="min-h-0 flex-1 flex flex-col">
          <h2 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-2">Recent Automation</h2>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-1.5">
            {!summary || summary.recent_protocol_operations.length === 0 ? (
              <div className="text-slate-600 text-sm">No recent automation runs.</div>
            ) : (
              summary.recent_protocol_operations.slice(0, 6).map((op) => (
                <div key={op.id} className="flex items-center justify-between text-xs bg-slate-900/60 border border-slate-800 rounded-lg px-3 py-1.5 gap-2">
                  <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${op.success ? "bg-emerald-500" : "bg-red-500"}`} />
                  <span className="font-bold truncate flex-1">{op.device_hostname} · {op.protocol} {op.operation}</span>
                  <span className="text-slate-500 tabular-nums shrink-0">{timeAgo(op.created_at)} ago</span>
                </div>
              ))
            )}
          </div>
        </div>
        <div className="min-h-0 flex-1 flex flex-col">
          <h2 className="text-sm font-bold text-slate-400 uppercase tracking-wider mb-2">Recent Reboots</h2>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-1.5">
            {!summary || summary.recent_reboots.length === 0 ? (
              <div className="text-slate-600 text-sm">No recent reboots.</div>
            ) : (
              summary.recent_reboots.slice(0, 6).map((r) => (
                <div key={r.hostname} className="flex items-center justify-between text-xs bg-slate-900/60 border border-slate-800 rounded-lg px-3 py-1.5 gap-2">
                  <span className="w-1.5 h-1.5 rounded-full shrink-0 bg-amber-400" />
                  <span className="font-bold truncate flex-1">{r.hostname}</span>
                  <span className="text-slate-500 tabular-nums shrink-0">Up {Math.floor(r.uptime_seconds / 60)}m</span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}