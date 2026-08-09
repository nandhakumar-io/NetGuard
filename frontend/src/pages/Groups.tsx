import React, { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Device, DeviceGroup } from "../lib/types";

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

/** Rolls a list of devices up into up/degraded/down/unknown counts, so a
 * NOC admin can see rack/DC health without opening every card. */
function healthRollup(devices: GroupDevice[]) {
  const counts = { online: 0, degraded: 0, offline: 0, unknown: 0 };
  for (const d of devices) {
    counts[d.status in counts ? (d.status as keyof typeof counts) : "unknown"]++;
  }
  return counts;
}

function HealthBadge({ devices }: { devices: GroupDevice[] }) {
  const c = healthRollup(devices);
  if (devices.length === 0) return null;
  return (
    <div className="flex items-center gap-2 text-[11px] font-semibold">
      {c.online > 0 && <span className="text-risklow flex items-center gap-1">● {c.online} up</span>}
      {c.degraded > 0 && <span className="text-riskmed flex items-center gap-1">● {c.degraded} degraded</span>}
      {c.offline > 0 && <span className="text-slate-400 dark:text-slate-500 flex items-center gap-1">● {c.offline} down</span>}
      {c.unknown > 0 && <span className="text-slate-300 dark:text-slate-600 flex items-center gap-1">● {c.unknown} unknown</span>}
    </div>
  );
}

function RackCard({
  rack,
  dcName,
  canManage,
  onMove,
  selected,
  onToggleSelect,
}: {
  rack: GroupRack;
  dcName: string;
  canManage: boolean;
  onMove: (deviceId: string, dataCenter: string, rack: string) => void;
  selected: Set<string>;
  onToggleSelect: (deviceId: string) => void;
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
      <div className="bg-slate-100 dark:bg-slate-900 px-4 py-2 flex items-center justify-between border-b border-slate-200 dark:border-slate-700 gap-3">
        <p className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 flex items-center gap-1.5 shrink-0">
          🗄️ Rack: {rack.name}
        </p>
        <HealthBadge devices={rack.devices} />
        <span className="text-[11px] text-slate-400 dark:text-slate-500 shrink-0 ml-auto">{rack.devices.length} device{rack.devices.length === 1 ? "" : "s"}</span>
      </div>
      <div className="divide-y divide-slate-100 dark:divide-slate-800">
        {rack.devices.length === 0 && (
          <p className="text-xs text-slate-400 dark:text-slate-500 italic px-4 py-3">Empty rack — drag a device here.</p>
        )}
        {rack.devices.map((d) => (
          <div
            key={d.id}
            draggable={canManage}
            onDragStart={(e) => e.dataTransfer.setData("text/device-id", d.id)}
            className="flex items-center gap-2 px-4 py-2 hover:bg-slate-50 dark:hover:bg-slate-900 transition-colors group"
          >
            {canManage && (
              <input
                type="checkbox"
                checked={selected.has(d.id)}
                onChange={() => onToggleSelect(d.id)}
                onClick={(e) => e.stopPropagation()}
                className="shrink-0 accent-brandblue"
              />
            )}
            {d.rack_position != null && (
              <span className="text-[10px] font-mono text-slate-400 dark:text-slate-500 w-6 text-right">U{d.rack_position}</span>
            )}
            <span className={`w-2 h-2 rounded-full shrink-0 ${statusColor[d.status] || statusColor.unknown}`} />
            <Link
              to={`/devices?q=${encodeURIComponent(d.hostname)}`}
              className="text-sm font-medium text-navy dark:text-white group-hover:text-brandblue truncate flex-1"
            >
              {d.hostname}
            </Link>
            {d.device_type && (
              <span className="text-[10px] uppercase tracking-wide text-slate-400 dark:text-slate-500">{d.device_type}</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/** Named/logical device groups (app.models.device_group.DeviceGroup) --
 * distinct from the Data Center/Rack physical-placement view below.
 * Supports create/edit/delete and nesting (a group can have a parent),
 * plus per-group device membership management. Only Network Admins get
 * the management controls (GROUP_MANAGER_ROLES on the backend matches),
 * everyone else sees a read-only tree. */
function NamedGroupsPanel({ canManage }: { canManage: boolean }) {
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
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

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
    setFormError(null);
    setFormOpen(true);
  };

  const openEdit = (group: DeviceGroup) => {
    setEditingGroup(group);
    setFormName(group.name);
    setFormDescription(group.description || "");
    setFormType(group.group_type);
    setFormParentId(group.parent_group_id || "");
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
    if (!window.confirm(confirmMsg)) return;
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
  const [groups, setGroups] = useState<GroupDataCenter[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [moveNotice, setMoveNotice] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkTarget, setBulkTarget] = useState("");
  const [bulkBusy, setBulkBusy] = useState(false);

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

  const toggleSelect = (deviceId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(deviceId) ? next.delete(deviceId) : next.add(deviceId);
      return next;
    });
  };

  // Every known "dataCenter / rack" combo, for the bulk-move target picker.
  const rackOptions = useMemo(() => {
    const opts: { label: string; dc: string; rack: string }[] = [];
    for (const dc of groups) {
      for (const r of dc.racks) {
        opts.push({ label: `${dc.name} / ${r.name}`, dc: dc.name, rack: r.name });
      }
    }
    return opts;
  }, [groups]);

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
            Named logical groups you define, plus devices organized by physical Data Center → Rack.
          </p>
        </div>
        <button
          onClick={load}
          className="text-brandblue font-medium hover:text-navy dark:hover:text-white bg-white dark:bg-slate-800 border border-brandblue hover:bg-slate-50 dark:hover:bg-slate-700 px-3 py-1.5 rounded-full transition shadow-sm text-xs"
        >
          ↻ Refresh
        </button>
      </div>

      <NamedGroupsPanel canManage={canManage} />

      <div>
        <h2 className="text-lg font-bold text-navy dark:text-white mb-1">Data Center / Rack</h2>
        <p className="text-sm text-slate-500 dark:text-slate-400 mb-3">Physical placement — {canManage ? "drag a device onto a rack to move it." : "read-only."}</p>
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

      {canManage && selected.size > 0 && (
        <div className="sticky top-2 z-10 flex items-center gap-3 flex-wrap bg-navy text-white shadow-lg rounded-full px-4 py-2 text-sm">
          <span className="font-bold">{selected.size} selected</span>
          <select
            value={bulkTarget}
            onChange={(e) => setBulkTarget(e.target.value)}
            className="bg-white/10 border border-white/30 rounded-full px-3 py-1 text-xs outline-none"
          >
            <option value="" className="text-navy">Move to rack…</option>
            {rackOptions.map((o) => (
              <option key={o.label} value={o.label} className="text-navy">
                {o.label}
              </option>
            ))}
          </select>
          <button
            onClick={runBulkMove}
            disabled={!bulkTarget || bulkBusy}
            className="bg-brandblue hover:bg-blue-600 disabled:opacity-40 rounded-full px-3 py-1 text-xs font-bold"
          >
            {bulkBusy ? "Moving…" : "Move"}
          </button>
          <button onClick={() => setSelected(new Set())} className="text-white/70 hover:text-white text-xs ml-auto">
            Clear
          </button>
        </div>
      )}

      {loading ? (
        <p className="text-sm text-slate-400 dark:text-slate-500 italic">Loading groups…</p>
      ) : filteredGroups.length === 0 ? (
        <p className="text-sm text-slate-400 dark:text-slate-500 italic">No devices match.</p>
      ) : (
        <div className="flex flex-col gap-6">
          {filteredGroups.map((dc) => (
            <div key={dc.name} className="bg-slate-50 dark:bg-slate-900/40 border border-slate-200 dark:border-slate-700 rounded-2xl p-4">
              <div className="flex items-center justify-between mb-3 px-1 gap-3 flex-wrap">
                <h2 className="text-lg font-bold text-navy dark:text-white flex items-center gap-2">
                  🏢 {dc.name}
                </h2>
                <HealthBadge devices={dc.racks.flatMap((r) => r.devices)} />
                <span className="text-xs text-slate-400 dark:text-slate-500 ml-auto">{dc.device_count} device{dc.device_count === 1 ? "" : "s"} · {dc.racks.length} rack{dc.racks.length === 1 ? "" : "s"}</span>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {dc.racks.map((rack) => (
                  <RackCard
                    key={rack.name}
                    rack={rack}
                    dcName={dc.name}
                    canManage={canManage}
                    onMove={handleMove}
                    selected={selected}
                    onToggleSelect={toggleSelect}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}