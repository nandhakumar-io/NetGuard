import React, { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useConfirm } from "../lib/confirm";
import { Device, DeviceGroup, DeviceGroupRule, DeviceGroupRuleMatch, GroupHealthRollup } from "../lib/types";

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

// Enterprise physical-placement hierarchy: Company (implicit, single-
// tenant) -> Block -> Data Center -> Rack -> Device. `block` is the top
// grouping level -- a campus/region/business-unit that can own one or
// more data centers -- with `data_center` nested under it and `rack`
// nested under that. See backend app.api.devices.get_device_groups.
interface GroupDataCenter {
  name: string;
  device_count: number;
  racks: GroupRack[];
}

interface GroupBlock {
  name: string;
  device_count: number;
  data_centers: GroupDataCenter[];
}

const statusColor: Record<string, string> = {
  online: "bg-emerald-500",
  offline: "bg-slate-400",
  degraded: "bg-amber-500",
  unknown: "bg-slate-300 dark:bg-slate-600",
};

const statusRing: Record<string, string> = {
  online: "ring-emerald-500/30",
  offline: "ring-slate-400/30",
  degraded: "ring-amber-500/30",
  unknown: "ring-slate-400/20",
};

function healthRollup(devices: GroupDevice[]) {
  const counts = { online: 0, degraded: 0, offline: 0, unknown: 0 };
  for (const d of devices) {
    counts[d.status in counts ? (d.status as keyof typeof counts) : "unknown"]++;
  }
  return counts;
}

function HealthBar({ devices }: { devices: GroupDevice[] }) {
  const c = healthRollup(devices);
  const total = devices.length;
  if (total === 0) return null;
  const pct = (n: number) => `${((n / total) * 100).toFixed(1)}%`;
  return (
    <div className="flex items-center gap-2">
      <div className="flex h-1.5 w-20 rounded-full overflow-hidden bg-slate-200 dark:bg-slate-700">
        <div className="bg-emerald-500" style={{ width: pct(c.online) }} />
        <div className="bg-amber-500" style={{ width: pct(c.degraded) }} />
        <div className="bg-slate-400" style={{ width: pct(c.offline) }} />
      </div>
      <span className="text-[10px] font-semibold text-emerald-600 dark:text-emerald-400">{c.online}/{total}</span>
    </div>
  );
}

function HealthBadge({ devices }: { devices: GroupDevice[] }) {
  const c = healthRollup(devices);
  if (devices.length === 0) return null;
  return (
    <div className="flex items-center gap-2 text-[11px] font-semibold">
      {c.online > 0 && <span className="text-emerald-600 dark:text-emerald-400 flex items-center gap-1">● {c.online} up</span>}
      {c.degraded > 0 && <span className="text-amber-500 flex items-center gap-1">● {c.degraded} degraded</span>}
      {c.offline > 0 && <span className="text-slate-400 dark:text-slate-500 flex items-center gap-1">● {c.offline} down</span>}
      {c.unknown > 0 && <span className="text-slate-300 dark:text-slate-600 flex items-center gap-1">● {c.unknown} unknown</span>}
    </div>
  );
}

function RackCard({
  rack,
  dcName,
  blockName,
  canManage,
  onMove,
  onAddDevice,
  onRename,
  onDelete,
  selected,
  onToggleSelect,
  onDeviceAction,
}: {
  rack: GroupRack;
  dcName: string;
  blockName: string;
  canManage: boolean;
  onMove: (deviceId: string, dataCenter: string, block: string, rack: string) => void;
  onAddDevice: (dc: string, block: string, rack: string) => void;
  selected: Set<string>;
  onToggleSelect: (deviceId: string) => void;
  onRename: (oldName: string, type: "data_center"| "block" | "rack", dcName: string, blockName?: string) => void;
  onDelete: (name: string, type: "data_center"| "block" | "rack", dcName: string, blockName?: string) => void;
  onDeviceAction: (action: "edit" | "remove" | "replace", device: GroupDevice, dcName: string, blockName: string, rackName: string) => void;
}) {
  const [dragOver, setDragOver] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const c = healthRollup(rack.devices);
  const total = rack.devices.length;

  return (
    <div
      className={`rounded-xl overflow-hidden border transition-all duration-200 ${
        dragOver
          ? "border-blue-500 ring-2 ring-blue-500/25 shadow-lg shadow-blue-500/10"
          : "border-slate-200 dark:border-slate-700 shadow-sm hover:shadow-md"
      } bg-white dark:bg-slate-800`}
      onDragOver={(e) => { if (!canManage) return; e.preventDefault(); setDragOver(true); }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        if (!canManage) return;
        e.preventDefault(); setDragOver(false);
        const deviceId = e.dataTransfer.getData("text/device-id");
        if (deviceId) onMove(deviceId, dcName, blockName, rack.name);
      }}
    >
      {/* Rack header */}
      <div className="px-3 py-2 flex items-center gap-2 bg-gradient-to-r from-slate-100 to-slate-50 dark:from-slate-900 dark:to-slate-800/60 border-b border-slate-200 dark:border-slate-700">
        <button onClick={() => setCollapsed(c => !c)} className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 shrink-0">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
            className={`transition-transform duration-200 ${collapsed ? "-rotate-90" : ""}`}>
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
        <span className="text-[11px] font-mono font-bold text-slate-500 dark:text-slate-400 shrink-0">🗄</span>
        <span className="text-xs font-bold text-slate-700 dark:text-slate-200 truncate flex-1">{rack.name}</span>
        <HealthBar devices={rack.devices} />
        <span className="text-[10px] text-slate-400 shrink-0">{total}U</span>
        {canManage && rack.name !== "Unassigned" && (
          <>
            <button onClick={() => onRename(rack.name, "rack", dcName, blockName)} title="Rename"
              className="text-[11px] text-slate-400 hover:text-blue-500 shrink-0 leading-none">✎</button>
            <button onClick={() => onDelete(rack.name, "rack", dcName, blockName)} title="Delete"
              className="text-[11px] text-slate-400 hover:text-red-500 shrink-0 leading-none">✕</button>
          </>
        )}
        {canManage && (
          <button onClick={() => onAddDevice(dcName, blockName, rack.name)}
            className="text-[10px] font-bold text-blue-600 dark:text-blue-400 hover:text-blue-800 shrink-0 border border-blue-300 dark:border-blue-700 rounded px-1.5 py-0.5 transition-colors">
            + Add
          </button>
        )}
      </div>

      {/* Utilization bar */}
      {total > 0 && (
        <div className="h-0.5 flex">
          <div className="bg-emerald-500 transition-all" style={{ width: `${(c.online/total)*100}%` }} />
          <div className="bg-amber-400 transition-all" style={{ width: `${(c.degraded/total)*100}%` }} />
          <div className="bg-slate-300 dark:bg-slate-600 transition-all" style={{ width: `${(c.offline/total)*100}%` }} />
        </div>
      )}

      {/* Device list */}
      {!collapsed && (
        <div className="divide-y divide-slate-100 dark:divide-slate-700/50">
          {total === 0 ? (
            <div className="px-4 py-4 flex flex-col items-center gap-1">
              <svg width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24" className="text-slate-300 dark:text-slate-600">
                <rect x="2" y="3" width="20" height="18" rx="2"/><path d="M8 7h8M8 12h4"/>
              </svg>
              <p className="text-[11px] text-slate-400 italic">
                {canManage ? "Empty — drag a device here or use + Add" : "Empty rack"}
              </p>
            </div>
          ) : (
            rack.devices.map((d) => (
              <div key={d.id} draggable={canManage}
                onDragStart={(e) => e.dataTransfer.setData("text/device-id", d.id)}
                className="flex items-center gap-2 px-3 py-1.5 hover:bg-slate-50 dark:hover:bg-slate-700/40 transition-colors group cursor-default">
                {canManage && (
                  <input type="checkbox" checked={selected.has(d.id)}
                    onChange={() => onToggleSelect(d.id)} onClick={(e) => e.stopPropagation()}
                    className="shrink-0 accent-blue-600" />
                )}
                {d.rack_position != null && (
                  <span className="text-[9px] font-mono text-slate-400 w-5 text-right shrink-0">U{d.rack_position}</span>
                )}
                <span className={`w-2 h-2 rounded-full ring-2 shrink-0 ${statusColor[d.status] || statusColor.unknown} ${statusRing[d.status] || statusRing.unknown}`} />
                <Link to={`/devices?q=${encodeURIComponent(d.hostname)}`}
                  className="text-xs font-semibold text-slate-800 dark:text-slate-100 group-hover:text-blue-600 dark:group-hover:text-blue-400 truncate flex-1">
                  {d.hostname}
                </Link>
                {d.device_type && (
                  <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-700 text-slate-500 dark:text-slate-400 shrink-0">
                    {d.device_type}
                  </span>
                )}
                {canManage && (
                  <div className="hidden group-hover:flex items-center gap-1.5 shrink-0">
                    <button onClick={() => onDeviceAction("edit", d, dcName, blockName, rack.name)} title="Edit placement"
                      className="text-[11px] text-slate-400 hover:text-blue-500 leading-none">✎</button>
                    <button onClick={() => onDeviceAction("replace", d, dcName, blockName, rack.name)} title="Replace device"
                      className="text-[11px] text-slate-400 hover:text-indigo-500 leading-none">⇄</button>
                    <button onClick={() => onDeviceAction("remove", d, dcName, blockName, rack.name)} title="Remove from rack"
                      className="text-[11px] text-slate-400 hover:text-red-500 leading-none">✕</button>
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

/** Enterprise Data Center card — collapsible tier between Block and Rack,
 * with health rollup, rack grid, utilization bar, and drag-drop support. */
function DataCenterCard({
  dataCenter,
  blockName,
  canManage,
  onMove,
  onAddDevice,
  onRename,
  onDelete,
  onCreateRack,
  selected,
  onToggleSelect,
  onDeviceAction,
}: {
  dataCenter: GroupDataCenter;
  blockName: string;
  canManage: boolean;
  onMove: (deviceId: string, block: string, dataCenter: string, rack: string) => void;
  onAddDevice: (block: string, dc: string, rack: string) => void;
  onRename: (oldName: string, type: "block" | "data_center" | "rack", blockName: string, dcName?: string) => void;
  onDelete: (name: string, type: "block" | "data_center" | "rack", blockName: string, dcName?: string) => void;
  onCreateRack: (blockName: string, dcName: string) => void;
  selected: Set<string>;
  onToggleSelect: (deviceId: string) => void;
  onDeviceAction: (action: "edit" | "remove" | "replace", device: GroupDevice, dcName: string, blockName: string, rackName: string) => void;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const isUnassigned = dataCenter.name === "Unassigned";
  const allDevices = dataCenter.racks.flatMap((r) => r.devices);

  return (
    <div
      className={`rounded-xl overflow-hidden border transition-all duration-200 ${
        dragOver
          ? "border-blue-400 ring-2 ring-blue-400/20 shadow-md"
          : isUnassigned
          ? "border-dashed border-slate-300 dark:border-slate-600"
          : "border-slate-200 dark:border-slate-700 shadow-sm"
      } ${isUnassigned ? "bg-white/50 dark:bg-slate-800/30" : "bg-white dark:bg-slate-800"}`}
      onDragOver={(e) => { if (!canManage) return; e.preventDefault(); setDragOver(true); }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        if (!canManage) return;
        e.preventDefault(); setDragOver(false);
        const deviceId = e.dataTransfer.getData("text/device-id");
        if (deviceId) onMove(deviceId, blockName, dataCenter.name, dataCenter.racks[0]?.name || "Unassigned");
      }}
    >
      {/* Header */}
      <div className="px-4 py-2.5 flex items-center gap-2 bg-gradient-to-r from-indigo-50 to-transparent dark:from-indigo-950/30 dark:to-transparent border-b border-slate-200 dark:border-slate-700">
        <button onClick={() => setCollapsed(c => !c)}
          className="text-indigo-400 hover:text-indigo-600 dark:hover:text-indigo-300 shrink-0" title={collapsed ? "Expand" : "Collapse"}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
            className={`transition-transform duration-200 ${collapsed ? "-rotate-90" : ""}`}>
            <path d="M6 9l6 6 6-6"/>
          </svg>
        </button>
        <div className="w-5 h-5 rounded bg-indigo-100 dark:bg-indigo-900/50 text-indigo-600 dark:text-indigo-400 flex items-center justify-center shrink-0">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <rect x="2" y="3" width="20" height="18" rx="2"/><path d="M2 9h20M2 15h20"/>
          </svg>
        </div>
        <span className={`text-sm font-bold ${isUnassigned ? "text-slate-400 dark:text-slate-500 italic" : "text-slate-800 dark:text-slate-100"} truncate flex-1`}>
          {dataCenter.name}
        </span>
        <HealthBar devices={allDevices} />
        <span className="text-[10px] text-slate-400 dark:text-slate-500 shrink-0">
          {dataCenter.racks.length} rack{dataCenter.racks.length !== 1 ? "s" : ""} · {dataCenter.device_count} dev
        </span>
        {canManage && !isUnassigned && (
          <>
            <button onClick={() => onRename(dataCenter.name, "data_center", blockName)}
              className="text-[11px] text-slate-400 hover:text-blue-500 shrink-0" title="Rename">✎</button>
            <button onClick={() => onDelete(dataCenter.name, "data_center", blockName)}
              className="text-[11px] text-slate-400 hover:text-red-500 shrink-0" title="Delete">✕</button>
          </>
        )}
        {canManage && !isUnassigned && (
          <button onClick={() => onCreateRack(blockName, dataCenter.name)}
            className="text-[10px] font-bold text-indigo-600 dark:text-indigo-400 border border-indigo-200 dark:border-indigo-700 rounded px-1.5 py-0.5 hover:bg-indigo-50 dark:hover:bg-indigo-900/30 transition-colors shrink-0">
            + New rack
          </button>
        )}
        {canManage && (
          <button onClick={() => onAddDevice(blockName, dataCenter.name, "")}
            className="text-[10px] font-bold text-indigo-600 dark:text-indigo-400 border border-indigo-200 dark:border-indigo-700 rounded px-1.5 py-0.5 hover:bg-indigo-50 dark:hover:bg-indigo-900/30 transition-colors shrink-0">
            + Add
          </button>
        )}
      </div>

      {!collapsed && (
        <div className="p-3">
          {dataCenter.racks.length === 0 ? (
            <div className="flex flex-col items-center py-5 gap-2 border border-dashed border-slate-200 dark:border-slate-700 rounded-lg">
              <svg width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24" className="text-slate-300 dark:text-slate-600">
                <rect x="2" y="4" width="20" height="16" rx="2"/><path d="M8 9h8M8 13h4"/>
              </svg>
              <p className="text-xs text-slate-400 italic">
                {canManage ? "No racks yet — use + New rack or + Add above" : "No racks in this data center."}
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
              {dataCenter.racks.map((rack) => (
                <RackCard key={rack.name} rack={rack} dcName={dataCenter.name} blockName={blockName}
                  canManage={canManage}
                  onMove={(deviceId, dc, block, rackName) => onMove(deviceId, block, dc, rackName)}
                  onAddDevice={(dc, block, rackName) => onAddDevice(block, dc, rackName)}
                  onRename={(oldName, type, dcName, blockNameArg) =>
                    onRename(oldName, type === "data_center" ? "data_center" : type, blockNameArg || blockName, dcName)}
                  onDelete={(name, type, dcName, blockNameArg) =>
                    onDelete(name, type === "data_center" ? "data_center" : type, blockNameArg || blockName, dcName)}
                  selected={selected} onToggleSelect={onToggleSelect}
                  onDeviceAction={onDeviceAction}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** One "block" (campus/region/business-unit) at the top of the
 * enterprise hierarchy -- owns one or more data centers, each with its
 * own racks. The outermost tier under the implicit single "Company"
 * root shown at the top of the page. Collapsible since a large org can
 * have many data centers per block. */
function BlockCard({
  block,
  canManage,
  onMove,
  onAddDevice,
  onRename,
  onDelete,
  onCreateDataCenter,
  onCreateRack,
  selected,
  onToggleSelect,
  onDeviceAction,
}: {
  block: GroupBlock;
  canManage: boolean;
  onMove: (deviceId: string, block: string, dataCenter: string, rack: string) => void;
  onAddDevice: (block: string, dc: string, rack: string) => void;
  onRename: (oldName: string, type: "block" | "data_center" | "rack", blockName: string, dcName?: string) => void;
  onDelete: (name: string, type: "block" | "data_center" | "rack", blockName: string, dcName?: string) => void;
  onCreateDataCenter: (blockName: string) => void;
  onCreateRack: (blockName: string, dcName: string) => void;
  selected: Set<string>;
  onToggleSelect: (deviceId: string) => void;
  onDeviceAction: (action: "edit" | "remove" | "replace", device: GroupDevice, dcName: string, blockName: string, rackName: string) => void;
}) {
  const [dragOver, setDragOver] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const isUnassigned = block.name === "Unassigned";
  const rackCount = block.data_centers.reduce((n, dc) => n + dc.racks.length, 0);
  const allDevices = block.data_centers.flatMap((dc) => dc.racks.flatMap((r) => r.devices));

  return (
    <div
      className={`rounded-2xl overflow-hidden border transition-all duration-200 ${
        dragOver
          ? "border-blue-400 ring-2 ring-blue-400/20 shadow-xl"
          : isUnassigned
          ? "border-dashed border-slate-300 dark:border-slate-600"
          : "border-slate-200 dark:border-slate-700 shadow-md hover:shadow-lg"
      }`}
      onDragOver={(e) => { if (!canManage) return; e.preventDefault(); setDragOver(true); }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        if (!canManage) return;
        e.preventDefault(); setDragOver(false);
        const deviceId = e.dataTransfer.getData("text/device-id");
        const firstDc = block.data_centers[0];
        if (deviceId) onMove(deviceId, block.name, firstDc?.name || "Unassigned", firstDc?.racks[0]?.name || "Unassigned");
      }}
    >
      {/* Block header */}
      <div className={`px-5 py-3.5 flex items-center gap-3 ${
        isUnassigned
          ? "bg-slate-50 dark:bg-slate-900/20 border-b border-slate-200 dark:border-slate-700"
          : "bg-gradient-to-r from-blue-700 to-blue-600 dark:from-blue-900 dark:to-blue-800"
      }`}>
        <button onClick={() => setCollapsed(c => !c)}
          className={`shrink-0 ${isUnassigned ? "text-slate-400" : "text-blue-200 hover:text-white"}`}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
            className={`transition-transform duration-200 ${collapsed ? "-rotate-90" : ""}`}>
            <path d="M6 9l6 6 6-6"/>
          </svg>
        </button>
        <div className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 ${
          isUnassigned ? "bg-slate-200 dark:bg-slate-700 text-slate-400" : "bg-white/15 text-white"
        }`}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <rect x="2" y="7" width="20" height="14" rx="1"/><path d="M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2"/>
            <line x1="12" y1="12" x2="12" y2="16"/><line x1="10" y1="12" x2="14" y2="12"/>
          </svg>
        </div>
        <div className="flex-1 min-w-0">
          <h2 className={`text-base font-bold truncate ${isUnassigned ? "text-slate-500 dark:text-slate-400 italic" : "text-white"}`}>
            {block.name}
          </h2>
          <p className={`text-[11px] mt-0.5 ${isUnassigned ? "text-slate-400" : "text-blue-200"}`}>
            {block.data_centers.length} DC{block.data_centers.length !== 1 ? "s" : ""} &middot; {rackCount} rack{rackCount !== 1 ? "s" : ""} &middot; {block.device_count} device{block.device_count !== 1 ? "s" : ""}
          </p>
        </div>
        <div className="shrink-0"><HealthBar devices={allDevices} /></div>
        {canManage && !isUnassigned && (
          <>
            <button onClick={() => onRename(block.name, "block", block.name)} title="Rename"
              className="text-blue-200 hover:text-white shrink-0 text-sm leading-none">✎</button>
            <button onClick={() => onDelete(block.name, "block", block.name)} title="Delete"
              className="text-blue-200 hover:text-red-300 shrink-0 text-sm leading-none">✕</button>
          </>
        )}
        {canManage && !isUnassigned && (
          <button onClick={() => onCreateDataCenter(block.name)}
            className="text-[11px] font-bold shrink-0 border border-white/40 text-white hover:bg-white/20 rounded-full px-2.5 py-1 transition-colors">
            + New DC
          </button>
        )}
        {canManage && (
          <button onClick={() => onAddDevice(block.name, "", "")}
            className={`text-[11px] font-bold shrink-0 border rounded-full px-2.5 py-1 transition-colors ${
              isUnassigned
                ? "border-slate-300 dark:border-slate-600 text-slate-500 hover:text-slate-800"
                : "border-white/40 text-white hover:bg-white/20"
            }`}>
            + Add device
          </button>
        )}
      </div>

      {!collapsed && (
        <div className="bg-slate-50 dark:bg-slate-900/30 p-4 flex flex-col gap-3">
          {block.data_centers.length === 0 ? (
            <div className="flex flex-col items-center py-8 gap-2 border border-dashed border-slate-200 dark:border-slate-700 rounded-xl">
              <svg width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24" className="text-slate-300 dark:text-slate-600">
                <rect x="2" y="3" width="20" height="18" rx="2"/><path d="M2 9h20M2 15h20"/>
              </svg>
              <p className="text-sm text-slate-400 italic">No data centers in this block yet.</p>
              {canManage && !isUnassigned && (
                <button onClick={() => onCreateDataCenter(block.name)}
                  className="text-xs font-bold text-blue-600 dark:text-blue-400 hover:underline">+ New data center</button>
              )}
              {canManage && <button onClick={() => onAddDevice(block.name, "", "")}
                className="text-xs font-bold text-blue-600 dark:text-blue-400 hover:underline">+ Add first device</button>}
            </div>
          ) : (
            block.data_centers.map((dc) => (
              <DataCenterCard
                key={dc.name}
                dataCenter={dc}
                blockName={block.name}
                canManage={canManage}
                onMove={onMove}
                onAddDevice={onAddDevice}
                onRename={onRename}
                onDelete={onDelete}
                onCreateRack={onCreateRack}
                selected={selected}
                onToggleSelect={onToggleSelect}
                onDeviceAction={onDeviceAction}
              />
            ))
          )}
        </div>
      )}
    </div>
  );
}



/**
 * distinct from the Data Center/Rack physical-placement view below.
 * Supports create/edit/delete and nesting (a group can have a parent),
 * plus per-group device membership management. Only Network Admins get
 * the management controls (GROUP_MANAGER_ROLES on the backend matches),
 * everyone else sees a read-only tree. */
function NamedGroupsPanel({ canManage }: { canManage: boolean }) {
  const confirm = useConfirm();
  const [groups, setGroups] = useState<DeviceGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [editingGroup, setEditingGroup] = useState<DeviceGroup | null>(null);
  const [formName, setFormName] = useState("");
  const [formDescription, setFormDescription] = useState("");
  const [formType, setFormType] = useState("static");
  const [formParentId, setFormParentId] = useState<string>("");
  const [formIsDynamic, setFormIsDynamic] = useState(false);
  const [formRules, setFormRules] = useState<DeviceGroupRule[]>([]);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [rollups, setRollups] = useState<Record<string, GroupHealthRollup>>({});
  const [ruleBusyId, setRuleBusyId] = useState<string | null>(null);
  const [rulePreview, setRulePreview] = useState<{ groupId: string; matches: DeviceGroupRuleMatch[] } | null>(null);

  const [expandedGroupId, setExpandedGroupId] = useState<string | null>(null);
  const [groupDevices, setGroupDevices] = useState<Device[]>([]);
  const [groupDevicesLoading, setGroupDevicesLoading] = useState(false);
  const [allDevices, setAllDevices] = useState<Device[] | null>(null);
  const [addDeviceId, setAddDeviceId] = useState("");

  const load = () => {
    setLoading(true);
    api
      .get<DeviceGroup[]>("/device-groups")
      .then((res) => {
        setGroups(res.data);
        setError(null);
      })
      .catch(() => setError("Failed to load groups."))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const byParent = useMemo(() => {
    const map = new Map<string, DeviceGroup[]>();
    for (const g of groups) {
      const key = g.parent_group_id || "__root__";
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(g);
    }
    return map;
  }, [groups]);

  const openCreate = (parentId?: string) => {
    setEditingGroup(null);
    setFormName("");
    setFormDescription("");
    setFormType("static");
    setFormParentId(parentId || "");
    setFormIsDynamic(false);
    setFormRules([]);
    setFormError(null);
    setFormOpen(true);
  };

  const openEdit = (group: DeviceGroup) => {
    setEditingGroup(group);
    setFormName(group.name);
    setFormDescription(group.description || "");
    setFormType(group.group_type);
    setFormParentId(group.parent_group_id || "");
    setFormIsDynamic(group.is_dynamic);
    setFormRules(group.membership_rules || []);
    setFormError(null);
    setFormOpen(true);
  };

  const saveGroup = async () => {
    if (!formName.trim()) {
      setFormError("Name is required.");
      return;
    }
    setSaving(true);
    setFormError(null);
    const payload = {
      name: formName.trim(),
      description: formDescription.trim() || null,
      group_type: formType.trim() || "static",
      parent_group_id: formParentId || null,
      is_dynamic: formIsDynamic,
      membership_rules: formRules.filter((r) => r.field && r.pattern.trim()),
    };
    try {
      if (editingGroup) {
        await api.patch(`/device-groups/${editingGroup.id}`, payload);
        setNotice(`Updated group "${formName.trim()}".`);
      } else {
        await api.post("/device-groups", payload);
        setNotice(`Created group "${formName.trim()}".`);
      }
      setFormOpen(false);
      load();
      setTimeout(() => setNotice(null), 3000);
    } catch (err: any) {
      setFormError(err?.response?.data?.detail || "Failed to save group.");
    } finally {
      setSaving(false);
    }
  };

  const deleteGroup = async (group: DeviceGroup) => {
    const hasChildren = group.child_group_count > 0;
    const confirmMsg = hasChildren
      ? `Delete "${group.name}"? Its ${group.child_group_count} sub-group(s) will move up to its parent, and any member devices will become ungrouped.`
      : `Delete "${group.name}"? Member devices will become ungrouped.`;
    if (!(await confirm(confirmMsg, { confirmLabel: "Delete" }))) return;
    try {
      await api.delete(`/device-groups/${group.id}`);
      setNotice(`Deleted group "${group.name}".`);
      if (expandedGroupId === group.id) setExpandedGroupId(null);
      load();
      setTimeout(() => setNotice(null), 3000);
    } catch {
      setError("Failed to delete group.");
    }
  };

  const toggleExpand = (group: DeviceGroup) => {
    if (expandedGroupId === group.id) {
      setExpandedGroupId(null);
      return;
    }
    setExpandedGroupId(group.id);
    setGroupDevicesLoading(true);
    api
      .get<Device[]>(`/device-groups/${group.id}/devices`)
      .then((res) => setGroupDevices(res.data))
      .catch(() => setGroupDevices([]))
      .finally(() => setGroupDevicesLoading(false));
    loadRollup(group.id);
    if (allDevices === null) {
      api
        .get<Device[]>("/devices")
        .then((res) => setAllDevices(res.data))
        .catch(() => setAllDevices([]));
    }
  };

  const addDeviceToGroup = async (groupId: string) => {
    if (!addDeviceId) return;
    try {
      await api.post(`/device-groups/${groupId}/devices`, { device_ids: [addDeviceId] });
      setAddDeviceId("");
      const res = await api.get<Device[]>(`/device-groups/${groupId}/devices`);
      setGroupDevices(res.data);
      load(); // refresh device_count on the group list
    } catch {
      setError("Failed to add device to group.");
    }
  };

  const removeDeviceFromGroup = async (groupId: string, deviceId: string) => {
    try {
      await api.delete(`/device-groups/${groupId}/devices/${deviceId}`);
      setGroupDevices((prev) => prev.filter((d) => d.id !== deviceId));
      load();
    } catch {
      setError("Failed to remove device from group.");
    }
  };

  const previewGroupRules = async (groupId: string) => {
    setRuleBusyId(groupId);
    try {
      const res = await api.get<{ matches: DeviceGroupRuleMatch[] }>(`/device-groups/${groupId}/rules/preview`);
      setRulePreview({ groupId, matches: res.data.matches });
    } catch {
      setError("Failed to preview group rules.");
    } finally {
      setRuleBusyId(null);
    }
  };

  const applyGroupRules = async (group: DeviceGroup) => {
    setRuleBusyId(group.id);
    try {
      const res = await api.post<{ assigned_device_ids: string[]; already_member_device_ids: string[] }>(
        `/device-groups/${group.id}/rules/apply`
      );
      setNotice(
        `"${group.name}": ${res.data.assigned_device_ids.length} device(s) newly assigned, ` +
          `${res.data.already_member_device_ids.length} already members.`
      );
      setRulePreview(null);
      load();
      if (expandedGroupId === group.id) toggleExpand(group);
      setTimeout(() => setNotice(null), 4000);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to apply group rules.");
    } finally {
      setRuleBusyId(null);
    }
  };

  const loadRollup = (groupId: string) => {
    api
      .get<GroupHealthRollup>(`/device-groups/${groupId}/health-rollup`)
      .then((res) => setRollups((prev) => ({ ...prev, [groupId]: res.data })))
      .catch(() => undefined);
  };

  const renderGroup = (group: DeviceGroup, depth: number): React.ReactNode => {
    const children = byParent.get(group.id) || [];
    const isExpanded = expandedGroupId === group.id;
    const availableToAdd = allDevices?.filter((d) => !groupDevices.some((gd) => gd.id === d.id)) || [];

    return (
      <div key={group.id}>
        <div
          className="flex items-center gap-2 py-2 pr-2 border-b border-slate-100 dark:border-slate-800 hover:bg-slate-50 dark:hover:bg-slate-900/40"
          style={{ paddingLeft: 12 + depth * 20 }}
        >
          <button
            onClick={() => toggleExpand(group)}
            className="flex items-center gap-2 flex-1 text-left min-w-0"
          >
            <span className="text-slate-400 text-xs">{isExpanded ? "▾" : "▸"}</span>
            <span className="text-sm font-semibold text-navy dark:text-white truncate">📁 {group.name}</span>
            <span className="text-[10px] text-slate-400 dark:text-slate-500 shrink-0">
              {group.device_count} device{group.device_count === 1 ? "" : "s"}
              {group.child_group_count > 0 ? ` · ${group.child_group_count} sub-group${group.child_group_count === 1 ? "" : "s"}` : ""}
            </span>
            {group.description && (
              <span className="text-xs text-slate-400 dark:text-slate-500 truncate hidden sm:inline">— {group.description}</span>
            )}
            {group.is_dynamic && (
              <span className="text-[10px] font-bold text-brandblue bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-800 rounded-full px-2 py-0.5 shrink-0">
                dynamic
              </span>
            )}
          </button>
          {canManage && (
            <div className="flex items-center gap-2 shrink-0 text-xs">
              <button onClick={() => openCreate(group.id)} className="text-slate-400 hover:text-brandblue" title="Add sub-group">
                + sub-group
              </button>
              <button onClick={() => openEdit(group)} className="text-slate-400 hover:text-brandblue" title="Edit">
                Edit
              </button>
              <button onClick={() => deleteGroup(group)} className="text-slate-400 hover:text-riskcrit" title="Delete">
                Delete
              </button>
            </div>
          )}
        </div>

        {isExpanded && (
          <div className="bg-slate-50 dark:bg-slate-900/40 py-2" style={{ paddingLeft: 12 + depth * 20 + 20 }}>
            {rollups[group.id] && rollups[group.id].device_count > 0 && (
              <div className="flex items-center gap-3 text-[11px] font-semibold mb-2">
                {rollups[group.id].green_count > 0 && <span className="text-risklow">● {rollups[group.id].green_count} green</span>}
                {rollups[group.id].yellow_count > 0 && <span className="text-riskmed">● {rollups[group.id].yellow_count} yellow</span>}
                {rollups[group.id].red_count > 0 && <span className="text-riskcrit">● {rollups[group.id].red_count} red</span>}
                {rollups[group.id].gray_count > 0 && <span className="text-slate-400">● {rollups[group.id].gray_count} gray</span>}
                {rollups[group.id].unmonitored_count > 0 && (
                  <span className="text-slate-400">● {rollups[group.id].unmonitored_count} unmonitored</span>
                )}
                {rollups[group.id].average_health_score != null && (
                  <span className="text-slate-400">avg score {rollups[group.id].average_health_score!.toFixed(0)}</span>
                )}
              </div>
            )}
            {group.is_dynamic && canManage && (
              <div className="mb-3 flex flex-col gap-2">
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => previewGroupRules(group.id)}
                    disabled={ruleBusyId === group.id}
                    className="text-xs font-semibold text-brandblue hover:text-navy disabled:opacity-50"
                  >
                    Preview rule matches
                  </button>
                  <button
                    onClick={() => applyGroupRules(group)}
                    disabled={ruleBusyId === group.id}
                    className="text-xs font-bold text-white bg-brandblue hover:bg-navy disabled:opacity-50 px-2.5 py-1 rounded-full"
                  >
                    {ruleBusyId === group.id ? "Working…" : "Apply rules now"}
                  </button>
                </div>
                {rulePreview && rulePreview.groupId === group.id && (
                  <div className="text-xs bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg p-2 max-w-md">
                    {rulePreview.matches.length === 0 ? (
                      <p className="text-slate-400 italic">No devices currently match this group's rules.</p>
                    ) : (
                      <ul className="flex flex-col gap-0.5">
                        {rulePreview.matches.map((m) => (
                          <li key={m.device_id} className="flex items-center justify-between gap-2">
                            <span>{m.hostname}</span>
                            <span className="text-[10px] text-slate-400">
                              {m.matched_rule.field}:{m.matched_rule.pattern}
                              {m.already_member ? " (already member)" : ""}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </div>
            )}
            {groupDevicesLoading ? (
              <p className="text-xs text-slate-400 italic">Loading devices…</p>
            ) : groupDevices.length === 0 ? (
              <p className="text-xs text-slate-400 italic">No devices in this group yet.</p>
            ) : (
              <div className="flex flex-col gap-1 mb-2">
                {groupDevices.map((d) => (
                  <div key={d.id} className="flex items-center gap-2 text-sm">
                    <Link to={`/devices?q=${encodeURIComponent(d.hostname)}`} className="text-navy dark:text-white hover:text-brandblue">
                      {d.hostname}
                    </Link>
                    <span className="text-[10px] text-slate-400 font-mono">{d.ip_address}</span>
                    {canManage && (
                      <button
                        onClick={() => removeDeviceFromGroup(group.id, d.id)}
                        className="text-[10px] text-slate-400 hover:text-riskcrit ml-1"
                      >
                        remove
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
            {canManage && (
              <div className="flex items-center gap-2">
                <select
                  value={addDeviceId}
                  onChange={(e) => setAddDeviceId(e.target.value)}
                  className="border border-slate-300 dark:border-slate-600 dark:bg-slate-800 rounded-lg px-2 py-1 text-xs"
                >
                  <option value="">Add a device…</option>
                  {availableToAdd.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.hostname}
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => addDeviceToGroup(group.id)}
                  disabled={!addDeviceId}
                  className="text-xs font-bold text-brandblue disabled:text-slate-300"
                >
                  Add
                </button>
              </div>
            )}
          </div>
        )}

        {children.map((child) => renderGroup(child, depth + 1))}
      </div>
    );
  };

  const rootGroups = byParent.get("__root__") || [];

  return (
    <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-2xl overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-200 dark:border-slate-700">
        <div>
          <h2 className="text-sm font-bold text-navy dark:text-white">Named Groups</h2>
          <p className="text-xs text-slate-400 dark:text-slate-500">
            Explicit, user-defined groups (e.g. "Edge Firewalls", "Q3 Migration Batch") — nest a group under another to build a hierarchy.
          </p>
        </div>
        {canManage && (
          <button
            onClick={() => openCreate()}
            className="text-xs font-bold text-white bg-brandblue hover:bg-navy px-3 py-1.5 rounded-full shadow-sm transition-colors shrink-0"
          >
            + New group
          </button>
        )}
      </div>

      {notice && (
        <p className="mx-4 mt-3 text-[13px] font-medium text-brandblue bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-800 rounded-lg px-4 py-2">
          {notice}
        </p>
      )}
      {error && (
        <p className="mx-4 mt-3 text-riskcrit font-semibold text-sm bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-800 px-3 py-2 rounded-lg">
          {error}
        </p>
      )}

      {loading ? (
        <p className="text-sm text-slate-400 italic px-4 py-6">Loading groups…</p>
      ) : rootGroups.length === 0 ? (
        <p className="text-sm text-slate-400 italic px-4 py-6">
          No named groups yet.{canManage ? " Create one to start organizing devices logically." : ""}
        </p>
      ) : (
        <div>{rootGroups.map((g) => renderGroup(g, 0))}</div>
      )}

      {formOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm" onClick={() => setFormOpen(false)}>
          <div
            className="w-full max-w-md bg-white dark:bg-slate-800 rounded-xl shadow-2xl border border-slate-200 dark:border-slate-700 p-5 flex flex-col gap-3"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-bold text-navy dark:text-white">{editingGroup ? "Edit group" : "New group"}</h3>
            {formError && <p className="text-xs text-riskcrit font-semibold">{formError}</p>}
            <label className="text-xs font-bold text-slate-500">
              Name
              <input
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                className="mt-1 w-full border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-1.5 text-sm"
                placeholder="e.g. Edge Firewalls"
                autoFocus
              />
            </label>
            <label className="text-xs font-bold text-slate-500">
              Description
              <input
                value={formDescription}
                onChange={(e) => setFormDescription(e.target.value)}
                className="mt-1 w-full border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-1.5 text-sm"
                placeholder="Optional"
              />
            </label>
            <label className="text-xs font-bold text-slate-500">
              Type
              <input
                value={formType}
                onChange={(e) => setFormType(e.target.value)}
                className="mt-1 w-full border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-1.5 text-sm"
                placeholder="static"
              />
            </label>
            <label className="text-xs font-bold text-slate-500">
              Parent group
              <select
                value={formParentId}
                onChange={(e) => setFormParentId(e.target.value)}
                className="mt-1 w-full border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-1.5 text-sm"
              >
                <option value="">None (top-level)</option>
                {groups
                  .filter((g) => g.id !== editingGroup?.id)
                  .map((g) => (
                    <option key={g.id} value={g.id}>
                      {g.name}
                    </option>
                  ))}
              </select>
            </label>

            <label className="flex items-center gap-2 text-xs font-bold text-slate-500">
              <input type="checkbox" checked={formIsDynamic} onChange={(e) => setFormIsDynamic(e.target.checked)} />
              Dynamic membership (auto-add by rule)
            </label>

            {formIsDynamic && (
              <div className="flex flex-col gap-2 border border-slate-200 dark:border-slate-700 rounded-lg p-2">
                <p className="text-[11px] text-slate-400">
                  A device is added if it matches ANY rule below. Patterns are globs, e.g. <code>edge-*</code>.
                </p>
                {formRules.map((rule, i) => (
                  <div key={i} className="flex items-center gap-1.5">
                    <select
                      value={rule.field}
                      onChange={(e) =>
                        setFormRules((prev) =>
                          prev.map((r, idx) => (idx === i ? { ...r, field: e.target.value as DeviceGroupRule["field"] } : r))
                        )
                      }
                      className="border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-2 py-1 text-xs"
                    >
                      <option value="hostname">hostname</option>
                      <option value="tag">tag</option>
                      <option value="site">site</option>
                      <option value="device_type">device_type</option>
                      <option value="device_role">device_role</option>
                    </select>
                    <input
                      value={rule.pattern}
                      onChange={(e) =>
                        setFormRules((prev) => prev.map((r, idx) => (idx === i ? { ...r, pattern: e.target.value } : r)))
                      }
                      placeholder="e.g. edge-*"
                      className="flex-1 border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-2 py-1 text-xs"
                    />
                    <button
                      onClick={() => setFormRules((prev) => prev.filter((_, idx) => idx !== i))}
                      className="text-slate-400 hover:text-riskcrit text-xs"
                    >
                      ✕
                    </button>
                  </div>
                ))}
                <button
                  onClick={() => setFormRules((prev) => [...prev, { field: "hostname", pattern: "" }])}
                  className="text-xs font-semibold text-brandblue hover:text-navy self-start"
                >
                  + Add rule
                </button>
              </div>
            )}

            <div className="flex items-center justify-end gap-2 mt-2">
              <button onClick={() => setFormOpen(false)} className="text-xs font-bold text-slate-500 px-3 py-1.5">
                Cancel
              </button>
              <button
                onClick={saveGroup}
                disabled={saving}
                className="text-xs font-bold text-white bg-brandblue hover:bg-navy disabled:opacity-50 px-4 py-1.5 rounded-full shadow-sm"
              >
                {saving ? "Saving…" : editingGroup ? "Save changes" : "Create group"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function Groups() {
  const { user } = useAuth();
  const canManage = user?.role === "network_admin";
  const [groups, setGroups] = useState<GroupBlock[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [moveNotice, setMoveNotice] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkTarget, setBulkTarget] = useState("");
  const [bulkBusy, setBulkBusy] = useState(false);

  // Explicit "Add device to rack" flow -- primary path for placing a
  // device now (drag-and-drop below is a secondary convenience, and
  // doesn't work on touch devices). Prefilled dc/rack when launched
  // from a specific rack's "+ Add device" button.
  const [addOpen, setAddOpen] = useState(false);
  const [addDeviceId, setAddDeviceId] = useState("");
  const [addDeviceQuery, setAddDeviceQuery] = useState("");
  const [addDc, setAddDc] = useState("");
  const [addDcNew, setAddDcNew] = useState("");
  const [addBlock, setAddBlock] = useState("");
  const [addBlockNew, setAddBlockNew] = useState("");
  const [addRack, setAddRack] = useState("");
  const [addRackNew, setAddRackNew] = useState("");
  const [addPosition, setAddPosition] = useState("");
  const [addBusy, setAddBusy] = useState(false);
  const [addFormError, setAddFormError] = useState<string | null>(null);
  const [allDevices, setAllDevices] = useState<Device[] | null>(null);

  // Modal mode: "add" (default) places a device into a rack; "edit"
  // reuses the same form to change an already-placed device's slot;
  // "replace" swaps a new device into a device's current slot and
  // unassigns the old one.
  const [modalMode, setModalMode] = useState<"add" | "edit" | "replace">("add");
  const [modalOldDeviceId, setModalOldDeviceId] = useState<string | null>(null);

  const confirm = useConfirm();

  const loadAllDevices = () => {
    api
      .get<Device[]>("/devices")
      .then((res) => setAllDevices(res.data))
      .catch(() => setAllDevices([]));
  };

  const openAddDevice = (
    prefillBlock?: string,
    prefillDc?: string,
    prefillRack?: string,
    opts?: { mode?: "add" | "edit" | "replace"; deviceId?: string; position?: number | null }
  ) => {
    if (allDevices === null) loadAllDevices();
    setAddDeviceId(opts?.mode === "edit" && opts.deviceId ? opts.deviceId : "");
    setAddDeviceQuery("");
    setAddBlock(prefillBlock && prefillBlock !== "Unassigned" ? prefillBlock : "");
    setAddBlockNew("");
    setAddDc(prefillDc && prefillDc !== "Unassigned" ? prefillDc : "");
    setAddDcNew("");
    setAddRack(prefillRack && prefillRack !== "Unassigned" ? prefillRack : "");
    setAddRackNew("");
    setAddPosition(opts?.position != null ? String(opts.position) : "");
    setAddFormError(null);
    setModalMode(opts?.mode || "add");
    setModalOldDeviceId(opts?.mode === "replace" ? opts?.deviceId || null : null);
    setAddOpen(true);
  };

  // Edit / replace / remove entry point for the per-device row actions.
  const handleDeviceAction = async (
    action: "edit" | "remove" | "replace",
    device: GroupDevice,
    dcName: string,
    blockName: string,
    rackName: string
  ) => {
    if (action === "edit") {
      openAddDevice(blockName, dcName, rackName, {
        mode: "edit",
        deviceId: device.id,
        position: device.rack_position,
      });
      return;
    }
    if (action === "replace") {
      openAddDevice(blockName, dcName, rackName, {
        mode: "replace",
        deviceId: device.id,
        position: device.rack_position,
      });
      return;
    }
    // remove: unassign device from the hierarchy entirely
    if (!(await confirm(`Remove ${device.hostname} from ${blockName} / ${dcName} / ${rackName}? It will become unassigned.`))) {
      return;
    }
    try {
      await api.patch(`/devices/${device.id}`, { block: null, data_center: null, rack: null, rack_position: null });
      setMoveNotice(`Removed ${device.hostname} from the rack.`);
      load();
      setTimeout(() => setMoveNotice(null), 3000);
    } catch {
      setError("Failed to remove device from rack.");
    }
  };

  const load = () => {
    setLoading(true);
    api
      .get<GroupBlock[]>("/devices/groups/summary")
      .then((res) => {
        setGroups(res.data);
        setError(null);
      })
      .catch(() => setError("Failed to load groups."))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  // Drag-and-drop move: sets all three placement fields (block,
  // data_center, rack) on the device. Previously this only sent
  // data_center + rack, silently dropping the block a device was
  // dropped into -- fixed here so a drag onto a rack under a
  // non-Unassigned block actually sticks.
  const handleMove = async (deviceId: string, block: string, dataCenter: string, rack: string) => {
    try {
      await api.patch(`/devices/${deviceId}`, {
        block: block === "Unassigned" ? null : block,
        data_center: dataCenter === "Unassigned" ? null : dataCenter,
        rack: rack === "Unassigned" ? null : rack,
      });
      setMoveNotice(`Moved device to ${block} / ${dataCenter} / ${rack}.`);
      load();
      setTimeout(() => setMoveNotice(null), 3000);
    } catch {
      setError("Failed to move device.");
    }
  };

  const assignDevice = async () => {
    const block = (addBlock === "__new__" ? addBlockNew : addBlock).trim();
    const dc = (addDc === "__new__" ? addDcNew : addDc).trim();
    const rack = (addRack === "__new__" ? addRackNew : addRack).trim();
    if (!addDeviceId) {
      setAddFormError("Pick a device.");
      return;
    }
    if (!block || !dc || !rack) {
      setAddFormError("A block, data center, and rack are all required.");
      return;
    }
    const position = addPosition.trim() ? Number(addPosition) : null;
    if (addPosition.trim() && (!Number.isInteger(position) || (position as number) < 1)) {
      setAddFormError("Rack position must be a positive whole number (U slot), or left blank.");
      return;
    }
    if (modalMode === "replace" && addDeviceId === modalOldDeviceId) {
      setAddFormError("Pick a different device to replace this one with.");
      return;
    }
    setAddBusy(true);
    setAddFormError(null);
    try {
      await api.patch(`/devices/${addDeviceId}`, {
        block: block,
        data_center: dc,
        rack: rack,
        rack_position: position,
      });
      if (modalMode === "replace" && modalOldDeviceId) {
        await api.patch(`/devices/${modalOldDeviceId}`, {
          block: null,
          data_center: null,
          rack: null,
          rack_position: null,
        });
      }
      const deviceLabel = allDevices?.find((d) => d.id === addDeviceId)?.hostname || "Device";
      setMoveNotice(
        modalMode === "replace"
          ? `Replaced device with ${deviceLabel} in ${block} / ${dc} / ${rack}.`
          : modalMode === "edit"
          ? `Updated placement for ${deviceLabel}.`
          : `Added ${deviceLabel} to ${block} / ${dc} / ${rack}.`
      );
      setAddOpen(false);
      setModalMode("add");
      setModalOldDeviceId(null);
      loadAllDevices();
      load();
      setTimeout(() => setMoveNotice(null), 3000);
    } catch (e: any) {
      setAddFormError(e?.response?.data?.detail || "Failed to save device placement.");
    } finally {
      setAddBusy(false);
    }
  };

  const toggleSelect = (deviceId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(deviceId)) {
        next.delete(deviceId);
      } else {
        next.add(deviceId);
      }
      return next;
    });
  };

  // Every known "block / dataCenter / rack" combo, for the bulk-move
  // target picker.
  const rackOptions = useMemo(() => {
    const opts: { label: string; block: string; dc: string; rack: string }[] = [];
    for (const block of groups) {
      for (const dc of block.data_centers) {
        for (const r of dc.racks) {
          opts.push({ label: `${block.name} / ${dc.name} / ${r.name}`, block: block.name, dc: dc.name, rack: r.name });
        }
      }
    }
    return opts;
  }, [groups]);

  // Existing block names (for the "Add device" picker's dropdown).
  const blockOptions = useMemo(() => groups.map((b) => b.name).filter((n) => n !== "Unassigned"), [groups]);

  const dcsForAddBlock = useMemo(() => {
    const blockName = addBlock === "__new__" ? null : addBlock;
    if (!blockName) return [];
    const block = groups.find((g) => g.name === blockName);
    return block ? block.data_centers.map((dc) => dc.name).filter((n) => n !== "Unassigned") : [];
  }, [groups, addBlock]);

  const racksForAddDc = useMemo(() => {
    const blockName = addBlock === "__new__" ? null : addBlock;
    const dcName = addDc === "__new__" ? null : addDc;
    if (!blockName || !dcName) return [];
    const block = groups.find((g) => g.name === blockName);
    const dc = block?.data_centers.find((d) => d.name === dcName);
    return dc ? dc.racks.map((r) => r.name).filter((n) => n !== "Unassigned") : [];
  }, [groups, addBlock, addDc]);

  const addDeviceOptions = useMemo(() => {
    const q = addDeviceQuery.trim().toLowerCase();
    let list = allDevices || [];
    if (modalMode === "replace" && modalOldDeviceId) list = list.filter((d) => d.id !== modalOldDeviceId);
    const filtered = q ? list.filter((d) => d.hostname.toLowerCase().includes(q)) : list;
    return filtered.slice(0, 50);
  }, [allDevices, addDeviceQuery, modalMode, modalOldDeviceId]);

  useEffect(() => {
    if (addBlock !== "__new__" && addDc !== "__new__" && addDc && !dcsForAddBlock.includes(addDc)) {
      setAddDc("");
    }
  }, [addBlock]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (addDc !== "__new__" && addRack !== "__new__" && addRack && !racksForAddDc.includes(addRack)) {
      setAddRack("");
    }
  }, [addDc]); // eslint-disable-line react-hooks/exhaustive-deps

  const runBulkMove = async () => {
    if (!bulkTarget || selected.size === 0) return;
    const target = rackOptions.find((o) => o.label === bulkTarget);
    if (!target) return;
    setBulkBusy(true);
    const ids = Array.from(selected);
    try {
      await Promise.all(
        ids.map((id) =>
          api.patch(`/devices/${id}`, {
            data_center: target.dc === "Unassigned" ? null : target.dc,
            block: target.block === "Unassigned" ? null : target.block,
            rack: target.rack === "Unassigned" ? null : target.rack,
          })
        )
      );
      setMoveNotice(`Moved ${ids.length} device${ids.length === 1 ? "" : "s"} to ${bulkTarget}.`);
      setSelected(new Set());
      setBulkTarget("");
      load();
      setTimeout(() => setMoveNotice(null), 3000);
    } catch {
      setError("Bulk move failed for one or more devices.");
    } finally {
      setBulkBusy(false);
    }
  };

  // Backend path segment (see app.api.devices' /groups/blocks,
  // /groups/blocks/{block}/data-centers/{dc}, .../racks/{rack}) for a
  // given tier -- shared by rename/delete below.
  const tierPath = (type: "block" | "data_center" | "rack", blockName: string, dcName?: string, name?: string) => {
    const base = `/devices/groups/blocks/${encodeURIComponent(blockName)}`;
    if (type === "block") return base;
    if (type === "data_center") return `${base}/data-centers/${encodeURIComponent(name || "")}`;
    return `${base}/data-centers/${encodeURIComponent(dcName || "")}/racks/${encodeURIComponent(name || "")}`;
  };

  // Renames a block/data-center/rack via the PhysicalLocation-backed
  // endpoints (see app.api.devices) instead of bulk-patching every
  // member device by hand -- one call now covers both an empty
  // placeholder tier (nothing to patch device-side at all, so the old
  // device-only approach silently no-op'd) and a populated one (the
  // backend moves every device in it, same net effect the old
  // Promise.all did).
  const handleRenameGroup = async (
    oldName: string,
    type: "block" | "data_center" | "rack",
    blockName: string,
    dcName?: string
  ) => {
    const newName = window.prompt(`Rename ${type.replace("_", " ")} "${oldName}" to:`, oldName);
    if (!newName || newName.trim() === oldName || newName.trim() === "") return;

    setLoading(true);
    try {
      await api.patch(tierPath(type, type === "block" ? oldName : blockName, dcName, type === "block" ? undefined : oldName));
      setMoveNotice(`${type.replace("_", " ")} renamed successfully`);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || `Failed to rename ${type.replace("_", " ")}.`);
      setLoading(false);
    }
  };

  const handleDeleteGroup = async (
    name: string,
    type: "block" | "data_center" | "rack",
    blockName: string,
    dcName?: string
  ) => {
    let deviceCount = 0;
    const block = groups.find((g) => g.name === blockName);
    if (block) {
      if (type === "block") {
        deviceCount = block.device_count;
      } else if (type === "data_center") {
        deviceCount = block.data_centers.find((dc) => dc.name === name)?.device_count || 0;
      } else {
        const dc = block.data_centers.find((dc) => dc.name === dcName);
        deviceCount = dc?.racks.find((r) => r.name === name)?.devices.length || 0;
      }
    }

    const confirmMsg =
      deviceCount > 0
        ? `Delete ${type.replace("_", " ")} "${name}"? This will move ${deviceCount} device${deviceCount === 1 ? "" : "s"} to Unassigned.`
        : `Delete ${type.replace("_", " ")} "${name}"?`;
    if (!(await confirm(confirmMsg))) return;

    setLoading(true);
    try {
      await api.delete(tierPath(type, type === "block" ? name : blockName, dcName, type === "block" ? undefined : name));
      setMoveNotice(`${type.replace("_", " ")} deleted successfully`);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || `Failed to delete ${type.replace("_", " ")}.`);
      setLoading(false);
    }
  };

  // Create-empty flows -- POST to the same PhysicalLocation-backed
  // endpoints, so a block/DC/rack can be set up ahead of any device
  // being placed in it (previously the only way a tier came into
  // existence at all was the "+ Add device" modal below implicitly
  // creating it).
  const handleCreateBlock = async () => {
    const name = window.prompt("New block name:");
    if (!name || !name.trim()) return;
    try {
      await api.post("/devices/groups/blocks", { name: name.trim() });
      setMoveNotice(`Block "${name.trim()}" created.`);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to create block.");
    }
  };

  const handleCreateDataCenter = async (blockName: string) => {
    const name = window.prompt(`New data center in "${blockName}":`);
    if (!name || !name.trim()) return;
    try {
      await api.post("/devices/groups/data-centers", { block: blockName, name: name.trim() });
      setMoveNotice(`Data center "${name.trim()}" created.`);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to create data center.");
    }
  };

  const handleCreateRack = async (blockName: string, dcName: string) => {
    const name = window.prompt(`New rack in "${blockName} / ${dcName}":`);
    if (!name || !name.trim()) return;
    try {
      await api.post("/devices/groups/racks", { block: blockName, data_center: dcName, name: name.trim() });
      setMoveNotice(`Rack "${name.trim()}" created.`);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to create rack.");
    }
  };

  const filteredGroups = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return groups;
    return groups
      .map((block) => ({
        ...block,
        data_centers: block.data_centers
          .map((dc) => ({
            ...dc,
            racks: dc.racks
              .map((r) => ({
                ...r,
                devices: r.devices.filter((d) => d.hostname.toLowerCase().includes(q)),
              }))
              .filter((r) => r.devices.length > 0 || r.name.toLowerCase().includes(q)),
          }))
          .filter((dc) => dc.racks.length > 0 || dc.name.toLowerCase().includes(q)),
      }))
      .filter((block) => block.data_centers.length > 0 || block.name.toLowerCase().includes(q));
  }, [groups, query]);

  const totals = useMemo(() => {
    const blockCount = groups.length;
    const dcCount = groups.reduce((n, block) => n + block.data_centers.length, 0);
    const rackCount = groups.reduce((n, block) => n + block.data_centers.reduce((m, dc) => m + dc.racks.length, 0), 0);
    const deviceCount = groups.reduce((n, block) => n + block.device_count, 0);
    return { blockCount, dcCount, rackCount, deviceCount };
  }, [groups]);

  return (
    <div className="pb-16 flex flex-col gap-6 md:p-2">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-bold text-navy dark:text-white">Groups</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Named logical groups you define, plus devices organized by physical Company → Block → Data Center → Rack.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {canManage && (
            <button
              onClick={() => openAddDevice()}
              className="bg-brandblue hover:bg-blue-600 text-white font-bold px-3 py-1.5 rounded-full transition shadow-sm text-xs"
            >
              + Add Device to Rack
            </button>
          )}
          <button
            onClick={load}
            className="text-brandblue font-medium hover:text-navy dark:hover:text-white bg-white dark:bg-slate-800 border border-brandblue hover:bg-slate-50 dark:hover:bg-slate-700 px-3 py-1.5 rounded-full transition shadow-sm text-xs"
          >
            ↻ Refresh
          </button>
        </div>
      </div>

      <NamedGroupsPanel canManage={canManage} />

      <div>
        <div className="flex items-center gap-2 mb-1">
          <span className="text-xs font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500 flex items-center gap-1.5">
            🏛️ Company
          </span>
        </div>
        <h2 className="text-lg font-bold text-navy dark:text-white mb-1">Block / Data Center / Rack</h2>
        <p className="text-sm text-slate-500 dark:text-slate-400 mb-3">
          Physical & organizational placement — every block can own multiple data centers, and every data center can own multiple racks.{" "}
          {canManage ? "Drag a device onto a rack to move it." : "Read-only."}
        </p>
      </div>

      {/* Enterprise stats bar */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { label: "Blocks", value: totals.blockCount, icon: "M2 7h20v14H2zM16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2", color: "blue" },
          { label: "Data Centers", value: totals.dcCount, icon: "M2 3h20v18H2zM2 9h20M2 15h20", color: "indigo" },
          { label: "Racks", value: totals.rackCount, icon: "M2 4h20v16H2zM8 4v16M16 4v16", color: "slate" },
          { label: "Devices", value: totals.deviceCount, icon: "M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18", color: "emerald" },
        ].map(({ label, value, icon, color }) => (
          <div key={label} className={`bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 flex items-center gap-3 shadow-sm`}>
            <div className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 bg-${color}-100 dark:bg-${color}-900/30 text-${color}-600 dark:text-${color}-400`}>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d={icon}/></svg>
            </div>
            <div>
              <p className="text-2xl font-bold text-slate-900 dark:text-white leading-none">{value}</p>
              <p className="text-[11px] text-slate-400 dark:text-slate-500 mt-0.5">{label}</p>
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <input
          className="border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 shadow-sm rounded-full px-4 py-2 text-sm flex-1 max-w-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none transition-shadow"
          placeholder="Search hostname, block, data center, or rack…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {query && (
          <button onClick={() => setQuery("")} className="text-xs text-slate-400 hover:text-slate-600 dark:hover:text-slate-200">
            ✕ Clear
          </button>
        )}
      </div>

      {error && (
        <p className="text-red-600 dark:text-red-400 font-semibold text-sm bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-800 px-3 py-2 rounded-lg">
          {error}
        </p>
      )}
      {moveNotice && (
        <p className="text-[13px] font-medium text-blue-700 dark:text-blue-300 bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-800 shadow-sm rounded-lg px-4 py-2.5">
          {moveNotice}
        </p>
      )}

      {canManage && selected.size > 0 && (
        <div className="sticky top-2 z-10 flex items-center gap-3 flex-wrap bg-slate-900 dark:bg-slate-950 text-white shadow-xl rounded-2xl px-5 py-3 border border-slate-700">
          <span className="font-bold text-sm">{selected.size} device{selected.size !== 1 ? "s" : ""} selected</span>
          <select
            value={bulkTarget}
            onChange={(e) => setBulkTarget(e.target.value)}
            className="bg-white/10 border border-white/20 rounded-lg px-3 py-1.5 text-xs outline-none flex-1 max-w-xs"
          >
            <option value="" className="text-slate-900">Move to rack…</option>
            {rackOptions.map((o) => (
              <option key={o.label} value={o.label} className="text-slate-900">{o.label}</option>
            ))}
          </select>
          <button onClick={runBulkMove} disabled={!bulkTarget || bulkBusy}
            className="bg-blue-600 hover:bg-blue-500 disabled:opacity-40 rounded-lg px-4 py-1.5 text-xs font-bold transition-colors">
            {bulkBusy ? "Moving…" : "Move"}
          </button>
          <button onClick={() => setSelected(new Set())} className="text-slate-400 hover:text-white text-xs ml-auto">
            ✕ Clear
          </button>
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-16 gap-3">
          <div className="w-5 h-5 rounded-full border-2 border-blue-500 border-t-transparent animate-spin" />
          <p className="text-sm text-slate-400">Loading infrastructure…</p>
        </div>
      ) : filteredGroups.length === 0 ? (
        <div className="text-center py-16 border-2 border-dashed border-slate-200 dark:border-slate-700 rounded-2xl">
          <svg width="40" height="40" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24" className="text-slate-300 dark:text-slate-600 mx-auto mb-3">
            <rect x="2" y="7" width="20" height="14" rx="1"/><path d="M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2"/><line x1="12" y1="12" x2="12" y2="16"/><line x1="10" y1="12" x2="14" y2="12"/>
          </svg>
          <p className="text-sm text-slate-400 italic mb-4">
            {query.trim() ? "No devices match your search." : "No devices placed in the infrastructure hierarchy yet."}
          </p>
          {canManage && !query.trim() && (
            <button onClick={() => openAddDevice()}
              className="bg-blue-600 hover:bg-blue-500 text-white font-bold px-5 py-2 rounded-full transition shadow-sm text-sm">
              + Place First Device
            </button>
          )}
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          {/* Company root banner */}
          <div className="rounded-2xl overflow-hidden border border-slate-200 dark:border-slate-700 shadow-sm">
            <div className="px-5 py-4 flex items-center gap-4 bg-gradient-to-r from-slate-800 to-slate-700 dark:from-slate-950 dark:to-slate-900">
              <div className="w-10 h-10 rounded-xl bg-white/10 flex items-center justify-center shrink-0 text-white">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>
                </svg>
              </div>
              <div className="flex-1 min-w-0">
                <h2 className="text-lg font-bold text-white">Company</h2>
                <p className="text-[11px] text-slate-400">
                  {totals.blockCount} block{totals.blockCount !== 1 ? "s" : ""} &middot; {totals.dcCount} data center{totals.dcCount !== 1 ? "s" : ""} &middot; {totals.rackCount} rack{totals.rackCount !== 1 ? "s" : ""} &middot; {totals.deviceCount} device{totals.deviceCount !== 1 ? "s" : ""}
                </p>
              </div>
              <HealthBadge devices={filteredGroups.flatMap(b => b.data_centers.flatMap(dc => dc.racks.flatMap(r => r.devices)))} />
              {canManage && (
                <button onClick={handleCreateBlock}
                  className="text-[11px] font-bold shrink-0 border border-white/30 text-white hover:bg-white/10 rounded-full px-2.5 py-1 transition-colors">
                  + New block
                </button>
              )}
            </div>
            <div className="p-4 bg-slate-50 dark:bg-slate-900/50 flex flex-col gap-4">
              {filteredGroups.map((block) => (
                <BlockCard
                  key={block.name}
                  block={block}
                  canManage={canManage}
                  onMove={handleMove}
                  onAddDevice={openAddDevice}
                  onRename={handleRenameGroup}
                  onDelete={handleDeleteGroup}
                  onCreateDataCenter={handleCreateDataCenter}
                  onCreateRack={handleCreateRack}
                  selected={selected}
                  onToggleSelect={toggleSelect}
                  onDeviceAction={handleDeviceAction}
                />
              ))}
            </div>
          </div>
        </div>
      )}

      {addOpen && (
        <div
          className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
          onClick={() => { setAddOpen(false); setModalMode("add"); setModalOldDeviceId(null); }}
        >
          <div
            className="bg-white dark:bg-slate-800 rounded-2xl shadow-xl w-full max-w-md p-5 flex flex-col gap-4"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h3 className="text-lg font-bold text-navy dark:text-white">
                {modalMode === "edit" ? "Edit Device Placement" : modalMode === "replace" ? "Replace Device" : "Add Device to Rack"}
              </h3>
              <button onClick={() => { setAddOpen(false); setModalMode("add"); setModalOldDeviceId(null); }} className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 text-xl leading-none">
                ×
              </button>
            </div>
            {modalMode === "replace" && (
              <p className="text-xs text-slate-500 dark:text-slate-400 -mt-2">
                Pick the device that should take over this slot. The current device will be moved to Unassigned.
              </p>
            )}

            {addFormError && (
              <p className="text-riskcrit text-xs font-semibold bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-800 px-3 py-2 rounded-lg">
                {addFormError}
              </p>
            )}

            <div className="flex flex-col gap-1">
              <label className="text-xs font-bold uppercase tracking-wide text-slate-500 dark:text-slate-400">Device</label>
              <input
                value={addDeviceQuery}
                onChange={(e) => setAddDeviceQuery(e.target.value)}
                placeholder="Search hostname…"
                className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-1.5 text-sm bg-transparent outline-none focus:ring-2 focus:ring-brandblue"
              />
              <select
                value={addDeviceId}
                onChange={(e) => setAddDeviceId(e.target.value)}
                size={6}
                className="border border-slate-300 dark:border-slate-600 rounded-lg text-sm bg-transparent outline-none focus:ring-2 focus:ring-brandblue"
              >
                {allDevices === null && <option disabled>Loading devices…</option>}
                {allDevices !== null && addDeviceOptions.length === 0 && <option disabled>No devices found</option>}
                {addDeviceOptions.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.hostname} {d.data_center ? `(currently ${d.block || "?"}/${d.data_center}/${d.rack || "?"})` : "(unplaced)"}
                  </option>
                ))}
              </select>
            </div>

            <div className="flex flex-col gap-1">
              <label className="text-xs font-bold uppercase tracking-wide text-slate-500 dark:text-slate-400">Block</label>
              <select
                value={addBlock}
                onChange={(e) => setAddBlock(e.target.value)}
                className="border border-slate-300 dark:border-slate-600 rounded-lg px-2 py-1.5 text-sm bg-transparent outline-none focus:ring-2 focus:ring-brandblue"
              >
                <option value="">Select…</option>
                {blockOptions.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
                <option value="__new__">+ New block…</option>
              </select>
              {addBlock === "__new__" && (
                <input
                  autoFocus
                  value={addBlockNew}
                  onChange={(e) => setAddBlockNew(e.target.value)}
                  placeholder="e.g. APAC Region"
                  className="border border-slate-300 dark:border-slate-600 rounded-lg px-2 py-1.5 text-sm bg-transparent outline-none focus:ring-2 focus:ring-brandblue mt-1"
                />
              )}
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1">
                <label className="text-xs font-bold uppercase tracking-wide text-slate-500 dark:text-slate-400">Data Center</label>
                <select
                  value={addDc}
                  onChange={(e) => setAddDc(e.target.value)}
                  disabled={!addBlock}
                  className="border border-slate-300 dark:border-slate-600 rounded-lg px-2 py-1.5 text-sm bg-transparent outline-none focus:ring-2 focus:ring-brandblue disabled:opacity-40"
                >
                  <option value="">Select…</option>
                  {dcsForAddBlock.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                  <option value="__new__">+ New data center…</option>
                </select>
                {addDc === "__new__" && (
                  <input
                    autoFocus
                    value={addDcNew}
                    onChange={(e) => setAddDcNew(e.target.value)}
                    placeholder="e.g. DC-East"
                    className="border border-slate-300 dark:border-slate-600 rounded-lg px-2 py-1.5 text-sm bg-transparent outline-none focus:ring-2 focus:ring-brandblue mt-1"
                  />
                )}
              </div>

              <div className="flex flex-col gap-1">
                <label className="text-xs font-bold uppercase tracking-wide text-slate-500 dark:text-slate-400">Rack</label>
                <select
                  value={addRack}
                  onChange={(e) => setAddRack(e.target.value)}
                  disabled={!addDc}
                  className="border border-slate-300 dark:border-slate-600 rounded-lg px-2 py-1.5 text-sm bg-transparent outline-none focus:ring-2 focus:ring-brandblue disabled:opacity-40"
                >
                  <option value="">Select…</option>
                  {racksForAddDc.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                  <option value="__new__">+ New rack…</option>
                </select>
                {addRack === "__new__" && (
                  <input
                    autoFocus
                    value={addRackNew}
                    onChange={(e) => setAddRackNew(e.target.value)}
                    placeholder="e.g. R12"
                    className="border border-slate-300 dark:border-slate-600 rounded-lg px-2 py-1.5 text-sm bg-transparent outline-none focus:ring-2 focus:ring-brandblue mt-1"
                  />
                )}
              </div>
            </div>

            <div className="flex flex-col gap-1">
              <label className="text-xs font-bold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                Rack Position (U slot, optional)
              </label>
              <input
                type="number"
                min={1}
                value={addPosition}
                onChange={(e) => setAddPosition(e.target.value)}
                placeholder="e.g. 12"
                className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-1.5 text-sm bg-transparent outline-none focus:ring-2 focus:ring-brandblue w-32"
              />
            </div>

            <div className="flex items-center justify-end gap-2 mt-1">
              <button
                onClick={() => setAddOpen(false)}
                className="px-3 py-1.5 rounded-full text-sm font-medium text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-700 transition"
              >
                Cancel
              </button>
              <button
                onClick={assignDevice}
                disabled={addBusy}
                className="bg-brandblue hover:bg-blue-600 disabled:opacity-40 text-white font-bold px-4 py-1.5 rounded-full text-sm transition"
              >
                {addBusy
                  ? "Saving…"
                  : modalMode === "edit"
                  ? "Save Placement"
                  : modalMode === "replace"
                  ? "Replace Device"
                  : "Add to Rack"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}