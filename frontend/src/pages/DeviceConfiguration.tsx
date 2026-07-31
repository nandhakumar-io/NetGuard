import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  BackupHistoryEntry,
  CompareConfigResponse,
  Device,
  RunningConfig,
  StartupConfig,
} from "../lib/types";
import ConfigDiff from "../components/ConfigDiff";

type Tab = "running" | "startup" | "history" | "compare";

export default function DeviceConfiguration() {
  const { user } = useAuth();
  const canManage = user?.role === "network_admin";

  const [devices, setDevices] = useState<Device[]>([]);
  const [deviceId, setDeviceId] = useState<string>("");
  const [tab, setTab] = useState<Tab>("running");

  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Running config
  const [running, setRunning] = useState<RunningConfig | null>(null);
  const [runningLoading, setRunningLoading] = useState(false);

  // Startup config
  const [startup, setStartup] = useState<StartupConfig | null>(null);
  const [startupLoading, setStartupLoading] = useState(false);

  // Backup history
  const [history, setHistory] = useState<BackupHistoryEntry[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [backingUp, setBackingUp] = useState(false);

  // Restore
  const [restoreTarget, setRestoreTarget] = useState<BackupHistoryEntry | null>(null);
  const [restoreReason, setRestoreReason] = useState("");
  const [restoring, setRestoring] = useState(false);

  // Compare
  const [baseSnapshotId, setBaseSnapshotId] = useState<string>("");
  const [targetSnapshotId, setTargetSnapshotId] = useState<string>("");
  const [compareResult, setCompareResult] = useState<CompareConfigResponse | null>(null);
  const [comparing, setComparing] = useState(false);

  const device = useMemo(() => devices.find((d) => d.id === deviceId) || null, [devices, deviceId]);

  useEffect(() => {
    api
      .get<Device[]>("/devices")
      .then((res) => {
        setDevices(res.data);
        if (res.data.length > 0) setDeviceId(res.data[0].id);
      })
      .catch(() => setError("Failed to load devices. Is the backend running?"));
  }, []);

  const resetTabState = () => {
    setError(null);
    setNotice(null);
    setCompareResult(null);
  };

  const loadRunning = () => {
    if (!deviceId) return;
    setRunningLoading(true);
    setError(null);
    api
      .get<RunningConfig>(`/devices/${deviceId}/config/running`)
      .then((res) => setRunning(res.data))
      .catch((err) => setError(err?.response?.data?.detail || "Failed to read running configuration."))
      .finally(() => setRunningLoading(false));
  };

  const loadStartup = () => {
    if (!deviceId) return;
    setStartupLoading(true);
    setError(null);
    api
      .get<StartupConfig>(`/devices/${deviceId}/config/startup`)
      .then((res) => setStartup(res.data))
      .catch((err) => setError(err?.response?.data?.detail || "Failed to read startup configuration."))
      .finally(() => setStartupLoading(false));
  };

  const loadHistory = () => {
    if (!deviceId) return;
    setHistoryLoading(true);
    setError(null);
    api
      .get<BackupHistoryEntry[]>(`/devices/${deviceId}/config/backups`)
      .then((res) => setHistory(res.data))
      .catch((err) => setError(err?.response?.data?.detail || "Failed to load backup history."))
      .finally(() => setHistoryLoading(false));
  };

  useEffect(() => {
    if (!deviceId) return;
    resetTabState();
    setRunning(null);
    setStartup(null);
    setBaseSnapshotId("");
    setTargetSnapshotId("");
    if (tab === "running") loadRunning();
    if (tab === "startup") loadStartup();
    if (tab === "history" || tab === "compare") loadHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId, tab]);

  // Once backup history loads for the Compare tab, default "Base" to the
  // most recent backup (an explicit id) so the dropdown's selection always
  // matches what's actually being compared, rather than relying on the
  // backend's "both omitted" special case.
  useEffect(() => {
    if (tab === "compare" && !baseSnapshotId && history.length > 0) {
      setBaseSnapshotId(history[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, history]);

  const runBackup = async () => {
    if (!deviceId) return;
    setBackingUp(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.post(`/devices/${deviceId}/config/backup`, {});
      setNotice(res.data.message);
      loadHistory();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to back up configuration.");
    } finally {
      setBackingUp(false);
    }
  };

  const downloadBackup = (snapshotId: string) => {
    if (!deviceId) return;
    const url = `${api.defaults.baseURL}/devices/${deviceId}/config/backups/${snapshotId}/download`;
    const token = localStorage.getItem("netguard_token");
    fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      .then((res) => {
        if (!res.ok) throw new Error("download failed");
        return res.blob();
      })
      .then((blob) => {
        const link = document.createElement("a");
        link.href = window.URL.createObjectURL(blob);
        const cd = "config";
        link.download = `${device?.hostname || cd}_${snapshotId.slice(0, 8)}.cfg`;
        link.click();
        window.URL.revokeObjectURL(link.href);
      })
      .catch(() => setError("Failed to download configuration backup."));
  };

  const confirmRestore = async () => {
    if (!deviceId || !restoreTarget) return;
    setRestoring(true);
    setError(null);
    try {
      const res = await api.post(`/devices/${deviceId}/config/restore`, {
        snapshot_id: restoreTarget.id,
        reason: restoreReason || undefined,
      });
      setNotice(res.data.message);
      setRestoreTarget(null);
      setRestoreReason("");
      loadHistory();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to restore configuration.");
    } finally {
      setRestoring(false);
    }
  };

  const runCompare = async () => {
    if (!deviceId) return;
    setComparing(true);
    setError(null);
    setCompareResult(null);
    try {
      const res = await api.post<CompareConfigResponse>(`/devices/${deviceId}/config/compare`, {
        base_snapshot_id: baseSnapshotId || null,
        target_snapshot_id: targetSnapshotId || null,
      });
      setCompareResult(res.data);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to compare configurations.");
    } finally {
      setComparing(false);
    }
  };

  const tabs: { key: Tab; label: string }[] = [
    { key: "running", label: "Running Config" },
    { key: "startup", label: "Startup Config" },
    { key: "history", label: "Backup History" },
    { key: "compare", label: "Compare" },
  ];

  return (
    <div>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy">Device Configuration</h1>
          <p className="text-sm text-slate-500 mt-1">
            View, back up, restore, download, and compare device configurations.
          </p>
        </div>
        <select
          className="border border-slate-300 rounded-lg px-3 py-2 text-sm bg-white min-w-[220px]"
          value={deviceId}
          onChange={(e) => setDeviceId(e.target.value)}
        >
          {devices.length === 0 && <option value="">No devices</option>}
          {devices.map((d) => (
            <option key={d.id} value={d.id}>
              {d.hostname} ({d.ip_address})
            </option>
          ))}
        </select>
      </div>

      {error && <p className="text-riskcrit text-sm mt-4">{error}</p>}
      {notice && (
        <p className="text-xs text-brandblue bg-blue-50 border border-blue-100 rounded-lg px-3 py-2 mt-3">
          {notice}{" "}
          <button onClick={() => setNotice(null)} className="ml-2 text-slate-400 hover:text-slate-600">
            ✕
          </button>
        </p>
      )}

      <div className="flex gap-1 mt-5 border-b border-slate-200">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              tab === t.key
                ? "border-brandblue text-brandblue"
                : "border-transparent text-slate-500 hover:text-navy"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {!deviceId && (
        <p className="mt-6 text-sm text-slate-400 italic">Add a device on the Devices page to get started.</p>
      )}

      {/* --- Running Config --- */}
      {deviceId && tab === "running" && (
        <div className="mt-5 bg-white border border-slate-200 rounded-xl p-5">
          <div className="flex items-center justify-between mb-3">
            <p className="text-xs text-slate-500">
              Live read from {device?.hostname}
              {running && (
                <>
                  {" "}
                  via <span className="uppercase font-semibold text-slate-600">{running.protocol}</span> ·{" "}
                  {new Date(running.retrieved_at).toLocaleString()}
                </>
              )}
            </p>
            <button
              onClick={loadRunning}
              disabled={runningLoading}
              className="text-xs text-brandblue font-medium hover:text-navy disabled:opacity-50"
            >
              {runningLoading ? "Loading…" : "↻ Refresh"}
            </button>
          </div>
          {runningLoading && <p className="text-xs text-slate-400">Loading running configuration…</p>}
          {!runningLoading && running && (
            <pre className="bg-slate-900 text-slate-200 text-xs rounded-lg p-4 overflow-x-auto max-h-[520px] whitespace-pre-wrap leading-relaxed">
              {running.config || "(empty configuration)"}
            </pre>
          )}
        </div>
      )}

      {/* --- Startup Config --- */}
      {deviceId && tab === "startup" && (
        <div className="mt-5 bg-white border border-slate-200 rounded-xl p-5">
          <div className="flex items-center justify-between mb-3">
            <p className="text-xs text-slate-500">
              {startup?.source === "snapshot"
                ? "From the most recent configuration backup"
                : "Source of truth for boot-time configuration"}
            </p>
            <button
              onClick={loadStartup}
              disabled={startupLoading}
              className="text-xs text-brandblue font-medium hover:text-navy disabled:opacity-50"
            >
              {startupLoading ? "Loading…" : "↻ Refresh"}
            </button>
          </div>
          {startupLoading && <p className="text-xs text-slate-400">Loading startup configuration…</p>}
          {!startupLoading && startup && startup.source === "unavailable" && (
            <p className="text-xs text-slate-400 italic">
              No startup configuration on file yet — take a backup to capture one.
            </p>
          )}
          {!startupLoading && startup && startup.config && (
            <pre className="bg-slate-900 text-slate-200 text-xs rounded-lg p-4 overflow-x-auto max-h-[520px] whitespace-pre-wrap leading-relaxed">
              {startup.config}
            </pre>
          )}
        </div>
      )}

      {/* --- Backup History --- */}
      {deviceId && tab === "history" && (
        <div className="mt-5">
          <div className="flex items-center justify-between mb-3">
            <p className="text-xs text-slate-500">
              {history.length} backup{history.length === 1 ? "" : "s"} on file for {device?.hostname}
            </p>
            {canManage && (
              <button
                onClick={runBackup}
                disabled={backingUp}
                className="bg-brandblue text-white rounded-lg px-4 py-2 text-xs font-semibold hover:bg-navy transition-colors disabled:opacity-50"
              >
                {backingUp ? "Backing up…" : "+ Backup Now"}
              </button>
            )}
          </div>

          <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-navy text-white">
                <tr>
                  <th className="text-left px-4 py-3 font-semibold">Version</th>
                  <th className="text-left px-4 py-3 font-semibold">Checksum</th>
                  <th className="text-left px-4 py-3 font-semibold">Created</th>
                  <th className="text-left px-4 py-3 font-semibold">Startup Config</th>
                  <th className="text-right px-4 py-3 font-semibold">Actions</th>
                </tr>
              </thead>
              <tbody>
                {historyLoading && (
                  <tr>
                    <td colSpan={5} className="text-center text-slate-400 py-8">
                      Loading backup history…
                    </td>
                  </tr>
                )}
                {!historyLoading && history.length === 0 && (
                  <tr>
                    <td colSpan={5} className="text-center text-slate-400 py-8">
                      No backups yet.{canManage ? ' Click "Backup Now" to take one.' : ""}
                    </td>
                  </tr>
                )}
                {history.map((h, i) => (
                  <tr key={h.id} className={i % 2 ? "bg-slate-50" : "bg-white"}>
                    <td className="px-4 py-3 font-mono text-navy">
                      v{h.version}
                      {i === 0 && (
                        <span className="ml-2 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-slate-100 text-slate-500">
                          latest
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-500">{h.checksum.slice(0, 16)}…</td>
                    <td className="px-4 py-3 text-slate-600">{new Date(h.created_at).toLocaleString()}</td>
                    <td className="px-4 py-3 text-slate-500">{h.has_startup_config ? "Yes" : "—"}</td>
                    <td className="px-4 py-3 text-right space-x-3 whitespace-nowrap">
                      <button
                        onClick={() => downloadBackup(h.id)}
                        className="text-xs text-brandblue font-medium hover:text-navy"
                      >
                        ⭳ Download
                      </button>
                      {canManage && (
                        <button
                          onClick={() => {
                            setRestoreTarget(h);
                            setRestoreReason("");
                          }}
                          className="text-xs text-riskcrit font-medium hover:text-red-800"
                        >
                          ↺ Restore
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!canManage && (
            <p className="mt-3 text-[11px] text-slate-400 italic">
              Only a Network Administrator can take a backup or restore a configuration.
            </p>
          )}
        </div>
      )}

      {/* --- Compare --- */}
      {deviceId && tab === "compare" && (
        <div className="mt-5 bg-white border border-slate-200 rounded-xl p-5">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Base</label>
              <select
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                value={baseSnapshotId}
                onChange={(e) => setBaseSnapshotId(e.target.value)}
              >
                <option value="">Live running config</option>
                {history.map((h) => (
                  <option key={h.id} value={h.id}>
                    v{h.version} · {new Date(h.created_at).toLocaleString()}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Target</label>
              <select
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                value={targetSnapshotId}
                onChange={(e) => setTargetSnapshotId(e.target.value)}
              >
                <option value="">Live running config</option>
                {history.map((h) => (
                  <option key={h.id} value={h.id}>
                    v{h.version} · {new Date(h.created_at).toLocaleString()}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <button
            onClick={runCompare}
            disabled={comparing}
            className="mt-4 bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
          >
            {comparing ? "Comparing…" : "Compare"}
          </button>

          {compareResult && (
            <div className="mt-5">
              <div className="flex items-center gap-3 text-xs text-slate-500 mb-2">
                <span>
                  <span className="font-semibold text-navy">{compareResult.base_label}</span> vs{" "}
                  <span className="font-semibold text-navy">{compareResult.target_label}</span>
                </span>
                {compareResult.identical && (
                  <span className="px-2 py-0.5 rounded bg-green-50 text-risklow font-semibold">Identical</span>
                )}
              </div>
              <ConfigDiff diffText={compareResult.identical ? null : compareResult.diff} />
              {compareResult.identical && (
                <p className="text-xs text-slate-400 italic mt-2">No differences between the two configurations.</p>
              )}
            </div>
          )}
        </div>
      )}

      {/* --- Restore confirmation modal --- */}
      {restoreTarget && (
        <div className="fixed inset-0 bg-navy/40 flex items-center justify-center z-50 px-4">
          <div className="bg-white rounded-xl shadow-xl max-w-md w-full p-5">
            <h3 className="font-semibold text-navy">Restore {device?.hostname}?</h3>
            <p className="text-xs text-slate-500 mt-2">
              This immediately pushes backup <span className="font-mono">v{restoreTarget.version}</span> (
              {new Date(restoreTarget.created_at).toLocaleString()}) to the device. A safety snapshot of the
              current live config is taken first. For a governed restore that runs through change validation and
              automatic health-check rollback, use "Roll back" from the Devices page instead.
            </p>
            <label className="block text-xs font-medium text-slate-600 mt-4 mb-1">Reason (optional)</label>
            <input
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
              placeholder="e.g. reverting unauthorized change"
              value={restoreReason}
              onChange={(e) => setRestoreReason(e.target.value)}
            />
            <div className="flex gap-2 justify-end mt-5">
              <button
                onClick={() => setRestoreTarget(null)}
                disabled={restoring}
                className="px-4 py-2 text-sm font-medium text-slate-500 hover:text-slate-700 disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={confirmRestore}
                disabled={restoring}
                className="bg-riskcrit text-white rounded-lg px-4 py-2 text-sm font-semibold hover:opacity-90 disabled:opacity-50"
              >
                {restoring ? "Restoring…" : "Confirm restore"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}