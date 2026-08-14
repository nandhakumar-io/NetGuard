import { FormEvent, useEffect, useState } from "react";
import { api } from "../lib/api";
import { useToast } from "../lib/toast";
import { useConfirm } from "../lib/confirm";
import { EmptyState } from "../components/EmptyState";
import { PushSubscription } from "../lib/types";

/** Settings page for the current user's mobile push subscriptions (ntfy /
 *  Pushover) -- backs GET/POST/PATCH/DELETE /push-subscriptions and
 *  POST /push-subscriptions/{id}/test. Self-scoped: every user only ever
 *  sees and manages their own subscriptions, same as Security's session
 *  list, so there's no admin/other-user view here.
 *
 *  The backend validates `target` for the ntfy provider (must resolve to
 *  a public address -- see app.api.push_subscriptions._validate_target_if_url)
 *  so a rejected create/update surfaces here as a plain 422 error message,
 *  not a silent failure.
 */

type Provider = "ntfy" | "pushover";

interface FormState {
  label: string;
  provider: Provider;
  target: string;
  include_non_critical: boolean;
}

const EMPTY_FORM: FormState = {
  label: "My Phone",
  provider: "ntfy",
  target: "",
  include_non_critical: false,
};

function targetPlaceholder(provider: Provider): string {
  return provider === "ntfy" ? "https://ntfy.sh/your-private-topic" : "Pushover user key";
}

function targetHint(provider: Provider): string {
  return provider === "ntfy"
    ? "The full ntfy topic URL to publish to. Must be a public host -- internal/private addresses are rejected."
    : "Your Pushover user key, from your Pushover dashboard (not a URL).";
}

function formatRelative(iso: string | null): string {
  if (!iso) return "Never";
  const date = new Date(iso);
  const diffMs = Date.now() - date.getTime();
  const diffMin = Math.round(diffMs / 60000);
  if (diffMin < 1) return "Just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  return `${diffDay}d ago`;
}

function extractErrorMessage(err: unknown, fallback: string): string {
  const anyErr = err as { response?: { data?: { detail?: unknown } } };
  const detail = anyErr?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  return fallback;
}

export default function PushSettings() {
  const toast = useToast();
  const confirm = useConfirm();

  const [subscriptions, setSubscriptions] = useState<PushSubscription[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [testingId, setTestingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [togglingId, setTogglingId] = useState<string | null>(null);

  const load = async () => {
    try {
      const res = await api.get<PushSubscription[]>("/push-subscriptions");
      setSubscriptions(res.data);
      setLoadError(null);
    } catch (err) {
      setLoadError(extractErrorMessage(err, "Failed to load push subscriptions."));
    }
  };

  useEffect(() => {
    load();
  }, []);

  const openCreateForm = () => {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setFormError(null);
    setShowForm(true);
  };

  const openEditForm = (sub: PushSubscription) => {
    setEditingId(sub.id);
    setForm({
      label: sub.label,
      provider: sub.provider,
      target: sub.target,
      include_non_critical: sub.include_non_critical,
    });
    setFormError(null);
    setShowForm(true);
  };

  const closeForm = () => {
    setShowForm(false);
    setEditingId(null);
    setFormError(null);
  };

  const submitForm = async (e: FormEvent) => {
    e.preventDefault();
    setFormError(null);
    setSaving(true);
    try {
      if (editingId) {
        const res = await api.patch<PushSubscription>(`/push-subscriptions/${editingId}`, form);
        setSubscriptions((prev) => (prev ? prev.map((s) => (s.id === editingId ? res.data : s)) : prev));
        toast.success("Push subscription updated.");
      } else {
        const res = await api.post<PushSubscription>("/push-subscriptions", form);
        setSubscriptions((prev) => (prev ? [res.data, ...prev] : [res.data]));
        toast.success("Push subscription added.");
      }
      closeForm();
    } catch (err) {
      setFormError(extractErrorMessage(err, "Could not save this subscription."));
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async (sub: PushSubscription) => {
    setTestingId(sub.id);
    try {
      const res = await api.post<{ sent: boolean; message: string }>(`/push-subscriptions/${sub.id}/test`);
      if (res.data.sent) {
        toast.success(res.data.message);
      } else {
        toast.error(res.data.message);
      }
    } catch (err) {
      toast.error(extractErrorMessage(err, "Failed to send test push."));
    } finally {
      setTestingId(null);
    }
  };

  const handleToggleEnabled = async (sub: PushSubscription) => {
    setTogglingId(sub.id);
    try {
      const res = await api.patch<PushSubscription>(`/push-subscriptions/${sub.id}`, { enabled: !sub.enabled });
      setSubscriptions((prev) => (prev ? prev.map((s) => (s.id === sub.id ? res.data : s)) : prev));
    } catch (err) {
      toast.error(extractErrorMessage(err, "Failed to update subscription."));
    } finally {
      setTogglingId(null);
    }
  };

  const handleDelete = async (sub: PushSubscription) => {
    const ok = await confirm(`Remove "${sub.label}"? You'll stop receiving push alerts on this device.`, {
      confirmLabel: "Remove",
    });
    if (!ok) return;
    setDeletingId(sub.id);
    try {
      await api.delete(`/push-subscriptions/${sub.id}`);
      setSubscriptions((prev) => (prev ? prev.filter((s) => s.id !== sub.id) : prev));
      toast.success("Push subscription removed.");
    } catch (err) {
      toast.error(extractErrorMessage(err, "Failed to remove subscription."));
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-navy dark:text-white">Push Notifications</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Get critical alerts and escalations pushed straight to your phone via ntfy or Pushover.
          </p>
        </div>
        <button
          onClick={openCreateForm}
          className="shrink-0 inline-flex items-center gap-1.5 px-3 py-2 rounded-lg bg-brandblue text-white text-sm font-medium hover:bg-brandblue/90 transition-colors"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 5v14M5 12h14" />
          </svg>
          Add device
        </button>
      </div>

      <div className="bg-white dark:bg-noc-panel2 border border-slate-200 dark:border-noc-borderlit rounded-xl overflow-hidden">
        {loadError && (
          <div className="p-4 text-sm text-red-600 dark:text-red-400 border-b border-slate-200 dark:border-noc-borderlit">
            {loadError}
          </div>
        )}

        {subscriptions === null && !loadError && (
          <div className="p-8 text-center text-sm text-slate-400">Loading…</div>
        )}

        {subscriptions !== null && subscriptions.length === 0 && (
          <EmptyState
            title="No devices registered"
            message="Add a device to start receiving push notifications for critical incidents and escalations."
            action={
              <button
                onClick={openCreateForm}
                className="px-3 py-1.5 rounded-lg bg-brandblue text-white text-sm font-medium hover:bg-brandblue/90 transition-colors"
              >
                Add device
              </button>
            }
          />
        )}

        {subscriptions && subscriptions.length > 0 && (
          <ul className="divide-y divide-slate-100 dark:divide-noc-borderlit">
            {subscriptions.map((sub) => (
              <li key={sub.id} className="p-4 flex items-center gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-navy dark:text-white truncate">{sub.label}</span>
                    <span className="text-xs uppercase tracking-wide px-1.5 py-0.5 rounded bg-slate-100 dark:bg-white/5 text-slate-500 dark:text-slate-400">
                      {sub.provider}
                    </span>
                    {!sub.enabled && (
                      <span className="text-xs px-1.5 py-0.5 rounded bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300">
                        Paused
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-slate-500 dark:text-slate-400 mt-0.5 truncate">
                    {sub.provider === "ntfy" ? sub.target : "•••• (Pushover key hidden)"}
                  </div>
                  <div className="text-xs text-slate-400 dark:text-slate-500 mt-0.5">
                    {sub.include_non_critical ? "All alerts" : "Critical only"} · Last push: {formatRelative(sub.last_pushed_at)}
                  </div>
                </div>

                <div className="flex items-center gap-1.5 shrink-0">
                  <button
                    onClick={() => handleTest(sub)}
                    disabled={testingId === sub.id}
                    className="px-2.5 py-1.5 rounded-lg text-xs font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-white/5 disabled:opacity-50 transition-colors"
                  >
                    {testingId === sub.id ? "Sending…" : "Test"}
                  </button>
                  <button
                    onClick={() => handleToggleEnabled(sub)}
                    disabled={togglingId === sub.id}
                    className="px-2.5 py-1.5 rounded-lg text-xs font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-white/5 disabled:opacity-50 transition-colors"
                  >
                    {sub.enabled ? "Pause" : "Resume"}
                  </button>
                  <button
                    onClick={() => openEditForm(sub)}
                    className="px-2.5 py-1.5 rounded-lg text-xs font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-white/5 transition-colors"
                  >
                    Edit
                  </button>
                  <button
                    onClick={() => handleDelete(sub)}
                    disabled={deletingId === sub.id}
                    className="px-2.5 py-1.5 rounded-lg text-xs font-medium text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-950/40 disabled:opacity-50 transition-colors"
                  >
                    {deletingId === sub.id ? "Removing…" : "Remove"}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {showForm && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          onClick={closeForm}
        >
          <div
            className="bg-white dark:bg-noc-panel2 border border-slate-200 dark:border-noc-borderlit rounded-xl shadow-xl w-full max-w-md p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-base font-semibold text-navy dark:text-white mb-4">
              {editingId ? "Edit device" : "Add device"}
            </h2>

            <form onSubmit={submitForm} className="space-y-4">
              <div>
                <label className="block text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">Label</label>
                <input
                  type="text"
                  required
                  value={form.label}
                  onChange={(e) => setForm((f) => ({ ...f, label: e.target.value }))}
                  className="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-noc-borderlit bg-white dark:bg-noc-panel text-sm text-navy dark:text-white focus:outline-none focus:ring-2 focus:ring-brandblue/40"
                  placeholder="My Phone"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">Provider</label>
                <div className="flex gap-2">
                  {(["ntfy", "pushover"] as Provider[]).map((p) => (
                    <button
                      key={p}
                      type="button"
                      onClick={() => setForm((f) => ({ ...f, provider: p }))}
                      className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium border transition-colors ${
                        form.provider === p
                          ? "border-brandblue bg-brandblue/10 text-brandblue"
                          : "border-slate-200 dark:border-noc-borderlit text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-white/5"
                      }`}
                    >
                      {p === "ntfy" ? "ntfy" : "Pushover"}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">
                  {form.provider === "ntfy" ? "Topic URL" : "User key"}
                </label>
                <input
                  type="text"
                  required
                  value={form.target}
                  onChange={(e) => setForm((f) => ({ ...f, target: e.target.value }))}
                  className="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-noc-borderlit bg-white dark:bg-noc-panel text-sm text-navy dark:text-white focus:outline-none focus:ring-2 focus:ring-brandblue/40"
                  placeholder={targetPlaceholder(form.provider)}
                />
                <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">{targetHint(form.provider)}</p>
              </div>

              <label className="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
                <input
                  type="checkbox"
                  checked={form.include_non_critical}
                  onChange={(e) => setForm((f) => ({ ...f, include_non_critical: e.target.checked }))}
                  className="rounded border-slate-300 dark:border-noc-borderlit text-brandblue focus:ring-brandblue/40"
                />
                Also push non-critical (warning/info) alerts
              </label>

              {formError && <p className="text-sm text-red-600 dark:text-red-400">{formError}</p>}

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={closeForm}
                  className="px-3 py-2 rounded-lg text-sm font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-white/5 transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={saving}
                  className="px-3 py-2 rounded-lg text-sm font-medium bg-brandblue text-white hover:bg-brandblue/90 disabled:opacity-50 transition-colors"
                >
                  {saving ? "Saving…" : editingId ? "Save changes" : "Add device"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}