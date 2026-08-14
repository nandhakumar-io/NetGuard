import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useConfirm } from "../lib/confirm";
import { useToast } from "../lib/toast";
import { ConflictReport, FreeIPResult, IPReservation, Subnet, SubnetAddressEntry } from "../lib/types";
import { EmptyState, LoadingRows } from "../components/EmptyState";

const emptySubnetForm = {
  cidr: "",
  name: "",
  vlan_id: "",
  site: "",
  description: "",
  tags: "",
};

const emptyReservationForm = {
  ip_address: "",
  state: "reserved" as IPReservation["state"],
  note: "",
};

const stateStyle: Record<string, string> = {
  free: "bg-slate-100 text-slate-500 dark:bg-white/5 dark:text-slate-400",
  assigned: "bg-brandblue/10 text-brandblue",
  reserved: "bg-amber-100 text-amber-700 dark:bg-amber-950/60 dark:text-amber-300",
  gateway: "bg-purple-100 text-purple-700 dark:bg-purple-950/60 dark:text-purple-300",
  broadcast: "bg-slate-200 text-slate-600 dark:bg-white/10 dark:text-slate-300",
  network: "bg-slate-200 text-slate-600 dark:bg-white/10 dark:text-slate-300",
};

function utilizationColor(pct: number): string {
  if (pct >= 90) return "bg-riskcrit";
  if (pct >= 75) return "bg-riskmed";
  return "bg-risklow";
}

function UtilizationBar({ pct }: { pct: number }) {
  return (
    <div className="flex items-center gap-2 w-40">
      <div className="flex-1 h-2 rounded-full bg-slate-100 dark:bg-white/10 overflow-hidden">
        <div className={`h-full rounded-full ${utilizationColor(pct)}`} style={{ width: `${Math.min(pct, 100)}%` }} />
      </div>
      <span className="text-xs font-semibold text-slate-500 dark:text-slate-400 w-10 text-right">{pct}%</span>
    </div>
  );
}

export default function IPAMPage() {
  const { user } = useAuth();
  const confirm = useConfirm();
  const toast = useToast();
  const canManage = user?.role === "network_admin" || user?.role === "network_engineer";

  const [subnets, setSubnets] = useState<Subnet[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [conflicts, setConflicts] = useState<ConflictReport>({ conflicts: [] });

  const [siteFilter, setSiteFilter] = useState("");
  const [search, setSearch] = useState("");

  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(emptySubnetForm);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [detailSubnet, setDetailSubnet] = useState<Subnet | null>(null);

  const load = () => {
    setLoading(true);
    Promise.all([
      api.get<Subnet[]>("/ipam", { params: siteFilter ? { site: siteFilter } : {} }),
      api.get<ConflictReport>("/ipam/conflicts"),
    ])
      .then(([subnetsRes, conflictsRes]) => {
        setSubnets(subnetsRes.data);
        setConflicts(conflictsRes.data);
        setError(null);
      })
      .catch(() => setError("Failed to load IPAM data."))
      .finally(() => setLoading(false));
  };

  useEffect(load, [siteFilter]);

  const sites = useMemo(() => Array.from(new Set(subnets.map((s) => s.site).filter(Boolean))) as string[], [subnets]);

  const filteredSubnets = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return subnets;
    return subnets.filter(
      (s) =>
        s.cidr.toLowerCase().includes(q) ||
        (s.name ?? "").toLowerCase().includes(q) ||
        (s.site ?? "").toLowerCase().includes(q) ||
        String(s.vlan_id ?? "").includes(q)
    );
  }, [subnets, search]);

  const openNew = () => {
    setForm(emptySubnetForm);
    setSaveError(null);
    setShowForm(true);
  };

  const submitSubnet = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setSaveError(null);
    try {
      await api.post("/ipam", {
        cidr: form.cidr,
        name: form.name || null,
        vlan_id: form.vlan_id ? Number(form.vlan_id) : null,
        site: form.site || null,
        description: form.description || null,
        tags: form.tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      });
      setShowForm(false);
      toast.success(`Subnet ${form.cidr} created.`);
      load();
    } catch (err: any) {
      setSaveError(err?.response?.data?.detail ?? "Failed to create subnet.");
    } finally {
      setSaving(false);
    }
  };

  const deleteSubnet = async (subnet: Subnet) => {
    if (
      !(await confirm(`Delete subnet ${subnet.cidr}? Any reservations in it are removed too. Devices are untouched.`, {
        confirmLabel: "Delete subnet",
      }))
    )
      return;
    try {
      await api.delete(`/ipam/${subnet.id}`);
      toast.success(`Subnet ${subnet.cidr} deleted.`);
      if (detailSubnet?.id === subnet.id) setDetailSubnet(null);
      load();
    } catch {
      toast.error("Failed to delete subnet.");
    }
  };

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900 dark:text-white">IPAM</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Subnet inventory, utilization, and free-address lookup — no more spreadsheet-of-record.
          </p>
        </div>
        {canManage && (
          <button
            onClick={openNew}
            className="px-4 py-2 bg-blue-600 text-white rounded-md text-sm font-medium hover:bg-blue-700"
          >
            Add subnet
          </button>
        )}
      </div>

      {conflicts.conflicts.length > 0 && (
        <div className="rounded-xl border border-riskcrit/30 bg-riskcrit/5 dark:bg-riskcrit/10 p-4">
          <p className="text-sm font-semibold text-riskcrit">
            {conflicts.conflicts.length} IP {conflicts.conflicts.length === 1 ? "conflict" : "conflicts"} in inventory
          </p>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-0.5 mb-3">
            More than one device is statically assigned the same management IP.
          </p>
          <div className="space-y-1.5">
            {conflicts.conflicts.map((c) => (
              <div key={c.ip_address} className="text-xs flex items-center gap-2 flex-wrap">
                <span className="font-mono font-semibold text-slate-700 dark:text-slate-200">{c.ip_address}</span>
                <span className="text-slate-400">→</span>
                {c.hostnames.map((h, i) => (
                  <span
                    key={c.device_ids[i]}
                    className="px-2 py-0.5 rounded-full bg-white dark:bg-slate-800 border border-riskcrit/30 text-riskcrit"
                  >
                    {h}
                  </span>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="flex items-center gap-3">
        <input
          className="border border-slate-300 dark:border-slate-600 dark:bg-slate-800 rounded-lg px-3 py-2 text-sm w-64"
          placeholder="Search CIDR, name, VLAN, site…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          className="border border-slate-300 dark:border-slate-600 dark:bg-slate-800 rounded-lg px-3 py-2 text-sm"
          value={siteFilter}
          onChange={(e) => setSiteFilter(e.target.value)}
        >
          <option value="">All sites</option>
          {sites.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      {error && <p className="text-riskcrit text-sm">{error}</p>}

      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl shadow-sm overflow-hidden">
        {loading ? (
          <LoadingRows rows={5} className="p-4" />
        ) : filteredSubnets.length === 0 ? (
          <EmptyState
            title="No subnets found"
            message={subnets.length === 0 ? "Add your first managed subnet to get started." : "Try adjusting your search or filters."}
          />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs font-medium text-slate-500 dark:text-slate-400 uppercase tracking-wide border-b border-slate-200 dark:border-slate-700">
                <th className="px-4 py-3">CIDR</th>
                <th className="px-4 py-3">Name</th>
                <th className="px-4 py-3">VLAN</th>
                <th className="px-4 py-3">Site</th>
                <th className="px-4 py-3">Utilization</th>
                <th className="px-4 py-3">Free / Usable</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody>
              {filteredSubnets.map((s) => (
                <tr
                  key={s.id}
                  className="border-b border-slate-100 dark:border-slate-700/60 last:border-0 hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer"
                  onClick={() => setDetailSubnet(s)}
                >
                  <td className="px-4 py-3 font-mono font-semibold text-slate-800 dark:text-slate-100">{s.cidr}</td>
                  <td className="px-4 py-3 text-slate-600 dark:text-slate-300">{s.name ?? "—"}</td>
                  <td className="px-4 py-3 text-slate-600 dark:text-slate-300">{s.vlan_id ?? "—"}</td>
                  <td className="px-4 py-3 text-slate-600 dark:text-slate-300">{s.site ?? "—"}</td>
                  <td className="px-4 py-3">
                    <UtilizationBar pct={s.utilization_pct} />
                  </td>
                  <td className="px-4 py-3 text-slate-600 dark:text-slate-300">
                    {s.free_count} / {s.usable_addresses}
                  </td>
                  <td className="px-4 py-3 text-right" onClick={(e) => e.stopPropagation()}>
                    {canManage && (
                      <button
                        onClick={() => deleteSubnet(s)}
                        className="text-xs font-medium text-riskcrit hover:underline"
                      >
                        Delete
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white dark:bg-slate-800 rounded-xl shadow-xl max-w-lg w-full p-6">
            <h2 className="text-lg font-bold text-navy dark:text-white mb-4">Add Subnet</h2>
            <form onSubmit={submitSubnet} className="flex flex-col gap-3">
              <input
                className="border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-2 text-sm font-mono"
                placeholder="CIDR (e.g. 10.20.30.0/24)"
                value={form.cidr}
                onChange={(e) => setForm({ ...form, cidr: e.target.value })}
                required
              />
              <input
                className="border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-2 text-sm"
                placeholder="Name (optional, e.g. Branch-East Access VLAN)"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
              <div className="grid grid-cols-2 gap-3">
                <input
                  className="border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-2 text-sm"
                  placeholder="VLAN ID (optional)"
                  type="number"
                  value={form.vlan_id}
                  onChange={(e) => setForm({ ...form, vlan_id: e.target.value })}
                />
                <input
                  className="border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-2 text-sm"
                  placeholder="Site (optional)"
                  value={form.site}
                  onChange={(e) => setForm({ ...form, site: e.target.value })}
                />
              </div>
              <textarea
                className="border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-2 text-sm"
                placeholder="Description (optional)"
                rows={2}
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
              />
              <input
                className="border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-2 text-sm"
                placeholder="Tags, comma-separated (optional)"
                value={form.tags}
                onChange={(e) => setForm({ ...form, tags: e.target.value })}
              />
              {saveError && <p className="text-riskcrit text-xs">{saveError}</p>}
              <div className="flex justify-end gap-2 mt-2">
                <button
                  type="button"
                  onClick={() => setShowForm(false)}
                  className="px-4 py-2 rounded-md text-sm font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-white/5"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={saving}
                  className="px-4 py-2 bg-blue-600 text-white rounded-md text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
                >
                  {saving ? "Creating…" : "Create subnet"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {detailSubnet && (
        <SubnetDetailModal
          subnet={detailSubnet}
          canManage={canManage}
          onClose={() => setDetailSubnet(null)}
          onChanged={load}
        />
      )}
    </div>
  );
}

function SubnetDetailModal({
  subnet,
  canManage,
  onClose,
  onChanged,
}: {
  subnet: Subnet;
  canManage: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const confirm = useConfirm();
  const toast = useToast();

  const [addresses, setAddresses] = useState<SubnetAddressEntry[]>([]);
  const [reservations, setReservations] = useState<IPReservation[]>([]);
  const [loading, setLoading] = useState(true);
  const [addrError, setAddrError] = useState<string | null>(null);
  const [findingFree, setFindingFree] = useState(false);
  const [freeResult, setFreeResult] = useState<FreeIPResult | null>(null);
  const [onlyFree, setOnlyFree] = useState(false);

  const [showReserve, setShowReserve] = useState(false);
  const [reserveForm, setReserveForm] = useState(emptyReservationForm);
  const [reserveError, setReserveError] = useState<string | null>(null);
  const [reserving, setReserving] = useState(false);

  const loadAddresses = () => {
    setLoading(true);
    Promise.all([
      api.get<SubnetAddressEntry[]>(`/ipam/${subnet.id}/addresses`),
      api.get<IPReservation[]>(`/ipam/${subnet.id}/reservations`),
    ])
      .then(([addrRes, resvRes]) => {
        setAddresses(addrRes.data);
        setReservations(resvRes.data);
        setAddrError(null);
      })
      .catch((err) => setAddrError(err?.response?.data?.detail ?? "Failed to load address table."))
      .finally(() => setLoading(false));
  };

  useEffect(loadAddresses, [subnet.id]);

  const findFreeIP = async () => {
    setFindingFree(true);
    setFreeResult(null);
    try {
      const res = await api.get<FreeIPResult>(`/ipam/${subnet.id}/free-ip`);
      setFreeResult(res.data);
    } catch {
      toast.error("Failed to find a free IP.");
    } finally {
      setFindingFree(false);
    }
  };

  const submitReservation = async (e: React.FormEvent) => {
    e.preventDefault();
    setReserving(true);
    setReserveError(null);
    try {
      await api.post(`/ipam/${subnet.id}/reservations`, {
        ip_address: reserveForm.ip_address,
        state: reserveForm.state,
        note: reserveForm.note || null,
      });
      setShowReserve(false);
      setReserveForm(emptyReservationForm);
      toast.success(`${reserveForm.ip_address} reserved.`);
      loadAddresses();
      onChanged();
    } catch (err: any) {
      setReserveError(err?.response?.data?.detail ?? "Failed to reserve address.");
    } finally {
      setReserving(false);
    }
  };

  const deleteReservation = async (entry: SubnetAddressEntry) => {
    const reservation = reservations.find((r) => r.ip_address === entry.ip_address);
    if (!reservation) return; // gateway/broadcast/network on a small subnet may be structural, not a stored row
    if (!(await confirm(`Remove the ${entry.state} hold on ${entry.ip_address}?`, { confirmLabel: "Remove" }))) return;
    try {
      await api.delete(`/ipam/${subnet.id}/reservations/${reservation.id}`);
      toast.success(`Removed hold on ${entry.ip_address}.`);
      loadAddresses();
      onChanged();
    } catch {
      toast.error("Failed to remove reservation.");
    }
  };

  const visibleAddresses = onlyFree ? addresses.filter((a) => a.state === "free") : addresses;
  const util = subnet.total_addresses ? Math.round((subnet.used_count / subnet.total_addresses) * 1000) / 10 : 0;

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-white dark:bg-slate-800 rounded-xl shadow-xl max-w-3xl w-full max-h-[90vh] overflow-y-auto p-6">
        <div className="flex items-start justify-between mb-1">
          <div>
            <h2 className="text-lg font-bold text-navy dark:text-white font-mono">{subnet.cidr}</h2>
            <p className="text-sm text-slate-500 dark:text-slate-400">
              {subnet.name ?? "Unnamed subnet"}
              {subnet.vlan_id != null && ` · VLAN ${subnet.vlan_id}`}
              {subnet.site && ` · ${subnet.site}`}
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 text-sm">
            ✕
          </button>
        </div>

        <div className="flex items-center gap-4 mt-4 mb-4">
          <UtilizationBar pct={subnet.utilization_pct} />
          <span className="text-xs text-slate-500 dark:text-slate-400">
            {subnet.used_count} used · {subnet.free_count} free · {subnet.usable_addresses} usable of{" "}
            {subnet.total_addresses} total
          </span>
        </div>

        {canManage && (
          <div className="flex items-center gap-2 mb-4 flex-wrap">
            <button
              onClick={findFreeIP}
              disabled={findingFree}
              className="px-3 py-1.5 rounded-md text-xs font-medium bg-slate-100 dark:bg-white/10 text-slate-700 dark:text-slate-200 hover:bg-slate-200 dark:hover:bg-white/20 disabled:opacity-50"
            >
              {findingFree ? "Finding…" : "Find free IP"}
            </button>
            {freeResult && (
              <span className="text-xs font-mono">
                {freeResult.free_ip ? (
                  <span className="text-risklow font-semibold">{freeResult.free_ip}</span>
                ) : (
                  <span className="text-riskcrit">{freeResult.message}</span>
                )}
              </span>
            )}
            <button
              onClick={() => {
                setReserveForm(emptyReservationForm);
                setReserveError(null);
                setShowReserve(true);
              }}
              className="px-3 py-1.5 rounded-md text-xs font-medium bg-blue-600 text-white hover:bg-blue-700 ml-auto"
            >
              Reserve address
            </button>
          </div>
        )}

        <div className="flex items-center gap-2 mb-2">
          <label className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
            <input type="checkbox" checked={onlyFree} onChange={(e) => setOnlyFree(e.target.checked)} />
            Free addresses only
          </label>
        </div>

        {addrError && <p className="text-riskcrit text-sm mb-2">{addrError}</p>}

        <div className="border border-slate-200 dark:border-slate-700 rounded-lg overflow-hidden">
          {loading ? (
            <LoadingRows rows={6} className="p-4" />
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs font-medium text-slate-500 dark:text-slate-400 uppercase tracking-wide border-b border-slate-200 dark:border-slate-700">
                  <th className="px-3 py-2">IP</th>
                  <th className="px-3 py-2">State</th>
                  <th className="px-3 py-2">Device / Note</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody className="max-h-96">
                {visibleAddresses.map((a) => {
                  const hasReservation = reservations.some((r) => r.ip_address === a.ip_address);
                  return (
                    <tr key={a.ip_address} className="border-b border-slate-100 dark:border-slate-700/60 last:border-0">
                      <td className="px-3 py-2 font-mono text-slate-700 dark:text-slate-200">{a.ip_address}</td>
                      <td className="px-3 py-2">
                        <span className={`px-2 py-0.5 rounded-full text-[11px] font-semibold ${stateStyle[a.state]}`}>
                          {a.state}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-slate-600 dark:text-slate-300">{a.hostname ?? a.note ?? "—"}</td>
                      <td className="px-3 py-2 text-right">
                        {canManage && hasReservation && (
                          <button
                            onClick={() => deleteReservation(a)}
                            className="text-xs font-medium text-riskcrit hover:underline"
                          >
                            Remove
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        {showReserve && (
          <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-[60] p-4">
            <div className="bg-white dark:bg-slate-800 rounded-xl shadow-xl max-w-sm w-full p-6">
              <h3 className="text-base font-bold text-navy dark:text-white mb-3">Reserve address</h3>
              <form onSubmit={submitReservation} className="flex flex-col gap-3">
                <input
                  className="border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-2 text-sm font-mono"
                  placeholder={`IP inside ${subnet.cidr}`}
                  value={reserveForm.ip_address}
                  onChange={(e) => setReserveForm({ ...reserveForm, ip_address: e.target.value })}
                  required
                />
                <select
                  className="border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-2 text-sm"
                  value={reserveForm.state}
                  onChange={(e) => setReserveForm({ ...reserveForm, state: e.target.value as IPReservation["state"] })}
                >
                  <option value="reserved">Reserved (admin hold)</option>
                  <option value="gateway">Gateway</option>
                  <option value="broadcast">Broadcast</option>
                  <option value="network">Network</option>
                </select>
                <input
                  className="border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-2 text-sm"
                  placeholder="Note (optional)"
                  value={reserveForm.note}
                  onChange={(e) => setReserveForm({ ...reserveForm, note: e.target.value })}
                />
                {reserveError && <p className="text-riskcrit text-xs">{reserveError}</p>}
                <div className="flex justify-end gap-2 mt-1">
                  <button
                    type="button"
                    onClick={() => setShowReserve(false)}
                    className="px-4 py-2 rounded-md text-sm font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-white/5"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={reserving}
                    className="px-4 py-2 bg-blue-600 text-white rounded-md text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
                  >
                    {reserving ? "Reserving…" : "Reserve"}
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}