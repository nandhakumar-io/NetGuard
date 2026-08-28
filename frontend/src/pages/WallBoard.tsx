import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { Alert, AlertSeverity, DashboardSummary, OnCallSchedule, SyslogSummary, TopologyResponse } from "../lib/types";

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
// If a poll succeeds but data is older than this, something's off (tab
// throttled in the background, a proxy/cache serving stale responses,
// etc.) even though connError wouldn't catch it -- that flag only fires
// on an outright failed request, not "requests keep succeeding but
// nothing's actually changing." Distinct staleness check for a display
// nobody's actively watching moment-to-moment.
const STALE_AFTER_MS = POLL_MS * 3;
// Kiosk browsers left open for weeks accrue memory/DOM cruft and can drift
// from whatever's currently deployed. A quiet, once-a-day reload at a
// fixed off-hours time keeps a 24/7 wall display healthy without anyone
// having to walk over and refresh it by hand.
const DAILY_RELOAD_HOUR = 4;

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
  tenant_name?: string | null;
}

const SEVERITY_DOT: Record<AlertSeverity, string> = {
  critical: "bg-red-500",
  warning: "bg-amber-400",
  info: "bg-sky-400",
};

const SEVERITY_ROW: Record<AlertSeverity, string> = {
  critical: "border-red-300 bg-red-50 dark:border-red-500/40 dark:bg-red-950/30",
  warning: "border-amber-300 bg-amber-50 dark:border-amber-400/30 dark:bg-amber-950/20",
  info: "border-sky-300 bg-sky-50 dark:border-sky-400/20 dark:bg-sky-950/10",
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
  critical: "bg-red-50 text-red-700 border-red-300 dark:bg-red-500/20 dark:text-red-300 dark:border-red-500/40",
  major: "bg-amber-50 text-amber-700 border-amber-300 dark:bg-amber-500/20 dark:text-amber-300 dark:border-amber-500/40",
  minor: "bg-slate-100 text-slate-600 border-slate-300 dark:bg-slate-500/20 dark:text-slate-300 dark:border-slate-500/40",
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

function StatTile({ label, value, tone, accent }: { label: string; value: string | number; tone?: string; accent?: "critical" | "warning" | "ok" }) {
  const accentBar =
    accent === "critical"
      ? "bg-red-500"
      : accent === "warning"
      ? "bg-amber-400"
      : accent === "ok"
      ? "bg-emerald-500"
      : "bg-slate-200 dark:bg-slate-800";
  return (
    <div className="relative flex-1 min-w-[128px] rounded-lg bg-white dark:bg-slate-900/60 border border-slate-200 dark:border-slate-800 shadow-sm overflow-hidden">
      <span className={`absolute inset-x-0 top-0 h-0.5 ${accentBar}`} />
      <div className="px-3.5 py-2.5">
        <div className={`text-2xl font-black tabular-nums leading-none ${tone || "text-navy dark:text-white"}`}>{value}</div>
        <div className="text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-500 font-bold mt-1.5">{label}</div>
      </div>
    </div>
  );
}

// Inline trend line for a numeric history series (oldest -> newest), the
// same shape *_history/history come back as from GET /dashboard/summary.
// Deliberately tiny/axis-less -- this sits next to a single stat as
// "which way is this moving", not a standalone chart someone reads
// values off of.
function Sparkline({ data, width = 68, height = 22, color = "#2563eb", fill = true }: { data: number[]; width?: number; height?: number; color?: string; fill?: boolean }) {
  const clean = (data || []).filter((v) => typeof v === "number" && !Number.isNaN(v));
  if (clean.length < 2) {
    return <div style={{ width, height }} className="shrink-0" />;
  }
  const min = Math.min(...clean);
  const max = Math.max(...clean);
  const span = max - min || 1;
  const stepX = width / (clean.length - 1);
  const points = clean.map((v, i) => {
    const x = i * stepX;
    const y = height - ((v - min) / span) * (height - 3) - 1.5;
    return [x, y];
  });
  const linePath = points.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const areaPath = `${linePath} L${width},${height} L0,${height} Z`;
  const last = points[points.length - 1];
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="shrink-0 overflow-visible">
      {fill && <path d={areaPath} fill={color} opacity={0.12} />}
      <path d={linePath} fill="none" stroke={color} strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={last[0]} cy={last[1]} r={1.8} fill={color} />
    </svg>
  );
}

// Odometer-style radial gauge -- the "big dial" NOC wallboards use for
// the handful of numbers a room-wide glance should resolve in under a
// second (fleet health, availability, uptime%) where a StatTile's plain
// number doesn't communicate "how close to the edge is this" the way an
// arc filling up (or draining) toward a colored danger zone does.
function RadialGauge({
  value,
  label,
  suffix = "%",
  size = 108,
  thresholds = { warn: 90, bad: 70 }, // value >= warn -> ok, >= bad -> warn, below -> critical (lower-is-worse gauges)
  invert = false, // set true for "lower is better" metrics (not currently used, kept for future gauges)
}: {
  value: number | null | undefined;
  label: string;
  suffix?: string;
  size?: number;
  thresholds?: { warn: number; bad: number };
  invert?: boolean;
}) {
  const stroke = Math.max(6, size * 0.09);
  const r = (size - stroke) / 2;
  const cx = size / 2;
  const cy = size / 2;
  // 270° sweep (like a real speedometer) starting at -225deg (bottom-left)
  // ending at +45deg (bottom-right), leaving a gap at the bottom for the
  // label -- reads as a dial, not a full/closed ring.
  const startAngle = -225;
  const sweep = 270;
  const pct = value === null || value === undefined ? 0 : Math.max(0, Math.min(100, value));
  const valueAngle = startAngle + (pct / 100) * sweep;

  const polar = (angleDeg: number) => {
    const rad = (angleDeg * Math.PI) / 180;
    return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
  };
  const arcPath = (a0: number, a1: number) => {
    const [x0, y0] = polar(a0);
    const [x1, y1] = polar(a1);
    const largeArc = a1 - a0 > 180 ? 1 : 0;
    return `M${x0.toFixed(2)},${y0.toFixed(2)} A${r},${r} 0 ${largeArc} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`;
  };

  const ok = value !== null && value !== undefined && (invert ? value <= thresholds.bad : value >= thresholds.warn);
  const warn = value !== null && value !== undefined && !ok && (invert ? value <= thresholds.warn : value >= thresholds.bad);
  const color = value === null || value === undefined ? "#94a3b8" : ok ? "#10b981" : warn ? "#f59e0b" : "#ef4444";

  return (
    <div className="flex flex-col items-center justify-center shrink-0" style={{ width: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <path d={arcPath(startAngle, startAngle + sweep)} fill="none" stroke="currentColor" className="text-slate-200 dark:text-slate-800" strokeWidth={stroke} strokeLinecap="round" />
        {pct > 0 && (
          <path d={arcPath(startAngle, valueAngle)} fill="none" stroke={color} strokeWidth={stroke} strokeLinecap="round" />
        )}
        <text x={cx} y={cy - 2} textAnchor="middle" className="fill-navy dark:fill-white" style={{ fontSize: size * 0.22, fontWeight: 900 }}>
          {value === null || value === undefined ? "—" : Math.round(value)}
        </text>
        <text x={cx} y={cy + size * 0.15} textAnchor="middle" className="fill-slate-400 dark:fill-slate-500" style={{ fontSize: size * 0.11, fontWeight: 700 }}>
          {value !== null && value !== undefined ? suffix : ""}
        </text>
      </svg>
      <div className="text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-500 font-bold text-center -mt-1.5">{label}</div>
    </div>
  );
}

// Multi-series area/line chart for fleet_health_history -- avg CPU /
// memory / bandwidth utilization across the fleet over time, the trend
// a real NOC keeps in peripheral vision to catch a slow creep (e.g.
// average CPU climbing over the shift) that no single-point stat tile
// or per-device sparkline would surface.
function TrendChart({
  history,
  height = 108,
}: {
  history: { timestamp: string | null; avg_cpu: number | null; avg_memory: number | null; avg_bandwidth: number | null }[];
  height?: number;
}) {
  const width = 100; // percentage-based viewBox, scales to container via CSS width
  const series: { key: "avg_cpu" | "avg_memory" | "avg_bandwidth"; label: string; color: string }[] = [
    { key: "avg_cpu", label: "Avg CPU", color: "#2563eb" },
    { key: "avg_memory", label: "Avg Memory", color: "#8b5cf6" },
    { key: "avg_bandwidth", label: "Avg Bandwidth", color: "#0ea5e9" },
  ];

  const points = (history || []).filter((h) => h.timestamp);
  if (points.length < 2) {
    return (
      <div className="rounded-xl border border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-900/60 flex items-center justify-center text-slate-400 dark:text-slate-600 text-sm font-bold" style={{ height }}>
        Not enough history yet.
      </div>
    );
  }
  const stepX = width / (points.length - 1);

  const buildPath = (key: "avg_cpu" | "avg_memory" | "avg_bandwidth") => {
    const coords = points.map((p, i) => {
      const v = p[key] ?? 0;
      const y = height - (Math.max(0, Math.min(100, v)) / 100) * (height - 6) - 3;
      return [i * stepX, y];
    });
    return coords.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
  };

  const first = points[0]?.timestamp;
  const last = points[points.length - 1]?.timestamp;
  const fmtTime = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "");

  return (
    <div className="rounded-xl border border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-900/60 px-4 pt-3 pb-2">
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-4">
          {series.map((s) => (
            <span key={s.key} className="flex items-center gap-1.5 text-[11px] font-bold text-slate-500 dark:text-slate-400">
              <span className="w-2 h-2 rounded-full" style={{ backgroundColor: s.color }} />
              {s.label}
            </span>
          ))}
        </div>
        <span className="text-[10px] text-slate-400 dark:text-slate-600 tabular-nums">{fmtTime(first)} – {fmtTime(last)}</span>
      </div>
      <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
        {[25, 50, 75].map((gy) => (
          <line key={gy} x1={0} x2={width} y1={height - (gy / 100) * (height - 6) - 3} y2={height - (gy / 100) * (height - 6) - 3} stroke="currentColor" className="text-slate-200 dark:text-slate-800" strokeWidth={0.5} />
        ))}
        {series.map((s) => (
          <path key={s.key} d={buildPath(s.key)} fill="none" stroke={s.color} strokeWidth={1.2} vectorEffect="non-scaling-stroke" strokeLinecap="round" strokeLinejoin="round" />
        ))}
      </svg>
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
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [onCallNames, setOnCallNames] = useState<string[] | null>(null);
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
        setLastUpdated(new Date());
      })
      .catch(() => setConnError(true));
  }, []);

  useEffect(() => {
    loadAll();
    const t = setInterval(loadAll, POLL_MS);
    return () => clearInterval(t);
  }, [loadAll]);

  // Who's on call right now -- a NOC wallboard's classic "who do I call"
  // answer, previously nowhere on this screen at all. Separate,
  // much slower poll than the main loadAll: a rotation's current holder
  // only ever changes at a shift handover, not every 20s, and this is
  // one extra request per enabled schedule (GET .../current has no
  // batch form), so there's no reason to hit it on the same cadence as
  // alerts/topology.
  useEffect(() => {
    let mounted = true;
    const loadOnCall = () => {
      api
        .get<OnCallSchedule[]>("/on-call-schedules")
        .then((res) => {
          const enabled = res.data.filter((s) => s.enabled);
          return Promise.all(
            enabled.map((s) =>
              api
                .get<{ schedule_id: string; current_contact: string | null; is_secondary: boolean }>(
                  `/on-call-schedules/${s.id}/current`
                )
                .then((r) => r.data.current_contact)
                .catch(() => null)
            )
          );
        })
        .then((contacts) => {
          if (!mounted) return;
          setOnCallNames(contacts.filter((c): c is string => !!c));
        })
        .catch(() => {
          if (mounted) setOnCallNames(null);
        });
    };
    loadOnCall();
    const t = setInterval(loadOnCall, 5 * 60 * 1000);
    return () => {
      mounted = false;
      clearInterval(t);
    };
  }, []);

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), CLOCK_MS);
    return () => clearInterval(t);
  }, []);

  // Keep the display awake -- the entire point of a wall board is that
  // nobody's touching the mouse/keyboard, which is exactly what a screen
  // saver / display sleep timer looks for. Re-acquire on tab visibility
  // change since the OS releases the lock whenever the tab is hidden.
  useEffect(() => {
    let wakeLock: WakeLockSentinel | null = null;
    const acquire = async () => {
      try {
        if ("wakeLock" in navigator) {
          wakeLock = await (navigator as Navigator & { wakeLock: { request: (t: "screen") => Promise<WakeLockSentinel> } }).wakeLock.request("screen");
        }
      } catch {
        // Wake lock isn't available/granted in every browser or context --
        // fail silently, the board still works, it just may need whatever
        // OS-level "never sleep" setting the kiosk machine already has.
      }
    };
    acquire();
    const onVisible = () => {
      if (document.visibilityState === "visible") acquire();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      wakeLock?.release().catch(() => {});
    };
  }, []);

  // Quiet once-a-day reload at a fixed off-hours time -- see DAILY_RELOAD_HOUR.
  useEffect(() => {
    if (now.getHours() === DAILY_RELOAD_HOUR && now.getMinutes() === 0 && now.getSeconds() === 0) {
      window.location.reload();
    }
  }, [now]);

  const dataStale = lastUpdated !== null && now.getTime() - lastUpdated.getTime() > STALE_AFTER_MS;

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
    <div
      ref={containerRef}
      className="fixed inset-0 bg-slate-100 dark:bg-slate-950 text-navy dark:text-white flex flex-col overflow-hidden font-sans bg-[radial-gradient(ellipse_at_top,_rgba(37,99,235,0.06),_transparent_60%)] dark:bg-[radial-gradient(ellipse_at_top,_rgba(37,99,235,0.10),_transparent_60%)]"
    >
      {/* --- Top bar: identity, clock, controls --- */}
      <div className="flex items-center justify-between px-5 py-2.5 border-b border-slate-200 dark:border-slate-800 shrink-0 bg-white/70 dark:bg-slate-900/40 backdrop-blur-sm">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-brandblue flex items-center justify-center shrink-0">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-white">
              <path d="M12 2l8 4v6c0 5-3.4 8.4-8 10-4.6-1.6-8-5-8-10V6l8-4z" />
            </svg>
          </div>
          <span className="font-bold text-lg tracking-tight">NetGuard6</span>
          <span className="text-slate-500 dark:text-slate-500 text-sm">NOC Wall Board</span>
          <span className="hidden md:flex items-center gap-1.5 text-[11px] font-bold px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-300 dark:bg-emerald-500/10 dark:text-emerald-400 dark:border-emerald-500/30">
            <span className="relative flex h-1.5 w-1.5">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-500 opacity-75" />
              <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-emerald-500" />
            </span>
            LIVE
          </span>
          {connError && (
            <span className="text-[11px] font-bold px-2 py-0.5 rounded-full bg-red-50 text-red-700 border border-red-300 dark:bg-red-500/20 dark:text-red-300 dark:border-red-500/40">
              Connection issue — showing last known state
            </span>
          )}
          {!connError && dataStale && (
            <span className="text-[11px] font-bold px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-300 dark:bg-amber-500/20 dark:text-amber-300 dark:border-amber-500/40">
              Data stale — last updated {timeAgo(lastUpdated ? lastUpdated.toISOString() : null)} ago
            </span>
          )}
        </div>
        <div className="flex items-center gap-4">
          <div className="hidden lg:block text-right pr-4 border-r border-slate-200 dark:border-slate-800">
            <div className="text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-500 font-bold">Fleet Health</div>
            <div className={`text-sm font-black tabular-nums leading-none mt-0.5 ${healthTone(summary?.global_health_score)}`}>
              {summary ? `${summary.global_health_score} / 100` : "—"}
            </div>
          </div>
          <div className="text-right">
            <div className="text-2xl font-black tabular-nums leading-none">
              {now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
            </div>
            <div className="text-[11px] text-slate-500 dark:text-slate-500">
              {now.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}
            </div>
          </div>
          <button
            onClick={() => setPaused((p) => !p)}
            title={paused ? "Resume auto-rotation" : "Pause auto-rotation"}
            className="w-9 h-9 rounded-lg flex items-center justify-center text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-navy dark:hover:text-white"
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
            className="w-9 h-9 rounded-lg flex items-center justify-center text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-navy dark:hover:text-white"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M8 3H5a2 2 0 00-2 2v3M16 3h3a2 2 0 012 2v3M8 21H5a2 2 0 01-2-2v-3M16 21h3a2 2 0 002-2v-3" />
            </svg>
          </button>
        </div>
      </div>

      {/* --- Odometer row: the handful of numbers a room-wide glance
          should resolve as "how close to the edge" in under a second,
          not just a flat number -- real NOC wallboard dials. --- */}
      <div className="flex items-center justify-center gap-6 px-5 py-2.5 border-b border-slate-200 dark:border-slate-800 shrink-0 bg-white/50 dark:bg-slate-900/30 overflow-x-auto">
        <RadialGauge value={summary?.global_health_score} label="Fleet Health" size={104} thresholds={{ warn: 90, bad: 70 }} />
        <RadialGauge value={summary?.fleet_health_weighted_pct} label="Availability" size={104} thresholds={{ warn: 99, bad: 95 }} />
        <RadialGauge
          value={summary?.uplink_availability?.uptime_pct ?? undefined}
          label={`Uplink Uptime${summary?.uplink_availability ? ` (${summary.uplink_availability.window_days}d)` : ""}`}
          size={104}
          thresholds={{ warn: 99.5, bad: 98 }}
        />
        <RadialGauge value={summary?.deployment_success_rate} label="Deploy Success" size={104} thresholds={{ warn: 95, bad: 80 }} />
        {summary?.fleet_health_breakdown && (
          <div className="flex flex-col items-center justify-center shrink-0 px-2">
            <div className="flex items-end gap-1 h-[72px]">
              {(
                [
                  ["healthy", summary.fleet_health_breakdown.healthy, "#10b981"],
                  ["degraded", summary.fleet_health_breakdown.degraded, "#f59e0b"],
                  ["offline", summary.fleet_health_breakdown.offline, "#ef4444"],
                  ["unknown", summary.fleet_health_breakdown.unknown, "#94a3b8"],
                ] as [string, number, string][]
              ).map(([k, v, color]) => {
                const total = Object.values(summary.fleet_health_breakdown).reduce((a, b) => a + b, 0) || 1;
                const h = Math.max(3, (v / total) * 72);
                return (
                  <div key={k} title={`${k}: ${v}`} className="flex flex-col items-center justify-end h-full w-4">
                    <span className="text-[9px] font-black tabular-nums text-slate-500 dark:text-slate-500 mb-0.5">{v}</span>
                    <div className="w-4 rounded-t-sm" style={{ height: h, backgroundColor: color }} />
                  </div>
                );
              })}
            </div>
            <div className="text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-500 font-bold text-center mt-1">Fleet Mix</div>
          </div>
        )}
      </div>

      {/* --- Always-visible stat strip -- a real NOC needs more than just
          alert counts here: fleet availability, uplink uptime, syslog
          error volume, and change/deployment activity are the numbers an
          on-call engineer actually glances up for. Wraps to a second row
          on smaller wall displays rather than clipping. --- */}
      <div className="flex flex-wrap gap-2 px-3 py-2.5 border-b border-slate-200 dark:border-slate-800 shrink-0">
        <StatTile
          label="On-Call"
          value={onCallNames === null ? "—" : onCallNames.length === 0 ? "Unstaffed" : onCallNames.join(", ")}
          tone={onCallNames && onCallNames.length === 0 ? "text-red-600 dark:text-red-400" : "text-navy dark:text-white"}
          accent={onCallNames && onCallNames.length === 0 ? "critical" : onCallNames ? "ok" : undefined}
        />
        <StatTile label="Devices Online" value={summary ? `${summary.devices_online}/${summary.devices_total}` : "—"} />
        <StatTile label="Fleet Health" value={summary ? `${summary.global_health_score}` : "—"} tone={healthTone(summary?.global_health_score)} />
        <StatTile
          label="Fleet Availability"
          value={summary?.fleet_health_weighted_pct !== undefined ? `${summary.fleet_health_weighted_pct.toFixed(1)}%` : "—"}
          tone={healthTone(summary?.fleet_health_weighted_pct)}
        />
        <StatTile label="Critical Alerts" value={criticalCount} tone={criticalCount > 0 ? "text-red-600 dark:text-red-400" : "text-navy dark:text-white"} accent={criticalCount > 0 ? "critical" : "ok"} />
        <StatTile label="Warning Alerts" value={warningCount} tone={warningCount > 0 ? "text-amber-600 dark:text-amber-400" : "text-navy dark:text-white"} accent={warningCount > 0 ? "warning" : "ok"} />
        <StatTile label="Open Incidents" value={openIncidents.length} tone={openIncidents.length > 0 ? "text-red-600 dark:text-red-400" : "text-navy dark:text-white"} accent={openIncidents.length > 0 ? "critical" : "ok"} />
        <StatTile
          label="Uplinks Up"
          value={summary?.uplink_availability ? `${summary.uplink_availability.uplinks_up}/${summary.uplink_availability.uplinks_total}` : "—"}
          tone={
            summary?.uplink_availability && summary.uplink_availability.uplinks_up < summary.uplink_availability.uplinks_total
              ? "text-amber-600 dark:text-amber-400"
              : "text-navy dark:text-white"
          }
          accent={summary?.uplink_availability && summary.uplink_availability.uplinks_up < summary.uplink_availability.uplinks_total ? "warning" : "ok"}
        />
        <StatTile label="Ports Down" value={summary?.down_ports?.length ?? "—"} tone={(summary?.down_ports?.length ?? 0) > 0 ? "text-amber-600 dark:text-amber-400" : "text-navy dark:text-white"} accent={(summary?.down_ports?.length ?? 0) > 0 ? "warning" : "ok"} />
        <StatTile label="Active Deployments" value={summary?.active_deployments ?? "—"} />
        <StatTile label="Failed Deployments" value={summary?.failed_deployments ?? "—"} tone={(summary?.failed_deployments ?? 0) > 0 ? "text-red-600 dark:text-red-400" : "text-navy dark:text-white"} accent={(summary?.failed_deployments ?? 0) > 0 ? "critical" : "ok"} />
        <StatTile label="Open Drifts" value={summary?.open_drifts ?? "—"} />
        <StatTile label="Pending Changes" value={summary?.pending_change_requests ?? "—"} />
        <StatTile label="Syslog Errors (1h)" value={syslogSummary ? syslogErrorCount : "—"} tone={syslogErrorCount > 0 ? "text-amber-600 dark:text-amber-400" : "text-navy dark:text-white"} accent={syslogErrorCount > 0 ? "warning" : "ok"} />
      </div>

      {/* --- Rotating main panel --- */}
      <div className="flex-1 min-h-0 overflow-hidden p-5">
        {activePanel === "alerts" && <AlertsPanel alerts={sortedAlerts} total={alerts.length} />}
        {activePanel === "topology" && <TopologyPanel topology={topology} summary={summary} />}
        {activePanel === "fleet" && <FleetPanel summary={summary} />}
        {activePanel === "ops" && <OpsPanel incidents={openIncidents} devicesById={devicesById} summary={summary} />}
      </div>

      {/* --- Panel indicator / manual switch, with a rotation-progress bar
          under the active tab so the room can see at a glance how long
          until it auto-advances (paused hides the bar entirely rather
          than showing a stalled one). --- */}
      <div className="flex flex-col items-center justify-center gap-1.5 py-3 border-t border-slate-200 dark:border-slate-800 shrink-0 bg-white/70 dark:bg-slate-900/40 backdrop-blur-sm">
        <div className="flex items-center gap-2">
          {PANELS.map((p) => (
            <button
              key={p.id}
              onClick={() => setActivePanel(p.id)}
              className={`relative overflow-hidden flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold transition-colors ${
                activePanel === p.id ? "bg-brandblue text-white" : "bg-slate-100 text-slate-500 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-700"
              }`}
            >
              {activePanel === p.id && !paused && (
                <span
                  key={`${p.id}-${now.getSeconds()}`}
                  className="absolute inset-0 bg-white/25 origin-left animate-[wallboard-progress_var(--rotate-ms)_linear_1]"
                  style={{ ["--rotate-ms" as string]: `${ROTATE_MS}ms` }}
                />
              )}
              <span className="relative">{p.label}</span>
            </button>
          ))}
        </div>
        <div className="text-[10px] text-slate-400 dark:text-slate-600 tabular-nums">
          {paused ? "Auto-rotation paused" : `Auto-rotating every ${ROTATE_MS / 1000}s`}
        </div>
      </div>
      <style>{`@keyframes wallboard-progress { from { transform: scaleX(0); } to { transform: scaleX(1); } }`}</style>
    </div>
  );
}

function healthTone(score: number | undefined): string {
  if (score === undefined) return "text-navy dark:text-white";
  if (score >= 90) return "text-emerald-600 dark:text-emerald-400";
  if (score >= 70) return "text-amber-600 dark:text-amber-400";
  return "text-red-600 dark:text-red-400";
}

function AlertsPanel({ alerts, total }: { alerts: Alert[]; total: number }) {
  return (
    <div className="h-full flex flex-col">
      <h2 className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">
        Top Active Alerts {total > alerts.length && <span className="text-slate-400 dark:text-slate-600">(showing {alerts.length} of {total})</span>}
      </h2>
      {alerts.length === 0 ? (
        <div className="flex-1 flex items-center justify-center text-slate-400 dark:text-slate-600 text-xl font-bold">No active alerts — fleet is quiet.</div>
      ) : (
        <div className="flex-1 min-h-0 overflow-y-auto grid grid-cols-2 gap-2.5 content-start">
          {alerts.map((a) => (
            <div key={a.id} className={`rounded-xl border px-4 py-3 flex items-start gap-3 ${SEVERITY_ROW[a.severity]}`}>
              <span className={`w-2.5 h-2.5 rounded-full mt-1.5 shrink-0 ${SEVERITY_DOT[a.severity]}`} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <div className="font-bold text-sm truncate">{a.category}</div>
                  <div className="text-[11px] text-slate-500 dark:text-slate-500 shrink-0">{timeAgo(a.last_seen_at || null)}</div>
                </div>
                {a.tenant_name && <div className="text-[11px] font-bold text-indigo-600 dark:text-indigo-400 mb-0.5">{a.tenant_name}</div>}
                <div className="text-sm text-slate-700 dark:text-slate-300 truncate">{a.message}</div>
                {a.occurrence_count && a.occurrence_count > 1 && (
                  <div className="text-[11px] text-slate-500 dark:text-slate-500 mt-0.5">×{a.occurrence_count} occurrences</div>
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
    return <div className="h-full flex items-center justify-center text-slate-400 dark:text-slate-600 text-xl font-bold">Loading topology…</div>;
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
        <h2 className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">
          Topology Health — {topology.nodes.length} devices, {topology.edges.length} links
          {unhealthy > 0 && <span className="text-red-400"> · {unhealthy} unhealthy</span>}
        </h2>
        <div className="flex-1 min-h-0 overflow-y-auto space-y-4">
          {sites.map(([site, nodes]) => (
          <div key={site}>
            <div className="text-xs font-bold text-slate-500 dark:text-slate-500 mb-1.5">{site} <span className="text-slate-400 dark:text-slate-600">({nodes.length})</span></div>
            <div className="flex flex-wrap gap-1.5">
              {nodes.map((n) => (
                <div
                  key={n.id}
                  title={`${n.hostname} — ${n.status}${n.health_color ? `, health: ${n.health_color}` : ""}${n.tenant_name ? ` (${n.tenant_name})` : ""}`}
                  className={`w-3.5 h-3.5 rounded-sm ${
                    n.status === "offline" ? STATUS_DOT.offline : n.health_color ? HEALTH_DOT[n.health_color] : STATUS_DOT[n.status] || STATUS_DOT.unknown
                  }`}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
      <div className="flex items-center gap-4 pt-3 mt-2 border-t border-slate-200 dark:border-slate-800 text-[11px] text-slate-500 dark:text-slate-500">
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-sm bg-emerald-500" /> Healthy</span>
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-sm bg-amber-400" /> Degraded</span>
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-sm bg-red-500" /> Offline / Unhealthy</span>
        <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-sm bg-slate-500" /> No data</span>
      </div>
      </div>
      {(summary?.offline_devices && summary.offline_devices.length > 0) && (
        <div className="w-80 flex flex-col shrink-0 border-l border-slate-200 dark:border-slate-800 pl-5 min-h-0">
          <h2 className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">Offline & Degraded</h2>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
            {summary.offline_devices.map(d => (
              <div key={d.id} className="rounded-lg bg-slate-50 border border-slate-200 dark:bg-slate-900/60 dark:border-slate-800 px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-bold text-sm truncate">
                    {d.hostname}
                    {d.tenant_name && <span className="ml-2 text-[10px] font-normal text-slate-500 dark:text-slate-400">({d.tenant_name})</span>}
                  </span>
                  <span className={`text-[10px] uppercase font-bold px-1.5 py-0.5 rounded border shrink-0 ${d.status === 'offline' ? 'bg-red-50 text-red-700 border-red-300 dark:bg-red-500/20 dark:text-red-300 dark:border-red-500/40' : 'bg-amber-50 text-amber-700 border-amber-300 dark:bg-amber-500/20 dark:text-amber-300 dark:border-amber-500/40'}`}>{d.status}</span>
                </div>
                {d.last_seen && <div className="text-xs text-slate-500 dark:text-slate-500 mt-1">Last seen: {timeAgo(d.last_seen)} ago</div>}
                {d.last_error && <div className="text-[10px] text-red-600 dark:text-red-400 truncate mt-0.5" title={d.last_error}>{d.last_error}</div>}
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
    return <div className="h-full flex items-center justify-center text-slate-400 dark:text-slate-600 text-xl font-bold">Loading fleet metrics…</div>;
  }
  return (
    <div className="h-full flex flex-col gap-4 min-h-0">
      <TrendChart history={summary.fleet_health_history} height={92} />
      <div className="flex-1 min-h-0 grid grid-cols-4 gap-5">
      <div className="min-h-0 flex flex-col">
        <h2 className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">Top CPU</h2>
        <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
          {summary.top_cpu_devices.length === 0 ? (
            <div className="text-slate-400 dark:text-slate-600 text-sm">No data.</div>
          ) : (
            summary.top_cpu_devices.slice(0, 8).map((d) => (
              <div key={d.hostname} className="flex items-center justify-between gap-2 rounded-lg bg-slate-50 border border-slate-200 dark:bg-slate-900/60 dark:border-slate-800 px-3 py-2">
                <span className="text-sm font-bold truncate">
                  {d.hostname}
                  {d.tenant_name && <span className="ml-2 text-[10px] font-normal text-slate-500 dark:text-slate-400">({d.tenant_name})</span>}
                </span>
                <div className="flex items-center gap-2 shrink-0">
                  <Sparkline data={d.cpu_history} color={d.cpu >= 90 ? "#ef4444" : d.cpu >= 75 ? "#f59e0b" : "#2563eb"} width={44} height={18} />
                  <span className={`text-sm font-black tabular-nums ${d.cpu >= 90 ? "text-red-600 dark:text-red-400" : d.cpu >= 75 ? "text-amber-600 dark:text-amber-400" : "text-slate-600 dark:text-slate-300"}`}>
                    {d.cpu.toFixed(0)}%
                  </span>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
      <div className="min-h-0 flex flex-col">
        <h2 className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">Top Memory</h2>
        <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
          {summary.top_memory_devices.length === 0 ? (
            <div className="text-slate-400 dark:text-slate-600 text-sm">No data.</div>
          ) : (
            summary.top_memory_devices.slice(0, 8).map((d) => (
              <div key={d.hostname} className="flex items-center justify-between gap-2 rounded-lg bg-slate-50 border border-slate-200 dark:bg-slate-900/60 dark:border-slate-800 px-3 py-2">
                <span className="text-sm font-bold truncate">
                  {d.hostname}
                  {d.tenant_name && <span className="ml-2 text-[10px] font-normal text-slate-500 dark:text-slate-400">({d.tenant_name})</span>}
                </span>
                <div className="flex items-center gap-2 shrink-0">
                  <Sparkline data={d.memory_history} color={d.memory >= 90 ? "#ef4444" : d.memory >= 75 ? "#f59e0b" : "#8b5cf6"} width={44} height={18} />
                  <span className={`text-sm font-black tabular-nums ${d.memory >= 90 ? "text-red-600 dark:text-red-400" : d.memory >= 75 ? "text-amber-600 dark:text-amber-400" : "text-slate-600 dark:text-slate-300"}`}>
                    {d.memory.toFixed(0)}%
                  </span>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
      <div className="min-h-0 flex flex-col">
        <h2 className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">Top Bandwidth</h2>
        <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
          {summary.top_bandwidth_devices.length === 0 ? (
            <div className="text-slate-400 dark:text-slate-600 text-sm">No data.</div>
          ) : (
            summary.top_bandwidth_devices.slice(0, 8).map((d) => (
              <div key={d.hostname} className="flex items-center justify-between gap-2 rounded-lg bg-slate-50 border border-slate-200 dark:bg-slate-900/60 dark:border-slate-800 px-3 py-2">
                <span className="text-sm font-bold truncate">
                  {d.hostname}
                  {d.tenant_name && <span className="ml-2 text-[10px] font-normal text-slate-500 dark:text-slate-400">({d.tenant_name})</span>}
                </span>
                <div className="flex items-center gap-2 shrink-0">
                  <Sparkline data={d.bandwidth_history} color={d.bandwidth >= 90 ? "#ef4444" : d.bandwidth >= 75 ? "#f59e0b" : "#0ea5e9"} width={44} height={18} />
                  <span className={`text-sm font-black tabular-nums ${d.bandwidth >= 90 ? "text-red-600 dark:text-red-400" : d.bandwidth >= 75 ? "text-amber-600 dark:text-amber-400" : "text-slate-600 dark:text-slate-300"}`}>
                    {d.bandwidth.toFixed(0)}%
                  </span>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
      <div className="min-h-0 flex flex-col gap-4">
        <div className="min-h-0 flex-1 flex flex-col">
          <h2 className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">
            Uplinks {summary.uplink_availability && (
              <span className="text-slate-400 dark:text-slate-600 normal-case font-medium">
                ({summary.uplink_availability.uplinks_up}/{summary.uplink_availability.uplinks_total} up
                {summary.uplink_availability.uptime_pct !== null ? `, ${summary.uplink_availability.uptime_pct.toFixed(1)}% ${summary.uplink_availability.window_days}d` : ""})
              </span>
            )}
          </h2>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
            {summary.uplinks.length === 0 ? (
              <div className="text-slate-400 dark:text-slate-600 text-sm">No uplinks configured.</div>
            ) : (
              summary.uplinks.slice(0, 4).map((u) => (
                <div key={u.hostname + (u.role || "")} className="rounded-lg bg-slate-50 border border-slate-200 dark:bg-slate-900/60 dark:border-slate-800 px-3 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-bold truncate">
                      {u.hostname}
                      {u.tenant_name && <span className="ml-2 text-[10px] font-normal text-slate-500 dark:text-slate-400">({u.tenant_name})</span>}
                    </span>
                    <span className={`w-2 h-2 rounded-full shrink-0 ${STATUS_DOT[u.status] || STATUS_DOT.unknown}`} />
                  </div>
                  <div className="flex items-center justify-between text-[11px] text-slate-500 dark:text-slate-500 mt-0.5">
                    <span>{u.role || "uplink"}</span>
                    <div className="flex items-center gap-1.5">
                      <Sparkline data={u.history} color={u.utilization_pct >= 90 ? "#ef4444" : u.utilization_pct >= 75 ? "#f59e0b" : "#10b981"} width={36} height={14} fill={false} />
                      <span className="tabular-nums">{u.utilization_pct.toFixed(0)}% · {formatBps(u.throughput_bps)}</span>
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
        <div className="min-h-0 flex-1 flex flex-col">
          <h2 className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">Top Errors</h2>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
            {summary.top_error_devices.length === 0 ? (
              <div className="text-slate-400 dark:text-slate-600 text-sm">No data.</div>
            ) : (
              summary.top_error_devices.slice(0, 4).map((d) => (
                <div key={d.hostname} className="flex items-center justify-between rounded-lg bg-slate-50 border border-slate-200 dark:bg-slate-900/60 dark:border-slate-800 px-3 py-2">
                  <span className="text-sm font-bold truncate">
                    {d.hostname}
                    {d.tenant_name && <span className="ml-2 text-[10px] font-normal text-slate-500 dark:text-slate-400">({d.tenant_name})</span>}
                  </span>
                  <span className="text-sm font-black text-amber-600 dark:text-amber-400 tabular-nums">
                    {d.interface_errors}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
      {(summary.down_ports.length > 0 || summary.flapping_interfaces.length > 0) && (
        <div className="col-span-4 flex gap-5 pt-1 border-t border-slate-200 dark:border-slate-800">
          {summary.down_ports.length > 0 && (
            <div className="flex-1 min-w-0">
              <div className="text-[11px] font-bold uppercase tracking-wider text-amber-600 dark:text-amber-400 mt-2 mb-1.5">Ports Down ({summary.down_ports.length})</div>
              <div className="flex flex-wrap gap-1.5">
                {summary.down_ports.slice(0, 12).map((p, idx) => (
                  <span key={idx} className="text-[11px] px-2 py-1 rounded-md bg-amber-50 border border-amber-300 text-amber-700 dark:bg-amber-500/10 dark:border-amber-500/30 dark:text-amber-300">
                    {p.hostname}{p.tenant_name ? ` (${p.tenant_name})` : ""} · {p.interface}
                  </span>
                ))}
              </div>
            </div>
          )}
          {summary.flapping_interfaces.length > 0 && (
            <div className="flex-1 min-w-0">
              <div className="text-[11px] font-bold uppercase tracking-wider text-red-600 dark:text-red-400 mt-2 mb-1.5">Flapping ({summary.flapping_interfaces.length})</div>
              <div className="flex flex-wrap gap-1.5">
                {summary.flapping_interfaces.slice(0, 12).map((p, idx) => (
                  <span key={idx} className="text-[11px] px-2 py-1 rounded-md bg-red-50 border border-red-300 text-red-700 dark:bg-red-500/10 dark:border-red-500/30 dark:text-red-300">
                    {p.hostname}{p.tenant_name ? ` (${p.tenant_name})` : ""} · {p.interface} ×{p.flap_count}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
      </div>
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
        <h2 className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">Open Incidents ({incidents.length})</h2>
        {incidents.length === 0 ? (
          <div className="flex-1 flex items-center justify-center text-slate-400 dark:text-slate-600 text-lg font-bold">No open incidents.</div>
        ) : (
          <div className="flex-1 min-h-0 overflow-y-auto space-y-2">
            {incidents.map((inc) => (
              <div key={inc.id} className="rounded-xl border border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900/60 px-4 py-3 flex items-center gap-4">
                <span className={`text-[11px] font-bold px-2 py-0.5 rounded-full border shrink-0 ${INCIDENT_SEVERITY_BADGE[inc.severity] || INCIDENT_SEVERITY_BADGE.minor}`}>
                  {inc.severity.toUpperCase()}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="font-bold truncate">
                    {inc.title}
                    {inc.tenant_name && <span className="ml-2 text-xs font-normal text-indigo-600 dark:text-indigo-400">({inc.tenant_name})</span>}
                  </div>
                  <div className="text-xs text-slate-500 dark:text-slate-500">
                    {inc.alert_ids.length} alert{inc.alert_ids.length === 1 ? "" : "s"} · status: {inc.status.replace(/_/g, " ")}
                  </div>
                </div>
                <div className="text-xs text-slate-500 dark:text-slate-500 shrink-0">open {timeAgo(inc.detected_at || inc.created_at)}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="min-h-0 flex flex-col gap-4">
        <div>
          <h2 className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-2">Deployment Activity</h2>
          <div className="grid grid-cols-3 gap-2">
            <div className="rounded-lg bg-slate-50 border border-slate-200 dark:bg-slate-900/60 dark:border-slate-800 px-3 py-2">
              <div className="text-lg font-black tabular-nums">{summary?.active_deployments ?? "—"}</div>
              <div className="text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-500 font-bold">In flight</div>
            </div>
            <div className="rounded-lg bg-slate-50 border border-slate-200 dark:bg-slate-900/60 dark:border-slate-800 px-3 py-2">
              <div className={`text-lg font-black tabular-nums ${(summary?.failed_deployments ?? 0) > 0 ? "text-red-600 dark:text-red-400" : ""}`}>{summary?.failed_deployments ?? "—"}</div>
              <div className="text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-500 font-bold">Failed</div>
            </div>
            <div className="rounded-lg bg-slate-50 border border-slate-200 dark:bg-slate-900/60 dark:border-slate-800 px-3 py-2">
              <div className="text-lg font-black tabular-nums">{summary?.deployment_success_rate !== undefined ? `${summary.deployment_success_rate.toFixed(0)}%` : "—"}</div>
              <div className="text-[10px] uppercase tracking-wider text-slate-500 dark:text-slate-500 font-bold">Success rate</div>
            </div>
          </div>
        </div>

        <div className="min-h-0 flex-1 flex flex-col">
          <h2 className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-2">Recent Config Backups</h2>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-1.5">
            {!summary || summary.recent_backups.length === 0 ? (
              <div className="text-slate-400 dark:text-slate-600 text-sm">No recent backups.</div>
            ) : (
              summary.recent_backups.slice(0, 6).map((b) => (
                <div key={b.id} className="flex items-center justify-between text-xs bg-slate-50 border border-slate-200 dark:bg-slate-900/60 dark:border-slate-800 rounded-lg px-3 py-1.5">
                  <span className="font-bold truncate">{b.hostname}</span>
                  <span className="text-slate-500 dark:text-slate-500 tabular-nums shrink-0">{timeAgo(b.created_at)} ago</span>
                </div>
              ))
            )}
          </div>
        </div>

      </div>
      <div className="min-h-0 flex flex-col gap-4">
        <div className="min-h-0 flex-1 flex flex-col">
          <h2 className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-2">Recent Automation</h2>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-1.5">
            {!summary || summary.recent_protocol_operations.length === 0 ? (
              <div className="text-slate-400 dark:text-slate-600 text-sm">No recent automation runs.</div>
            ) : (
              summary.recent_protocol_operations.slice(0, 6).map((op) => (
                <div key={op.id} className="flex items-center justify-between text-xs bg-slate-50 border border-slate-200 dark:bg-slate-900/60 dark:border-slate-800 rounded-lg px-3 py-1.5 gap-2">
                  <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${op.success ? "bg-emerald-500" : "bg-red-500"}`} />
                  <span className="font-bold truncate flex-1">{op.device_hostname} · {op.protocol} {op.operation}</span>
                  <span className="text-slate-500 dark:text-slate-500 tabular-nums shrink-0">{timeAgo(op.created_at)} ago</span>
                </div>
              ))
            )}
          </div>
        </div>
        <div className="min-h-0 flex-1 flex flex-col">
          <h2 className="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-2">Recent Reboots</h2>
          <div className="flex-1 min-h-0 overflow-y-auto space-y-1.5">
            {!summary || summary.recent_reboots.length === 0 ? (
              <div className="text-slate-400 dark:text-slate-600 text-sm">No recent reboots.</div>
            ) : (
              summary.recent_reboots.slice(0, 6).map((r) => (
                <div key={r.hostname} className="flex items-center justify-between text-xs bg-slate-50 border border-slate-200 dark:bg-slate-900/60 dark:border-slate-800 rounded-lg px-3 py-1.5 gap-2">
                  <span className="w-1.5 h-1.5 rounded-full shrink-0 bg-amber-400" />
                  <span className="font-bold truncate flex-1">
                    {r.hostname}
                    {r.tenant_name && <span className="ml-2 text-[10px] font-normal text-slate-500 dark:text-slate-400">({r.tenant_name})</span>}
                  </span>
                  <span className="text-slate-500 dark:text-slate-500 tabular-nums shrink-0">Up {Math.floor(r.uptime_seconds / 60)}m</span>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}