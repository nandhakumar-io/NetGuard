import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";

interface ConfigSearchMatch {
  line_number: number;
  text: string;
}

interface ConfigSearchDeviceResult {
  device_id: string;
  hostname: string;
  ip_address: string;
  vendor: string;
  total_match_count: number;
  truncated: boolean;
  matches: ConfigSearchMatch[];
}

interface ConfigSearchResponse {
  query: string;
  regex: boolean;
  devices_searched: number;
  devices_with_no_snapshot: number;
  devices_matched: number;
  results: ConfigSearchDeviceResult[];
}

const EXAMPLES = [
  { label: "Telnet still enabled", query: "transport input.*telnet", regex: true },
  { label: "SNMP community 'public'", query: "community public", regex: false },
  { label: "Default/no NTP server", query: "ntp server", regex: false },
];

export default function ConfigSearchPage() {
  const [query, setQuery] = useState("");
  const [regex, setRegex] = useState(false);
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [result, setResult] = useState<ConfigSearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runSearch = async (q?: string, useRegex?: boolean) => {
    const effectiveQuery = q ?? query;
    const effectiveRegex = useRegex ?? regex;
    if (!effectiveQuery.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.get<ConfigSearchResponse>("/config-search", {
        params: { query: effectiveQuery, regex: effectiveRegex, case_sensitive: caseSensitive, limit_devices: 200 },
      });
      setResult(res.data);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Search failed.");
      setResult(null);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="pb-16 max-w-5xl mx-auto flex flex-col gap-6 pt-2">
      <div>
        <h1 className="text-3xl font-bold text-navy dark:text-white">Config Search</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1 font-medium">
          Search every device's most recent configuration backup at once — "which devices still have telnet
          enabled", "find every device with this ACL line", etc.
        </p>
      </div>

      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 shadow-sm flex flex-col gap-3">
        <div className="flex gap-2 flex-wrap items-center">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && runSearch()}
            placeholder={regex ? "e.g. transport input.*telnet" : "e.g. telnet"}
            className="flex-1 min-w-[240px] border border-slate-200 dark:border-slate-700 dark:bg-slate-900 rounded-lg px-3 py-2 text-sm font-mono"
          />
          <button
            onClick={() => runSearch()}
            disabled={loading || !query.trim()}
            className="text-xs font-bold uppercase tracking-wider text-white bg-brandblue hover:bg-navy px-4 py-2 rounded-lg shadow-sm disabled:opacity-50"
          >
            {loading ? "Searching…" : "Search"}
          </button>
        </div>
        <div className="flex items-center gap-4 text-xs text-slate-500 dark:text-slate-400">
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={regex} onChange={(e) => setRegex(e.target.checked)} />
            Regex
          </label>
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={caseSensitive} onChange={(e) => setCaseSensitive(e.target.checked)} />
            Case sensitive
          </label>
          <span className="text-slate-300 dark:text-slate-600">|</span>
          <span>Try:</span>
          {EXAMPLES.map((ex) => (
            <button
              key={ex.label}
              onClick={() => {
                setQuery(ex.query);
                setRegex(ex.regex);
                runSearch(ex.query, ex.regex);
              }}
              className="font-semibold text-brandblue hover:text-navy dark:text-white underline decoration-dotted"
            >
              {ex.label}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="text-xs text-riskcrit bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-900 rounded-lg px-3 py-2">
          {error}
        </div>
      )}

      {result && (
        <>
          <div className="flex flex-wrap items-center gap-4 text-xs text-slate-500 dark:text-slate-400 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2">
            <span>
              <span className="font-bold text-navy dark:text-white">{result.devices_matched}</span> device
              {result.devices_matched === 1 ? "" : "s"} matched
            </span>
            <span>{result.devices_searched} devices searched</span>
            {result.devices_with_no_snapshot > 0 && (
              <span>{result.devices_with_no_snapshot} devices skipped (no backup on file)</span>
            )}
          </div>

          {result.devices_matched === 0 && !loading && (
            <p className="text-sm text-slate-400 dark:text-slate-500 italic text-center py-8">
              No matches across any device's latest config.
            </p>
          )}

          <div className="flex flex-col gap-4">
            {result.results.map((r) => (
              <div
                key={r.device_id}
                className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4 shadow-sm"
              >
                <div className="flex items-center justify-between mb-2">
                  <Link
                    to="/devices"
                    className="text-sm font-bold text-navy dark:text-white hover:text-brandblue transition-colors"
                  >
                    {r.hostname}{" "}
                    <span className="text-slate-400 dark:text-slate-500 font-mono font-normal text-xs">
                      ({r.ip_address}, {r.vendor})
                    </span>
                  </Link>
                  <span className="text-[10px] font-bold uppercase text-slate-400 dark:text-slate-500 bg-slate-100 dark:bg-slate-700 px-2 py-0.5 rounded">
                    {r.total_match_count} match{r.total_match_count === 1 ? "" : "es"}
                  </span>
                </div>
                <div className="font-mono text-xs bg-slate-50 dark:bg-slate-900 rounded-lg overflow-hidden">
                  {r.matches.map((m) => (
                    <div
                      key={m.line_number}
                      className="flex gap-3 px-3 py-1 border-b border-slate-100 dark:border-slate-800 last:border-0"
                    >
                      <span className="text-slate-300 dark:text-slate-600 select-none shrink-0 w-10 text-right">
                        {m.line_number}
                      </span>
                      <span className="text-slate-700 dark:text-slate-300 whitespace-pre-wrap break-all">{m.text}</span>
                    </div>
                  ))}
                </div>
                {r.truncated && (
                  <p className="text-[11px] text-slate-400 dark:text-slate-500 mt-2 italic">
                    Showing the first {r.matches.length} of {r.total_match_count} matches on this device.
                  </p>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}