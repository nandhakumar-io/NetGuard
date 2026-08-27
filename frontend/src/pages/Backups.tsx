import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";

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
  offsite_results: OffsiteResult[] | null;
}

interface OffsiteResult {
  destination_id: string;
  name: string;
  type: string;
  status: "success" | "failed";
  error: string | null;
}

interface BackupDestination {
  id: string;
  name: string;
  type: "s3" | "azure_blob" | "sftp";
  enabled: boolean;
  config: Record<string, any>;
  created_by: string | null;
  created_at: string | null;
  last_run_at: string | null;
  last_run_status: "success" | "failed" | null;
  last_error: string | null;
}

interface DeviceBackupRow {
  device_id: string;
  hostname: string;
  ip_address: string | null;
  vendor: string | null;
  backup_count: number;
  last_backup_at: string | null;
  last_backup_version: number | null;
  last_backup_snapshot_id: string | null;
  days_since_backup: number | null;
}

const DEST_TYPE_LABEL: Record<string, string> = {
  s3: "AWS S3",
  azure_blob: "Azure Blob Storage",
  sftp: "Remote Server (SFTP)",
};

const DEST_FIELDS: Record<string, { key: string; label: string; secret?: boolean; placeholder?: string }[]> = {
  s3: [
    { key: "bucket", label: "Bucket" },
    { key: "region", label: "Region", placeholder: "us-east-1" },
    { key: "access_key_id", label: "Access Key ID" },
    { key: "secret_access_key", label: "Secret Access Key", secret: true },
    { key: "prefix", label: "Key Prefix (optional)", placeholder: "netguard-backups" },
    { key: "endpoint_url", label: "Custom Endpoint URL (optional)", placeholder: "for S3-compatible stores" },
  ],
  azure_blob: [
    { key: "connection_string", label: "Connection String", secret: true, placeholder: "or use account name + key below" },
    { key: "account_name", label: "Storage Account Name" },
    { key: "account_key", label: "Storage Account Key", secret: true },
    { key: "container", label: "Container" },
    { key: "prefix", label: "Blob Prefix (optional)" },
  ],
  sftp: [
    { key: "host", label: "Host" },
    { key: "port", label: "Port", placeholder: "22" },
    { key: "username", label: "Username" },
    { key: "password", label: "Password", secret: true, placeholder: "or use a private key below" },
    { key: "private_key", label: "Private Key (PEM, optional)", secret: true },
    { key: "remote_dir", label: "Remote Directory", placeholder: "/backups" },
  ],
};

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
  const { user } = useAuth();
  // Whole-database backups and off-site destinations are a full export
  // of every tenant's data at once -- MSP-staff-only, same posture as
  // the backend's _msp_admin_only gate in app.api.backups. Per-device
  // config backups below stay available to a tenant's own Network
  // Administrator (backend scopes those to their own devices).
  const isMspStaff = !!user?.is_msp_staff;

  const [backups, setBackups] = useState<BackupJob[]>([]);
  const [total, setTotal] = useState(0);
  const [completed, setCompleted] = useState(0);
  const [failed, setFailed] = useState(0);
  const [storedBytes, setStoredBytes] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [actioningId, setActioningId] = useState<string | null>(null);

  const [destinations, setDestinations] = useState<BackupDestination[]>([]);
  const [destLoading, setDestLoading] = useState(true);
  const [destError, setDestError] = useState<string | null>(null);
  const [destActioningId, setDestActioningId] = useState<string | null>(null);
  const [showAddDest, setShowAddDest] = useState(false);
  const [editingDest, setEditingDest] = useState<BackupDestination | null>(null);

  const [deviceBackups, setDeviceBackups] = useState<DeviceBackupRow[]>([]);
  const [deviceTotal, setDeviceTotal] = useState(0);
  const [neverBackedUp, setNeverBackedUp] = useState(0);
  const [deviceLoading, setDeviceLoading] = useState(true);
  const [deviceError, setDeviceError] = useState<string | null>(null);
  const [deviceActioningId, setDeviceActioningId] = useState<string | null>(null);
  const [runningFleetBackup, setRunningFleetBackup] = useState(false);

  const loadDeviceBackups = async () => {
    setDeviceLoading(true);
    setDeviceError(null);
    try {
      const res = await api.get("/backups/devices");
      setDeviceBackups(res.data.devices);
      setDeviceTotal(res.data.total_devices);
      setNeverBackedUp(res.data.never_backed_up);
    } catch (err: any) {
      setDeviceError(err?.response?.data?.detail || "Failed to load device config backups");
    } finally {
      setDeviceLoading(false);
    }
  };

  // Which off-site destination(s) a manual device config backup should
  // also be pushed to, in addition to always being saved locally (every
  // snapshot is stored in NetGuard's own DB regardless of this choice).
  // "all" (default) = every enabled destination, matching the historical
  // behavior; "local" = skip off-site entirely; otherwise a specific
  // destination id.
  const [backupScope, setBackupScope] = useState<string>("all");

  const scopeToDestinationIds = (): string[] | undefined => {
    if (backupScope === "all") return undefined;
    if (backupScope === "local") return [];
    return [backupScope];
  };

  const runDeviceBackup = async (row: DeviceBackupRow) => {
    setDeviceActioningId(row.device_id);
    try {
      await api.post(`/backups/devices/${row.device_id}`, { destination_ids: scopeToDestinationIds() });
      await loadDeviceBackups();
    } catch (err: any) {
      alert(err?.response?.data?.detail || `Failed to back up ${row.hostname}`);
    } finally {
      setDeviceActioningId(null);
    }
  };

  const runFleetBackup = async () => {
    setRunningFleetBackup(true);
    setDeviceError(null);
    try {
      const res = await api.post("/backups/devices/bulk", { destination_ids: scopeToDestinationIds() });
      const succeeded = res.data?.succeeded ?? res.data?.success_count;
      const failed = res.data?.failed ?? res.data?.failure_count;
      await loadDeviceBackups();
      if (succeeded !== undefined || failed !== undefined) {
        alert(`Fleet backup complete: ${succeeded ?? 0} succeeded, ${failed ?? 0} failed.`);
      }
    } catch (err: any) {
      setDeviceError(err?.response?.data?.detail || "Failed to start fleet backup");
    } finally {
      setRunningFleetBackup(false);
    }
  };

  const loadDestinations = async () => {
    setDestLoading(true);
    setDestError(null);
    try {
      const res = await api.get("/backups/destinations");
      setDestinations(res.data);
    } catch (err: any) {
      setDestError(err?.response?.data?.detail || "Failed to load backup destinations");
    } finally {
      setDestLoading(false);
    }
  };

  const toggleDestEnabled = async (d: BackupDestination) => {
    setDestActioningId(d.id);
    try {
      await api.patch(`/backups/destinations/${d.id}`, { enabled: !d.enabled });
      await loadDestinations();
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to update destination");
    } finally {
      setDestActioningId(null);
    }
  };

  const testDestination = async (d: BackupDestination) => {
    setDestActioningId(d.id);
    try {
      await api.post(`/backups/destinations/${d.id}/test`);
      alert(`${d.name}: connection OK.`);
    } catch (err: any) {
      alert(err?.response?.data?.detail || `${d.name}: connection test failed.`);
    } finally {
      setDestActioningId(null);
    }
  };

  const removeDestination = async (d: BackupDestination) => {
    if (!confirm(`Remove backup destination "${d.name}"? Future backups will stop copying here.`)) return;
    setDestActioningId(d.id);
    try {
      await api.delete(`/backups/destinations/${d.id}`);
      await loadDestinations();
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to remove destination");
    } finally {
      setDestActioningId(null);
    }
  };

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
    if (isMspStaff) {
      load();
      loadDestinations();
    } else {
      setLoading(false);
      setDestLoading(false);
    }
    loadDeviceBackups();
  }, [isMspStaff]);

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
      {isMspStaff && (
        <>
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

      {/* Cloud Destinations */}
      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl overflow-hidden mb-6">
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-100 dark:border-slate-700/50">
          <div>
            <h2 className="font-bold text-navy dark:text-white text-sm">Cloud &amp; Remote Destinations</h2>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
              Every completed database backup and device config backup below is also pushed to each enabled destination (AWS S3, Azure Blob Storage, or a remote server over SFTP).
            </p>
          </div>
          <button
            onClick={() => setShowAddDest(true)}
            className="flex items-center gap-1.5 bg-brandblue text-white font-bold px-3 py-1.5 rounded-lg text-xs hover:opacity-90 shrink-0"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M12 5v14M5 12h14" /></svg>
            Add Destination
          </button>
        </div>

        {destError && <div className="m-4 p-3 rounded-lg bg-red-50 text-red-700 text-sm">{destError}</div>}

        {destLoading ? (
          <div className="text-center py-6 text-slate-400 text-sm">Loading…</div>
        ) : destinations.length === 0 ? (
          <div className="text-center py-6 text-slate-400 text-sm">No cloud or remote destinations configured. Backups are stored locally only.</div>
        ) : (
          <div className="divide-y divide-slate-100 dark:divide-slate-700/50">
            {destinations.map((d) => (
              <div key={d.id} className="flex items-center justify-between px-4 py-3">
                <div className="flex items-center gap-3 min-w-0">
                  <span className={`px-2 py-0.5 rounded-full text-[11px] font-bold shrink-0 ${
                    d.type === "s3" ? "bg-amber-100 text-amber-700"
                      : d.type === "azure_blob" ? "bg-blue-100 text-blue-700"
                      : "bg-purple-100 text-purple-700"
                  }`}>
                    {DEST_TYPE_LABEL[d.type] || d.type}
                  </span>
                  <div className="min-w-0">
                    <div className="font-bold text-navy dark:text-white text-sm truncate">{d.name}</div>
                    <div className="text-xs text-slate-400 truncate">
                      {d.enabled ? "Enabled" : "Disabled"}
                      {d.last_run_at && (
                        <>
                          {" · Last run "}
                          <span className={d.last_run_status === "failed" ? "text-red-500 font-bold" : "text-emerald-600 font-bold"}>
                            {d.last_run_status === "failed" ? "failed" : "OK"}
                          </span>
                          {" "}{fmtDate(d.last_run_at)}
                        </>
                      )}
                      {d.last_run_status === "failed" && d.last_error && (
                        <span className="block text-red-500 truncate" title={d.last_error}>{d.last_error}</span>
                      )}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  <button
                    disabled={destActioningId === d.id}
                    onClick={() => testDestination(d)}
                    className="text-xs font-bold text-brandblue hover:underline disabled:opacity-40"
                  >
                    Test
                  </button>
                  <button
                    disabled={destActioningId === d.id}
                    onClick={() => setEditingDest(d)}
                    className="text-xs font-bold text-slate-500 hover:underline disabled:opacity-40"
                  >
                    Edit
                  </button>
                  <button
                    disabled={destActioningId === d.id}
                    onClick={() => toggleDestEnabled(d)}
                    className="text-xs font-bold text-slate-500 hover:underline disabled:opacity-40"
                  >
                    {d.enabled ? "Disable" : "Enable"}
                  </button>
                  <button
                    disabled={destActioningId === d.id}
                    onClick={() => removeDestination(d)}
                    className="text-xs font-bold text-red-600 hover:underline disabled:opacity-40"
                  >
                    Remove
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
        </>
      )}

      {!isMspStaff && (
        <div className="mb-6">
          <h1 className="text-2xl font-bold text-navy dark:text-white">Device Configuration Backups</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400">Per-device running-config snapshots for your fleet.</p>
        </div>
      )}

      {/* Device Config Backups */}
      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl overflow-hidden mb-6">
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-100 dark:border-slate-700/50">
          <div>
            <h2 className="font-bold text-navy dark:text-white text-sm">Device Configuration Backups</h2>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5">
              Per-device running-config snapshots, separate from the application database backups above.
              {neverBackedUp > 0 && (
                <span className="text-amber-600 font-bold"> {neverBackedUp} device{neverBackedUp === 1 ? "" : "s"} never backed up.</span>
              )}
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <label className="text-xs text-slate-500 dark:text-slate-400 font-medium" htmlFor="backup-scope">
              Save to
            </label>
            <select
              id="backup-scope"
              value={backupScope}
              onChange={(e) => setBackupScope(e.target.value)}
              className="text-xs border border-slate-200 dark:border-slate-700 rounded-lg px-2 py-1.5 bg-white dark:bg-slate-900 text-slate-700 dark:text-slate-200"
              title="Where 'Back Up Now' / 'Back Up All Devices Now' should also copy the config, in addition to always being saved locally"
            >
              <option value="local">Locally only</option>
              <option value="all">Locally + all destinations</option>
              {destinations.map((d) => (
                <option key={d.id} value={d.id}>
                  Locally + {d.name}
                </option>
              ))}
            </select>
            <button
              onClick={runFleetBackup}
              disabled={runningFleetBackup || deviceLoading}
              className="flex items-center gap-1.5 bg-brandblue text-white font-bold px-3 py-1.5 rounded-lg text-xs hover:opacity-90 disabled:opacity-50 shrink-0"
            >
              {runningFleetBackup ? "Backing up…" : "Back Up All Devices Now"}
            </button>
          </div>
        </div>

        {deviceError && <div className="m-4 p-3 rounded-lg bg-red-50 text-red-700 text-sm">{deviceError}</div>}

        {deviceLoading ? (
          <div className="text-center py-6 text-slate-400 text-sm">Loading…</div>
        ) : deviceBackups.length === 0 ? (
          <div className="text-center py-6 text-slate-400 text-sm">No managed devices found.</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-slate-50 dark:bg-slate-900/40 text-[11px] uppercase tracking-wide text-slate-400 font-bold">
              <tr>
                <th className="text-left py-2.5 px-4">Device</th>
                <th className="text-left py-2.5 px-4">Vendor</th>
                <th className="text-left py-2.5 px-4">Backups on File</th>
                <th className="text-left py-2.5 px-4">Last Backup</th>
                <th className="text-right py-2.5 px-4">Actions</th>
              </tr>
            </thead>
            <tbody>
              {deviceBackups.map((row) => (
                <tr key={row.device_id} className="border-t border-slate-100 dark:border-slate-700/50">
                  <td className="py-2.5 px-4">
                    <div className="font-bold text-navy dark:text-white text-sm">{row.hostname}</div>
                    <div className="text-xs text-slate-400 font-mono">{row.ip_address || "—"}</div>
                  </td>
                  <td className="py-2.5 px-4 text-slate-500 dark:text-slate-400 capitalize">{row.vendor || "—"}</td>
                  <td className="py-2.5 px-4 text-slate-500 dark:text-slate-400">{row.backup_count}</td>
                  <td className="py-2.5 px-4">
                    {row.last_backup_at ? (
                      <>
                        <div className="text-slate-600 dark:text-slate-300">{fmtDate(row.last_backup_at)}</div>
                        <div className={`text-[11px] ${row.days_since_backup !== null && row.days_since_backup > 7 ? "text-amber-600 font-bold" : "text-slate-400"}`}>
                          {row.days_since_backup === 0 ? "today" : `${row.days_since_backup}d ago`}
                          {row.last_backup_version !== null && ` · v${row.last_backup_version}`}
                        </div>
                      </>
                    ) : (
                      <span className="text-xs text-amber-600 font-bold">Never backed up</span>
                    )}
                  </td>
                  <td className="py-2.5 px-4 text-right">
                    <button
                      disabled={deviceActioningId === row.device_id}
                      onClick={() => runDeviceBackup(row)}
                      className="text-xs font-bold text-brandblue hover:underline disabled:opacity-40 disabled:no-underline"
                    >
                      {deviceActioningId === row.device_id ? "Backing up…" : "Back Up Now"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
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
              <th className="text-left py-2.5 px-4">Off-site</th>
              <th className="text-right py-2.5 px-4">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={8} className="text-center py-8 text-slate-400">Loading…</td></tr>
            ) : backups.length === 0 ? (
              <tr><td colSpan={8} className="text-center py-8 text-slate-400">No backups yet. Run one to get started.</td></tr>
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
                  <td className="py-2.5 px-4">
                    {!b.offsite_results || b.offsite_results.length === 0 ? (
                      <span className="text-xs text-slate-400">—</span>
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {b.offsite_results.map((r) => (
                          <span
                            key={r.destination_id}
                            title={r.status === "failed" ? (r.error || "Upload failed") : `${r.name}: uploaded`}
                            className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                              r.status === "failed" ? "bg-red-100 text-red-700" : "bg-emerald-100 text-emerald-700"
                            }`}
                          >
                            {r.name}
                          </span>
                        ))}
                      </div>
                    )}
                  </td>
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

      {showAddDest && (
        <AddDestinationModal
          onClose={() => setShowAddDest(false)}
          onCreated={loadDestinations}
        />
      )}

      {editingDest && (
        <EditDestinationModal
          destination={editingDest}
          onClose={() => setEditingDest(null)}
          onSaved={loadDestinations}
        />
      )}
    </div>
  );
}

function EditDestinationModal({
  destination,
  onClose,
  onSaved,
}: {
  destination: BackupDestination;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(destination.name);
  // Only pre-fill non-secret fields with their real value -- secret
  // fields come back from GET as `true`/`false` (see backend
  // backup_destination_service.masked_config), never the plaintext, so
  // those inputs start blank. Blank stays blank on submit (the backend
  // only overwrites a field if a non-empty value is sent), which is what
  // lets "just rename it" or "just flip enabled" work without having to
  // re-type every secret.
  const [fields, setFields] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    const fieldDefs = DEST_FIELDS[destination.type] || [];
    for (const f of fieldDefs) {
      if (!f.secret && typeof destination.config[f.key] === "string") {
        initial[f.key] = destination.config[f.key];
      }
    }
    return initial;
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setField = (key: string, value: string) => setFields((f) => ({ ...f, [key]: value }));

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const payload: Record<string, any> = { config: fields };
      if (name !== destination.name) payload.name = name;
      await api.patch(`/backups/destinations/${destination.id}`, payload);
      onSaved();
      onClose();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to update destination");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-white dark:bg-slate-800 rounded-xl w-full max-w-md p-6 max-h-[90vh] overflow-y-auto">
        <h2 className="text-lg font-bold text-navy dark:text-white mb-1">Edit Backup Destination</h2>
        <p className="text-xs text-slate-400 mb-4">{DEST_TYPE_LABEL[destination.type] || destination.type}</p>
        {error && <div className="mb-3 p-2.5 rounded-lg bg-red-50 text-red-700 text-xs">{error}</div>}
        <div className="space-y-3">
          <div>
            <label className="text-xs font-bold text-slate-500 block mb-1">Name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-sm"
            />
          </div>
          {(DEST_FIELDS[destination.type] || []).map((f) => {
            const isSet = f.secret && destination.config[f.key] === true;
            return (
              <div key={f.key}>
                <label className="text-xs font-bold text-slate-500 block mb-1">
                  {f.label}
                  {f.secret && (
                    <span className={`ml-1.5 font-normal normal-case ${isSet ? "text-emerald-600" : "text-slate-400"}`}>
                      {isSet ? "· currently set" : "· not set"}
                    </span>
                  )}
                </label>
                <input
                  type={f.secret ? "password" : "text"}
                  value={fields[f.key] || ""}
                  onChange={(e) => setField(f.key, e.target.value)}
                  placeholder={f.secret ? "Leave blank to keep current value" : f.placeholder}
                  className="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-sm"
                />
              </div>
            );
          })}
        </div>
        <div className="flex justify-end gap-2 mt-5">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm font-bold text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-700">Cancel</button>
          <button
            onClick={submit}
            disabled={submitting || !name}
            className="px-4 py-2 rounded-lg text-sm font-bold bg-brandblue text-white hover:opacity-90 disabled:opacity-50"
          >
            {submitting ? "Saving…" : "Save Changes"}
          </button>
        </div>
      </div>
    </div>
  );
}

function AddDestinationModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [type, setType] = useState<"s3" | "azure_blob" | "sftp">("s3");
  const [fields, setFields] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setField = (key: string, value: string) => setFields((f) => ({ ...f, [key]: value }));

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await api.post("/backups/destinations", { name, type, enabled: true, config: fields });
      onCreated();
      onClose();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to add destination");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-white dark:bg-slate-800 rounded-xl w-full max-w-md p-6 max-h-[90vh] overflow-y-auto">
        <h2 className="text-lg font-bold text-navy dark:text-white mb-4">Add Backup Destination</h2>
        {error && <div className="mb-3 p-2.5 rounded-lg bg-red-50 text-red-700 text-xs">{error}</div>}
        <div className="space-y-3">
          <div>
            <label className="text-xs font-bold text-slate-500 block mb-1">Name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Prod S3 Off-site"
              className="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-sm"
            />
          </div>
          <div>
            <label className="text-xs font-bold text-slate-500 block mb-1">Type</label>
            <select
              value={type}
              onChange={(e) => { setType(e.target.value as any); setFields({}); }}
              className="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-sm"
            >
              <option value="s3">AWS S3</option>
              <option value="azure_blob">Azure Blob Storage</option>
              <option value="sftp">Remote Server (SFTP)</option>
            </select>
          </div>
          {DEST_FIELDS[type].map((f) => (
            <div key={f.key}>
              <label className="text-xs font-bold text-slate-500 block mb-1">{f.label}</label>
              <input
                type={f.secret ? "password" : "text"}
                value={fields[f.key] || ""}
                onChange={(e) => setField(f.key, e.target.value)}
                placeholder={f.placeholder}
                className="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-sm"
              />
            </div>
          ))}
        </div>
        <div className="flex justify-end gap-2 mt-5">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm font-bold text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-700">Cancel</button>
          <button
            onClick={submit}
            disabled={submitting || !name}
            className="px-4 py-2 rounded-lg text-sm font-bold bg-brandblue text-white hover:opacity-90 disabled:opacity-50"
          >
            {submitting ? "Adding…" : "Add Destination"}
          </button>
        </div>
      </div>
    </div>
  );
}