import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { ChangeRequest, Device } from "../lib/types";
import RiskBadge from "../components/RiskBadge";
import ConfigDiff from "../components/ConfigDiff";

const statusStyle: Record<string, string> = {
  draft: "bg-slate-100 text-slate-600",
  pending_approval: "bg-amber-100 text-amber-700",
  approved: "bg-blue-100 text-blue-700",
  rejected: "bg-red-100 text-red-700",
  success: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  rolled_back: "bg-red-100 text-red-700",
};

export default function ChangeRequests() {
  const [requests, setRequests] = useState<ChangeRequest[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [selected, setSelected] = useState<ChangeRequest | null>(null);
  const [form, setForm] = useState({ device_id: "", description: "", proposed_config: "" });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    api.get<ChangeRequest[]>("/change-requests").then((res) => setRequests(res.data));
    api.get<Device[]>("/devices").then((res) => setDevices(res.data));
  };

  useEffect(load, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await api.post("/change-requests", form);
      setForm({ device_id: "", description: "", proposed_config: "" });
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to submit change request.");
    } finally {
      setLoading(false);
    }
  };

  const act = async (id: string, action: "approve" | "reject") => {
    await api.post(`/change-requests/${id}/${action}`);
    load();
    setSelected(null);
  };

  const hostnameFor = (deviceId: string) => devices.find((d) => d.id === deviceId)?.hostname || deviceId.slice(0, 8);

  return (
    <div>
      <h1 className="text-2xl font-bold text-navy">Change Requests</h1>
      <p className="text-sm text-slate-500 mt-1">
        Submit configuration changes for AI risk analysis, validation, and approval.
      </p>

      <form onSubmit={submit} className="mt-6 bg-white border border-slate-200 rounded-xl p-5 space-y-3">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <select
            className="border border-slate-300 rounded-lg px-3 py-2 text-sm"
            value={form.device_id}
            onChange={(e) => setForm({ ...form, device_id: e.target.value })}
            required
          >
            <option value="">Select device…</option>
            {devices.map((d) => (
              <option key={d.id} value={d.id}>
                {d.hostname} ({d.ip_address})
              </option>
            ))}
          </select>
          <input
            className="border border-slate-300 rounded-lg px-3 py-2 text-sm"
            placeholder="Change description"
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            required
          />
        </div>
        <textarea
          className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm font-mono"
          rows={5}
          placeholder={"Proposed configuration, e.g.\ninterface Gi0/1\n ip address 10.2.2.1 255.255.255.0"}
          value={form.proposed_config}
          onChange={(e) => setForm({ ...form, proposed_config: e.target.value })}
          required
        />
        <button
          type="submit"
          disabled={loading || !devices.length}
          className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
        >
          {loading ? "Analyzing…" : "Submit Change Request"}
        </button>
        {!devices.length && <p className="text-xs text-slate-400">Add a device first under the Devices page.</p>}
        {error && <p className="text-riskcrit text-sm">{error}</p>}
      </form>

      <div className="mt-6 grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden self-start">
          <table className="w-full text-sm">
            <thead className="bg-navy text-white">
              <tr>
                <th className="text-left px-4 py-3 font-semibold">Device</th>
                <th className="text-left px-4 py-3 font-semibold">Risk</th>
                <th className="text-left px-4 py-3 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody>
              {requests.length === 0 && (
                <tr>
                  <td colSpan={3} className="text-center text-slate-400 py-8">
                    No change requests yet.
                  </td>
                </tr>
              )}
              {requests.map((r, i) => (
                <tr
                  key={r.id}
                  onClick={() => setSelected(r)}
                  className={`cursor-pointer hover:bg-blue-50 ${i % 2 ? "bg-slate-50" : "bg-white"} ${
                    selected?.id === r.id ? "ring-2 ring-inset ring-brandblue" : ""
                  }`}
                >
                  <td className="px-4 py-3 font-medium text-navy">{hostnameFor(r.device_id)}</td>
                  <td className="px-4 py-3">{r.risk_score != null && <RiskBadge score={r.risk_score} />}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-semibold capitalize ${statusStyle[r.status] || ""}`}>
                      {r.status.replace("_", " ")}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="bg-white border border-slate-200 rounded-xl p-5">
          {!selected ? (
            <p className="text-sm text-slate-400 italic">Select a change request to view details.</p>
          ) : (
            <div className="space-y-4">
              <div>
                <h3 className="font-semibold text-navy">{selected.description}</h3>
                <p className="text-xs text-slate-500 mt-1">Device: {hostnameFor(selected.device_id)}</p>
              </div>
              {selected.risk_score != null && (
                <div>
                  <p className="text-xs font-semibold text-slate-500 uppercase mb-1">AI Risk Analysis</p>
                  <RiskBadge score={selected.risk_score} />
                  {selected.risk_findings && (
                    <ul className="text-xs text-slate-600 mt-2 list-disc list-inside space-y-0.5">
                      {selected.risk_findings.split("; ").map((f, i) => (
                        <li key={i}>{f}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
              <div>
                <p className="text-xs font-semibold text-slate-500 uppercase mb-1">Configuration Diff</p>
                <ConfigDiff diffText={selected.config_diff} />
              </div>
              {selected.status === "pending_approval" && (
                <div className="flex gap-2 pt-2">
                  <button
                    onClick={() => act(selected.id, "approve")}
                    className="bg-risklow text-white rounded-lg px-4 py-2 text-sm font-semibold hover:opacity-90"
                  >
                    Approve
                  </button>
                  <button
                    onClick={() => act(selected.id, "reject")}
                    className="bg-riskcrit text-white rounded-lg px-4 py-2 text-sm font-semibold hover:opacity-90"
                  >
                    Reject
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
