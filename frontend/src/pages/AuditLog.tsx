import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { AuditLogEntry } from "../lib/types";

export default function AuditLog() {
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);

  useEffect(() => {
    api.get<AuditLogEntry[]>("/audit-logs").then((res) => setLogs(res.data));
  }, []);

  return (
    <div>
      <h1 className="text-2xl font-bold text-navy">Audit Trail</h1>
      <p className="text-sm text-slate-500 mt-1">Immutable log of every action taken on the platform.</p>

      <div className="mt-6 bg-white border border-slate-200 rounded-xl overflow-hidden">
        <table className="w-full text-sm">
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
            {logs.length === 0 && (
              <tr>
                <td colSpan={5} className="text-center text-slate-400 py-8">
                  No audit events yet.
                </td>
              </tr>
            )}
            {logs.map((l, i) => (
              <tr key={l.id} className={i % 2 ? "bg-slate-50" : "bg-white"}>
                <td className="px-4 py-3 text-slate-500">{new Date(l.time).toLocaleString()}</td>
                <td className="px-4 py-3 font-medium text-navy">{l.user}</td>
                <td className="px-4 py-3 text-slate-600">{l.action}</td>
                <td className="px-4 py-3 text-slate-600">{l.device || "—"}</td>
                <td className="px-4 py-3 text-slate-600">{l.result}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
