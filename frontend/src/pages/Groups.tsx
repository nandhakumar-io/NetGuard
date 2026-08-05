import React, { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";

interface GroupDevice {
  id: string;
  hostname: string;
  status: "online" | "offline" | "degraded" | "unknown";
  device_type: string | null;
  rack_position: number | null;
}

interface GroupRack {
  name: string;
  devices: GroupDevice[];
}

interface GroupDataCenter {
  name: string;
  device_count: number;
  racks: GroupRack[];
}

const statusColor: Record<string, string> = {
  online: "bg-risklow",
  offline: "bg-slate-400",
  degraded: "bg-riskmed",
  unknown: "bg-slate-300 dark:bg-slate-600",
};

function RackCard({
  rack,
  dcName,
  canManage,
  onMove,
}: {
  rack: GroupRack;
  dcName: string;
  canManage: boolean;
  onMove: (deviceId: string, dataCenter: string, rack: string) => void;
}) {
  const [dragOver, setDragOver] = useState(false);

  return (
    <div
      className={`bg-white dark:bg-slate-800 border rounded-xl shadow-sm overflow-hidden transition-colors ${
        dragOver ? "border-brandblue ring-2 ring-brandblue/30" : "border-slate-200 dark:border-slate-700"
      }`}
      onDragOver={(e) => {
        if (!canManage) return;
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        if (!canManage) return;
        e.preventDefault();
        setDragOver(false);
        const deviceId = e.dataTransfer.getData("text/device-id");
        if (deviceId) onMove(deviceId, dcName, rack.name);
      }}
    >
      <div className="bg-slate-100 dark:bg-slate-900 px-4 py-2 flex items-center justify-between border-b border-slate-200 dark:border-slate-700">
        <p className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 flex items-center gap-1.5">
          🗄️ Rack: {rack.name}
        </p>
        <span className="text-[11px] text-slate-400 dark:text-slate-500">{rack.devices.length} device{rack.devices.length === 1 ? "" : "s"}</span>
      </div>
      <div className="divide-y divide-slate-100 dark:divide-slate-800">
        {rack.devices.length === 0 && (
          <p className="text-xs text-slate-400 dark:text-slate-500 italic px-4 py-3">Empty rack — drag a device here.</p>
        )}
        {rack.devices.map((d) => (
          <Link
            key={d.id}
            to={`/devices?q=${encodeURIComponent(d.hostname)}`}
            draggable={canManage}
            onDragStart={(e) => e.dataTransfer.setData("text/device-id", d.id)}
            className="flex items-center gap-2 px-4 py-2 hover:bg-slate-50 dark:hover:bg-slate-900 transition-colors group"
          >
            {d.rack_position != null && (
              <span className="text-[10px] font-mono text-slate-400 dark:text-slate-500 w-6 text-right">U{d.rack_position}</span>
            )}
            <span className={`w-2 h-2 rounded-full shrink-0 ${statusColor[d.status] || statusColor.unknown}`} />
            <span className="text-sm font-medium text-navy dark:text-white group-hover:text-brandblue truncate">{d.hostname}</span>
            {d.device_type && (
              <span className="ml-auto text-[10px] uppercase tracking-wide text-slate-400 dark:text-slate-500">{d.device_type}</span>
            )}
          </Link>
        ))}
      </div>
    </div>
  );
}

export default function Groups() {
  const { user } = useAuth();
  const canManage = user?.role === "network_admin";
  const [groups, setGroups] = useState<GroupDataCenter[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [moveNotice, setMoveNotice] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    api
      .get<GroupDataCenter[]>("/devices/groups/summary")
      .then((res) => {
        setGroups(res.data);
        setError(null);
      })
      .catch(() => setError("Failed to load groups."))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const handleMove = async (deviceId: string, dataCenter: string, rack: string) => {
    try {
      await api.patch(`/devices/${deviceId}`, {
        data_center: dataCenter === "Unassigned" ? null : dataCenter,
        rack: rack === "Unassigned" ? null : rack,
      });
      setMoveNotice(`Moved device to ${dataCenter} / ${rack}.`);
      load();
      setTimeout(() => setMoveNotice(null), 3000);
    } catch {
      setError("Failed to move device.");
    }
  };

  const filteredGroups = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return groups;
    return groups
      .map((dc) => ({
        ...dc,
        racks: dc.racks
          .map((r) => ({
            ...r,
            devices: r.devices.filter((d) => d.hostname.toLowerCase().includes(q)),
          }))
          .filter((r) => r.devices.length > 0 || r.name.toLowerCase().includes(q)),
      }))
      .filter((dc) => dc.racks.length > 0 || dc.name.toLowerCase().includes(q));
  }, [groups, query]);

  const totals = useMemo(() => {
    const dcCount = groups.length;
    const rackCount = groups.reduce((n, dc) => n + dc.racks.length, 0);
    const deviceCount = groups.reduce((n, dc) => n + dc.device_count, 0);
    return { dcCount, rackCount, deviceCount };
  }, [groups]);

  return (
    <div className="pb-16 flex flex-col gap-6 md:p-2">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-bold text-navy dark:text-white">Groups</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Devices organized by Data Center → Rack. {canManage ? "Drag a device onto a rack to move it." : ""}
          </p>
        </div>
        <button
          onClick={load}
          className="text-brandblue font-medium hover:text-navy dark:hover:text-white bg-white dark:bg-slate-800 border border-brandblue hover:bg-slate-50 dark:hover:bg-slate-700 px-3 py-1.5 rounded-full transition shadow-sm text-xs"
        >
          ↻ Refresh
        </button>
      </div>

      <div className="flex flex-wrap gap-3">
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 shadow-sm rounded-lg px-4 py-2 text-[13px] text-slate-500 dark:text-slate-400">
          Data Centers <span className="text-navy dark:text-white font-bold ml-1">{totals.dcCount}</span>
        </div>
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 shadow-sm rounded-lg px-4 py-2 text-[13px] text-slate-500 dark:text-slate-400">
          Racks <span className="text-navy dark:text-white font-bold ml-1">{totals.rackCount}</span>
        </div>
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 shadow-sm rounded-lg px-4 py-2 text-[13px] text-slate-500 dark:text-slate-400">
          Devices <span className="text-navy dark:text-white font-bold ml-1">{totals.deviceCount}</span>
        </div>
      </div>

      <input
        className="border border-slate-300 dark:border-slate-600 shadow-sm rounded-full px-4 py-1.5 text-sm w-full max-w-sm focus:ring-2 focus:ring-brandblue focus:border-transparent outline-none"
        placeholder="Search hostname, data center, or rack…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />

      {error && (
        <p className="text-riskcrit font-semibold text-sm bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-800 px-3 py-2 rounded-lg">
          {error}
        </p>
      )}
      {moveNotice && (
        <p className="text-[13px] font-medium text-brandblue bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-800 shadow-sm rounded-lg px-4 py-2.5">
          {moveNotice}
        </p>
      )}

      {loading ? (
        <p className="text-sm text-slate-400 dark:text-slate-500 italic">Loading groups…</p>
      ) : filteredGroups.length === 0 ? (
        <p className="text-sm text-slate-400 dark:text-slate-500 italic">No devices match.</p>
      ) : (
        <div className="flex flex-col gap-6">
          {filteredGroups.map((dc) => (
            <div key={dc.name} className="bg-slate-50 dark:bg-slate-900/40 border border-slate-200 dark:border-slate-700 rounded-2xl p-4">
              <div className="flex items-center justify-between mb-3 px-1">
                <h2 className="text-lg font-bold text-navy dark:text-white flex items-center gap-2">
                  🏢 {dc.name}
                </h2>
                <span className="text-xs text-slate-400 dark:text-slate-500">{dc.device_count} device{dc.device_count === 1 ? "" : "s"} · {dc.racks.length} rack{dc.racks.length === 1 ? "" : "s"}</span>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {dc.racks.map((rack) => (
                  <RackCard key={rack.name} rack={rack} dcName={dc.name} canManage={canManage} onMove={handleMove} />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}