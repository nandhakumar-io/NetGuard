import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import {
  Device,
  RunningConfig,
  StartupConfig,
  BackupHistoryEntry,
  CompareConfigResponse,
} from "../lib/types";
import ConfigDiff from "../components/ConfigDiff";

/** Full-page device configuration browser: pick a device, view its
 * running/startup config side by side, and compare any two snapshots.
 * This is the standalone counterpart to the "Configuration" tab embedded
 * in the Devices page -- useful when you land here directly (e.g. a
 * `?device=<id>` deep link) rather than drilling in from the inventory
 * table. Reuses the same /devices/{id}/config/* endpoints as that tab so
 * behavior stays consistent between the two.
 */
export default function DeviceConfiguration() {
  const [searchParams, setSearchParams] = useSearchParams();
  const deviceIdParam = searchParams.get("device");

  const [devices, setDevices] = useState<Device[]>([]);
  const [devicesLoading, setDevicesLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string>(deviceIdParam || "");

  const [running, setRunning] = useState<RunningConfig | null>(null);
  const [startup, setStartup] = useState<StartupConfig | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);

  const [history, setHistory] = useState<BackupHistoryEntry[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  const [baseId, setBaseId] = useState<string>("");
  const [targetId, setTargetId] = useState<string>("");
  const [compareResult, setCompareResult] = useState<CompareConfigResponse | null>(null);
  const [comparing, setComparing] = useState(false);
  const [compareError, setCompareError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<Device[]>("/devices")
      .then((res) => {
        setDevices(res.data);
        if (!selectedId && res.data.length > 0) {
          setSelectedId(deviceIdParam && res.data.some((d) => d.id === deviceIdParam) ? deviceIdParam : res.data[0].id);
        }
      })
      .finally(() => setDevicesLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectedDevice = useMemo(() => devices.find((d) => d.id === selectedId) || null, [devices, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    setSearchParams({ device: selectedId }, { replace: true });

    setConfigLoading(true);
    setConfigError(null);
    setRunning(null);
    setStartup(null);
    Promise.all([
      api.get<RunningConfig>(`/devices/${selectedId}/config/running`).catch(() => null),
      api.get<StartupConfig>(`/devices/${selectedId}/config/startup`).catch(() => null),
    ]).then(([runRes, startRes]) => {
      if (!runRes && !startRes) setConfigError("Failed to load configuration for this device.");
      if (runRes) setRunning(runRes.data);
      if (startRes) setStartup(startRes.data);
      setConfigLoading(false);
    });

    setHistoryLoading(true);
    setHistory([]);
    setBaseId("");
    setTargetId("");
    setCompareResult(null);
    setCompareError(null);
    api
      .get<BackupHistoryEntry[]>(`/devices/${selectedId}/config/backups`)
      .then((res) => setHistory(res.data))
      .catch(() => {})
      .finally(() => setHistoryLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  const runCompare = async () => {
    if (!selectedId) return;
    setComparing(true);
    setCompareError(null);
    setCompareResult(null);
    try {
      const res = await api.post<CompareConfigResponse>(`/devices/${selectedId}/config/compare`, {
        base_snapshot_id: baseId || null,
        target_snapshot_id: targetId || null,
      });
      setCompareResult(res.data);
    } catch (err: any) {
      setCompareError(err?.response?.data?.detail || "Failed to compare configurations.");
    } finally {
      setComparing(false);
    }
  };

  return (
    <div className="pb-16 flex flex-col gap-6 md:p-2">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy">Device Configuration</h1>
          <p className="text-sm text-slate-500 mt-1">
            Browse running/startup configuration and compare snapshots for any device.
          </p>
        </div>
        {!devicesLoading && devices.length > 0 && (
          <select
            value={selectedId}
            onChange={(e) => setSelectedId(e.target.value)}
            className="border border-slate-300 rounded-lg px-3 py-2 text-sm bg-white shadow-sm min-w-[220px]"
          >
            {devices.map((d) => (
              <option key={d.id} value={d.id}>
                {d.hostname} ({d.ip_address})
              </option>
            ))}
          </select>
        )}
      </div>

      {devicesLoading && <p className="text-sm text-slate-400">Loading devices...</p>}

      {!devicesLoading && devices.length === 0 && (
        <div className="bg-white border border-slate-200 rounded-xl p-10 text-center text-slate-400">
          No devices in inventory yet.
        </div>
      )}

      {!devicesLoading && selectedDevice && (
        <>
          <div className="bg-white border border-slate-200 rounded-xl shadow-sm p-5">
            <div className="flex items-center gap-3 mb-4">
              <h2 className="font-bold text-navy">{selectedDevice.hostname}</h2>
              <span className="text-xs text-slate-400 font-mono">{selectedDevice.ip_address}</span>
              <span className="text-xs bg-slate-100 border border-slate-200 rounded-full px-2 py-0.5 capitalize text-slate-600 font-semibold">
                {selectedDevice.vendor}
              </span>
            </div>

            {configLoading ? (
              <p className="text-xs text-slate-400">Loading configurations...</p>
            ) : configError ? (
              <p className="text-xs text-riskcrit">{configError}</p>
            ) : (
              <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                <div>
                  <h4 className="text-xs font-bold uppercase text-slate-500 mb-2">Running Configuration</h4>
                  <pre className="bg-slate-900 border border-slate-700 text-slate-300 text-[11px] rounded-lg p-3 overflow-x-auto max-h-[420px] whitespace-pre-wrap leading-relaxed shadow-inner">
                    {running?.config || "(no configuration available)"}
                  </pre>
                </div>
                <div>
                  <h4 className="text-xs font-bold uppercase text-slate-500 mb-2">Startup Configuration</h4>
                  {startup?.source === "unavailable" ? (
                    <p className="text-xs text-slate-400 italic mt-4">No startup configuration on file yet.</p>
                  ) : (
                    <pre className="bg-slate-900 border border-slate-700 text-slate-300 text-[11px] rounded-lg p-3 overflow-x-auto max-h-[420px] whitespace-pre-wrap leading-relaxed shadow-inner">
                      {startup?.config || "(no startup configuration available)"}
                    </pre>
                  )}
                </div>
              </div>
            )}
          </div>

          <div className="bg-white border border-slate-200 rounded-xl shadow-sm p-5">
            <h3 className="text-xs uppercase font-bold text-slate-500 mb-3 tracking-wider">Compare Snapshots</h3>
            {historyLoading ? (
              <p className="text-xs text-slate-400">Loading snapshot history...</p>
            ) : history.length === 0 ? (
              <p className="text-xs text-slate-400 italic">No snapshots on file for this device yet.</p>
            ) : (
              <div className="flex flex-col gap-4">
                <div className="flex flex-wrap gap-3 items-end">
                  <div>
                    <label className="text-[11px] font-bold uppercase text-slate-500 block mb-1">Base</label>
                    <select
                      value={baseId}
                      onChange={(e) => setBaseId(e.target.value)}
                      className="border border-slate-300 rounded-lg px-2.5 py-1.5 text-xs bg-white"
                    >
                      <option value="">Current running config</option>
                      {history.map((s) => (
                        <option key={s.id} value={s.id}>
                          v{s.version} — {new Date(s.created_at).toLocaleString()}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="text-[11px] font-bold uppercase text-slate-500 block mb-1">Target</label>
                    <select
                      value={targetId}
                      onChange={(e) => setTargetId(e.target.value)}
                      className="border border-slate-300 rounded-lg px-2.5 py-1.5 text-xs bg-white"
                    >
                      <option value="">Current running config</option>
                      {history.map((s) => (
                        <option key={s.id} value={s.id}>
                          v{s.version} — {new Date(s.created_at).toLocaleString()}
                        </option>
                      ))}
                    </select>
                  </div>
                  <button
                    onClick={runCompare}
                    disabled={comparing}
                    className="text-xs font-bold uppercase tracking-wide text-white bg-brandblue hover:bg-navy px-4 py-2 rounded-lg shadow-sm disabled:opacity-50"
                  >
                    {comparing ? "Comparing…" : "Compare"}
                  </button>
                </div>

                {compareError && <p className="text-xs text-riskcrit">{compareError}</p>}

                {compareResult && (
                  <div>
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-xs font-semibold text-slate-600">
                        {compareResult.base_label} → {compareResult.target_label}
                      </span>
                      {compareResult.identical ? (
                        <span className="text-[11px] bg-green-50 text-green-700 border border-green-200 rounded-full px-2 py-0.5 font-bold">
                          Identical
                        </span>
                      ) : (
                        <span className="text-[11px] bg-amber-50 text-riskmed border border-amber-200 rounded-full px-2 py-0.5 font-bold">
                          Differs
                        </span>
                      )}
                    </div>
                    <ConfigDiff diffText={compareResult.diff} />
                  </div>
                )}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}