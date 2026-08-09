import { useEffect, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../lib/api";

interface SuccessRateResponse {
  window_days: number;
  total_finished: number;
  total_succeeded: number;
  total_failed: number;
  total_rolled_back: number;
  overall_success_rate: number;
  series: { date: string; total: number; succeeded: number; failed: number; rolled_back: number; success_rate: number }[];
}

interface OnCallLoadResponse {
  window_days: number;
  total_escalations: number;
  total_escalated_alerts: number;
  by_policy: { policy_id: string; policy_name: string; channel: string; escalations: number; alerts: number }[];
  by_contact: { contact: string; escalations: number; alerts: number }[];
  daily: { date: string; escalations: number; alerts: number }[];
}

function StatCard({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl p-4">
      <p className="text-xs text-slate-400 uppercase tracking-wide font-semibold">{label}</p>
      <p className={`text-2xl font-bold mt-1 ${tone || "text-navy dark:text-white"}`}>{value}</p>
    </div>
  );
}

export default function Insights() {
  const [days, setDays] = useState(30);
  const [success, setSuccess] = useState<SuccessRateResponse | null>(null);
  const [onCall, setOnCall] = useState<OnCallLoadResponse | null>(null);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    Promise.all([
      api.get<SuccessRateResponse>("/reports/deployment-success-rate", { params: { days, group_by: "day" } }),
      api.get<OnCallLoadResponse>("/escalation-policies/on-call-load", { params: { days } }),
    ])
      .then(([s, o]) => {
        setSuccess(s.data);
        setOnCall(o.data);
      })
      .finally(() => setLoading(false));
  };

  useEffect(load, [days]);

  return (
    <div>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy dark:text-white">Insights</h1>
          <p className="text-sm text-slate-500 mt-1">
            Change success rate and on-call escalation load, trailing {days} days.
          </p>
        </div>
        <select
          className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-2 text-sm"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
        >
          <option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option>
          <option value={90}>Last 90 days</option>
        </select>
      </div>

      {/* Change success rate */}
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400 mt-6 mb-3">Change Success Rate</h2>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
        <StatCard
          label="Overall success rate"
          value={success ? `${success.overall_success_rate}%` : "—"}
          tone={success && success.overall_success_rate < 90 ? "text-red-600" : "text-green-600"}
        />
        <StatCard label="Finished deployments" value={success?.total_finished ?? "—"} />
        <StatCard label="Failed" value={success?.total_failed ?? "—"} tone="text-red-600" />
        <StatCard label="Rolled back" value={success?.total_rolled_back ?? "—"} tone="text-amber-600" />
      </div>
      <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl p-4 mb-8">
        {!loading && (success?.series.length ?? 0) === 0 ? (
          <p className="text-sm text-slate-400 text-center py-10">No finished deployments in this window.</p>
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={success?.series}>
              <defs>
                <linearGradient id="successGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#16a34a" stopOpacity={0.35} />
                  <stop offset="100%" stopColor="#16a34a" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} />
              <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} unit="%" />
              <Tooltip formatter={(v: number, name) => [name === "success_rate" ? `${v}%` : v, name]} />
              <Area type="monotone" dataKey="success_rate" stroke="#16a34a" fill="url(#successGradient)" strokeWidth={2} />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* On-call load */}
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400 mb-3">On-Call Load</h2>
      <div className="grid grid-cols-2 gap-4 mb-4">
        <StatCard label="Total escalations" value={onCall?.total_escalations ?? "—"} />
        <StatCard label="Escalated alerts" value={onCall?.total_escalated_alerts ?? "—"} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl p-4">
          <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-2">Escalations per day</p>
          {!loading && (onCall?.daily.length ?? 0) === 0 ? (
            <p className="text-sm text-slate-400 text-center py-10">No escalations in this window.</p>
          ) : (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={onCall?.daily}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                <Tooltip />
                <Bar dataKey="escalations" fill="#2563eb" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>

        <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl overflow-hidden">
          <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide px-4 pt-4">By contact / policy</p>
          <table className="w-full text-sm mt-2">
            <thead>
              <tr className="text-left text-xs text-slate-400 border-b border-slate-200 dark:border-noc-border">
                <th className="px-4 py-2 font-semibold">Contact</th>
                <th className="px-4 py-2 font-semibold text-right">Escalations</th>
                <th className="px-4 py-2 font-semibold text-right">Alerts</th>
              </tr>
            </thead>
            <tbody>
              {(onCall?.by_contact ?? []).slice(0, 8).map((c, i) => (
                <tr key={c.contact} className={i % 2 ? "bg-slate-50 dark:bg-white/5" : ""}>
                  <td className="px-4 py-2 text-navy dark:text-white">{c.contact}</td>
                  <td className="px-4 py-2 text-right font-semibold">{c.escalations}</td>
                  <td className="px-4 py-2 text-right text-slate-500">{c.alerts}</td>
                </tr>
              ))}
              {(onCall?.by_contact.length ?? 0) === 0 && (
                <tr>
                  <td colSpan={3} className="text-center text-slate-400 py-8">No escalations in this window.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}