import { useEffect, useState, useCallback } from "react";
import { api } from "../lib/api";
import { useAuth, UserRole } from "../lib/auth";

interface JitElevation {
  id: string;
  user_id: string;
  user_email: string | null;
  elevated_role: string;
  reason: string;
  change_request_id: string | null;
  requested_by: string;
  requested_at: string | null;
  requested_duration_minutes: number;
  status: "pending" | "active" | "expired" | "revoked" | "rejected";
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  activated_at: string | null;
  expires_at: string | null;
  is_active_now: boolean;
  seconds_remaining: number | null;
  is_stale: boolean;
  time_to_approve_seconds: number | null;
  requires_dual_approval: boolean;
  dual_approval_reason: string | null;
  first_approved_by: string | null;
  first_approved_at: string | null;
  is_first_approval_needed: boolean;
}

interface JitApprovalMetrics {
  window_days: number;
  decided_count: number;
  rejected_count: number;
  mean_seconds: number | null;
  median_seconds: number | null;
  p90_seconds: number | null;
  stale_active_count: number;
}

function fmtDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = seconds / 60;
  if (m < 60) return `${m.toFixed(1)}m`;
  return `${(m / 60).toFixed(1)}h`;
}

const ROLE_OPTIONS: UserRole[] = ["network_admin", "network_engineer", "noc_engineer", "security", "auditor"];

const STATUS_BADGE: Record<string, string> = {
  pending: "bg-amber-100 text-amber-700",
  active: "bg-emerald-100 text-emerald-700",
  expired: "bg-slate-100 text-slate-500",
  revoked: "bg-red-100 text-red-700",
  rejected: "bg-red-100 text-red-700",
};

function fmtRemaining(seconds: number | null): string {
  if (seconds === null) return "";
  if (seconds <= 0) return "expiring…";
  const m = Math.floor(seconds / 60);
  const h = Math.floor(m / 60);
  if (h > 0) return `${h}h ${m % 60}m left`;
  return `${m}m left`;
}

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

export default function JitAccess() {
  const { user } = useAuth();
  const isAdmin = user?.role === "network_admin";

  const [mine, setMine] = useState<JitElevation[]>([]);
  const [pending, setPending] = useState<JitElevation[]>([]);
  const [all, setAll] = useState<JitElevation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [actioningId, setActioningId] = useState<string | null>(null);

  const [role, setRole] = useState<UserRole>("network_admin");
  const [reason, setReason] = useState("");
  const [duration, setDuration] = useState(60);
  const [changeRequestId, setChangeRequestId] = useState("");

  const [metrics, setMetrics] = useState<JitApprovalMetrics | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    const calls = [api.get<JitElevation[]>("/jit-access/mine")];
    if (isAdmin) {
      calls.push(api.get<JitElevation[]>("/jit-access/pending"));
      calls.push(api.get<JitElevation[]>("/jit-access"));
    }
    Promise.all(calls)
      .then((results) => {
        setMine(results[0].data);
        if (isAdmin) {
          setPending((results[1]?.data as JitElevation[]) || []);
          setAll((results[2]?.data as JitElevation[]) || []);
        }
      })
      .catch((err) => setError(err?.response?.data?.detail || "Failed to load JIT access data."))
      .finally(() => setLoading(false));
    if (isAdmin) {
      api
        .get<JitApprovalMetrics>("/jit-access/metrics", { params: { days: 30 } })
        .then((res) => setMetrics(res.data))
        .catch(() => setMetrics(null));
    }
  }, [isAdmin]);

  useEffect(() => {
    load();
    // Refresh periodically so countdowns and admin queues stay current
    // without a manual reload -- same lightweight-polling pattern as the
    // rest of the app's non-websocket pages.
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const submitRequest = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await api.post("/jit-access/request", {
        elevated_role: role,
        reason,
        duration_minutes: duration,
        change_request_id: changeRequestId.trim() || null,
      });
      setReason("");
      setChangeRequestId("");
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to submit request.");
    } finally {
      setSubmitting(false);
    }
  };

  const decide = async (id: string, action: "approve" | "reject" | "revoke") => {
    setActioningId(id);
    setError(null);
    try {
      await api.post(`/jit-access/${id}/${action}`, {});
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || `Failed to ${action}.`);
    } finally {
      setActioningId(null);
    }
  };

  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-xl font-bold text-navy dark:text-white">Just-In-Time Access</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          Request a temporary, time-bound role elevation instead of holding it permanently. Grants expire
          automatically -- no standing admin access left lying around.
        </p>
      </div>

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 text-red-700 text-sm px-3 py-2">{error}</div>
      )}

      {/* --- Request form --- */}
      <form
        onSubmit={submitRequest}
        className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4 space-y-3"
      >
        <h2 className="text-sm font-bold text-navy dark:text-white">Request elevation</h2>
        <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
          <label className="text-xs text-slate-500 flex flex-col gap-1">
            Elevated role
            <select
              value={role}
              onChange={(e) => setRole(e.target.value as UserRole)}
              className="border border-slate-200 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-2 py-1.5 text-sm"
            >
              {ROLE_OPTIONS.map((r) => (
                <option key={r} value={r}>
                  {r.replace(/_/g, " ")}
                </option>
              ))}
            </select>
          </label>
          <label className="text-xs text-slate-500 flex flex-col gap-1">
            Duration (minutes)
            <input
              type="number"
              min={5}
              max={480}
              value={duration}
              onChange={(e) => setDuration(Number(e.target.value))}
              className="border border-slate-200 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-2 py-1.5 text-sm"
            />
          </label>
          <label className="text-xs text-slate-500 flex flex-col gap-1 sm:col-span-2">
            Change request ID (optional)
            <input
              type="text"
              value={changeRequestId}
              onChange={(e) => setChangeRequestId(e.target.value)}
              placeholder="e.g. the CR you need to push"
              className="border border-slate-200 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-2 py-1.5 text-sm"
            />
          </label>
        </div>
        <label className="text-xs text-slate-500 flex flex-col gap-1">
          Reason
          <textarea
            required
            minLength={3}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={2}
            className="border border-slate-200 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-2 py-1.5 text-sm"
            placeholder="Why do you need this, and for what?"
          />
        </label>
        <button
          type="submit"
          disabled={submitting}
          className="bg-brandblue text-white text-xs font-bold rounded-lg px-3 py-2 disabled:opacity-50"
        >
          {submitting ? "Submitting…" : "Request access"}
        </button>
      </form>

      {/* --- Approval time-to-approve + stale-access metrics --- */}
      {isAdmin && metrics && (
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4">
          <h2 className="text-sm font-bold text-navy dark:text-white mb-3">
            JIT metrics <span className="text-slate-400 font-normal">(last {metrics.window_days}d)</span>
          </h2>
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 text-center">
            <div>
              <p className="text-lg font-bold text-navy dark:text-white">{fmtDuration(metrics.median_seconds)}</p>
              <p className="text-[11px] text-slate-400 uppercase tracking-wide">Median time to approve</p>
            </div>
            <div>
              <p className="text-lg font-bold text-navy dark:text-white">{fmtDuration(metrics.mean_seconds)}</p>
              <p className="text-[11px] text-slate-400 uppercase tracking-wide">Mean</p>
            </div>
            <div>
              <p className="text-lg font-bold text-navy dark:text-white">{fmtDuration(metrics.p90_seconds)}</p>
              <p className="text-[11px] text-slate-400 uppercase tracking-wide">p90</p>
            </div>
            <div>
              <p className="text-lg font-bold text-navy dark:text-white">{metrics.decided_count}</p>
              <p className="text-[11px] text-slate-400 uppercase tracking-wide">Approved ({metrics.rejected_count} rejected)</p>
            </div>
            <div>
              <p className={`text-lg font-bold ${metrics.stale_active_count > 0 ? "text-red-600" : "text-navy dark:text-white"}`}>
                {metrics.stale_active_count}
              </p>
              <p className="text-[11px] text-slate-400 uppercase tracking-wide">Stale active grants</p>
            </div>
          </div>
        </div>
      )}

      {/* --- Admin approval queue --- */}
      {isAdmin && (
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4">
          <h2 className="text-sm font-bold text-navy dark:text-white mb-3">
            Pending approvals {pending.length > 0 && <span className="text-amber-600">({pending.length})</span>}
          </h2>
          {pending.length === 0 ? (
            <p className="text-xs text-slate-400">Nothing waiting on a decision.</p>
          ) : (
            <div className="space-y-2">
              {pending.map((el) => (
                <div
                  key={el.id}
                  className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border border-slate-100 dark:border-slate-700 rounded-lg px-3 py-2"
                >
                  <div className="text-xs">
                    <div className="font-bold text-navy dark:text-white">
                      {el.user_email} → {el.elevated_role.replace(/_/g, " ")} for {el.requested_duration_minutes}m
                    </div>
                    <div className="text-slate-500">{el.reason}</div>
                    {el.change_request_id && (
                      <div className="text-slate-400">CR: {el.change_request_id}</div>
                    )}
                    <div className="text-slate-400">Requested {fmtDate(el.requested_at)}</div>
                    {el.requires_dual_approval && (
                      <div className="mt-1 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-100 text-red-700 font-bold">
                        {el.first_approved_by
                          ? `1 of 2 approvals in — ${el.dual_approval_reason}`
                          : `2 approvals required — ${el.dual_approval_reason}`}
                      </div>
                    )}
                  </div>
                  <div className="flex gap-2 shrink-0">
                    <button
                      onClick={() => decide(el.id, "approve")}
                      disabled={actioningId === el.id}
                      className="bg-emerald-600 text-white text-xs font-bold rounded-lg px-3 py-1.5 disabled:opacity-50"
                    >
                      {el.requires_dual_approval && !el.first_approved_by ? "Approve (1 of 2)" : "Approve"}
                    </button>
                    <button
                      onClick={() => decide(el.id, "reject")}
                      disabled={actioningId === el.id}
                      className="bg-red-600 text-white text-xs font-bold rounded-lg px-3 py-1.5 disabled:opacity-50"
                    >
                      Reject
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* --- My elevations --- */}
      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4">
        <h2 className="text-sm font-bold text-navy dark:text-white mb-3">My requests</h2>
        {loading ? (
          <p className="text-xs text-slate-400">Loading…</p>
        ) : mine.length === 0 ? (
          <p className="text-xs text-slate-400">No elevation requests yet.</p>
        ) : (
          <div className="overflow-x-auto">
          <table className="w-full text-xs min-w-[480px]">
            <thead>
              <tr className="text-left text-slate-400 border-b border-slate-100 dark:border-slate-700">
                <th className="py-1.5 pr-2">Role</th>
                <th className="py-1.5 pr-2">Status</th>
                <th className="py-1.5 pr-2">Window</th>
                <th className="py-1.5 pr-2">Reason</th>
                <th className="py-1.5 pr-2"></th>
              </tr>
            </thead>
            <tbody>
              {mine.map((el) => (
                <tr key={el.id} className="border-b border-slate-50 dark:border-slate-700/50">
                  <td className="py-1.5 pr-2 font-bold text-navy dark:text-white capitalize">
                    {el.elevated_role.replace(/_/g, " ")}
                  </td>
                  <td className="py-1.5 pr-2">
                    <span className={`px-2 py-0.5 rounded-full font-bold ${STATUS_BADGE[el.status]}`}>
                      {el.status}
                    </span>
                  </td>
                  <td className="py-1.5 pr-2 text-slate-500">
                    {el.is_active_now
                      ? fmtRemaining(el.seconds_remaining)
                      : el.expires_at
                      ? `expired ${fmtDate(el.expires_at)}`
                      : "—"}
                  </td>
                  <td className="py-1.5 pr-2 text-slate-500 max-w-xs truncate">{el.reason}</td>
                  <td className="py-1.5 pr-2">
                    {el.is_active_now && (
                      <button
                        onClick={() => decide(el.id, "revoke")}
                        disabled={actioningId === el.id}
                        className="text-red-600 font-bold hover:underline disabled:opacity-50"
                      >
                        End now
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>

      {/* --- Full history (admin only) --- */}
      {isAdmin && (
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4">
          <h2 className="text-sm font-bold text-navy dark:text-white mb-3">All elevations (org-wide)</h2>
          {all.length === 0 ? (
            <p className="text-xs text-slate-400">No elevations on record.</p>
          ) : (
            <div className="overflow-x-auto">
            <table className="w-full text-xs min-w-[560px]">
              <thead>
                <tr className="text-left text-slate-400 border-b border-slate-100 dark:border-slate-700">
                  <th className="py-1.5 pr-2">User</th>
                  <th className="py-1.5 pr-2">Role</th>
                  <th className="py-1.5 pr-2">Status</th>
                  <th className="py-1.5 pr-2">Requested</th>
                  <th className="py-1.5 pr-2">Decided by</th>
                  <th className="py-1.5 pr-2"></th>
                </tr>
              </thead>
              <tbody>
                {all.map((el) => (
                  <tr key={el.id} className="border-b border-slate-50 dark:border-slate-700/50">
                    <td className="py-1.5 pr-2 text-slate-700 dark:text-slate-200">{el.user_email}</td>
                    <td className="py-1.5 pr-2 font-bold text-navy dark:text-white capitalize">
                      {el.elevated_role.replace(/_/g, " ")}
                    </td>
                    <td className="py-1.5 pr-2">
                      <span className={`px-2 py-0.5 rounded-full font-bold ${STATUS_BADGE[el.status]}`}>
                        {el.status}
                      </span>
                      {el.is_stale && (
                        <span
                          className="ml-1 px-2 py-0.5 rounded-full font-bold bg-red-100 text-red-700"
                          title="Still marked active in the DB past its expected window -- the expiry sweep hasn't caught it yet."
                        >
                          stale
                        </span>
                      )}
                    </td>
                    <td className="py-1.5 pr-2 text-slate-500">{fmtDate(el.requested_at)}</td>
                    <td className="py-1.5 pr-2 text-slate-500">{el.decided_by ? fmtDate(el.decided_at) : "—"}</td>
                    <td className="py-1.5 pr-2">
                      {el.is_active_now && (
                        <button
                          onClick={() => decide(el.id, "revoke")}
                          disabled={actioningId === el.id}
                          className="text-red-600 font-bold hover:underline disabled:opacity-50"
                        >
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}