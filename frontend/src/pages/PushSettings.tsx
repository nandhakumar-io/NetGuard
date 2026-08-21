/**
 * PushSettings — per-user push notification subscription manager.
 *
 * Supports three providers wired by the backend:
 *   1. ntfy   — user pastes their topic URL (e.g. https://ntfy.sh/my-topic)
 *   2. Pushover — user pastes their Pushover user key
 *   3. Browser — registers the current browser via the Web Push API (VAPID)
 *
 * All subscriptions are self-scoped (GET /push-subscriptions returns only the
 * current user's) so no admin role is needed.
 */
import { useEffect, useRef, useState } from "react";
import api from "../api/axios";

// ─── Types ──────────────────────────────────────────────────────────────────

interface PushSub {
  id: string;
  label: string;
  provider: "ntfy" | "pushover" | "browser";
  target: string;
  include_non_critical: boolean;
  include_actions: string[] | null;
  enabled: boolean;
  created_at: string | null;
  last_pushed_at: string | null;
}

interface VapidKey {
  configured: boolean;
  public_key: string | null;
}

const PROVIDER_META = {
  ntfy: {
    label: "ntfy",
    icon: "📨",
    color: "from-cyan-500 to-teal-600",
    bg: "bg-cyan-50 dark:bg-cyan-950/30",
    border: "border-cyan-200 dark:border-cyan-800",
    text: "text-cyan-700 dark:text-cyan-300",
    placeholder: "https://ntfy.sh/your-topic",
    hint: "Paste the full topic URL from the ntfy app.",
  },
  pushover: {
    label: "Pushover",
    icon: "🔔",
    color: "from-orange-500 to-rose-600",
    bg: "bg-orange-50 dark:bg-orange-950/30",
    border: "border-orange-200 dark:border-orange-800",
    text: "text-orange-700 dark:text-orange-300",
    placeholder: "Your Pushover user key (30 characters)",
    hint: "Find your user key on pushover.net under Your Profile.",
  },
  browser: {
    label: "Browser",
    icon: "🌐",
    color: "from-violet-500 to-indigo-600",
    bg: "bg-violet-50 dark:bg-violet-950/30",
    border: "border-violet-200 dark:border-violet-800",
    text: "text-violet-700 dark:text-violet-300",
    placeholder: "",
    hint: "Subscribes this browser session via the Web Push API. Requires your browser to grant notification permission.",
  },
};

const ALL_ACTIONS = [
  { key: "acknowledge", label: "Acknowledge" },
  { key: "escalate", label: "Escalate" },
  { key: "run_runbook", label: "Run Runbook" },
];

function fmtDate(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

// ─── Add / Edit modal ────────────────────────────────────────────────────────

function SubscriptionModal({
  editSub,
  vapid,
  onClose,
  onSaved,
}: {
  editSub: PushSub | null;
  vapid: VapidKey;
  onClose: () => void;
  onSaved: (sub: PushSub) => void;
}) {
  const isEditing = !!editSub;
  const [provider, setProvider] = useState<"ntfy" | "pushover" | "browser">(
    editSub?.provider || "ntfy"
  );
  const [label, setLabel] = useState(editSub?.label || "My Phone");
  const [target, setTarget] = useState(
    editSub && editSub.provider !== "browser" ? editSub.target : ""
  );
  const [includeNonCritical, setIncludeNonCritical] = useState(
    editSub?.include_non_critical || false
  );
  const [includeActions, setIncludeActions] = useState<string[]>(
    editSub?.include_actions || []
  );
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const toggleAction = (key: string) => {
    setIncludeActions((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]
    );
  };

  const subscribeBrowser = async (): Promise<{ endpoint: string; p256dh: string; auth: string } | null> => {
    if (!vapid.public_key) return null;
    try {
      const reg = await navigator.serviceWorker.ready;
      const existing = await reg.pushManager.getSubscription();
      if (existing) await existing.unsubscribe();
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: vapid.public_key,
      });
      const json = sub.toJSON() as { endpoint: string; keys: { p256dh: string; auth: string } };
      return { endpoint: json.endpoint, p256dh: json.keys.p256dh, auth: json.keys.auth };
    } catch {
      return null;
    }
  };

  const handleSave = async () => {
    setError(null);
    setSaving(true);
    try {
      let payload: Record<string, unknown>;
      if (isEditing) {
        payload = {
          label,
          include_non_critical: includeNonCritical,
          include_actions: includeActions.length ? includeActions : null,
        };
        if (provider !== "browser") payload.target = target;
        const res = await api.patch<PushSub>(`/push-subscriptions/${editSub!.id}`, payload);
        onSaved(res.data);
      } else {
        if (provider === "browser") {
          const perm = await Notification.requestPermission();
          if (perm !== "granted") { setError("Notification permission denied."); setSaving(false); return; }
          const keys = await subscribeBrowser();
          if (!keys) { setError("Browser subscription failed. Ensure service worker is registered."); setSaving(false); return; }
          payload = { label, provider, include_non_critical: includeNonCritical, include_actions: includeActions.length ? includeActions : null, ...keys };
        } else {
          payload = { label, provider, target, include_non_critical: includeNonCritical, include_actions: includeActions.length ? includeActions : null };
        }
        const res = await api.post<PushSub>("/push-subscriptions", payload);
        onSaved(res.data);
      }
      onClose();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      setError(err?.response?.data?.detail || "Failed to save subscription.");
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    if (!target && provider !== "browser") { setError("Enter a target first."); return; }
    if (provider === "browser") { setError("Save first, then test from the subscription card."); return; }
    setTesting(true); setTestResult(null); setError(null);
    try {
      const res = await api.post<{ sent: boolean; message: string }>("/push-subscriptions/test-target", { provider, target });
      setTestResult(res.data.message);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      setError(err?.response?.data?.detail || "Test failed.");
    } finally {
      setTesting(false);
    }
  };

  const meta = PROVIDER_META[provider];

  return (
    <div className="fixed inset-0 z-50 bg-black/50 backdrop-blur-sm flex items-center justify-center p-4">
      <div className="bg-white dark:bg-slate-900 rounded-2xl shadow-2xl border border-slate-200 dark:border-slate-700 w-full max-w-lg">
        <div className={`bg-gradient-to-r ${meta.color} px-6 py-4 rounded-t-2xl`}>
          <h2 className="text-lg font-bold text-white">
            {isEditing ? `Edit ${meta.label} Subscription` : "Add Push Subscription"}
          </h2>
          <p className="text-white/70 text-xs mt-0.5">
            Push notifications delivered to your device on critical alerts.
          </p>
        </div>

        <div className="p-6 flex flex-col gap-5">
          {/* Provider selector (add mode only) */}
          {!isEditing && (
            <div>
              <label className="block text-xs font-semibold text-slate-500 dark:text-slate-400 mb-2 uppercase tracking-wider">Provider</label>
              <div className="grid grid-cols-3 gap-2">
                {(["ntfy", "pushover", "browser"] as const).map((p) => {
                  const m = PROVIDER_META[p];
                  const disabled = p === "browser" && !vapid.configured;
                  return (
                    <button key={p} disabled={disabled} onClick={() => setProvider(p)}
                      className={`flex flex-col items-center gap-1.5 py-3 px-2 rounded-xl border-2 transition-all ${
                        provider === p
                          ? `${m.border} ${m.bg}`
                          : "border-slate-200 dark:border-slate-700 hover:border-slate-300"
                      } ${disabled ? "opacity-40 cursor-not-allowed" : "cursor-pointer"}`}>
                      <span className="text-xl">{m.icon}</span>
                      <span className={`text-xs font-bold ${provider === p ? m.text : "text-slate-500"}`}>{m.label}</span>
                      {disabled && <span className="text-[9px] text-slate-400">Not configured</span>}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {/* Label */}
          <div>
            <label className="block text-xs font-semibold text-slate-500 dark:text-slate-400 mb-1.5 uppercase tracking-wider">Device Label</label>
            <input value={label} onChange={(e) => setLabel(e.target.value)}
              placeholder="e.g. Work iPhone, Home Browser"
              className="w-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-500" />
          </div>

          {/* Target (not browser) */}
          {provider !== "browser" && (
            <div>
              <label className="block text-xs font-semibold text-slate-500 dark:text-slate-400 mb-1.5 uppercase tracking-wider">
                {provider === "ntfy" ? "Topic URL" : "User Key"}
              </label>
              <div className="flex gap-2">
                <input value={target} onChange={(e) => setTarget(e.target.value)}
                  placeholder={meta.placeholder}
                  className="flex-1 border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 rounded-lg px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-blue-500 font-mono" />
                <button onClick={handleTest} disabled={testing || !target}
                  className="text-xs font-bold px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 disabled:opacity-40 transition-colors whitespace-nowrap">
                  {testing ? "…" : "Test"}
                </button>
              </div>
              <p className="text-[11px] text-slate-400 mt-1">{meta.hint}</p>
            </div>
          )}

          {provider === "browser" && (
            <div className={`${meta.bg} ${meta.border} border rounded-lg px-4 py-3 text-sm ${meta.text}`}>
              {meta.hint}
            </div>
          )}

          {/* Options */}
          <div className="flex flex-col gap-3">
            <label className="flex items-start gap-3 cursor-pointer">
              <input type="checkbox" checked={includeNonCritical} onChange={(e) => setIncludeNonCritical(e.target.checked)}
                className="mt-0.5 accent-blue-600 shrink-0" />
              <div>
                <p className="text-sm font-semibold text-slate-700 dark:text-slate-200">Include warnings</p>
                <p className="text-xs text-slate-400">Also receive warning and info alerts, not just critical ones.</p>
              </div>
            </label>

            {provider !== "browser" && (
              <div>
                <p className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-2">Quick-action buttons (ntfy only)</p>
                <div className="flex gap-2 flex-wrap">
                  {ALL_ACTIONS.map(({ key, label: alabel }) => (
                    <button key={key} type="button" onClick={() => toggleAction(key)}
                      className={`text-xs font-semibold px-3 py-1.5 rounded-full border transition-colors ${
                        includeActions.includes(key)
                          ? "bg-blue-600 text-white border-blue-600"
                          : "border-slate-200 dark:border-slate-700 text-slate-500 hover:border-blue-400"
                      }`}>
                      {alabel}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>

          {testResult && (
            <p className={`text-xs rounded-lg px-3 py-2 font-medium ${testResult.includes("working") ? "bg-emerald-50 dark:bg-emerald-950/30 text-emerald-700 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800" : "bg-red-50 dark:bg-red-950/30 text-red-700 dark:text-red-300 border border-red-200 dark:border-red-800"}`}>
              {testResult}
            </p>
          )}
          {error && <p className="text-xs rounded-lg px-3 py-2 bg-red-50 dark:bg-red-950/30 text-red-700 dark:text-red-300 border border-red-200 dark:border-red-800">{error}</p>}

          <div className="flex gap-2 pt-1">
            <button onClick={onClose} className="flex-1 text-sm font-semibold border border-slate-200 dark:border-slate-700 rounded-lg py-2 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 transition-colors">Cancel</button>
            <button onClick={handleSave} disabled={saving}
              className="flex-1 text-sm font-bold bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded-lg py-2 transition-colors">
              {saving ? "Saving…" : isEditing ? "Save Changes" : "Add Subscription"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Subscription card ───────────────────────────────────────────────────────

function SubCard({
  sub,
  onEdit,
  onDelete,
  onToggle,
  onTest,
}: {
  sub: PushSub;
  onEdit: () => void;
  onDelete: () => void;
  onToggle: () => void;
  onTest: () => void;
}) {
  const meta = PROVIDER_META[sub.provider] || PROVIDER_META.ntfy;
  const [testing, setTesting] = useState(false);
  const [testMsg, setTestMsg] = useState<string | null>(null);

  const handleTest = async () => {
    setTesting(true); setTestMsg(null);
    try {
      const res = await api.post<{ sent: boolean; message: string }>(`/push-subscriptions/${sub.id}/test`);
      setTestMsg(res.data.message);
    } catch {
      setTestMsg("Test failed — check your target.");
    } finally {
      setTesting(false);
      setTimeout(() => setTestMsg(null), 5000);
    }
    onTest();
  };

  return (
    <div className={`rounded-xl border overflow-hidden transition-all ${sub.enabled ? "shadow-sm" : "opacity-60"} bg-white dark:bg-slate-800 border-slate-200 dark:border-slate-700`}>
      {/* Accent bar */}
      <div className={`h-1 bg-gradient-to-r ${meta.color}`} />

      <div className="px-4 py-3 flex items-start gap-3">
        <div className={`w-9 h-9 rounded-lg shrink-0 flex items-center justify-center text-lg ${meta.bg} border ${meta.border}`}>
          {meta.icon}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-bold text-slate-800 dark:text-slate-100 truncate">{sub.label}</span>
            <span className={`text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${meta.bg} ${meta.text} ${meta.border} border`}>
              {meta.label}
            </span>
            {sub.include_non_critical && (
              <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-amber-50 dark:bg-amber-950/30 text-amber-600 dark:text-amber-400 border border-amber-200 dark:border-amber-800">
                All severities
              </span>
            )}
          </div>
          <p className="text-xs text-slate-400 dark:text-slate-500 mt-0.5 truncate font-mono">
            {sub.provider === "browser" ? "Browser Web Push" : sub.target}
          </p>
          <p className="text-[11px] text-slate-400 mt-1">
            Last push: {fmtDate(sub.last_pushed_at)}
          </p>
          {testMsg && (
            <p className={`text-[11px] mt-1 font-semibold ${testMsg.includes("working") ? "text-emerald-600 dark:text-emerald-400" : "text-red-500"}`}>
              {testMsg}
            </p>
          )}
        </div>

        <div className="flex items-center gap-1 shrink-0">
          {/* Enable toggle */}
          <button onClick={onToggle} title={sub.enabled ? "Disable" : "Enable"}
            className={`w-8 h-4.5 rounded-full relative transition-colors ${sub.enabled ? "bg-emerald-500" : "bg-slate-300 dark:bg-slate-600"}`}
            style={{ width: 32, height: 18 }}>
            <span className={`absolute top-0.5 w-3.5 h-3.5 rounded-full bg-white shadow transition-transform ${sub.enabled ? "translate-x-3.5" : "translate-x-0.5"}`} />
          </button>
          <button onClick={handleTest} disabled={testing} title="Send test push"
            className="text-xs font-bold text-slate-400 hover:text-blue-600 dark:hover:text-blue-400 px-1 transition-colors">
            {testing ? "…" : "Test"}
          </button>
          <button onClick={onEdit} title="Edit" className="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 text-xs px-1 transition-colors">✎</button>
          <button onClick={onDelete} title="Delete" className="text-slate-400 hover:text-red-500 text-xs px-1 transition-colors">✕</button>
        </div>
      </div>
    </div>
  );
}

// ─── Main page ───────────────────────────────────────────────────────────────

export default function PushSettings() {
  const [subs, setSubs] = useState<PushSub[]>([]);
  const [vapid, setVapid] = useState<VapidKey>({ configured: false, public_key: null });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editSub, setEditSub] = useState<PushSub | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    Promise.all([
      api.get<PushSub[]>("/push-subscriptions"),
      api.get<VapidKey>("/push-subscriptions/vapid-public-key"),
    ])
      .then(([subsRes, vapidRes]) => {
        if (!mountedRef.current) return;
        setSubs(subsRes.data);
        setVapid(vapidRes.data);
      })
      .catch(() => { if (mountedRef.current) setError("Failed to load push subscriptions."); })
      .finally(() => { if (mountedRef.current) setLoading(false); });
    return () => { mountedRef.current = false; };
  }, []);

  const openAdd = () => { setEditSub(null); setModalOpen(true); };
  const openEdit = (sub: PushSub) => { setEditSub(sub); setModalOpen(true); };

  const handleSaved = (saved: PushSub) => {
    setSubs((prev) => {
      const idx = prev.findIndex((s) => s.id === saved.id);
      if (idx >= 0) { const next = [...prev]; next[idx] = saved; return next; }
      return [saved, ...prev];
    });
  };

  const handleDelete = async (sub: PushSub) => {
    if (!window.confirm(`Delete "${sub.label}"?`)) return;
    try {
      await api.delete(`/push-subscriptions/${sub.id}`);
      setSubs((prev) => prev.filter((s) => s.id !== sub.id));
    } catch { /* best effort */ }
  };

  const handleToggle = async (sub: PushSub) => {
    try {
      const res = await api.patch<PushSub>(`/push-subscriptions/${sub.id}`, { enabled: !sub.enabled });
      handleSaved(res.data);
    } catch { /* best effort */ }
  };

  return (
    <div className="max-w-2xl mx-auto py-8 px-4 flex flex-col gap-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 dark:text-white">Push Notifications</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Get real-time alerts on your phone or browser. Each subscription is private to your account.
          </p>
        </div>
        <button onClick={openAdd}
          className="bg-blue-600 hover:bg-blue-500 text-white font-bold text-sm px-4 py-2.5 rounded-xl shadow-sm shadow-blue-600/30 transition-colors flex items-center gap-2 shrink-0">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M12 5v14M5 12h14"/></svg>
          Add Device
        </button>
      </div>

      {/* Provider explainer */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {(["ntfy", "pushover", "browser"] as const).map((p) => {
          const m = PROVIDER_META[p];
          return (
            <div key={p} className={`rounded-xl ${m.bg} border ${m.border} px-4 py-3`}>
              <div className="flex items-center gap-2 mb-1">
                <span className="text-lg">{m.icon}</span>
                <span className={`text-sm font-bold ${m.text}`}>{m.label}</span>
                {p === "browser" && !vapid.configured && (
                  <span className="text-[9px] font-bold text-slate-400 bg-slate-100 dark:bg-slate-800 rounded px-1 py-0.5 leading-none">NOT CONFIGURED</span>
                )}
              </div>
              <p className="text-[11px] text-slate-500 dark:text-slate-400">
                {p === "ntfy" && "Open-source. Self-host or use ntfy.sh for free."}
                {p === "pushover" && "Reliable paid push service — one-time $5 purchase on iOS/Android."}
                {p === "browser" && (vapid.configured ? "Delivers via Web Push. Works on most desktop browsers." : "Requires VAPID keys configured server-side.")}
              </p>
            </div>
          );
        })}
      </div>

      {/* Subscriptions list */}
      {loading ? (
        <div className="flex items-center justify-center py-12 gap-3">
          <div className="w-5 h-5 rounded-full border-2 border-blue-500 border-t-transparent animate-spin" />
          <p className="text-sm text-slate-400">Loading subscriptions…</p>
        </div>
      ) : error ? (
        <p className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-800 px-4 py-3 rounded-xl">
          {error}
        </p>
      ) : subs.length === 0 ? (
        <div className="text-center py-16 border-2 border-dashed border-slate-200 dark:border-slate-700 rounded-2xl">
          <p className="text-4xl mb-3">🔕</p>
          <p className="text-sm font-semibold text-slate-600 dark:text-slate-300">No push subscriptions yet</p>
          <p className="text-xs text-slate-400 mt-1 mb-5">Add a device to receive real-time alert notifications.</p>
          <button onClick={openAdd}
            className="bg-blue-600 hover:bg-blue-500 text-white font-bold text-sm px-5 py-2.5 rounded-xl shadow-sm transition-colors">
            + Add Device
          </button>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {subs.map((sub) => (
            <SubCard key={sub.id} sub={sub}
              onEdit={() => openEdit(sub)}
              onDelete={() => handleDelete(sub)}
              onToggle={() => handleToggle(sub)}
              onTest={() => {}} />
          ))}
        </div>
      )}

      {/* Quick tips */}
      <div className="bg-slate-50 dark:bg-slate-900/50 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-4">
        <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">How it works</p>
        <ul className="flex flex-col gap-1.5 text-xs text-slate-500 dark:text-slate-400">
          <li>• Critical alerts always fire. Enable <strong className="text-slate-700 dark:text-slate-200">Include warnings</strong> to receive lower-severity alerts too.</li>
          <li>• <strong className="text-slate-700 dark:text-slate-200">ntfy</strong> supports action buttons in the notification — acknowledge or escalate without opening the browser.</li>
          <li>• You can register multiple devices (phone + laptop) under separate subscriptions.</li>
          <li>• Subscriptions are scoped to your account only — other users cannot see or manage them.</li>
        </ul>
      </div>

      {modalOpen && (
        <SubscriptionModal
          editSub={editSub}
          vapid={vapid}
          onClose={() => setModalOpen(false)}
          onSaved={handleSaved}
        />
      )}
    </div>
  );
}
