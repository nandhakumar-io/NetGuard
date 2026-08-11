import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { SyslogMessage, SyslogSummary } from "../lib/types";
import { useDebouncedValue } from "../lib/useDebouncedValue";
import { EmptyState } from "../components/EmptyState";

// Numeric syslog severity (0=most severe) -> display config. Matches
// app.models.syslog_message.SyslogSeverity's numeric values exactly, so
// the min-severity dropdown below can send the raw number the API expects.
const SEVERITY_CONFIG: Record<string, { value: number; label: string; color: string; bg: string }> = {
  EMERGENCY: { value: 0, label: "Emergency", color: "text-riskcrit", bg: "bg-red-50" },
  ALERT: { value: 1, label: "Alert", color: "text-riskcrit", bg: "bg-red-50" },
  CRITICAL: { value: 2, label: "Critical", color: "text-riskcrit", bg: "bg-red-50" },
  ERROR: { value: 3, label: "Error", color: "text-riskcrit", bg: "bg-red-50" },
  WARNING: { value: 4, label: "Warning", color: "text-riskmed", bg: "bg-amber-50" },
  NOTICE: { value: 5, label: "Notice", color: "text-brandblue", bg: "bg-blue-50" },
  INFORMATIONAL: { value: 6, label: "Info", color: "text-slate-500", bg: "bg-slate-50" },
  DEBUG: { value: 7, label: "Debug", color: "text-slate-400", bg: "bg-slate-50" },
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

export default function SyslogViewer() {
  const [messages, setMessages] = useState<SyslogMessage[]>([]);
  const [summary, setSummary] = useState<SyslogSummary | null>(null);
  const [categories, setCategories] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [minSeverity, setMinSeverity] = useState("6"); // default: Info and worse (hide Debug noise)
  const [categoryFilter, setCategoryFilter] = useState("");
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebouncedValue(search, 350);
  const [hours, setHours] = useState("24");
  const [onlyCorrelated, setOnlyCorrelated] = useState(false);
  const [liveMode, setLiveMode] = useState(true);

  const fetchMessages = useCallback(() => {
    const params = new URLSearchParams();
    params.set("min_severity", minSeverity);
    params.set("hours", hours);
    params.set("limit", "300");
    if (categoryFilter) params.set("category", categoryFilter);
    if (debouncedSearch.trim()) params.set("search", debouncedSearch.trim());

    api
      .get<SyslogMessage[]>(`/syslog?${params.toString()}`)
      .then((res) => {
        setMessages(onlyCorrelated ? res.data.filter((m) => m.correlated_category) : res.data);
        setError(null);
        setLoading(false);
      })
      .catch(() => {
        setError("Could not reach the syslog feed. Make sure the backend is running.");
        setLoading(false);
      });
  }, [minSeverity, hours, categoryFilter, debouncedSearch, onlyCorrelated]);

  const fetchSummary = useCallback(() => {
    api
      .get<SyslogSummary>(`/syslog/summary?hours=${hours}`)
      .then((res) => setSummary(res.data))
      .catch(() => {});
  }, [hours]);

  useEffect(() => {
    api
      .get<string[]>("/syslog/categories")
      .then((res) => setCategories(res.data))
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetchMessages();
    fetchSummary();
    if (!liveMode) return;
    const interval = setInterval(() => {
      fetchMessages();
      fetchSummary();
    }, 15000);
    return () => clearInterval(interval);
  }, [fetchMessages, fetchSummary, liveMode]);

  const maxVolume = summary?.volume_by_hour.length
    ? Math.max(...summary.volume_by_hour.map((p) => p.count), 1)
    : 1;

  return (
    <div className="pb-16 max-w-7xl mx-auto flex flex-col gap-6 pt-2">
      <div>
        <h1 className="text-3xl font-bold text-navy dark:text-white">Syslog</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1 font-medium">
          Collection &amp; correlation of raw device syslog -- auth failures, hardware faults, and ACL hits that
          SNMP polling alone never surfaces.
        </p>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-riskcrit text-sm font-semibold rounded-lg px-4 py-3 shadow-sm">
          {error}
        </div>
      )}

      {/* Summary strip */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4 shadow-sm">
          <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide">Messages ({hours}h)</p>
          <p className="text-2xl font-black text-navy dark:text-white mt-1">{summary?.total ?? "—"}</p>
        </div>
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4 shadow-sm">
          <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide">Correlated</p>
          <p className="text-2xl font-black text-brandblue mt-1">{summary?.correlated ?? "—"}</p>
        </div>
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4 shadow-sm">
          <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide">Critical+</p>
          <p className="text-2xl font-black text-riskcrit mt-1">
            {(summary?.by_severity.emergency ?? 0) + (summary?.by_severity.alert ?? 0) + (summary?.by_severity.critical ?? 0)}
          </p>
        </div>
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4 shadow-sm">
          <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide">Warnings</p>
          <p className="text-2xl font-black text-riskmed mt-1">{summary?.by_severity.warning ?? 0}</p>
        </div>
        {/* Hourly volume sparkbar */}
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4 shadow-sm col-span-2 lg:col-span-1">
          <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide mb-2">Volume/hr</p>
          <div className="flex items-end gap-0.5 h-8">
            {(summary?.volume_by_hour ?? []).slice(-24).map((p, i) => (
              <div
                key={i}
                title={`${new Date(p.hour).toLocaleTimeString()}: ${p.count}`}
                className="flex-1 bg-brandblue/70 dark:bg-brandblue rounded-sm min-w-[2px]"
                style={{ height: `${Math.max((p.count / maxVolume) * 100, 4)}%` }}
              />
            ))}
          </div>
        </div>
      </div>

      {/* Filters */}
      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4 shadow-sm flex flex-wrap items-center gap-3">
        <button
          onClick={() => setLiveMode((v) => !v)}
          title={liveMode ? "Pause live updates while you investigate a message" : "Resume auto-refresh"}
          className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-bold border transition-colors ${
            liveMode ? "bg-green-50 border-green-300 text-green-700" : "bg-slate-100 border-slate-300 text-slate-500"
          }`}
        >
          <span className={`w-2 h-2 rounded-full ${liveMode ? "bg-green-500 animate-pulse" : "bg-slate-400"}`} />
          {liveMode ? "Live" : "Paused"}
        </button>
        {!liveMode && (
          <button
            onClick={() => { fetchMessages(); fetchSummary(); }}
            className="text-xs font-bold px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-900"
          >
            ↻ Refresh now
          </button>
        )}
        <select
          value={minSeverity}
          onChange={(e) => setMinSeverity(e.target.value)}
          className="text-xs font-semibold border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 bg-white dark:bg-slate-900 text-navy dark:text-white"
        >
          {Object.entries(SEVERITY_CONFIG).map(([key, cfg]) => (
            <option key={key} value={cfg.value}>
              {cfg.label} and worse
            </option>
          ))}
        </select>
        <select
          value={categoryFilter}
          onChange={(e) => setCategoryFilter(e.target.value)}
          className="text-xs font-semibold border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 bg-white dark:bg-slate-900 text-navy dark:text-white"
        >
          <option value="">All categories</option>
          {categories.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <select
          value={hours}
          onChange={(e) => setHours(e.target.value)}
          className="text-xs font-semibold border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 bg-white dark:bg-slate-900 text-navy dark:text-white"
        >
          <option value="1">Last hour</option>
          <option value="6">Last 6 hours</option>
          <option value="24">Last 24 hours</option>
          <option value="168">Last 7 days</option>
        </select>
        <label className="flex items-center gap-2 text-xs font-semibold text-slate-600 dark:text-slate-300">
          <input type="checkbox" checked={onlyCorrelated} onChange={(e) => setOnlyCorrelated(e.target.checked)} />
          Correlated only
        </label>
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search message text…"
          className="flex-1 min-w-[140px] sm:min-w-[200px] text-xs font-medium border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 bg-white dark:bg-slate-900 text-navy dark:text-white"
        />
      </div>

      {/* Feed */}
      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl shadow-sm overflow-hidden">
        {loading ? (
          <p className="text-xs text-slate-400 p-6 text-center">Loading syslog feed…</p>
        ) : messages.length === 0 ? (
          <div className="text-center py-10">
            <span className="text-4xl mb-3 inline-block">📭</span>
            <p className="text-sm font-bold text-slate-500 dark:text-slate-400">No syslog messages match these filters.</p>
            <p className="text-xs font-medium text-slate-400 dark:text-slate-500 mt-1">
              Point a device's `logging host` at this app's syslog listener to start collecting.
            </p>
          </div>
        ) : (
          <div className="divide-y divide-slate-100 dark:divide-slate-800 max-h-[70vh] overflow-y-auto">
            {messages.map((m) => {
              const cfg = SEVERITY_CONFIG[m.severity] || SEVERITY_CONFIG.INFORMATIONAL;
              return (
                <div key={m.id} className="flex flex-col sm:flex-row gap-1.5 sm:gap-3 px-4 py-2.5 hover:bg-slate-50 dark:hover:bg-slate-900/50 transition-colors">
                  <div className="flex items-center gap-2 sm:contents">
                    <span
                      className={`shrink-0 text-[10px] font-black uppercase tracking-wide px-2 py-1 rounded ${cfg.bg} ${cfg.color} sm:w-20 text-center h-fit`}
                    >
                      {cfg.label}
                    </span>
                    <span className="text-[11px] font-semibold text-slate-400 dark:text-slate-500 sm:hidden ml-auto">
                      {timeAgo(m.received_at)}
                    </span>
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-xs font-bold text-navy dark:text-white">
                        {m.device_hostname || m.reported_hostname || m.source_ip}
                      </span>
                      {m.tag && <span className="text-[11px] font-mono text-slate-400 dark:text-slate-500">{m.tag}</span>}
                      {m.correlated_category && (
                        <span className="text-[10px] font-bold uppercase tracking-wide text-brandblue bg-blue-50 px-1.5 py-0.5 rounded">
                          {m.correlated_category}
                        </span>
                      )}
                    </div>
                    <p className="text-[13px] text-slate-700 dark:text-slate-300 font-mono break-words mt-0.5">{m.message}</p>
                  </div>
                  <span className="hidden sm:block shrink-0 text-[11px] font-semibold text-slate-400 dark:text-slate-500 pt-0.5">
                    {timeAgo(m.received_at)}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}