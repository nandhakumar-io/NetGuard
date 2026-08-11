import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { AuditLogEntry } from "../lib/types";

function toCsv(rows: AuditLogEntry[]): string {
  const header = ["Time", "User", "Action", "Device", "Result"];
  const escape = (v: string) => `"${v.replace(/"/g, '""')}"`;
  const lines = rows.map((r) =>
    [new Date(r.time).toISOString(), r.user, r.action, r.device || "", r.result].map(escape).join(",")
  );
  return [header.join(","), ...lines].join("\n");
}

export default function AuditLog() {
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [resultFilter, setResultFilter] = useState<string>("all");

  const load = () => {
    api
      .get<AuditLogEntry[]>("/audit-logs")
      .then((res) => setLogs(res.data))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const results = useMemo(() => Array.from(new Set(logs.map((l) => l.result))), [logs]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return logs.filter((l) => {
      if (resultFilter !== "all" && l.result !== resultFilter) return false;
      if (!q) return true;
      return (
        l.user.toLowerCase().includes(q) ||
        l.action.toLowerCase().includes(q) ||
        (l.device || "").toLowerCase().includes(q)
      );
    });
  }, [logs, query, resultFilter]);

  const resultBadge = (result: string) => {
    const r = result.toLowerCase();
    if (r.includes("fail") || r.includes("reject")) return "bg-red-100 text-red-700";
    if (r.includes("success") || r.includes("approved") || r.includes("enabled")) return "bg-green-100 text-green-700";
    return "bg-slate-100 text-slate-600";
  };

  const exportCsv = () => {
    const blob = new Blob([toCsv(filtered)], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `netguard-audit-log-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy">Audit Trail</h1>
          <p className="text-sm text-slate-500 mt-1">Immutable log of every action taken on the platform.</p>
        </div>
        <button
          onClick={exportCsv}
          disabled={!filtered.length}
          className="bg-white border border-slate-300 text-navy rounded-lg px-4 py-2 text-sm font-semibold hover:bg-slate-50 disabled:opacity-50"
        >
          Export CSV
        </button>
      </div>

      <div className="flex flex-wrap gap-2 items-center mt-5 mb-3">
        <input
          className="border border-slate-300 rounded-lg px-3 py-2 text-sm w-full max-w-xs"
          placeholder="Search user, action, or device…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          className="border border-slate-300 rounded-lg px-3 py-2 text-sm"
          value={resultFilter}
          onChange={(e) => setResultFilter(e.target.value)}
        >
          <option value="all">All results</option>
          {results.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <button onClick={load} className="text-xs text-brandblue font-medium hover:text-navy ml-auto">
          ↻ Refresh
        </button>
      </div>

      <div className="bg-white border border-slate-200 rounded-xl overflow-x-auto">
        <table className="w-full text-sm min-w-[700px]">
          <thead className="bg-navy text-white">
            <tr>
              <th className="text-left px-4 py-3 font-semibold">Time</th>
              <th className="text-left px-4 py-3 font-semibold">User</th>
              <th className="text-left px-4 py-3 font-semibold">Action</th>
              <th className="text-left px-4 py-3 font-semibold">Device</th>
              <th className="text-left px-4 py-3 font-semibold">Result</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={5} className="text-center text-slate-400 py-8">
                  Loading audit events…
                </td>
              </tr>
            )}
            {!loading && filtered.length === 0 && (
              <tr>
                <td colSpan={5} className="text-center text-slate-400 py-8">
                  {logs.length === 0 ? "No audit events yet." : "No events match your search."}
                </td>
              </tr>
            )}
            {filtered.map((l, i) => (
              <tr key={l.id} className={i % 2 ? "bg-slate-50" : "bg-white"}>
                <td className="px-4 py-3 text-slate-500 whitespace-nowrap">{new Date(l.time).toLocaleString()}</td>
                <td className="px-4 py-3 font-medium text-navy">{l.user}</td>
                <td className="px-4 py-3 text-slate-600">{l.action}</td>
                <td className="px-4 py-3 text-slate-600">{l.device || "—"}</td>
                <td className="px-4 py-3">
                  <span className={`px-2 py-1 rounded-full text-xs font-medium ${resultBadge(l.result)}`}>
                    {l.result}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!loading && (
        <p className="text-xs text-slate-400 mt-2">
          Showing {filtered.length} of {logs.length} events (most recent 100 from the server).
        </p>
      )}
    </div>
  );
}