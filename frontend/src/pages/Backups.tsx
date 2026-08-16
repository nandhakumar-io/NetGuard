import { useEffect, useState } from "react";
import { api } from "../lib/api";

interface BackupJob {
  id: string;
  status: "running" | "completed" | "failed";
  file_name: string | null;
  size_bytes: number | null;
  error_message: string | null;
  triggered_by: string | null;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
}

const STATUS_BADGE: Record<string, string> = {
  completed: "bg-emerald-100 text-emerald-700",
  failed: "bg-red-100 text-red-700",
  running: "bg-amber-100 text-amber-700",
};

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit",
  });
}

function fmtBytes(bytes: number | null): string {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let val = bytes;
  let i = 0;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(val < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

function fmtDuration(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
}

export default function Backups() {
  const [backups, setBackups] = useState<BackupJob[]>([]);
  const [total, setTotal] = useState(0);
  const [completed, setCompleted] = useState(0);
  const [failed, setFailed] = useState(0);
  const [storedBytes, setStoredBytes] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [actioningId, setActioningId] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get("/backups");
      setBackups(res.data.backups);
      setTotal(res.data.total);
      setCompleted(res.data.completed);
      setFailed(res.data.failed);
      setStoredBytes(res.data.stored_bytes);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to load backups");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const runBackup = async () => {
    setRunning(true);
    setError(null);
    try {
      await api.post("/backups/database");
      await load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to start backup");
    } finally {
      setRunning(false);
    }
  };

  const downloadBackup = async (job: BackupJob) => {
    setActioningId(job.id);
    try {
      const res = await api.get(`/backups/${job.id}/download`, { responseType: "blob" as any });
      const blob = new Blob([res.data], { type: "application/gzip" });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = job.file_name || `backup-${job.id}.sql.gz`;
      a.click();
      window.URL.revokeObjectURL(url);
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to download backup");
    } finally {
      setActioningId(null);
    }
  };

  const removeBackup = async (job: BackupJob) => {
    if (!confirm(`Delete backup ${job.file_name || job.id}? This cannot be undone.`)) return;
    setActioningId(job.id);
    try {
      await api.delete(`/backups/${job.id}`);
      await load();
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to delete backup");
    } finally {
      setActioningId(null);
    }
  };

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <div className="w-11 h-11 rounded-xl bg-brandblue flex items-center justify-center text-white">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14a9 3 0 0018 0V5" /><path d="M3 12a9 3 0 0018 0" />
            </svg>
          </div>
          <div>
            <h1 className="text-2xl font-bold text-navy dark:text-white">Database Backups</h1>
            <p className="text-sm text-slate-500 dark:text-slate-400">On-demand snapshots of the NetGuard application database</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={load} className="p-2 rounded-lg border border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-800">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M23 4v6h-6M1 20v-6h6" /><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" />
            </svg>
          </button>
          <button
            onClick={runBackup}
            disabled={running}
            className="flex items-center gap-1.5 bg-brandblue text-white font-bold px-4 py-2 rounded-lg hover:opacity-90 disabled:opacity-50"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14a9 3 0 0018 0V5" /><path d="M3 12a9 3 0 0018 0" />
            </svg>
            {running ? "Running…" : "Run Backup Now"}
          </button>
        </div>
      </div>

      {error && <div className="mb-4 p-3 rounded-lg bg-red-50 text-red-700 text-sm">{error}</div>}

      {/* Stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4">
          <div className="text-2xl font-bold text-navy dark:text-white">{total}</div>
          <div className="text-[11px] uppercase tracking-wide text-slate-400 font-bold mt-1">Total Runs</div>
        </div>
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4">
          <div className="text-2xl font-bold text-emerald-600">{completed}</div>
          <div className="text-[11px] uppercase tracking-wide text-slate-400 font-bold mt-1">Completed</div>
        </div>
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4">
          <div className="text-2xl font-bold text-red-600">{failed}</div>
          <div className="text-[11px] uppercase tracking-wide text-slate-400 font-bold mt-1">Failed</div>
        </div>
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4">
          <div className="text-2xl font-bold text-navy dark:text-white">{fmtBytes(storedBytes)}</div>
          <div className="text-[11px] uppercase tracking-wide text-slate-400 font-bold mt-1">Stored Locally</div>
        </div>
      </div>

      {/* Table */}
      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 dark:bg-slate-900/40 text-[11px] uppercase tracking-wide text-slate-400 font-bold">
            <tr>
              <th className="text-left py-2.5 px-4">File</th>
              <th className="text-left py-2.5 px-4">Status</th>
              <th className="text-left py-2.5 px-4">Size</th>
              <th className="text-left py-2.5 px-4">Triggered By</th>
              <th className="text-left py-2.5 px-4">Started</th>
              <th className="text-left py-2.5 px-4">Duration</th>
              <th className="text-right py-2.5 px-4">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={7} className="text-center py-8 text-slate-400">Loading…</td></tr>
            ) : backups.length === 0 ? (
              <tr><td colSpan={7} className="text-center py-8 text-slate-400">No backups yet. Run one to get started.</td></tr>
            ) : (
              backups.map((b) => (
                <tr key={b.id} className="border-t border-slate-100 dark:border-slate-700/50">
                  <td className="py-2.5 px-4">
                    <div className="font-mono text-xs text-navy dark:text-white">{b.file_name || "—"}</div>
                    {b.status === "failed" && b.error_message && (
                      <div className="text-[11px] text-red-500 mt-0.5 max-w-md truncate" title={b.error_message}>{b.error_message}</div>
                    )}
                  </td>
                  <td className="py-2.5 px-4">
                    <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${STATUS_BADGE[b.status] || "bg-slate-100 text-slate-500"}`}>
                      {b.status.charAt(0).toUpperCase() + b.status.slice(1)}
                    </span>
                  </td>
                  <td className="py-2.5 px-4 text-slate-500 dark:text-slate-400 font-mono">{fmtBytes(b.size_bytes)}</td>
                  <td className="py-2.5 px-4 text-slate-500 dark:text-slate-400">{b.triggered_by || "—"}</td>
                  <td className="py-2.5 px-4 text-slate-500 dark:text-slate-400">{fmtDate(b.started_at)}</td>
                  <td className="py-2.5 px-4 text-slate-500 dark:text-slate-400">{fmtDuration(b.duration_seconds)}</td>
                  <td className="py-2.5 px-4 text-right">
                    <div className="flex justify-end gap-3">
                      <button
                        disabled={actioningId === b.id || b.status !== "completed"}
                        onClick={() => downloadBackup(b)}
                        className="text-xs font-bold text-brandblue hover:underline disabled:opacity-40 disabled:no-underline"
                      >
                        Download
                      </button>
                      <button
                        disabled={actioningId === b.id}
                        onClick={() => removeBackup(b)}
                        className="text-xs font-bold text-red-600 hover:underline disabled:opacity-40 disabled:no-underline"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}