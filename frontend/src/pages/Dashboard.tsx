import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  RadialBar,
  RadialBarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../lib/api";
import { DashboardSummary, Alert, DeviceGroup, Device, FleetAvailabilitySummary, UnstableDevice, DashboardPreferenceResponse, DashboardLayoutEntry, DashboardWidgetInfo, DashboardThresholds, MetricThreshold, TimelineEvent } from "../lib/types";
import Sparkline from "../components/Sparkline";
import DashboardCustomizePanel from "../components/DashboardCustomizePanel"; // Trigger HMR
import { useAuth } from "../lib/auth";

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

const SEV_STYLES: Record<string, { dot: string; text: string; bg: string }> = {
  critical: { dot: "bg-red-500", text: "text-red-700", bg: "bg-red-50 border-red-100" },
  warning: { dot: "bg-amber-500", text: "text-amber-700", bg: "bg-amber-50 border-amber-100" },
  info: { dot: "bg-blue-500", text: "text-blue-700", bg: "bg-blue-50 border-blue-100" },
};

const DEFAULT_THRESHOLDS: DashboardThresholds = {
  cpu: { warn: 70, critical: 90 },
  memory: { warn: 75, critical: 90 },
  bandwidth: { warn: 70, critical: 90 },
};

// Colors a metric value against a user-configurable warn/critical band
// (falls back to `base` below the warn line) -- used by the CPU/RAM
// gauges and the Top CPU/Memory/Bandwidth widgets so "high" reflects what
// each admin actually set in Customize > Alert Thresholds, not a
// hardcoded 70/90 split baked into the component.
function bandColor(value: number, band: MetricThreshold, base: string): string {
  if (value >= band.critical) return "#EF4444";
  if (value >= band.warn) return "#F59E0B";
  return base;
}
function bandTextClass(value: number, band: MetricThreshold, base = "text-slate-800 dark:text-slate-100"): string {
  if (value >= band.critical) return "text-red-600";
  if (value >= band.warn) return "text-amber-600";
  return base;
}

const TIMELINE_ICON: Record<string, { fg: string; bg: string; icon: React.ReactNode }> = {
  alert: { fg: "text-amber-500", bg: "bg-amber-50", icon: <path d="M12 3l9 16H3L12 3zM12 10v4M12 17h.01" strokeLinecap="round" strokeLinejoin="round" /> },
  change_request: { fg: "text-indigo-500", bg: "bg-indigo-50", icon: <path d="M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4 12.5-12.5z" strokeLinecap="round" strokeLinejoin="round" /> },
  drift: { fg: "text-cyan-500", bg: "bg-cyan-50", icon: <path d="M8 3v4M16 3v4M3 9h18M5 5h14a2 2 0 012 2v12a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2z" strokeLinecap="round" strokeLinejoin="round" /> },
  deployment: { fg: "text-emerald-500", bg: "bg-emerald-50", icon: <path d="M4 12l8-8 8 8M12 4v16" strokeLinecap="round" strokeLinejoin="round" /> },
  // Syslog message-rate spikes and traffic bandwidth spikes both land here
  // as "anomaly" -- see syslog_service.detect_message_rate_anomalies /
  // flow_service.detect_bandwidth_anomalies. Distinct color so a spike
  // reads as "something worth a second look" without implying a
  // confirmed alert the way the amber alert icon does.
  anomaly: { fg: "text-rose-500", bg: "bg-rose-50", icon: <path d="M13 2L3 14h7l-1 8 11-13h-8l1-7z" strokeLinecap="round" strokeLinejoin="round" /> },
};

// "What Changed" now spans 5 event types (alert/change/drift/deploy/
// anomaly), so a host mid-incident -- flapping, getting remediated,
// drifting and re-alerting -- can easily produce a run of events that
// pushes everything else in the widget's fixed-height scroll area out
// of view. Past this many events for the same host in the fetched
// (recent, already time-bounded by the /dashboard/timeline limit)
// window, collapse them into one "hostname: N events" row instead of
// a flat list, so one noisy device doesn't dominate the timeline.
const TIMELINE_GROUP_THRESHOLD = 3;

type TimelineRow =
  | { kind: "single"; event: TimelineEvent }
  | { kind: "group"; hostname: string; events: TimelineEvent[] };

const TIMELINE_TYPE_LABEL: Record<string, string> = {
  alert: "alert",
  change_request: "change",
  drift: "drift",
  deployment: "deploy",
  anomaly: "anomaly",
};

function groupTimelineEvents(events: TimelineEvent[]): TimelineRow[] {
  const counts = new Map<string, number>();
  for (const ev of events) {
    if (ev.hostname) counts.set(ev.hostname, (counts.get(ev.hostname) || 0) + 1);
  }
  const seen = new Set<string>();
  const rows: TimelineRow[] = [];
  for (const ev of events) {
    if (ev.hostname && (counts.get(ev.hostname) || 0) > TIMELINE_GROUP_THRESHOLD) {
      if (seen.has(ev.hostname)) continue;
      seen.add(ev.hostname);
      rows.push({ kind: "group", hostname: ev.hostname, events: events.filter((e) => e.hostname === ev.hostname) });
    } else {
      rows.push({ kind: "single", event: ev });
    }
  }
  return rows;
}

interface GroupStat {
  id: string;
  name: string;
  online: number;
  offline: number;
  total: number;
}

function Card({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-2xl shadow-sm ${className}`}>{children}</div>;
}

// Small semicircular gauge (0-100) used for CPU / Memory / Health — a
// real progress ring, not a stylised HUD element.
function Gauge({ label, value, color, dark }: { label: string; value: number; color: string; dark?: boolean }) {
  const data = [{ value: Math.max(0, Math.min(value, 100)) }];
  return (
    <div className="flex flex-col items-center min-w-0 w-full">
      <div className="w-full max-w-24 h-16 relative">
        <ResponsiveContainer width="100%" height="100%">
          <RadialBarChart
            innerRadius="70%"
            outerRadius="100%"
            data={data}
            startAngle={180}
            endAngle={0}
            barSize={9}
          >
            <RadialBar dataKey="value" cornerRadius={6} fill={color} background={{ fill: dark ? "#1E293B" : "#EEF2F7" }} />
          </RadialBarChart>
        </ResponsiveContainer>
        <div className="absolute inset-0 flex items-end justify-center pb-1">
          <span className="text-lg font-bold text-slate-800 dark:text-slate-100">{Math.round(value)}%</span>
        </div>
      </div>
      <p className="text-[11px] text-slate-400 font-medium mt-0.5">{label}</p>
    </div>
  );
}

const STAT_ICONS: Record<string, { bg: string; fg: string; icon: React.ReactNode }> = {
  devices: { bg: "bg-indigo-50", fg: "text-indigo-500", icon: <path d="M4 6h16M4 12h16M4 18h10" strokeLinecap="round" /> },
  online: { bg: "bg-emerald-50", fg: "text-emerald-500", icon: <path d="M5 12l4 4L19 6" strokeLinecap="round" strokeLinejoin="round" /> },
  offline: { bg: "bg-red-50", fg: "text-red-500", icon: <path d="M18 6L6 18M6 6l12 12" strokeLinecap="round" /> },
  cpu: { bg: "bg-cyan-50", fg: "text-cyan-500", icon: <path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3M7 7h10v10H7z" strokeLinecap="round" strokeLinejoin="round" /> },
  memory: { bg: "bg-violet-50", fg: "text-violet-500", icon: <path d="M4 7h16v10H4zM8 7v10M12 7v10M16 7v10" strokeLinecap="round" /> },
  alerts: { bg: "bg-amber-50", fg: "text-amber-500", icon: <path d="M12 3l9 16H3L12 3zM12 10v4M12 17h.01" strokeLinecap="round" strokeLinejoin="round" /> },
  // NOC-priority stat cards -- these replace the old top-row CPU/RAM
  // gauges (utilization alone rarely says "something's broken"; a down
  // port or a flapping interface does). CPU/RAM stay visible in the
  // Fleet Health and Fleet History widgets below for anyone who wants
  // the trend, just not fighting for attention at the very top.
  downports: { bg: "bg-orange-50", fg: "text-orange-500", icon: <path d="M4 12h16M4 12l4-4M4 12l4 4M20 6v12" strokeLinecap="round" strokeLinejoin="round" /> },
  flapping: { bg: "bg-pink-50", fg: "text-pink-500", icon: <path d="M3 12h4l2-7 4 14 2-7h6" strokeLinecap="round" strokeLinejoin="round" /> },
};

function StatCard({
  iconKey,
  value,
  label,
  sublabel,
  valueClass = "text-slate-800 dark:text-slate-100",
}: {
  iconKey: keyof typeof STAT_ICONS;
  value: React.ReactNode;
  label: string;
  sublabel?: string;
  valueClass?: string;
}) {
  const cfg = STAT_ICONS[iconKey];
  return (
    <Card className="p-5">
      <div className={`w-9 h-9 rounded-lg ${cfg.bg} ${cfg.fg} flex items-center justify-center mb-4 dark:bg-opacity-20`}>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">{cfg.icon}</svg>
      </div>
      <p className={`text-2xl font-bold ${valueClass}`}>{value}</p>
      <p className="text-[11px] font-semibold text-slate-400 dark:text-slate-500 uppercase tracking-wide mt-1">{label}</p>
      {sublabel && <p className="text-[11px] text-slate-400 dark:text-slate-500 mt-0.5">{sublabel}</p>}
    </Card>
  );
}

// Grid span for each customizable widget id, in twelfths -- literal
// class strings (not dynamically built) so Tailwind's content scanner
// picks them up.
//
// Previously this was two tiers (`md:` 2-col, `xl:` 6-col) with NO tier
// in between, so any viewport from 768px up to 1280px (i.e. most laptop
// screens, and exactly what a NOC operator is usually on) fell back to
// the crude 2-col grid. On top of that, the 2-col spans didn't sum
// correctly for several rows -- e.g. uplinks(2) + active_alerts(1) = 3
// against a 2-col grid, leaving a half-empty dangling row -- which is
// the misaligned look on the dashboard. Fixed by switching to a single
// 12-col grid from `sm:` upward: every pair of spans below sums to
// exactly 12 at both the `sm:` (tablet, stacked pairs) and `lg:`
// (desktop, the "real" intended layout) tier, so there's no dead tier
// where things don't line up.
const WIDGET_SPAN: Record<string, string> = {
  fleet_health: "sm:col-span-6 lg:col-span-6",
  fleet_history_chart: "sm:col-span-6 lg:col-span-6",
  uplinks: "sm:col-span-6 lg:col-span-8",
  uplink_availability: "sm:col-span-6 lg:col-span-4",
  ipam_overview: "sm:col-span-6 lg:col-span-6",
  active_alerts: "sm:col-span-6 lg:col-span-4",
  fleet_availability: "sm:col-span-3 lg:col-span-4",
  top_flapping_devices: "sm:col-span-6 lg:col-span-8",
  offline_devices: "sm:col-span-3 lg:col-span-4",
  top_interface_errors: "sm:col-span-3 lg:col-span-4",
  flapping_interfaces: "sm:col-span-6 lg:col-span-4",
  top_cpu_devices: "sm:col-span-3 lg:col-span-4",
  top_memory_devices: "sm:col-span-3 lg:col-span-4",
  top_bandwidth_devices: "sm:col-span-6 lg:col-span-4",
  down_ports: "sm:col-span-3 lg:col-span-6",
  recent_reboots: "sm:col-span-3 lg:col-span-6",
  recent_backups: "sm:col-span-3 lg:col-span-6",
  recent_protocol_operations: "sm:col-span-3 lg:col-span-6",
  group_availability: "sm:col-span-6 lg:col-span-12",
  whats_changed: "sm:col-span-6 lg:col-span-6",
};

export default function Dashboard() {
  const { user } = useAuth();
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [recentAlerts, setRecentAlerts] = useState<Alert[]>([]);
  const [groupStats, setGroupStats] = useState<GroupStat[]>([]);
  const [availability, setAvailability] = useState<FleetAvailabilitySummary | null>(null);
  const [unstableDevices, setUnstableDevices] = useState<UnstableDevice[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [loading, setLoading] = useState(true);

  // --- Dashboard widget customization ---
  const [layout, setLayout] = useState<DashboardLayoutEntry[]>([]);
  const [availableWidgets, setAvailableWidgets] = useState<DashboardWidgetInfo[]>([]);
  const [showCustomize, setShowCustomize] = useState(false);
  const [savingLayout, setSavingLayout] = useState(false);
  const [thresholds, setThresholds] = useState<DashboardThresholds>(DEFAULT_THRESHOLDS);
  const [timelineEvents, setTimelineEvents] = useState<TimelineEvent[]>([]);
  const [expandedTimelineGroups, setExpandedTimelineGroups] = useState<Set<string>>(new Set());
  const timelineRows = useMemo(() => groupTimelineEvents(timelineEvents), [timelineEvents]);
  const [prefsError, setPrefsError] = useState<string | null>(null);

  const loadPreferences = () => {
    api
      .get<DashboardPreferenceResponse>("/dashboard/preferences")
      .then((res) => {
        setLayout(res.data.layout);
        setAvailableWidgets(res.data.available_widgets);
        setThresholds(res.data.thresholds || DEFAULT_THRESHOLDS);
        setPrefsError(null);
      })
      .catch(() => {
        // Previously swallowed silently -- a failure here (e.g. the API
        // rejecting the request because of a pending DB migration) left
        // both `layout` and `availableWidgets` empty, which made the
        // whole widget grid disappear with no explanation and nothing
        // to pick from in Customize. Surface it instead.
        setPrefsError("Couldn't load your dashboard layout — showing the API error below instead of an empty page.");
      });
  };

  const saveDashboardPrefs = (next: DashboardLayoutEntry[], nextThresholds: DashboardThresholds) => {
    setSavingLayout(true);
    api
      .put<DashboardPreferenceResponse>("/dashboard/preferences", { layout: next, thresholds: nextThresholds })
      .then((res) => {
        setLayout(res.data.layout);
        setAvailableWidgets(res.data.available_widgets);
        setThresholds(res.data.thresholds || DEFAULT_THRESHOLDS);
      })
      .finally(() => setSavingLayout(false));
  };

  const loadAll = () => {
    api
      .get<DashboardSummary>("/dashboard/summary")
      .then((res) => {
        setSummary(res.data);
        setError(null);
        setLastUpdated(new Date());
      })
      .catch(() => setError("Could not reach the NetGuard API."))
      .finally(() => setLoading(false));

    api.get<Alert[]>("/alerts?status=active&limit=6").then((res) => setRecentAlerts(res.data)).catch(() => {});

    api
      .get<FleetAvailabilitySummary>("/metrics/fleet-availability", { params: { hours: 24 } })
      .then((res) => setAvailability(res.data))
      .catch(() => {});

    api
      .get<UnstableDevice[]>("/metrics/unstable-devices", { params: { hours: 24, limit: 6 } })
      .then((res) => setUnstableDevices(res.data))
      .catch(() => {});

    api
      .get<TimelineEvent[]>("/dashboard/timeline", { params: { limit: 15 } })
      .then((res) => setTimelineEvents(res.data))
      .catch(() => {});

    api
      .get<DeviceGroup[]>("/device-groups")
      .then(async (res) => {
        const groups = res.data.slice(0, 8);
        const withCounts = await Promise.all(
          groups.map(async (g) => {
            try {
              const devRes = await api.get<Device[]>(`/device-groups/${g.id}/devices`);
              const online = devRes.data.filter((d) => d.status === "online").length;
              return { id: g.id, name: g.name, online, offline: devRes.data.length - online, total: devRes.data.length };
            } catch {
              return { id: g.id, name: g.name, online: 0, offline: g.device_count, total: g.device_count };
            }
          })
        );
        setGroupStats(withCounts);
      })
      .catch(() => {});
  };

  useEffect(() => {
    loadAll();
    loadPreferences();
    const interval = setInterval(loadAll, 15000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const hour = new Date().getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  const online = summary?.devices_online ?? 0;
  const total = summary?.devices_total ?? 0;
  const offline = Math.max(total - online, 0);
  const onlinePct = total > 0 ? (online / total) * 100 : 100;

  const lastHistory = summary?.fleet_health_history?.[summary.fleet_health_history.length - 1];
  const avgCpu = lastHistory?.avg_cpu ?? 0;
  const avgMem = lastHistory?.avg_memory ?? 0;
  const cpuHistory = (summary?.fleet_health_history || []).map((h) => h.avg_cpu ?? 0);
  const memHistory = (summary?.fleet_health_history || []).map((h) => h.avg_memory ?? 0);

  // Severity-weighted health -- a device that's "online" but has a
  // flapping port, a down interface, or is already flagged unstable isn't
  // healthy, so the headline number blends those signals in rather than
  // just counting reachability. See backend fleet_health_weighted_pct.
  const breakdown = summary?.fleet_health_breakdown;
  const weightedHealthPct = summary?.fleet_health_weighted_pct ?? onlinePct;
  const donutData = [
    { name: "Healthy", value: breakdown?.healthy ?? online, color: "#10B981" },
    { name: "Degraded", value: breakdown?.degraded ?? 0, color: "#F59E0B" },
    { name: "Offline", value: (breakdown?.offline ?? offline) + (breakdown?.unknown ?? 0), color: "#EF4444" },
  ].filter((d) => d.value > 0);

  const totalGroups = groupStats.length;

  if (loading && !summary) {
    return (
      <div className="flex items-center justify-center h-96 text-slate-400 text-sm">Loading dashboard…</div>
    );
  }

  return (
    <div className="font-sans">
      {/* ---- Header --------------------------------------------------- */}
      <div className="flex items-start justify-between gap-4 flex-wrap mb-6">
        <div>
          <h1 className="text-2xl font-bold text-slate-800 dark:text-slate-100">
            {greeting}, <span className="text-brandblue">{user ? user.full_name.split(" ")[0] : "there"}</span>
          </h1>
          <p className="text-sm text-slate-400 mt-1">
            {new Date().toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" })}
            {lastUpdated && <> · refreshed {timeAgo(lastUpdated.toISOString())}</>}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowCustomize(true)}
            className="h-9 px-3 rounded-full bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 shadow-sm flex items-center gap-1.5 text-xs font-semibold text-slate-500 dark:text-slate-400 hover:text-brandblue hover:border-brandblue/40 transition-colors"
            title="Customize dashboard widgets"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            Customize
          </button>
          <button
            onClick={loadAll}
            className="w-9 h-9 rounded-full bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 shadow-sm flex items-center justify-center text-slate-400 hover:text-brandblue hover:border-brandblue/40 transition-colors"
            title="Refresh"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 12a9 9 0 11-3-6.7M21 4v6h-6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </div>
      </div>

      {error && (
        <Card className="p-4 mb-6 border-red-200 bg-red-50 text-sm text-red-700">
          {error} Make sure the backend is running at{" "}
          <code className="bg-white px-2 py-0.5 rounded border border-red-200 ml-1">{import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1"}</code>.
        </Card>
      )}

      {prefsError && (
        <Card className="p-4 mb-6 border-amber-200 bg-amber-50 text-sm text-amber-800">
          {prefsError}
        </Card>
      )}

      {/* ---- Stat card row ---------------------------------------------- */}
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4 mb-6">
        <StatCard iconKey="devices" value={total} label="Devices" sublabel={`${totalGroups} groups`} />
        <StatCard
          iconKey="online"
          value={`${weightedHealthPct.toFixed(0)}%`}
          label="Fleet Health"
          valueClass={weightedHealthPct >= 95 ? "text-emerald-600" : weightedHealthPct >= 80 ? "text-amber-600" : "text-red-600"}
          sublabel={`${online}/${total} online${breakdown?.degraded ? ` · ${breakdown.degraded} flaky` : ""}`}
        />
        <StatCard iconKey="offline" value={offline} label="Offline" valueClass={offline > 0 ? "text-red-600" : "text-slate-800"} sublabel={`${summary?.critical_alerts ?? 0} critical`} />
        <Link to="/devices" className="block">
          <StatCard
            iconKey="downports"
            value={summary?.down_ports?.length ?? 0}
            label="Down Ports"
            valueClass={(summary?.down_ports?.length ?? 0) > 0 ? "text-orange-600" : "text-slate-800"}
            sublabel="right now, via SNMP"
          />
        </Link>
        <Link to="/topology" className="block">
          <StatCard
            iconKey="flapping"
            value={summary?.flapping_interfaces?.length ?? 0}
            label="Flapping"
            valueClass={(summary?.flapping_interfaces?.length ?? 0) > 0 ? "text-pink-600" : "text-slate-800"}
            sublabel="interfaces, last 24h"
          />
        </Link>
        <StatCard
          iconKey="alerts"
          value={(summary?.critical_alerts ?? 0) + (summary?.warning_alerts ?? 0)}
          label="Alerts"
          valueClass={(summary?.critical_alerts ?? 0) > 0 ? "text-amber-600" : "text-slate-800"}
          sublabel={`${summary?.critical_alerts ?? 0} critical`}
        />
      </div>

      {(() => {
        const widgetContent: Record<string, React.ReactNode> = {
          fleet_health: (
            <Card className="p-6 h-full">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-4">Fleet Health</p>
              <div className="flex items-center gap-6">
                <div className="w-32 h-32 relative shrink-0">
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie data={donutData.length ? donutData : [{ name: "No data", value: 1, color: "#E2E8F0" }]} dataKey="value" innerRadius={44} outerRadius={60} startAngle={90} endAngle={-270} strokeWidth={0}>
                        {(donutData.length ? donutData : [{ color: "#E2E8F0" }]).map((d, i) => (
                          <Cell key={i} fill={d.color} />
                        ))}
                      </Pie>
                    </PieChart>
                  </ResponsiveContainer>
                  <div className="absolute inset-0 flex flex-col items-center justify-center">
                    <span className={`text-2xl font-bold ${weightedHealthPct >= 95 ? "text-emerald-600" : weightedHealthPct >= 80 ? "text-amber-600" : "text-red-600"}`}>{weightedHealthPct.toFixed(0)}%</span>
                    <span className="text-[10px] text-slate-400 uppercase tracking-wide">Health</span>
                  </div>
                </div>
                <div className="flex-1 space-y-2.5 min-w-0">
                  <div className="flex items-center justify-between text-sm">
                    <span className="flex items-center gap-2 text-slate-500 dark:text-slate-400"><span className="w-2.5 h-2.5 rounded-full bg-emerald-500" />Healthy</span>
                    <span className="font-semibold text-slate-800 dark:text-slate-100">{breakdown?.healthy ?? online}</span>
                  </div>
                  <div className="flex items-center justify-between text-sm">
                    <span className="flex items-center gap-2 text-slate-500 dark:text-slate-400"><span className="w-2.5 h-2.5 rounded-full bg-amber-500" />Degraded <span className="text-[10px] text-slate-300 dark:text-slate-600">(flapping/unstable)</span></span>
                    <span className="font-semibold text-slate-800 dark:text-slate-100">{breakdown?.degraded ?? 0}</span>
                  </div>
                  <div className="flex items-center justify-between text-sm">
                    <span className="flex items-center gap-2 text-slate-500 dark:text-slate-400"><span className="w-2.5 h-2.5 rounded-full bg-red-500" />Offline</span>
                    <span className="font-semibold text-slate-800 dark:text-slate-100">{(breakdown?.offline ?? offline) + (breakdown?.unknown ?? 0)}</span>
                  </div>
                  <div className="h-px bg-slate-100 dark:bg-slate-700 my-1" />
                  <div className="flex items-center justify-between text-sm">
                    <span className="text-slate-500 dark:text-slate-400">Deploy success</span>
                    <span className="font-semibold text-slate-800 dark:text-slate-100">{summary?.deployment_success_rate ?? 100}%</span>
                  </div>
                  <div className="flex items-center justify-between text-sm">
                    <span className="text-slate-500 dark:text-slate-400">Open drifts</span>
                    <span className="font-semibold text-slate-800 dark:text-slate-100">{summary?.open_drifts ?? 0}</span>
                  </div>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-2 mt-6 pt-5 border-t border-slate-100 dark:border-slate-700">
                <Gauge label="CPU" value={avgCpu} color={bandColor(avgCpu, thresholds.cpu, "#06B6D4")} />
                <Gauge label="RAM" value={avgMem} color={bandColor(avgMem, thresholds.memory, "#8B5CF6")} />
                <Gauge label="HEALTH" value={summary?.global_health_score ?? 100} color="#10B981" />
              </div>
            </Card>
          ),

          fleet_history_chart: (
            <Card className="p-6 h-full">
              <div className="flex items-center justify-between mb-4">
                <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">Fleet History — CPU / Memory / Bandwidth</p>
                <Link to="/traffic-analysis" className="text-xs text-brandblue font-medium hover:underline">Details →</Link>
              </div>
              {(summary?.fleet_health_history?.length ?? 0) === 0 ? (
                <div className="h-64 flex items-center justify-center text-sm text-slate-400">Not enough polling history yet.</div>
              ) : (
                <div className="h-64">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={summary?.fleet_health_history}>
                      <defs>
                        <linearGradient id="cpuFillL" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor="#06B6D4" stopOpacity={0.25} />
                          <stop offset="95%" stopColor="#06B6D4" stopOpacity={0.01} />
                        </linearGradient>
                        <linearGradient id="memFillL" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor="#8B5CF6" stopOpacity={0.2} />
                          <stop offset="95%" stopColor="#8B5CF6" stopOpacity={0.01} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1E293B" vertical={false} />
                      <XAxis dataKey="timestamp" tickFormatter={(v) => (v ? new Date(v).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "")} tick={{ fontSize: 10, fill: "#64748B" }} minTickGap={40} axisLine={false} tickLine={false} />
                      <YAxis tickFormatter={(v) => `${v}%`} tick={{ fontSize: 10, fill: "#64748B" }} width={34} domain={[0, 100]} axisLine={false} tickLine={false} />
                      <Tooltip
                        contentStyle={{ background: "#1E293B", border: "1px solid #334155", borderRadius: 8, fontSize: 12, color: "#E2E8F0" }}
                        formatter={(v: number, name: string) => [`${v?.toFixed?.(1) ?? v}%`, name]}
                        labelFormatter={(v) => (v ? new Date(v).toLocaleString() : "")}
                      />
                      <Area type="monotone" dataKey="avg_cpu" name="CPU" stroke="#06B6D4" fill="url(#cpuFillL)" strokeWidth={2} connectNulls />
                      <Area type="monotone" dataKey="avg_memory" name="Memory" stroke="#8B5CF6" fill="url(#memFillL)" strokeWidth={2} connectNulls />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              )}
            </Card>
          ),

          whats_changed: (
            <Card className="p-6 h-full">
              <div className="flex items-center justify-between mb-4">
                <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">What Changed</p>
                <span className="text-[11px] text-slate-300">alerts · changes · drift · deploys · anomalies</span>
              </div>
              {timelineEvents.length === 0 ? (
                <div className="text-center py-8">
                  <p className="text-sm font-semibold text-emerald-600">Quiet</p>
                  <p className="text-xs text-slate-400 mt-1">Nothing new since your last visit.</p>
                </div>
              ) : (
                <div className="space-y-1 max-h-72 overflow-y-auto pr-1">
                  {timelineRows.map((row, i) => {
                    if (row.kind === "single") {
                      const ev = row.event;
                      const ic = TIMELINE_ICON[ev.type] || TIMELINE_ICON.alert;
                      return (
                        <Link
                          key={i}
                          to={ev.link}
                          className="flex items-start gap-2.5 rounded-lg px-2 py-2 hover:bg-slate-50 dark:hover:bg-slate-700/50 transition-colors"
                        >
                          <span className={`w-6 h-6 rounded-md ${ic.bg} ${ic.fg} flex items-center justify-center shrink-0 mt-0.5`}>
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">{ic.icon}</svg>
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center justify-between gap-2">
                              <p className="text-xs font-medium text-slate-700 dark:text-slate-200 truncate">{ev.title}</p>
                              <span className="text-[10px] text-slate-400 shrink-0">{timeAgo(ev.timestamp)}</span>
                            </div>
                            <p className="text-[11px] text-slate-400 truncate">
                              {ev.hostname ? `${ev.hostname} · ` : ""}
                              {ev.detail || ""}
                            </p>
                          </div>
                        </Link>
                      );
                    }

                    // Grouped: several events on the same host in this
                    // window -- one summary row with a type breakdown,
                    // expandable to the individual (still-linkable) events.
                    const isExpanded = expandedTimelineGroups.has(row.hostname);
                    const typeCounts = new Map<string, number>();
                    for (const ev of row.events) typeCounts.set(ev.type, (typeCounts.get(ev.type) || 0) + 1);
                    const typeSummary = Array.from(typeCounts.entries())
                      .map(([t, n]) => `${n} ${TIMELINE_TYPE_LABEL[t] || t}${n > 1 ? "s" : ""}`)
                      .join(" · ");
                    const mostRecent = row.events[0]?.timestamp;
                    return (
                      <div key={`group-${row.hostname}`}>
                        <button
                          onClick={() =>
                            setExpandedTimelineGroups((prev) => {
                              const next = new Set(prev);
                              if (next.has(row.hostname)) next.delete(row.hostname);
                              else next.add(row.hostname);
                              return next;
                            })
                          }
                          className="w-full flex items-start gap-2.5 rounded-lg px-2 py-2 hover:bg-slate-50 dark:hover:bg-slate-700/50 transition-colors text-left"
                        >
                          <span className="w-6 h-6 rounded-md bg-slate-100 dark:bg-slate-700 text-slate-500 dark:text-slate-300 flex items-center justify-center shrink-0 mt-0.5 text-[10px] font-bold">
                            {row.events.length}
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center justify-between gap-2">
                              <p className="text-xs font-medium text-slate-700 dark:text-slate-200 truncate">
                                {row.hostname} <span className="text-slate-400 font-normal">— {row.events.length} events</span>
                              </p>
                              <span className="text-[10px] text-slate-400 shrink-0">
                                {mostRecent ? timeAgo(mostRecent) : ""}
                              </span>
                            </div>
                            <p className="text-[11px] text-slate-400 truncate">{typeSummary}</p>
                          </div>
                          <span className="text-slate-400 text-xs shrink-0 mt-1">{isExpanded ? "▾" : "▸"}</span>
                        </button>
                        {isExpanded && (
                          <div className="ml-8 border-l border-slate-100 dark:border-slate-700 pl-2 space-y-0.5">
                            {row.events.map((ev, j) => {
                              const ic = TIMELINE_ICON[ev.type] || TIMELINE_ICON.alert;
                              return (
                                <Link
                                  key={j}
                                  to={ev.link}
                                  className="flex items-start gap-2 rounded-lg px-2 py-1.5 hover:bg-slate-50 dark:hover:bg-slate-700/50 transition-colors"
                                >
                                  <span className={`w-5 h-5 rounded-md ${ic.bg} ${ic.fg} flex items-center justify-center shrink-0 mt-0.5`}>
                                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">{ic.icon}</svg>
                                  </span>
                                  <div className="min-w-0 flex-1">
                                    <div className="flex items-center justify-between gap-2">
                                      <p className="text-[11px] font-medium text-slate-700 dark:text-slate-200 truncate">{ev.title}</p>
                                      <span className="text-[10px] text-slate-400 shrink-0">{timeAgo(ev.timestamp)}</span>
                                    </div>
                                    {ev.detail && <p className="text-[10px] text-slate-400 truncate">{ev.detail}</p>}
                                  </div>
                                </Link>
                              );
                            })}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </Card>
          ),

          uplinks: (
            <Card className="p-6 h-full">
              <div className="flex items-center justify-between mb-4">
                <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">Uplinks &amp; WAN Links</p>
                <Link to="/devices" className="text-xs text-brandblue font-medium hover:underline">All devices →</Link>
              </div>
              {(summary?.uplinks?.length ?? 0) === 0 ? (
                <p className="text-sm text-slate-400 italic py-6 text-center">No devices tagged as WAN/uplink/core/edge yet.</p>
              ) : (
                <div className="space-y-4">
                  {summary?.uplinks?.map((link, i) => (
                    <div key={i}>
                      <div className="flex justify-between items-center gap-2 text-sm mb-1.5">
                        <span className="flex items-center gap-2 font-medium text-slate-700 dark:text-slate-200 truncate">
                          <span className={`w-2 h-2 rounded-full shrink-0 ${link.status === "online" ? "bg-emerald-500" : link.status === "offline" ? "bg-red-500" : "bg-slate-300"}`} />
                          {link.hostname}
                          <span className="text-slate-400 font-normal text-xs">{link.ip_address}</span>
                        </span>
                        <span className="text-slate-500 text-xs font-medium shrink-0">{link.utilization_pct.toFixed(0)}% util</span>
                      </div>
                      <div className="w-full h-2 rounded-full bg-slate-100 dark:bg-slate-700 overflow-hidden">
                        <div
                          className="h-full rounded-full"
                          style={{
                            width: `${Math.min(link.utilization_pct, 100)}%`,
                            backgroundColor: bandColor(link.utilization_pct, thresholds.bandwidth, "#2563EB"),
                          }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          ),

          // Headline rollup for the list above -- "N/M WAN links up" plus
          // a trailing 30-day uptime %, so the operationally important
          // number ("are we actually okay right now, and have we been
          // okay") doesn't require scanning the per-link list.
          uplink_availability: (() => {
            const ua = summary?.uplink_availability;
            const allUp = !!ua && ua.uplinks_total > 0 && ua.uplinks_up === ua.uplinks_total;
            const anyDown = !!ua && ua.uplinks_up < ua.uplinks_total;
            return (
              <Card className="p-6 h-full flex flex-col justify-center">
                <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-3">Uplink Availability</p>
                {!ua || ua.uplinks_total === 0 ? (
                  <p className="text-sm text-slate-400 italic py-4">No devices flagged as WAN/uplink yet. Mark one from the Devices page.</p>
                ) : (
                  <div className="flex items-center gap-5">
                    <div className={`w-11 h-11 rounded-full flex items-center justify-center shrink-0 ${allUp ? "bg-emerald-500/15 text-emerald-600" : "bg-red-500/15 text-red-600"}`}>
                      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                        {allUp ? <path d="M20 6L9 17l-5-5" /> : <><path d="M12 9v4" /><path d="M12 17h.01" /><circle cx="12" cy="12" r="10" /></>}
                      </svg>
                    </div>
                    <div>
                      <p className={`text-2xl font-bold ${anyDown ? "text-red-600" : "text-navy dark:text-white"}`}>
                        {ua.uplinks_up}/{ua.uplinks_total} <span className="text-sm font-medium text-slate-400">links up</span>
                      </p>
                      <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
                        {ua.uptime_pct != null ? `${ua.uptime_pct.toFixed(2)}% uptime, trailing ${ua.window_days}d` : "Uptime history not yet available"}
                      </p>
                    </div>
                  </div>
                )}
              </Card>
            );
          })(),

          // Cross-subnet IPAM rollup -- subnets at risk of running out of
          // addresses, subnets nobody has ever scanned, and how much of
          // what nmap has actually found on the wire has an OS/device-type
          // fingerprint on file (see ipam_service.fingerprint_subnet).
          ipam_overview: (() => {
            const io = summary?.ipam_overview;
            const fp = io?.fingerprint_coverage;
            const fpPct = fp && fp.total_live_hosts > 0 ? Math.round((fp.identified / fp.total_live_hosts) * 100) : null;
            return (
              <Card className="p-6 h-full">
                <div className="flex items-center justify-between mb-4">
                  <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">IPAM Utilization Overview</p>
                  <Link to="/ipam" className="text-xs text-brandblue font-medium hover:underline">Open IPAM →</Link>
                </div>
                {!io || io.total_subnets === 0 ? (
                  <p className="text-sm text-slate-400 italic py-6 text-center">No subnets configured yet.</p>
                ) : (
                  <div className="flex flex-col gap-4">
                    <div className="grid grid-cols-3 gap-3 text-center">
                      <div>
                        <p className={`text-xl font-bold ${io.near_exhaustion_count > 0 ? "text-amber-600" : "text-navy dark:text-white"}`}>{io.near_exhaustion_count}</p>
                        <p className="text-[10px] text-slate-400 uppercase tracking-wide mt-0.5">Near exhaustion</p>
                      </div>
                      <div>
                        <p className={`text-xl font-bold ${io.never_scanned_count > 0 ? "text-slate-500" : "text-navy dark:text-white"}`}>{io.never_scanned_count}</p>
                        <p className="text-[10px] text-slate-400 uppercase tracking-wide mt-0.5">Never scanned</p>
                      </div>
                      <div>
                        <p className="text-xl font-bold text-navy dark:text-white">{fpPct != null ? `${fpPct}%` : "—"}</p>
                        <p className="text-[10px] text-slate-400 uppercase tracking-wide mt-0.5">Fingerprinted</p>
                      </div>
                    </div>
                    {fp && fp.total_live_hosts > 0 && (
                      <p className="text-[11px] text-slate-400 dark:text-slate-500 -mt-1">
                        {fp.identified}/{fp.total_live_hosts} live hosts identified by OS/device-type fingerprint
                      </p>
                    )}
                    {io.near_exhaustion.length > 0 && (
                      <div className="border-t border-slate-100 dark:border-slate-700 pt-3 space-y-2">
                        {io.near_exhaustion.map((s) => (
                          <Link
                            key={s.subnet_id}
                            to="/ipam"
                            className="flex items-center justify-between text-sm hover:text-brandblue"
                          >
                            <span className="font-mono text-xs text-slate-600 dark:text-slate-300 truncate">
                              {s.cidr}
                              {s.name && <span className="text-slate-400 font-sans ml-1.5">({s.name})</span>}
                            </span>
                            <span className="text-xs font-semibold text-amber-600 shrink-0 ml-2">{s.utilization_pct.toFixed(0)}%</span>
                          </Link>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </Card>
            );
          })(),

          active_alerts: (
            <Card className="p-6 h-full">
              <div className="flex items-center justify-between mb-4">
                <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">Active Alerts</p>
                <Link to="/alerts" className="text-xs text-brandblue font-medium hover:underline">View all →</Link>
              </div>
              {recentAlerts.length === 0 ? (
                <div className="text-center py-8">
                  <p className="text-sm font-semibold text-emerald-600">All clear</p>
                  <p className="text-xs text-slate-400 mt-1">No active alerts right now.</p>
                </div>
              ) : (
                <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                  {recentAlerts.map((alert) => {
                    const s = SEV_STYLES[alert.severity] || SEV_STYLES.info;
                    return (
                      <div key={alert.id} className={`flex gap-2.5 rounded-lg px-3 py-2 border ${s.bg}`}>
                        <span className={`w-1.5 h-1.5 rounded-full mt-1.5 shrink-0 ${s.dot}`} />
                        <div className="min-w-0">
                          <p className={`text-[10px] font-semibold uppercase tracking-wide ${s.text}`}>{alert.category}</p>
                          <p className="text-xs text-slate-700 dark:text-slate-200 truncate">{alert.message}</p>
                          <p className="text-[10px] text-slate-400 mt-0.5">{timeAgo(alert.created_at)}</p>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </Card>
          ),

          fleet_availability: (
            <Card className="p-6 h-full">
              <div className="flex items-center justify-between mb-1">
                <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">Fleet Availability</p>
                <span className="text-[11px] text-slate-300 dark:text-slate-500">last 24h</span>
              </div>
              {availability === null ? (
                <div className="h-24 flex items-center justify-center text-sm text-slate-400">Loading…</div>
              ) : availability.fleet_availability_pct === null ? (
                <p className="text-sm text-slate-400 italic py-6 text-center">
                  Not enough status history yet — availability builds up as devices are polled/pinged over time.
                </p>
              ) : (
                <>
                  <p
                    className={`text-4xl font-bold mt-2 ${
                      availability.fleet_availability_pct >= 99.9
                        ? "text-emerald-600"
                        : availability.fleet_availability_pct >= 99
                        ? "text-amber-600"
                        : "text-red-600"
                    }`}
                  >
                    {availability.fleet_availability_label}
                  </p>
                  <p className="text-[11px] text-slate-400 mt-1 mb-4">
                    {availability.devices_in_rollup} device{availability.devices_in_rollup === 1 ? "" : "s"} in rollup
                  </p>
                  {availability.worst_devices.length > 0 && (
                    <div className="pt-3 border-t border-slate-100 dark:border-slate-700 space-y-1.5">
                      <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wide mb-1">Lowest availability</p>
                      {availability.worst_devices.slice(0, 4).map((d) => (
                        <div key={d.device_id} className="flex items-center justify-between text-xs">
                          <span className="text-slate-600 dark:text-slate-300 truncate">{d.hostname}</span>
                          <span
                            className={`font-semibold shrink-0 ml-2 ${
                              d.availability_pct >= 99.9 ? "text-emerald-600" : d.availability_pct >= 99 ? "text-amber-600" : "text-red-600"
                            }`}
                          >
                            {d.availability_pct.toFixed(2)}%
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </Card>
          ),

          top_flapping_devices: (
            <Card className="p-6 h-full">
              <div className="flex items-center justify-between mb-4">
                <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">Top Flapping Devices</p>
                <span className="text-[11px] text-slate-300">reachability + interface + drift · last 24h</span>
              </div>
              {unstableDevices.length === 0 ? (
                <div className="text-center py-8">
                  <p className="text-sm font-semibold text-emerald-600">Stable</p>
                  <p className="text-xs text-slate-400 mt-1">No devices are flapping right now.</p>
                </div>
              ) : (
                <div className="space-y-2.5">
                  {unstableDevices.map((d) => {
                    const maxScore = unstableDevices[0].instability_score || 1;
                    const pct = Math.min((d.instability_score / maxScore) * 100, 100);
                    return (
                      <div key={d.device_id}>
                        <div className="flex items-center justify-between text-sm mb-1">
                          <span className="font-medium text-slate-700 dark:text-slate-200 truncate">{d.hostname}</span>
                          <span className="text-[11px] text-slate-400 shrink-0 ml-2">
                            {d.reachability_flaps} reach · {d.interface_flaps} iface · {d.drift_events} drift
                          </span>
                        </div>
                        <div className="w-full h-1.5 rounded-full bg-slate-100 dark:bg-slate-700 overflow-hidden">
                          <div
                            className={`h-full rounded-full ${pct >= 66 ? "bg-red-500" : pct >= 33 ? "bg-amber-500" : "bg-brandblue"}`}
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </Card>
          ),

          offline_devices: (
            <Card className="p-6 h-full">
              <div className="flex items-center justify-between mb-4">
                <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">Offline / Degraded Devices</p>
                <Link to="/devices" className="text-xs text-brandblue font-medium hover:underline">All devices →</Link>
              </div>
              {(summary?.offline_devices?.length ?? 0) === 0 ? (
                <div className="text-center py-8">
                  <p className="text-sm font-semibold text-emerald-600">All reachable</p>
                  <p className="text-xs text-slate-400 mt-1">No offline or degraded devices right now.</p>
                </div>
              ) : (
                <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                  {summary?.offline_devices?.map((d) => (
                    <div key={d.id} className="flex items-start justify-between gap-2 text-sm">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <span className={`w-2 h-2 rounded-full shrink-0 ${d.status === "offline" ? "bg-red-500" : "bg-amber-500"}`} />
                          <span className="font-medium text-slate-700 dark:text-slate-200 truncate">{d.hostname}</span>
                        </div>
                        <p className="text-[11px] text-slate-400 ml-4 truncate">{d.ip_address}{d.last_error ? ` · ${d.last_error}` : ""}</p>
                      </div>
                      <span className="text-[11px] text-slate-400 shrink-0">{d.last_seen ? timeAgo(d.last_seen) : "never"}</span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          ),

          top_interface_errors: (
            <Card className="p-6 h-full">
              <div className="flex items-center justify-between mb-4">
                <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">Top Interface Errors</p>
                <span className="text-[11px] text-slate-300 dark:text-slate-500">by device</span>
              </div>
              {(summary?.top_error_devices?.length ?? 0) === 0 ? (
                <div className="text-center py-8">
                  <p className="text-sm font-semibold text-emerald-600">Clean</p>
                  <p className="text-xs text-slate-400 mt-1">No interface errors on the latest poll.</p>
                </div>
              ) : (
                <div className="space-y-2.5">
                  {summary?.top_error_devices?.map((d, i) => (
                    <div key={i} className="flex items-center justify-between text-sm">
                      <div className="min-w-0">
                        <span className="font-medium text-slate-700 dark:text-slate-200 truncate">{d.hostname}</span>
                        <span className="text-slate-400 font-normal text-xs ml-2">{d.ip_address}</span>
                      </div>
                      <span className="text-red-600 font-semibold text-xs shrink-0 ml-2">{d.interface_errors.toLocaleString()} errs</span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          ),

          flapping_interfaces: (
            <Card className="p-6 h-full">
              <div className="flex items-center justify-between mb-4">
                <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">Flapping Interfaces</p>
                <span className="text-[11px] text-slate-300 dark:text-slate-500">last 24h</span>
              </div>
              {(summary?.flapping_interfaces?.length ?? 0) === 0 ? (
                <div className="text-center py-8">
                  <p className="text-sm font-semibold text-emerald-600">Stable</p>
                  <p className="text-xs text-slate-400 mt-1">No ports have flapped in the last 24h.</p>
                </div>
              ) : (
                <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                  {summary?.flapping_interfaces?.map((f, i) => (
                    <div key={i} className="flex items-start justify-between gap-2 text-sm">
                      <div className="min-w-0">
                        <span className="font-medium text-slate-700 dark:text-slate-200 truncate">{f.hostname}</span>
                        <p className="text-[11px] text-slate-400 truncate">{f.interface}</p>
                      </div>
                      <div className="text-right shrink-0">
                        <span className="text-amber-600 font-semibold text-xs">{f.flap_count}×</span>
                        <p className="text-[10px] text-slate-400">{f.last_change ? timeAgo(f.last_change) : ""}</p>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          ),

          top_cpu_devices: (
            <Card className="p-6 h-full">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-4">Top CPU Devices</p>
              {(summary?.top_cpu_devices?.length ?? 0) === 0 ? (
                <p className="text-sm text-slate-400 italic py-6 text-center">No metrics polled yet.</p>
              ) : (
                <div className="space-y-2.5">
                  {summary?.top_cpu_devices?.map((d, i) => (
                    <div key={i} className="flex items-center justify-between gap-3 text-sm">
                      <div className="min-w-0">
                        <span className="font-medium text-slate-700 dark:text-slate-200 truncate">{d.hostname}</span>
                        <span className="text-slate-400 font-normal text-xs ml-2">{d.ip_address}</span>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        <Sparkline values={d.cpu_history} color="#06B6D4" width={48} height={18} />
                        <span className={`font-semibold text-xs w-10 text-right ${bandTextClass(d.cpu, thresholds.cpu)}`}>{d.cpu.toFixed(0)}%</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          ),

          top_memory_devices: (
            <Card className="p-6 h-full">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-4">Top Memory Devices</p>
              {(summary?.top_memory_devices?.length ?? 0) === 0 ? (
                <p className="text-sm text-slate-400 italic py-6 text-center">No metrics polled yet.</p>
              ) : (
                <div className="space-y-2.5">
                  {summary?.top_memory_devices?.map((d, i) => (
                    <div key={i} className="flex items-center justify-between gap-3 text-sm">
                      <div className="min-w-0">
                        <span className="font-medium text-slate-700 dark:text-slate-200 truncate">{d.hostname}</span>
                        <span className="text-slate-400 font-normal text-xs ml-2">{d.ip_address}</span>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        <Sparkline values={d.memory_history} color="#8B5CF6" width={48} height={18} />
                        <span className={`font-semibold text-xs w-10 text-right ${bandTextClass(d.memory, thresholds.memory)}`}>{d.memory.toFixed(0)}%</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          ),

          top_bandwidth_devices: (
            <Card className="p-6 h-full">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-4">Top Bandwidth Devices</p>
              {(summary?.top_bandwidth_devices?.length ?? 0) === 0 ? (
                <p className="text-sm text-slate-400 italic py-6 text-center">No metrics polled yet.</p>
              ) : (
                <div className="space-y-2.5">
                  {summary?.top_bandwidth_devices?.map((d, i) => (
                    <div key={i} className="flex items-center justify-between gap-3 text-sm">
                      <div className="min-w-0">
                        <span className="font-medium text-slate-700 dark:text-slate-200 truncate">{d.hostname}</span>
                        <span className="text-slate-400 font-normal text-xs ml-2">{d.ip_address}</span>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        <Sparkline values={d.bandwidth_history} color="#0EA5E9" width={48} height={18} />
                        <span className={`font-semibold text-xs w-10 text-right ${bandTextClass(d.bandwidth, thresholds.bandwidth)}`}>{d.bandwidth.toFixed(0)}%</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          ),

          down_ports: (
            <Card className="p-6 h-full">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-4">Down Ports</p>
              {(summary?.down_ports?.length ?? 0) === 0 ? (
                <p className="text-sm text-slate-400 italic py-6 text-center">No down ports right now.</p>
              ) : (
                <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                  {summary?.down_ports?.map((p, i) => (
                    <div key={i} className="flex items-start justify-between gap-2 text-sm">
                      <div className="min-w-0">
                        <span className="font-medium text-slate-700 dark:text-slate-200 truncate">{p.hostname}</span>
                        <p className="text-[11px] text-slate-400 truncate">{p.interface}</p>
                      </div>
                      <span className="text-[11px] text-slate-400 shrink-0">{p.down_since ? timeAgo(p.down_since) : ""}</span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          ),

          recent_reboots: (
            <Card className="p-6 h-full">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-4">Recent Reboots</p>
              {(summary?.recent_reboots?.length ?? 0) === 0 ? (
                <p className="text-sm text-slate-400 italic py-6 text-center">No devices have restarted recently.</p>
              ) : (
                <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                  {summary?.recent_reboots?.map((r, i) => (
                    <div key={i} className="flex items-start justify-between gap-2 text-sm">
                      <div className="min-w-0">
                        <span className="font-medium text-slate-700 dark:text-slate-200 truncate">{r.hostname}</span>
                        <p className="text-[11px] text-slate-400 truncate">{r.ip_address}</p>
                      </div>
                      <span className="text-[11px] text-slate-400 shrink-0">up {Math.round(r.uptime_seconds / 60)}m</span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          ),

          recent_backups: (
            <Card className="p-6 h-full">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-4">Recent Backups</p>
              {(summary?.recent_backups?.length ?? 0) === 0 ? (
                <p className="text-sm text-slate-400 italic py-6 text-center">No config backups yet.</p>
              ) : (
                <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                  {summary?.recent_backups?.map((b) => (
                    <div key={b.id} className="flex items-center justify-between gap-2 text-sm">
                      <div className="min-w-0">
                        <span className="font-medium text-slate-700 dark:text-slate-200 truncate">{b.hostname}</span>
                        <span className="text-slate-400 font-normal text-xs ml-2">v{b.version}</span>
                      </div>
                      <span className="text-[11px] text-slate-400 shrink-0">{timeAgo(b.created_at)}</span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          ),

          recent_protocol_operations: (
            <Card className="p-6 h-full">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-4">Recent Protocol Operations</p>
              {(summary?.recent_protocol_operations?.length ?? 0) === 0 ? (
                <p className="text-sm text-slate-400 italic py-6 text-center">No protocol operations recorded yet.</p>
              ) : (
                <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                  {summary?.recent_protocol_operations?.map((op) => (
                    <div key={op.id} className="flex items-center justify-between gap-2 text-sm">
                      <div className="min-w-0">
                        <span className="font-medium text-slate-700 dark:text-slate-200 truncate">{op.device_hostname}</span>
                        <span className="text-slate-400 font-normal text-xs ml-2">{op.protocol.toUpperCase()} · {op.operation}</span>
                      </div>
                      <span className={`text-[11px] font-semibold shrink-0 ${op.success ? "text-emerald-600" : "text-red-600"}`}>
                        {op.success ? "OK" : "Failed"}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          ),

          group_availability: (
            <Card className="p-6 h-full">
              <div className="flex items-center justify-between mb-4">
                <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">Group Availability <span className="text-slate-300 dark:text-slate-500 font-normal normal-case">· last poll per group</span></p>
                <Link to="/groups" className="text-xs text-brandblue font-medium hover:underline">All groups →</Link>
              </div>
              {groupStats.length === 0 ? (
                <p className="text-sm text-slate-400 italic py-6 text-center">No device groups yet. Create one from the Groups page.</p>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
                  {groupStats.map((g) => {
                    const pct = g.total > 0 ? (g.online / g.total) * 100 : 0;
                    const tone = pct >= 90 ? "text-emerald-600" : pct >= 50 ? "text-amber-600" : "text-red-600";
                    const barTone = pct >= 90 ? "bg-emerald-500" : pct >= 50 ? "bg-amber-500" : "bg-red-500";
                    return (
                      <div key={g.id} className="border border-slate-100 dark:border-slate-700 rounded-xl p-4">
                        <div className="flex items-center justify-between mb-1">
                          <span className="text-sm font-semibold text-slate-700 dark:text-slate-200 truncate">{g.name}</span>
                          <span className={`text-sm font-bold ${tone}`}>{pct.toFixed(0)}%</span>
                        </div>
                        <p className="text-[11px] text-slate-400 mb-2">{g.online}/{g.total} online</p>
                        <div className="w-full h-1.5 rounded-full bg-slate-100 dark:bg-slate-700 overflow-hidden mb-2">
                          <div className={`h-full rounded-full ${barTone}`} style={{ width: `${pct}%` }} />
                        </div>
                        <div className="flex items-center gap-3 text-[11px] text-slate-400">
                          <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />{g.online} online</span>
                          <span className="flex items-center gap-1"><span className="w-1.5 h-1.5 rounded-full bg-red-500" />{g.offline} offline</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </Card>
          ),
        };

        const orderedIds = layout.length > 0 ? layout : availableWidgets.map((w) => ({ id: w.id, visible: w.default_visible }));

        return (
          <div className="grid grid-cols-1 sm:grid-cols-12 gap-5 mb-6">
            {orderedIds
              .filter((entry) => entry.visible && widgetContent[entry.id])
              .map((entry) => (
                <div key={entry.id} className={`min-w-0 ${WIDGET_SPAN[entry.id] || "sm:col-span-6 lg:col-span-6"}`}>
                  {widgetContent[entry.id]}
                </div>
              ))}
          </div>
        );
      })()}

      {showCustomize && (
        <DashboardCustomizePanel
          layout={layout.length > 0 ? layout : availableWidgets.map((w) => ({ id: w.id, visible: w.default_visible }))}
          availableWidgets={availableWidgets}
          thresholds={thresholds}
          saving={savingLayout}
          onClose={() => setShowCustomize(false)}
          onSave={(next, nextThresholds) => {
            saveDashboardPrefs(next, nextThresholds ?? thresholds);
            setShowCustomize(false);
          }}
        />
      )}
    </div>
  );
}