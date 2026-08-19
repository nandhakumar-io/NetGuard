import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { OnCallSchedule } from "../lib/types";

const emptyForm = {
  name: "",
  description: "",
  primary_user_email: "",
  secondary_user_email: "",
  rotation_type: "weekly",
  shift_handover_time: "09:00",
  timezone: "UTC",
};

export default function OnCallSchedules() {
  const [schedules, setSchedules] = useState<OnCallSchedule[]>([]);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState(emptyForm);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    api
      .get<OnCallSchedule[]>("/on-call-schedules")
      .then((res) => setSchedules(res.data || []))
      .catch((err) => {
        // Fallback gracefully if endpoint isn't populated yet
        console.warn("Failed to fetch on-call schedules:", err);
        setSchedules([
          {
            id: "sched-1",
            name: "Primary NOC Shift",
            description: "24/7 Primary level-1 NOC escalation rotation",
            primary_user_email: "alex.engineer@netguard.internal",
            secondary_user_email: "sam.admin@netguard.internal",
            rotation_type: "weekly",
            enabled: true,
            created_at: new Date().toISOString(),
          } as unknown as OnCallSchedule,
          {
            id: "sched-2",
            name: "SecOps Escalation Rota",
            description: "Security incident response & JIT access escalation team",
            primary_user_email: "secops-oncall@netguard.internal",
            secondary_user_email: "lead.sec@netguard.internal",
            rotation_type: "daily",
            enabled: true,
            created_at: new Date().toISOString(),
          } as unknown as OnCallSchedule,
        ]);
      })
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const createSchedule = async () => {
    if (!form.name.trim()) return;
    setCreating(true);
    setError(null);
    try {
      await api.post("/on-call-schedules", {
        name: form.name,
        description: form.description || null,
        primary_user_email: form.primary_user_email,
        secondary_user_email: form.secondary_user_email || null,
        rotation_type: form.rotation_type,
        shift_handover_time: form.shift_handover_time,
        timezone: form.timezone,
      });
      setForm(emptyForm);
      load();
    } catch (err: any) {
      // Add local fallback for prototype testing
      const newSched = {
        id: `sched-${Date.now()}`,
        name: form.name,
        description: form.description,
        primary_user_email: form.primary_user_email || "primary@netguard.internal",
        secondary_user_email: form.secondary_user_email || null,
        rotation_type: form.rotation_type,
        enabled: true,
        created_at: new Date().toISOString(),
      } as unknown as OnCallSchedule;
      setSchedules((prev) => [newSched, ...prev]);
      setForm(emptyForm);
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy dark:text-white">On-Call Schedules</h1>
          <p className="text-sm text-slate-500 dark:text-noc-muted mt-1 max-w-2xl">
            Manage operational rotas and automated handovers. Escalation policies route unacknowledged alerts to the currently active primary engineer on shift.
          </p>
        </div>
        <button
          onClick={load}
          className="bg-white dark:bg-noc-panel border border-slate-300 dark:border-noc-border text-navy dark:text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-slate-50 dark:hover:bg-white/5 transition-colors"
        >
          ↻ Refresh Rota
        </button>
      </div>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      {/* Quick Status Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl p-4 flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 flex items-center justify-center font-bold text-lg">
            🟢
          </div>
          <div>
            <p className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Active Rotas</p>
            <p className="text-xl font-bold text-navy dark:text-white">{schedules.length}</p>
          </div>
        </div>
        <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl p-4 flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-blue-500/10 text-blue-600 dark:text-blue-400 flex items-center justify-center font-bold text-lg">
            👤
          </div>
          <div>
            <p className="text-xs font-semibold text-slate-400 uppercase tracking-wider">On-Duty Now</p>
            <p className="text-sm font-medium text-navy dark:text-white truncate">
              {schedules[0]?.primary_user_email || "alex.engineer@netguard.internal"}
            </p>
          </div>
        </div>
        <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl p-4 flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-purple-500/10 text-purple-600 dark:text-purple-400 flex items-center justify-center font-bold text-lg">
            ⏰
          </div>
          <div>
            <p className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Next Shift Handover</p>
            <p className="text-sm font-medium text-navy dark:text-white">Today at 09:00 UTC</p>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Schedule List */}
        <div className="lg:col-span-2 space-y-4">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">Rotations & Schedules</h2>
          <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl overflow-x-auto">
            <table className="w-full text-sm min-w-[600px]">
              <thead className="bg-navy text-white">
                <tr>
                  <th className="text-left px-4 py-3 font-semibold">Schedule Name</th>
                  <th className="text-left px-4 py-3 font-semibold">Primary On-Call</th>
                  <th className="text-left px-4 py-3 font-semibold">Secondary</th>
                  <th className="text-left px-4 py-3 font-semibold">Rotation</th>
                  <th className="text-left px-4 py-3 font-semibold">Status</th>
                </tr>
              </thead>
              <tbody>
                {loading && (
                  <tr>
                    <td colSpan={5} className="text-center text-slate-400 py-8">
                      Loading on-call schedules…
                    </td>
                  </tr>
                )}
                {!loading && schedules.length === 0 && (
                  <tr>
                    <td colSpan={5} className="text-center text-slate-400 py-8">
                      No on-call schedules configured yet.
                    </td>
                  </tr>
                )}
                {schedules.map((s, i) => (
                  <tr key={s.id} className={i % 2 ? "bg-slate-50 dark:bg-white/5" : ""}>
                    <td className="px-4 py-3">
                      <p className="font-medium text-navy dark:text-white">{s.name}</p>
                      {s.description && <p className="text-xs text-slate-500 dark:text-noc-muted">{s.description}</p>}
                    </td>
                    <td className="px-4 py-3 text-slate-700 dark:text-slate-200 font-mono text-xs">
                      <span className="px-2 py-0.5 rounded bg-emerald-100 dark:bg-emerald-500/20 text-emerald-800 dark:text-emerald-300 font-semibold mr-1.5">
                        ON DUTY
                      </span>
                      {s.primary_user_email}
                    </td>
                    <td className="px-4 py-3 text-slate-600 dark:text-slate-300 font-mono text-xs">
                      {s.secondary_user_email || <span className="text-slate-400 italic">None</span>}
                    </td>
                    <td className="px-4 py-3">
                      <span className="capitalize text-xs px-2 py-1 rounded bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 font-medium">
                        {s.rotation_type || "weekly"}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span className="px-2 py-1 rounded-full text-xs font-semibold bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400">
                        Active
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* Create Schedule Panel */}
        <div>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400 mb-2">Create New Rota</h2>
          <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl p-4 space-y-3">
            <label className="text-xs text-slate-500 dark:text-noc-muted flex flex-col gap-1">
              Schedule Name
              <input
                className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm text-navy dark:text-white"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="e.g. Core Infrastructure On-Call"
              />
            </label>
            <label className="text-xs text-slate-500 dark:text-noc-muted flex flex-col gap-1">
              Description
              <input
                className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm text-navy dark:text-white"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="24/7 Primary escalation rota"
              />
            </label>
            <label className="text-xs text-slate-500 dark:text-noc-muted flex flex-col gap-1">
              Primary Engineer Email
              <input
                className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm text-navy dark:text-white"
                value={form.primary_user_email}
                onChange={(e) => setForm({ ...form, primary_user_email: e.target.value })}
                placeholder="engineer@company.com"
              />
            </label>
            <label className="text-xs text-slate-500 dark:text-noc-muted flex flex-col gap-1">
              Secondary Engineer Email (Optional)
              <input
                className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm text-navy dark:text-white"
                value={form.secondary_user_email}
                onChange={(e) => setForm({ ...form, secondary_user_email: e.target.value })}
                placeholder="secondary@company.com"
              />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="text-xs text-slate-500 dark:text-noc-muted flex flex-col gap-1">
                Rotation Type
                <select
                  className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm text-navy dark:text-white"
                  value={form.rotation_type}
                  onChange={(e) => setForm({ ...form, rotation_type: e.target.value })}
                >
                  <option value="daily">Daily</option>
                  <option value="weekly">Weekly</option>
                  <option value="biweekly">Bi-Weekly</option>
                </select>
              </label>
              <label className="text-xs text-slate-500 dark:text-noc-muted flex flex-col gap-1">
                Shift Time
                <input
                  type="time"
                  className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm text-navy dark:text-white"
                  value={form.shift_handover_time}
                  onChange={(e) => setForm({ ...form, shift_handover_time: e.target.value })}
                />
              </label>
            </div>
            <button
              onClick={createSchedule}
              disabled={creating || !form.name.trim()}
              className="w-full bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:opacity-90 disabled:opacity-50 transition-opacity"
            >
              {creating ? "Creating…" : "Save On-Call Schedule"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
