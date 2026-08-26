import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { TenantBoardRow } from "../lib/types";
import { useToast, errorMessage } from "../lib/toast";

// Single-pane cross-tenant view for MSP staff watching many tenants at
// once (see backend app.api.tenant_board). Where every other page in
// the app is scoped to "your" fleet, this is the one page that's
// explicitly the opposite: every tenant, side by side, sorted worst
// health first, so a shared NOC desk can see at a glance which
// customer needs attention right now without switching context per
// tenant. Gated server-side on User.is_msp_staff; the nav item itself
// is hidden for non-MSP-staff accounts (see components/Layout.tsx).
const POLL_MS = 30_000;

function healthColor(score: number): string {
  if (score >= 90) return "bg-emerald-500";
  if (score >= 70) return "bg-amber-500";
  if (score >= 40) return "bg-orange-500";
  return "bg-red-500";
}

function healthTextColor(score: number): string {
  if (score >= 90) return "text-emerald-600";
  if (score >= 70) return "text-amber-600";
  if (score >= 40) return "text-orange-600";
  return "text-red-600";
}

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function TenantBoard() {
  const toast = useToast();
  const [rows, setRows] = useState<TenantBoardRow[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const res = await api.get<{ tenants: TenantBoardRow[] }>("/tenant-board/summary");
        if (!cancelled) {
          setRows(res.data.tenants);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          const msg = errorMessage(err);
          setError(msg);
          toast.error(msg);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const totalCritical = (rows ?? []).reduce((sum, r) => sum + r.open_alerts_critical, 0);
  const totalIncidents = (rows ?? []).reduce((sum, r) => sum + r.open_incidents, 0);
  const tenantsAtRisk = (rows ?? []).filter((r) => r.health_score < 70).length;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">Tenant Board</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            Cross-tenant status for every managed customer, worst health first.
          </p>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-900/40 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </div>
      )}

      {!error && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
            <div className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Tenants at risk
            </div>
            <div className="mt-1 text-2xl font-semibold text-slate-900 dark:text-slate-50">{tenantsAtRisk}</div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
            <div className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Open critical alerts
            </div>
            <div className="mt-1 text-2xl font-semibold text-red-600">{totalCritical}</div>
          </div>
          <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
            <div className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Open incidents
            </div>
            <div className="mt-1 text-2xl font-semibold text-slate-900 dark:text-slate-50">{totalIncidents}</div>
          </div>
        </div>
      )}

      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <table className="w-full text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500 dark:border-slate-800 dark:bg-slate-950/40 dark:text-slate-400">
            <tr>
              <th className="px-4 py-3">Tenant</th>
              <th className="px-4 py-3">Health</th>
              <th className="px-4 py-3">Devices</th>
              <th className="px-4 py-3">Critical</th>
              <th className="px-4 py-3">Warning</th>
              <th className="px-4 py-3">Incidents</th>
              <th className="px-4 py-3">Latest critical alert</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {loading && (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-slate-400">
                  Loading tenant status…
                </td>
              </tr>
            )}
            {!loading && rows && rows.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-slate-400">
                  No tenants found.
                </td>
              </tr>
            )}
            {!loading &&
              rows?.map((row) => (
                <tr key={row.tenant_id} className="hover:bg-slate-50 dark:hover:bg-slate-800/40">
                  <td className="px-4 py-3">
                    <div className="font-medium text-slate-900 dark:text-slate-50">{row.tenant_name}</div>
                    <div className="text-xs text-slate-400">{row.tenant_slug}</div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span className={`h-2.5 w-2.5 rounded-full ${healthColor(row.health_score)}`} />
                      <span className={`font-semibold ${healthTextColor(row.health_score)}`}>{row.health_score}</span>
                    </div>
                  </td>
                  <td className="px-4 py-3 text-slate-600 dark:text-slate-300">
                    {row.device_count}
                    {row.devices_offline > 0 && (
                      <span className="ml-2 text-xs text-red-600">{row.devices_offline} offline</span>
                    )}
                    {row.devices_degraded > 0 && (
                      <span className="ml-2 text-xs text-amber-600">{row.devices_degraded} degraded</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {row.open_alerts_critical > 0 ? (
                      <span className="inline-flex items-center rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700 dark:bg-red-950/60 dark:text-red-300">
                        {row.open_alerts_critical}
                      </span>
                    ) : (
                      <span className="text-slate-400">0</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {row.open_alerts_warning > 0 ? (
                      <span className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700 dark:bg-amber-950/60 dark:text-amber-300">
                        {row.open_alerts_warning}
                      </span>
                    ) : (
                      <span className="text-slate-400">0</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-slate-600 dark:text-slate-300">{row.open_incidents}</td>
                  <td className="max-w-xs px-4 py-3 text-slate-600 dark:text-slate-300">
                    {row.latest_critical_alert_message ? (
                      <>
                        <div className="truncate" title={row.latest_critical_alert_message}>
                          {row.latest_critical_alert_message}
                        </div>
                        <div className="text-xs text-slate-400">{relativeTime(row.latest_critical_alert_at)}</div>
                      </>
                    ) : (
                      <span className="text-slate-400">—</span>
                    )}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}