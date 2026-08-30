import { FormEvent, useEffect, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useConfirm } from "../lib/confirm";
import { useToast } from "../lib/toast";
import { NotificationSettings, NotificationTestResult, SyslogDestination, WebhookEndpoint, WebhookTestResult, PushSubscription } from "../lib/types";
import { EmptyState } from "../components/EmptyState";

// --- Types (kept local -- these features are small enough not to warrant
// new entries in lib/types.ts's shared type set) -----------------------

type ChatOpsLink = {
  user_id: string;
  user_email: string;
  full_name: string;
  slack_user_id: string | null;
  msteams_user_id: string | null;
};

type ChatOpsCommandItem = {
  alert_id?: string | null;
  hostname?: string | null;
  severity?: string | null;
  category?: string | null;
};

type ChatOpsCommandResponse = {
  ok: boolean;
  text: string;
  severity: string | null;
  items: ChatOpsCommandItem[];
};

type GitRepoConfig = {
  id: string;
  name: string;
  repo_url: string;
  branch: string;
  template_path: string;
  direction: "pull" | "push" | "bidirectional";
  auto_sync_enabled: boolean;
  has_access_token: boolean;
  has_webhook_secret: boolean;
  last_synced_commit: string | null;
  last_synced_at: string | null;
  last_sync_status: string;
  last_sync_error: string | null;
  created_by: string;
  created_at: string;
};

type DigestSubscription = {
  id: string;
  tenant_id: string;
  cadence: "daily" | "weekly";
  hour_utc: number;
  day_of_week: number | null;
  recipients: string;
  severity_floor: "all" | "warning" | "critical";
  is_active: boolean;
  last_sent_at: string | null;
  created_by: string | null;
  created_at: string;
};

type TenantOption = { id: string; name: string };

const emptyLinkForm = { platform: "slack" as "slack" | "teams", external_user_id: "", user_email: "" };

const emptyRepoForm = {
  name: "",
  repo_url: "",
  branch: "main",
  template_path: "templates/",
  direction: "pull" as GitRepoConfig["direction"],
  auto_sync_enabled: true,
  access_token: "",
  webhook_secret: "",
};

const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

const severityFloorCopy: Record<DigestSubscription["severity_floor"], string> = {
  all: "Every severity still pages live — digest is a rollup only",
  warning: "Warning and below wait for the digest; critical still pages live",
  critical: "Everything waits for the digest — nothing pages live",
};

const emptyDigestForm = {
  tenant_id: "",
  cadence: "weekly" as DigestSubscription["cadence"],
  hour_utc: 8,
  day_of_week: 0,
  recipients: "",
  severity_floor: "all" as DigestSubscription["severity_floor"],
  is_active: true,
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1").replace(/\/$/, "");

const syncStatusStyle: Record<string, string> = {
  never_synced: "bg-slate-50 dark:bg-slate-800 text-slate-500 dark:text-slate-400 border border-slate-200 dark:border-slate-700",
  syncing: "bg-blue-600/10 dark:bg-blue-400/10 text-blue-600 dark:text-blue-400 border border-blue-600/30 dark:border-blue-400/30",
  succeeded: "bg-emerald-600/10 dark:bg-emerald-400/10 text-emerald-600 dark:text-emerald-400 border border-emerald-600/30 dark:border-emerald-400/30",
  failed: "bg-red-600/10 dark:bg-red-400/10 text-red-600 dark:text-red-400 border border-red-600/30 dark:border-red-400/30",
};

const directionCopy: Record<GitRepoConfig["direction"], string> = {
  pull: "Repo → Review Queue",
  push: "NetGuard → Repo Mirror",
  bidirectional: "Bidirectional",
};

const QUICK_COMMANDS = ["help", "fleet", "alerts", "drift"];

function Panel({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <section
      className={`rounded-lg overflow-hidden border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 ${className}`}
    >
      {children}
    </section>
  );
}

function CopyField({ label, value }: { label: string; value: string }) {
  const toast = useToast();
  return (
    <div>
      <p className="text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">{label}</p>
      <button
        type="button"
        onClick={() => {
          navigator.clipboard?.writeText(value);
          toast.success("Copied to clipboard.");
        }}
        className="w-full flex items-center gap-2 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-md px-3 py-2 text-left group hover:border-blue-600/40 dark:border-blue-400/40 transition-colors"
      >
        <code className="flex-1 font-mono text-[11px] text-slate-900 dark:text-slate-100 truncate">{value}</code>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="shrink-0 text-slate-500 dark:text-slate-400 group-hover:text-blue-600 dark:text-blue-400">
          <rect x="9" y="9" width="13" height="13" rx="2" />
          <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
        </svg>
      </button>
    </div>
  );
}

type IntegrationTabId = "chatops" | "notifications" | "syslog" | "gitops" | "digests";

const INTEGRATION_TABS: { id: IntegrationTabId; label: string; blurb: string; icon: React.ReactNode }[] = [
  {
    id: "chatops",
    label: "ChatOps",
    blurb: "Slack / Teams commands",
    icon: (
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M8 9h8M8 13h5" /><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
      </svg>
    ),
  },
  {
    id: "notifications",
    label: "Alert Notifications",
    blurb: "Webhooks & delivery",
    icon: (
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <path d="M18 8a6 6 0 00-12 0c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 01-3.46 0" />
      </svg>
    ),
  },
  {
    id: "syslog",
    label: "Remote Syslog",
    blurb: "Forward to SIEM/collector",
    icon: (
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <rect x="2" y="4" width="20" height="16" rx="2" /><path d="M2 8h20M6 12h.01M6 16h.01" />
      </svg>
    ),
  },
  {
    id: "gitops",
    label: "GitOps",
    blurb: "Config-as-code sync",
    icon: (
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <circle cx="18" cy="18" r="3" /><circle cx="6" cy="6" r="3" /><path d="M6 9v6a3 3 0 003 3h6M18 6h-1" />
      </svg>
    ),
  },
  {
    id: "digests",
    label: "Email Digests",
    blurb: "Scheduled activity reports",
    icon: (
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
        <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 8l9 6 9-6" />
      </svg>
    ),
  },
];

export default function IntegrationsPage() {
  const { user } = useAuth();
  const canManage = user?.role === "network_admin";
  const [tab, setTab] = useState<IntegrationTabId>("chatops");

  return (
    <div className="pb-16 max-w-6xl mx-auto flex flex-col gap-6 pt-2">
      <div>
        <h1 className="text-2xl font-bold text-navy dark:text-white">Integrations</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1 max-w-2xl">
          Two-way ChatOps — approve, reject, and roll back straight from Slack or Teams — plus Git-backed
          config-as-code sync for the template library.
        </p>
      </div>

      {/* Tab bar -- replaces the old always-stacked layout (ChatOps + Alert
          Notifications + GitOps rendered one after another, ~1000px of
          scroll before you ever reach GitOps). Each section is still its
          own self-contained component; we just mount one at a time. */}
      <div className="flex items-center gap-1 border-b border-slate-200 dark:border-slate-700 overflow-x-auto no-scrollbar">
        {INTEGRATION_TABS.map((t) => {
          const active = tab === t.id;
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`flex items-center gap-2 px-4 py-3 text-sm font-bold whitespace-nowrap border-b-2 -mb-px transition-colors ${
                active
                  ? "border-blue-600 dark:border-blue-400 text-navy dark:text-white"
                  : "border-transparent text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-100"
              }`}
            >
              <span className={active ? "text-blue-600 dark:text-blue-400" : "text-slate-400 dark:text-slate-500"}>{t.icon}</span>
              <span className="flex flex-col items-start leading-tight">
                <span>{t.label}</span>
                <span className="text-[10px] font-medium text-slate-400 dark:text-slate-500 hidden sm:inline">{t.blurb}</span>
              </span>
            </button>
          );
        })}
      </div>

      <div className={tab === "chatops" ? "flex flex-col gap-8" : "hidden"}>
        <ChatOpsSection canManage={canManage} />
      </div>
      <div className={tab === "notifications" ? "flex flex-col gap-8" : "hidden"}>
        <AlertNotificationsSection canManage={canManage} />
      </div>
      <div className={tab === "syslog" ? "flex flex-col gap-8" : "hidden"}>
        <RemoteSyslogSection canManage={canManage} />
      </div>
      <div className={tab === "gitops" ? "flex flex-col gap-8" : "hidden"}>
        <GitOpsSection canManage={canManage} />
      </div>
      <div className={tab === "digests" ? "flex flex-col gap-8" : "hidden"}>
        <DigestsSection canManage={canManage} />
      </div>
    </div>
  );
}

// =============================== ChatOps ===================================

// Self-service link status/management for the *current* user, shown to
// everyone regardless of role -- see GET/POST/DELETE /chatops/links/me.
// Previously the only way to link an account at all was the admin-only
// roster below, so every user's Slack/Teams ID had to be typed in by an
// admin by hand.
function MyChatOpsLinkPanel() {
  const { user } = useAuth();
  const toast = useToast();
  const [link, setLink] = useState<ChatOpsLink | null>(null);
  const [loading, setLoading] = useState(true);
  const [platform, setPlatform] = useState<"slack" | "teams">("slack");
  const [externalId, setExternalId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unlinkingPlatform, setUnlinkingPlatform] = useState<"slack" | "teams" | null>(null);

  const load = () => {
    setLoading(true);
    api
      .get<ChatOpsLink>("/chatops/links/me")
      .then((res) => setLink(res.data))
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!externalId.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await api.post("/chatops/links/me", { platform, external_user_id: externalId.trim() });
      setExternalId("");
      toast.success(`${platform === "slack" ? "Slack" : "Teams"} account linked.`);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to link account.");
    } finally {
      setSaving(false);
    }
  };

  const unlink = async (p: "slack" | "teams") => {
    setUnlinkingPlatform(p);
    try {
      await api.delete("/chatops/links/me", { params: { platform: p } });
      toast.success(`${p === "slack" ? "Slack" : "Teams"} account unlinked.`);
      load();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to unlink account.");
    } finally {
      setUnlinkingPlatform(null);
    }
  };

  return (
    <div className="p-5 border-b border-slate-200 dark:border-slate-700 bg-slate-50/60 dark:bg-slate-800/60">
      <p className="text-xs font-bold text-slate-400 dark:text-slate-500 uppercase tracking-wider mb-3">
        My link{user ? ` — ${user.email}` : ""}
      </p>

      {loading ? (
        <p className="text-xs text-slate-400 dark:text-slate-500">Loading…</p>
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2 flex-wrap">
            {link?.slack_user_id ? (
              <span className="text-[11px] font-mono bg-purple-600/10 dark:bg-purple-400/10 border border-purple-600/25 dark:border-purple-400/25 text-purple-600 dark:text-purple-400 px-2 py-1 rounded-full flex items-center gap-2">
                Slack: {link.slack_user_id}
                <button onClick={() => unlink("slack")} disabled={unlinkingPlatform === "slack"} className="font-bold hover:text-red-600 dark:hover:text-red-400 disabled:opacity-50">
                  ×
                </button>
              </span>
            ) : (
              <span className="text-[11px] text-slate-400 dark:text-slate-500">Slack: not linked</span>
            )}
            {link?.msteams_user_id ? (
              <span className="text-[11px] font-mono bg-blue-600/10 dark:bg-blue-400/10 border border-blue-600/25 dark:border-blue-400/25 text-blue-600 dark:text-blue-400 px-2 py-1 rounded-full flex items-center gap-2">
                Teams: {link.msteams_user_id}
                <button onClick={() => unlink("teams")} disabled={unlinkingPlatform === "teams"} className="font-bold hover:text-red-600 dark:hover:text-red-400 disabled:opacity-50">
                  ×
                </button>
              </span>
            ) : (
              <span className="text-[11px] text-slate-400 dark:text-slate-500">Teams: not linked</span>
            )}
          </div>

          <form onSubmit={submit} className="flex flex-wrap gap-2 items-start">
            <select
              value={platform}
              onChange={(e) => setPlatform(e.target.value as "slack" | "teams")}
              className="border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
            >
              <option value="slack">Slack</option>
              <option value="teams">Microsoft Teams</option>
            </select>
            <input
              value={externalId}
              onChange={(e) => setExternalId(e.target.value)}
              placeholder={platform === "slack" ? "Your Slack user ID (e.g. U0123ABC)" : "Your Teams user ID"}
              className="flex-1 min-w-[220px] border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
            />
            <button
              type="submit"
              disabled={saving || !externalId.trim()}
              className="bg-blue-600 dark:bg-blue-400 text-slate-50 dark:text-slate-950 rounded-md px-4 py-2 text-xs font-bold hover:brightness-110 transition disabled:opacity-50"
            >
              {saving ? "Linking…" : "Link"}
            </button>
          </form>
          {error && <p className="text-red-600 dark:text-red-400 text-xs">{error}</p>}
          <p className="text-[11px] text-slate-400 dark:text-slate-500">
            Find your Slack member ID via your Slack profile → "More" → "Copy member ID". A NetGuard admin
            can link/unlink on your behalf from the roster below if you don't have it handy.
          </p>
        </div>
      )}
    </div>
  );
}

function ChatOpsSection({ canManage }: { canManage: boolean }) {
  const confirm = useConfirm();
  const [links, setLinks] = useState<ChatOpsLink[]>([]);
  // Non-admins can't call GET /chatops/links (403 -- roster is
  // admin-only), so skip the fetch entirely for them; they get the
  // self-service "My Link" panel below instead.
  const [loading, setLoading] = useState(canManage);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(emptyLinkForm);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    api
      .get<ChatOpsLink[]>("/chatops/links")
      .then((res) => {
        setLinks(res.data);
        setError(null);
      })
      .catch((err) => setError(err?.response?.data?.detail || "Failed to load ChatOps links."))
      .finally(() => setLoading(false));
  };

  useEffect(() => { if (canManage) load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setSaveError(null);
    try {
      await api.post("/chatops/links", form);
      setShowForm(false);
      setForm(emptyLinkForm);
      load();
    } catch (err: any) {
      setSaveError(err?.response?.data?.detail || "Failed to link account.");
    } finally {
      setSaving(false);
    }
  };

  const unlink = async (link: ChatOpsLink, platform: "slack" | "teams") => {
    if (!(await confirm(`Unlink ${platform} from ${link.user_email}?`, { confirmLabel: "Unlink" }))) return;
    try {
      await api.delete(`/chatops/links/${link.user_id}`, { params: { platform } });
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to unlink account.");
    }
  };

  return (
    <Panel>
      <div className="p-5 flex items-start justify-between gap-4 flex-wrap border-b border-slate-200 dark:border-slate-700">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-lg bg-purple-600/10 dark:bg-purple-400/10 border border-purple-600/25 dark:border-purple-400/25 text-purple-600 dark:text-purple-400 flex items-center justify-center shrink-0">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M8 9h8M8 13h5" /><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
            </svg>
          </div>
          <div>
            <h2 className="text-lg font-bold text-navy dark:text-white">ChatOps</h2>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-1 max-w-2xl leading-relaxed">
              Linked users can run <code className="font-mono text-blue-600 dark:text-blue-400">/netguard approve &lt;id&gt;</code>,{" "}
              <code className="font-mono text-blue-600 dark:text-blue-400">reject</code>, <code className="font-mono text-blue-600 dark:text-blue-400">rollback</code>,{" "}
              <code className="font-mono text-blue-600 dark:text-blue-400">status &lt;hostname&gt;</code>,{" "}
              <code className="font-mono text-blue-600 dark:text-blue-400">alerts</code>, <code className="font-mono text-blue-600 dark:text-blue-400">fleet</code>,{" "}
              <code className="font-mono text-blue-600 dark:text-blue-400">drift</code>, and more from Slack or Teams.
            </p>
          </div>
        </div>
        {canManage && (
          <button
            onClick={() => {
              setForm(emptyLinkForm);
              setSaveError(null);
              setShowForm(true);
            }}
            className="bg-blue-600 dark:bg-blue-400 text-slate-50 dark:text-slate-950 rounded-md px-4 py-2 text-xs font-bold hover:brightness-110 transition shrink-0"
          >
            + Link Account
          </button>
        )}
      </div>

      {/* Webhook target endpoints -- copy-to-clipboard so wiring up the
          Slack slash command / Teams outgoing webhook doesn't require
          reading source or docs. */}
      <div className="p-5 border-b border-slate-200 dark:border-slate-700 grid grid-cols-1 sm:grid-cols-2 gap-3 bg-slate-50/40 dark:bg-slate-800/40">
        <CopyField label="Slack slash command URL" value={`${API_BASE}/chatops/slack/commands`} />
        <CopyField label="Teams outgoing webhook URL" value={`${API_BASE}/chatops/teams/commands`} />
      </div>

      <MyChatOpsLinkPanel />

      {canManage && error && <p className="text-red-600 dark:text-red-400 text-sm p-5">{error}</p>}

      {canManage && (
        <div className="px-5 pt-4">
          <p className="text-xs font-bold text-slate-400 dark:text-slate-500 uppercase tracking-wider">All linked accounts</p>
        </div>
      )}
      {!canManage ? null : loading ? (
        <p className="text-xs text-slate-400 dark:text-slate-500 p-5">Loading links…</p>
      ) : links.length === 0 ? (
        <p className="text-xs text-slate-400 dark:text-slate-500 italic p-5">No Slack or Teams accounts linked yet.</p>
      ) : (
        <div className="divide-y divide-slate-200 dark:divide-slate-700">
          {links.map((l) => (
            <div key={l.user_id} className="p-4 flex items-center justify-between gap-3 flex-wrap">
              <div>
                <p className="font-bold text-navy dark:text-white text-sm">{l.full_name}</p>
                <p className="text-xs text-slate-500 dark:text-slate-400">{l.user_email}</p>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                {l.slack_user_id && (
                  <span className="text-[11px] font-mono bg-purple-600/10 dark:bg-purple-400/10 border border-purple-600/25 dark:border-purple-400/25 text-purple-600 dark:text-purple-400 px-2 py-1 rounded-full flex items-center gap-2">
                    Slack: {l.slack_user_id}
                    {canManage && (
                      <button onClick={() => unlink(l, "slack")} className="font-bold hover:text-red-600 dark:text-red-400">
                        ×
                      </button>
                    )}
                  </span>
                )}
                {l.msteams_user_id && (
                  <span className="text-[11px] font-mono bg-blue-600/10 dark:bg-blue-400/10 border border-blue-600/25 dark:border-blue-400/25 text-blue-600 dark:text-blue-400 px-2 py-1 rounded-full flex items-center gap-2">
                    Teams: {l.msteams_user_id}
                    {canManage && (
                      <button onClick={() => unlink(l, "teams")} className="font-bold hover:text-red-600 dark:text-red-400">
                        ×
                      </button>
                    )}
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {showForm && (
        <form onSubmit={submit} className="p-5 border-t border-slate-200 dark:border-slate-700 flex flex-col gap-3 bg-slate-50/60 dark:bg-slate-800/60">
          {saveError && <p className="text-red-600 dark:text-red-400 text-xs">{saveError}</p>}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <select
              value={form.platform}
              onChange={(e) => setForm((f) => ({ ...f, platform: e.target.value as "slack" | "teams" }))}
              className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
            >
              <option value="slack">Slack</option>
              <option value="teams">Microsoft Teams</option>
            </select>
            <input
              required
              placeholder="External user ID (e.g. Slack U0123ABC)"
              value={form.external_user_id}
              onChange={(e) => setForm((f) => ({ ...f, external_user_id: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
            />
            <input
              required
              type="email"
              placeholder="NetGuard user email"
              value={form.user_email}
              onChange={(e) => setForm((f) => ({ ...f, user_email: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
            />
          </div>
          <div className="flex gap-2 justify-end">
            <button type="button" onClick={() => setShowForm(false)} className="px-4 py-2 text-xs font-bold text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:text-slate-100">
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="bg-blue-600 dark:bg-blue-400 text-slate-50 dark:text-slate-950 rounded-md px-5 py-2 text-xs font-bold hover:brightness-110 transition disabled:opacity-50"
            >
              {saving ? "Linking…" : "Link Account"}
            </button>
          </div>
        </form>
      )}

      <CommandTester />
    </Panel>
  );
}

// A live tester panel driven by POST /chatops/test-command -- runs the exact
// same parser/executor Slack and Teams hit, as the calling user, so someone
// can validate a command (and its RBAC) before wiring up a chat platform.
function CommandTester() {
  const [text, setText] = useState("help");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ChatOpsCommandResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const run = async (cmd?: string) => {
    const value = (cmd ?? text).trim();
    if (!value) return;
    setText(value);
    setRunning(true);
    setErr(null);
    try {
      const res = await api.post<ChatOpsCommandResponse>("/chatops/test-command", { text: value });
      setResult(res.data);
    } catch (e: any) {
      setErr(e?.response?.data?.detail || "Command failed.");
      setResult(null);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="p-5 border-t border-slate-200 dark:border-slate-700 bg-slate-50/40 dark:bg-slate-950/40">
      <div className="flex items-center gap-2 mb-1">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-blue-600 dark:text-blue-400">
          <path d="M4 17l6-6-6-6" /><path d="M12 19h8" />
        </svg>
        <p className="text-xs font-semibold text-blue-600 dark:text-blue-400">Command Tester</p>
      </div>
      <p className="text-xs text-slate-500 dark:text-slate-400 mb-3">
        Runs as you, through the same executor Slack/Teams use — a safe way to check a command and its
        permissions before wiring up a chat platform.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          run();
        }}
        className="flex flex-col sm:flex-row gap-2"
      >
        <div className="flex-1 flex items-center bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-md px-3 focus-within:border-blue-600 dark:border-blue-400">
          <span className="font-mono text-blue-600 dark:text-blue-400 text-sm mr-1.5 select-none">/netguard</span>
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="fleet"
            className="flex-1 bg-transparent py-2.5 text-sm text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 outline-none font-mono"
          />
        </div>
        <button
          type="submit"
          disabled={running}
          className="bg-blue-600 dark:bg-blue-400 text-slate-50 dark:text-slate-950 rounded-md px-5 py-2.5 text-sm font-bold hover:brightness-110 transition disabled:opacity-50 shrink-0"
        >
          {running ? "Running…" : "Run"}
        </button>
      </form>

      <div className="flex flex-wrap gap-1.5 mt-2.5">
        {QUICK_COMMANDS.map((c) => (
          <button
            key={c}
            type="button"
            onClick={() => run(c)}
            className="text-[11px] font-mono bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-400 hover:text-blue-600 dark:text-blue-400 hover:border-blue-600/40 dark:border-blue-400/40 rounded-full px-2.5 py-1 transition-colors"
          >
            {c}
          </button>
        ))}
      </div>

      {(result || err) && (
        <div
          className={`mt-3 rounded-md border px-4 py-3 font-mono text-xs whitespace-pre-wrap leading-relaxed ${
            err || result?.ok === false
              ? "border-red-600/30 dark:border-red-400/30 bg-red-600/10 dark:bg-red-400/10 text-red-600 dark:text-red-400"
              : "border-emerald-600/25 dark:border-emerald-400/25 bg-emerald-600/5 dark:bg-emerald-400/5 text-slate-900 dark:text-slate-100"
          }`}
        >
          {err || result?.text}
          {!!result?.items?.length && (
            <div className="mt-2 pt-2 border-t border-slate-200/60 dark:border-slate-700/60 space-y-1">
              {result.items.map((it, i) => (
                <div key={i} className="flex gap-3 text-[11px] text-slate-500 dark:text-slate-400">
                  {it.hostname && <span className="text-slate-900 dark:text-slate-100">{it.hostname}</span>}
                  {it.severity && <span className="uppercase">{it.severity}</span>}
                  {it.category && <span>{it.category}</span>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// =============================== GitOps =====================================

// =========================== Alert Notifications ============================
// Email (SMTP) is DB-configurable here (app.api.notification_settings) --
// previously env-var-only with no UI at all. Slack is a quick-add shortcut
// onto the same WebhookEndpoint system (app.api.webhooks) Alert Center's
// Webhooks tab already manages in full (multiple endpoints, event
// filters, delivery logs, retries) -- this panel intentionally stays
// thin and links there rather than re-implementing all of that.

const emptySmtpForm = {
  smtp_enabled: false, smtp_host: "", smtp_port: "587", smtp_username: "",
  smtp_password: "", smtp_from_email: "", smtp_use_tls: true, recipients: "",
};

function AlertNotificationsSection({ canManage }: { canManage: boolean }) {
  const toast = useToast();

  // --- Email (SMTP) ---
  const [smtp, setSmtp] = useState<NotificationSettings | null>(null);
  const [smtpForm, setSmtpForm] = useState(emptySmtpForm);
  // Non-admins cannot call GET /notification-settings (403), so start
  // with loading=false and skip the fetch entirely for them.
  const [smtpLoading, setSmtpLoading] = useState(canManage);
  const [smtpSaving, setSmtpSaving] = useState(false);
  const [smtpTesting, setSmtpTesting] = useState(false);
  const [smtpError, setSmtpError] = useState<string | null>(null);

  const loadSmtp = () => {
    setSmtpLoading(true);
    api
      .get<NotificationSettings>("/notification-settings")
      .then((res) => {
        setSmtp(res.data);
        setSmtpForm({
          smtp_enabled: res.data.smtp_enabled,
          smtp_host: res.data.smtp_host || "",
          smtp_port: String(res.data.smtp_port),
          smtp_username: res.data.smtp_username || "",
          smtp_password: "",
          smtp_from_email: res.data.smtp_from_email || "",
          smtp_use_tls: res.data.smtp_use_tls,
          recipients: res.data.recipients || "",
        });
        setSmtpError(null);
      })
      .catch((err) => setSmtpError(err?.response?.data?.detail || "Failed to load email settings."))
      .finally(() => setSmtpLoading(false));
  };

  // Only admins are allowed to call this endpoint; non-admins see the
  // read-only status string rendered below instead.
  useEffect(() => { if (canManage) loadSmtp(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const saveSmtp = async (e: React.FormEvent) => {
    e.preventDefault();
    setSmtpSaving(true);
    setSmtpError(null);
    try {
      const payload: Record<string, unknown> = {
        smtp_enabled: smtpForm.smtp_enabled,
        smtp_host: smtpForm.smtp_host || null,
        smtp_port: Number(smtpForm.smtp_port) || 587,
        smtp_username: smtpForm.smtp_username || null,
        smtp_from_email: smtpForm.smtp_from_email || null,
        smtp_use_tls: smtpForm.smtp_use_tls,
        recipients: smtpForm.recipients || null,
      };
      // Omit entirely (leave stored password unchanged) unless the operator typed a new one.
      if (smtpForm.smtp_password) payload.smtp_password = smtpForm.smtp_password;
      await api.put("/notification-settings", payload);
      toast.success("Email alert settings saved.");
      loadSmtp();
    } catch (err: any) {
      setSmtpError(err?.response?.data?.detail || "Failed to save email settings.");
    } finally {
      setSmtpSaving(false);
    }
  };

  const testSmtp = async () => {
    setSmtpTesting(true);
    try {
      const res = await api.post<NotificationTestResult>("/notification-settings/test");
      if (res.data.success) toast.success(res.data.detail);
      else toast.error(res.data.detail);
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Test email failed to send.");
    } finally {
      setSmtpTesting(false);
    }
  };

  // --- Slack quick-add (creates a WebhookEndpoint, webhook_type=slack) ---
  const [slackWebhooks, setSlackWebhooks] = useState<WebhookEndpoint[]>([]);
  const [slackLoading, setSlackLoading] = useState(true);
  const [showSlackForm, setShowSlackForm] = useState(false);
  const [slackForm, setSlackForm] = useState({ name: "", url: "" });
  const [slackSaving, setSlackSaving] = useState(false);
  const [slackError, setSlackError] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);

  const loadSlack = () => {
    setSlackLoading(true);
    api
      .get<WebhookEndpoint[]>("/webhooks")
      .then((res) => setSlackWebhooks(res.data.filter((w) => w.webhook_type === "slack")))
      .catch(() => {})
      .finally(() => setSlackLoading(false));
  };

  useEffect(loadSlack, []);

  const addSlackWebhook = async (e: React.FormEvent) => {
    e.preventDefault();
    setSlackSaving(true);
    setSlackError(null);
    try {
      await api.post("/webhooks", { name: slackForm.name, url: slackForm.url, webhook_type: "slack" });
      setShowSlackForm(false);
      setSlackForm({ name: "", url: "" });
      loadSlack();
      toast.success("Slack webhook added — alert notifications will now post there.");
    } catch (err: any) {
      setSlackError(err?.response?.data?.detail || "Failed to add Slack webhook.");
    } finally {
      setSlackSaving(false);
    }
  };

  const testSlackWebhook = async (id: string) => {
    setTestingId(id);
    try {
      const res = await api.post<WebhookTestResult>(`/webhooks/${id}/test`);
      if (res.data.success) toast.success(res.data.message || "Test message sent to Slack.");
      else toast.error(res.data.message || "Slack test delivery failed.");
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Slack test delivery failed.");
    } finally {
      setTestingId(null);
    }
  };

  return (
    <Panel>
      <div className="p-5 flex items-start gap-3 border-b border-slate-200 dark:border-slate-700">
        <div className="w-9 h-9 rounded-lg bg-emerald-600/10 dark:bg-emerald-400/10 border border-emerald-600/25 dark:border-emerald-400/25 text-emerald-600 dark:text-emerald-400 flex items-center justify-center shrink-0">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M22 2L11 13" /><path d="M22 2l-7 20-4-9-9-4z" />
          </svg>
        </div>
        <div>
          <h2 className="text-lg font-bold text-navy dark:text-white">Alert Notifications</h2>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-1 max-w-2xl leading-relaxed">
            Where critical alerts and rule breaches get sent, in addition to the in-app Notification Center.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 divide-y lg:divide-y-0 lg:divide-x divide-slate-200 dark:divide-slate-700">
        {/* --- Email --- */}
        <div className="p-5">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-bold text-navy dark:text-white">Email (SMTP)</h3>
            {smtp && (
              <span className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full ${smtp.smtp_enabled ? "bg-emerald-600/10 dark:bg-emerald-400/10 text-emerald-600 dark:text-emerald-400" : "bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400"}`}>
                {smtp.smtp_enabled ? "Enabled" : "Disabled"}
              </span>
            )}
          </div>

          {smtpLoading ? (
            <p className="text-xs text-slate-400 dark:text-slate-500">Loading…</p>
          ) : !canManage ? (
            <p className="text-xs text-slate-500 dark:text-slate-400">
              {smtp?.smtp_enabled ? `Email alerts are enabled, sending to ${smtp.recipients || "no recipients configured"}.` : "Email alerts aren't configured. A network admin can set this up."}
            </p>
          ) : (
            <form onSubmit={saveSmtp} className="flex flex-col gap-3">
              <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-400">
                <input type="checkbox" checked={smtpForm.smtp_enabled} onChange={(e) => setSmtpForm((f) => ({ ...f, smtp_enabled: e.target.checked }))} className="accent-blue-600 dark:accent-blue-400" />
                Enable email alerts
              </label>
              <div className="grid grid-cols-2 gap-2">
                <input placeholder="SMTP host" value={smtpForm.smtp_host} onChange={(e) => setSmtpForm((f) => ({ ...f, smtp_host: e.target.value }))} className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400" />
                <input placeholder="Port" value={smtpForm.smtp_port} onChange={(e) => setSmtpForm((f) => ({ ...f, smtp_port: e.target.value }))} className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400" />
              </div>
              <input placeholder="From address (e.g. netguard@yourco.com)" value={smtpForm.smtp_from_email} onChange={(e) => setSmtpForm((f) => ({ ...f, smtp_from_email: e.target.value }))} className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400" />
              <input placeholder="Recipients (comma-separated)" value={smtpForm.recipients} onChange={(e) => setSmtpForm((f) => ({ ...f, recipients: e.target.value }))} className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400" />
              <div className="grid grid-cols-2 gap-2">
                <input placeholder="SMTP username (optional)" value={smtpForm.smtp_username} onChange={(e) => setSmtpForm((f) => ({ ...f, smtp_username: e.target.value }))} className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400" />
                <input type="password" placeholder={smtp?.smtp_password_set ? "Password (unchanged)" : "Password (optional)"} value={smtpForm.smtp_password} onChange={(e) => setSmtpForm((f) => ({ ...f, smtp_password: e.target.value }))} className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400" />
              </div>
              <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-400">
                <input type="checkbox" checked={smtpForm.smtp_use_tls} onChange={(e) => setSmtpForm((f) => ({ ...f, smtp_use_tls: e.target.checked }))} className="accent-blue-600 dark:accent-blue-400" />
                Use STARTTLS
              </label>

              {smtpError && <p className="text-red-600 dark:text-red-400 text-xs">{smtpError}</p>}

              <div className="flex gap-2">
                <button type="submit" disabled={smtpSaving} className="bg-blue-600 dark:bg-blue-400 text-slate-50 dark:text-slate-950 rounded-md px-4 py-2 text-xs font-bold hover:brightness-110 transition disabled:opacity-50">
                  {smtpSaving ? "Saving…" : "Save"}
                </button>
                <button type="button" onClick={testSmtp} disabled={smtpTesting || !smtp?.smtp_enabled} className="border border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-400 rounded-md px-4 py-2 text-xs font-bold hover:border-blue-600 dark:hover:border-blue-400 hover:text-blue-600 dark:hover:text-blue-400 transition disabled:opacity-40">
                  {smtpTesting ? "Sending…" : "Send test email"}
                </button>
              </div>
            </form>
          )}
        </div>

        {/* --- Slack --- */}
        <div className="p-5">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-bold text-navy dark:text-white">Slack</h3>
            {canManage && (
              <button onClick={() => { setSlackForm({ name: "", url: "" }); setSlackError(null); setShowSlackForm(true); }} className="text-xs font-bold text-blue-600 dark:text-blue-400 hover:brightness-110">
                + Add webhook
              </button>
            )}
          </div>

          <p className="text-xs text-slate-500 dark:text-slate-400 mb-3 leading-relaxed">
            Posts alert notifications to a Slack channel via an{" "}
            <a href="https://api.slack.com/messaging/webhooks" target="_blank" rel="noreferrer" className="text-blue-600 dark:text-blue-400 hover:underline">incoming webhook</a>.
            This is separate from the ChatOps slash command above — that lets you run commands <em>from</em> Slack; this sends alerts <em>to</em> Slack.
          </p>

          {slackLoading ? (
            <p className="text-xs text-slate-400 dark:text-slate-500">Loading…</p>
          ) : slackWebhooks.length === 0 ? (
            <p className="text-xs text-slate-400 dark:text-slate-500">No Slack webhook configured yet.</p>
          ) : (
            <ul className="flex flex-col gap-2 mb-3">
              {slackWebhooks.map((wh) => (
                <li key={wh.id} className="flex items-center justify-between gap-2 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-md px-3 py-2">
                  <div className="min-w-0">
                    <p className="text-xs font-bold text-slate-900 dark:text-slate-100 truncate">{wh.name}</p>
                    <p className="text-[11px] text-slate-400 dark:text-slate-500 truncate font-mono">{wh.url}</p>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <span className={`text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${wh.enabled ? "text-emerald-600 dark:text-emerald-400" : "text-slate-400 dark:text-slate-500"}`}>{wh.enabled ? "On" : "Off"}</span>
                    <button onClick={() => testSlackWebhook(wh.id)} disabled={testingId === wh.id} className="text-[11px] font-bold text-blue-600 dark:text-blue-400 hover:brightness-110 disabled:opacity-50">
                      {testingId === wh.id ? "Sending…" : "Test"}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {slackWebhooks.length > 0 && (
            <p className="text-[11px] text-slate-400 dark:text-slate-500">
              Manage event filters, delivery logs, and retries from Alert Center → Webhooks.
            </p>
          )}

          {showSlackForm && (
            <form onSubmit={addSlackWebhook} className="mt-3 flex flex-col gap-2 border-t border-slate-200 dark:border-slate-700 pt-3">
              <input placeholder="Name (e.g. #network-alerts)" value={slackForm.name} onChange={(e) => setSlackForm((f) => ({ ...f, name: e.target.value }))} className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400" />
              <input placeholder="https://hooks.slack.com/services/…" value={slackForm.url} onChange={(e) => setSlackForm((f) => ({ ...f, url: e.target.value }))} className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400" />
              {slackError && <p className="text-red-600 dark:text-red-400 text-xs">{slackError}</p>}
              <div className="flex gap-2 justify-end">
                <button type="button" onClick={() => setShowSlackForm(false)} className="px-4 py-2 text-xs font-bold text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:text-slate-100">Cancel</button>
                <button type="submit" disabled={slackSaving || !slackForm.name || !slackForm.url} className="bg-blue-600 dark:bg-blue-400 text-slate-50 dark:text-slate-950 rounded-md px-4 py-2 text-xs font-bold hover:brightness-110 transition disabled:opacity-50">
                  {slackSaving ? "Adding…" : "Add"}
                </button>
              </div>
            </form>
          )}
        </div>
        
        {/* --- Push Notifications --- */}
        <PushNotificationsPanel />
        
      </div>
    </Panel>
  );
}

const emptySyslogDestForm = {
  name: "",
  host: "",
  port: "514",
  protocol: "udp" as "udp" | "tcp",
  facility: "16",
  min_severity: "info" as "info" | "warning" | "critical",
  use_rfc5424: false,
  enabled: true,
};

function RemoteSyslogSection({ canManage }: { canManage: boolean }) {
  const toast = useToast();
  const confirm = useConfirm();

  const [dests, setDests] = useState<SyslogDestination[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState(emptySyslogDestForm);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    api
      .get<SyslogDestination[]>("/syslog/destinations")
      .then((res) => setDests(res.data))
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const openAdd = () => {
    setEditingId(null);
    setForm(emptySyslogDestForm);
    setFormError(null);
    setShowForm(true);
  };

  const openEdit = (d: SyslogDestination) => {
    setEditingId(d.id);
    setForm({
      name: d.name,
      host: d.host,
      port: String(d.port),
      protocol: d.protocol,
      facility: String(d.facility),
      min_severity: d.min_severity,
      use_rfc5424: d.use_rfc5424,
      enabled: d.enabled,
    });
    setFormError(null);
    setShowForm(true);
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setFormError(null);
    const payload = {
      name: form.name,
      host: form.host,
      port: Number(form.port) || 514,
      protocol: form.protocol,
      facility: Number(form.facility) || 16,
      min_severity: form.min_severity,
      use_rfc5424: form.use_rfc5424,
      enabled: form.enabled,
    };
    try {
      if (editingId) {
        await api.patch(`/syslog/destinations/${editingId}`, payload);
        toast.success("Syslog destination updated.");
      } else {
        await api.post("/syslog/destinations", payload);
        toast.success("Remote syslog destination added — alert events will now forward there.");
      }
      setShowForm(false);
      load();
    } catch (err: any) {
      setFormError(err?.response?.data?.detail || "Failed to save syslog destination.");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (d: SyslogDestination) => {
    if (!(await confirm(`Remove syslog destination? Alert events will stop forwarding to ${d.name} (${d.host}:${d.port}).`, { confirmLabel: "Remove" }))) return;
    try {
      await api.delete(`/syslog/destinations/${d.id}`);
      toast.success("Syslog destination removed.");
      load();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to remove syslog destination.");
    }
  };

  const test = async (d: SyslogDestination) => {
    setTestingId(d.id);
    try {
      await api.post(`/syslog/destinations/${d.id}/test`);
      toast.success(`Test message sent to ${d.name}.`);
      load();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Test message failed to send.");
      load();
    } finally {
      setTestingId(null);
    }
  };

  return (
    <Panel>
      <div className="p-5 flex items-start justify-between gap-3 border-b border-slate-200 dark:border-slate-700">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-lg bg-blue-600/10 dark:bg-blue-400/10 border border-blue-600/25 dark:border-blue-400/25 text-blue-600 dark:text-blue-400 flex items-center justify-center shrink-0">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <rect x="2" y="4" width="20" height="16" rx="2" /><path d="M2 8h20M6 12h.01M6 16h.01" />
            </svg>
          </div>
          <div>
            <h2 className="text-lg font-bold text-navy dark:text-white">Remote Syslog Forwarding</h2>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-1 max-w-2xl leading-relaxed">
              Forward every NetGuard alert/event (same fan-out as email, Slack, Teams, and webhooks) to an
              external syslog collector — Splunk, Graylog, an rsyslog relay, or a SIEM ingest point — over
              UDP or TCP, in RFC 3164 or RFC 5424 framing.
            </p>
          </div>
        </div>
        {canManage && (
          <button onClick={openAdd} className="shrink-0 text-xs font-bold text-blue-600 dark:text-blue-400 hover:brightness-110">
            + Add destination
          </button>
        )}
      </div>

      <div className="p-5">
        {loading ? (
          <p className="text-xs text-slate-400 dark:text-slate-500">Loading…</p>
        ) : dests.length === 0 ? (
          <p className="text-xs text-slate-400 dark:text-slate-500">No remote syslog destinations configured yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-slate-400 dark:text-slate-500 uppercase tracking-wider text-[10px]">
                  <th className="pb-2 pr-3">Name</th>
                  <th className="pb-2 pr-3">Target</th>
                  <th className="pb-2 pr-3">Min severity</th>
                  <th className="pb-2 pr-3">Format</th>
                  <th className="pb-2 pr-3">Status</th>
                  <th className="pb-2 pr-3">Last sent</th>
                  <th className="pb-2"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {dests.map((d) => (
                  <tr key={d.id}>
                    <td className="py-2 pr-3 font-bold text-navy dark:text-white">{d.name}</td>
                    <td className="py-2 pr-3 font-mono text-slate-600 dark:text-slate-400">
                      {d.host}:{d.port} <span className="uppercase text-slate-400 dark:text-slate-500">({d.protocol})</span>
                    </td>
                    <td className="py-2 pr-3 capitalize text-slate-600 dark:text-slate-400">{d.min_severity}</td>
                    <td className="py-2 pr-3 text-slate-600 dark:text-slate-400">{d.use_rfc5424 ? "RFC 5424" : "RFC 3164"}</td>
                    <td className="py-2 pr-3">
                      {!d.enabled ? (
                        <span className="text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400">Disabled</span>
                      ) : d.last_error ? (
                        <span title={d.last_error} className="text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full bg-red-600/10 dark:bg-red-400/10 text-red-600 dark:text-red-400">Error</span>
                      ) : (
                        <span className="text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full bg-emerald-600/10 dark:bg-emerald-400/10 text-emerald-600 dark:text-emerald-400">Enabled</span>
                      )}
                    </td>
                    <td className="py-2 pr-3 text-slate-500 dark:text-slate-400">{d.last_sent_at ? new Date(d.last_sent_at).toLocaleString() : "—"}</td>
                    <td className="py-2 text-right whitespace-nowrap">
                      <button onClick={() => test(d)} disabled={testingId === d.id} className="text-blue-600 dark:text-blue-400 hover:brightness-110 font-bold mr-3 disabled:opacity-50">
                        {testingId === d.id ? "Testing…" : "Test"}
                      </button>
                      {canManage && (
                        <>
                          <button onClick={() => openEdit(d)} className="text-slate-500 dark:text-slate-400 hover:text-navy dark:hover:text-slate-100 font-bold mr-3">Edit</button>
                          <button onClick={() => remove(d)} className="text-red-600 dark:text-red-400 hover:brightness-110 font-bold">Remove</button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {showForm && canManage && (
          <form onSubmit={save} className="mt-4 border border-slate-200 dark:border-slate-700 rounded-lg p-4 flex flex-col gap-3 max-w-lg">
            <h3 className="text-sm font-bold text-navy dark:text-white">{editingId ? "Edit destination" : "Add remote syslog destination"}</h3>
            <input placeholder="Name (e.g. Splunk Prod)" value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400" />
            <div className="grid grid-cols-3 gap-2">
              <input placeholder="Host / IP" value={form.host} onChange={(e) => setForm((f) => ({ ...f, host: e.target.value }))} className="col-span-2 border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400" />
              <input placeholder="Port" value={form.port} onChange={(e) => setForm((f) => ({ ...f, port: e.target.value }))} className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400" />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <select value={form.protocol} onChange={(e) => setForm((f) => ({ ...f, protocol: e.target.value as "udp" | "tcp" }))} className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400">
                <option value="udp">UDP</option>
                <option value="tcp">TCP</option>
              </select>
              <select value={form.min_severity} onChange={(e) => setForm((f) => ({ ...f, min_severity: e.target.value as any }))} className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400">
                <option value="info">Min severity: Info</option>
                <option value="warning">Min severity: Warning</option>
                <option value="critical">Min severity: Critical</option>
              </select>
            </div>
            <div className="grid grid-cols-2 gap-2 items-center">
              <input placeholder="Facility (0-23, default 16)" value={form.facility} onChange={(e) => setForm((f) => ({ ...f, facility: e.target.value }))} className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400" />
              <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-400">
                <input type="checkbox" checked={form.use_rfc5424} onChange={(e) => setForm((f) => ({ ...f, use_rfc5424: e.target.checked }))} className="accent-blue-600 dark:accent-blue-400" />
                Use RFC 5424 framing
              </label>
            </div>
            <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-400">
              <input type="checkbox" checked={form.enabled} onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))} className="accent-blue-600 dark:accent-blue-400" />
              Enabled
            </label>

            {formError && <p className="text-red-600 dark:text-red-400 text-xs">{formError}</p>}

            <div className="flex gap-2">
              <button type="submit" disabled={saving || !form.name || !form.host} className="bg-blue-600 dark:bg-blue-400 text-slate-50 dark:text-slate-950 rounded-md px-4 py-2 text-xs font-bold hover:brightness-110 transition disabled:opacity-50">
                {saving ? "Saving…" : editingId ? "Save changes" : "Add destination"}
              </button>
              <button type="button" onClick={() => setShowForm(false)} className="border border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-400 rounded-md px-4 py-2 text-xs font-bold hover:border-blue-600 dark:hover:border-blue-400 hover:text-blue-600 dark:hover:text-blue-400 transition">
                Cancel
              </button>
            </div>
          </form>
        )}
      </div>
    </Panel>
  );
}

function GitOpsSection({ canManage }: { canManage: boolean }) {
  const confirm = useConfirm();
  const toast = useToast();
  const [repos, setRepos] = useState<GitRepoConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(emptyRepoForm);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [syncingId, setSyncingId] = useState<string | null>(null);
  // Editing an existing repo re-uses the same form, PATCHing instead of
  // POSTing. Access token / webhook secret fields start blank -- "leave
  // blank to keep the current value" rather than round-tripping a secret
  // the UI never actually has (has_access_token/has_webhook_secret are
  // just booleans from the API, never the plaintext).
  const [editingId, setEditingId] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    api
      .get<GitRepoConfig[]>("/gitops/repos")
      .then((res) => {
        setRepos(res.data);
        setError(null);
      })
      .catch((err) => setError(err?.response?.data?.detail || "Failed to load Git repo configs."))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const openCreateForm = () => {
    setEditingId(null);
    setForm(emptyRepoForm);
    setSaveError(null);
    setShowForm(true);
  };

  const openEditForm = (r: GitRepoConfig) => {
    setEditingId(r.id);
    setForm({
      name: r.name,
      repo_url: r.repo_url,
      branch: r.branch,
      template_path: r.template_path,
      direction: r.direction,
      auto_sync_enabled: r.auto_sync_enabled,
      access_token: "",
      webhook_secret: "",
    });
    setSaveError(null);
    setShowForm(true);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setSaveError(null);
    try {
      if (editingId) {
        // Only send credential fields if the operator actually typed
        // something new -- an empty string here would otherwise wipe out
        // a previously-saved token/secret on every unrelated edit (e.g.
        // just fixing a typo'd branch name).
        const payload: Record<string, unknown> = {
          name: form.name,
          repo_url: form.repo_url,
          branch: form.branch,
          template_path: form.template_path,
          direction: form.direction,
          auto_sync_enabled: form.auto_sync_enabled,
        };
        if (form.access_token) payload.access_token = form.access_token;
        if (form.webhook_secret) payload.webhook_secret = form.webhook_secret;
        await api.patch(`/gitops/repos/${editingId}`, payload);
        toast.success(`${form.name} updated.`);
      } else {
        await api.post("/gitops/repos", {
          ...form,
          access_token: form.access_token || null,
          webhook_secret: form.webhook_secret || null,
        });
      }
      setShowForm(false);
      setEditingId(null);
      setForm(emptyRepoForm);
      load();
    } catch (err: any) {
      setSaveError(err?.response?.data?.detail || `Failed to ${editingId ? "update" : "add"} repo.`);
    } finally {
      setSaving(false);
    }
  };

  const removeRepo = async (r: GitRepoConfig) => {
    if (!(await confirm(`Remove Git repo config '${r.name}'? This does not delete anything from the repo itself.`, { confirmLabel: "Remove" }))) return;
    try {
      await api.delete(`/gitops/repos/${r.id}`);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to remove repo.");
    }
  };

  const syncNow = async (r: GitRepoConfig) => {
    setSyncingId(r.id);
    try {
      const res = await api.post(`/gitops/repos/${r.id}/sync`);
      const { created, updated, unchanged, errors } = res.data;
      const summary = `${r.name}: +${created} created, ${updated} updated, ${unchanged} unchanged`;
      if (errors?.length) {
        toast.error(`${summary} — ${errors.length} error(s): ${errors.join("; ")}`);
      } else {
        toast.success(summary);
      }
      load();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || `Sync failed for ${r.name}.`);
    } finally {
      setSyncingId(null);
    }
  };

  return (
    <Panel>
      <div className="p-5 flex items-start justify-between gap-4 flex-wrap border-b border-slate-200 dark:border-slate-700">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-lg bg-blue-600/10 dark:bg-blue-400/10 border border-blue-600/25 dark:border-blue-400/25 text-blue-600 dark:text-blue-400 flex items-center justify-center shrink-0">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="18" cy="18" r="3" /><circle cx="6" cy="6" r="3" />
              <path d="M6 21V9a9 9 0 009 9" />
            </svg>
          </div>
          <div>
            <h2 className="text-lg font-bold text-navy dark:text-white">GitOps / Config-as-Code</h2>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-1 max-w-2xl leading-relaxed">
              Pull direction reads <code className="font-mono text-blue-600 dark:text-blue-400">*.j2</code> files under the template
              path and queues them as new template versions for review — never auto-published. Push direction
              mirrors published versions back out to the repo.
            </p>
          </div>
        </div>
        {canManage && (
          <button
            onClick={openCreateForm}
            className="bg-blue-600 dark:bg-blue-400 text-slate-50 dark:text-slate-950 rounded-md px-4 py-2 text-xs font-bold hover:brightness-110 transition shrink-0"
          >
            + Add Repo
          </button>
        )}
      </div>

      {error && <p className="text-red-600 dark:text-red-400 text-sm p-5">{error}</p>}

      {loading ? (
        <p className="text-xs text-slate-400 dark:text-slate-500 p-5">Loading repos…</p>
      ) : repos.length === 0 ? (
        <div className="p-8 text-center">
          <p className="text-sm text-slate-500 dark:text-slate-400">No Git repos configured yet.</p>
          <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">Add one to pull templates from a repo or mirror published versions out.</p>
        </div>
      ) : (
        <div className="divide-y divide-slate-200 dark:divide-slate-700">
          {repos.map((r) => (
            <div key={r.id} className="p-5 flex flex-col gap-3">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-2 flex-wrap">
                  <p className="font-bold text-navy dark:text-white text-sm">{r.name}</p>
                  <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 px-2 py-0.5 rounded-full">
                    {directionCopy[r.direction]}
                  </span>
                  <span className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full ${syncStatusStyle[r.last_sync_status] || syncStatusStyle.never_synced}`}>
                    {r.last_sync_status.replace("_", " ")}
                  </span>
                  {r.auto_sync_enabled && (
                    <span className="text-[10px] font-bold uppercase tracking-wider text-emerald-600 dark:text-emerald-400 bg-emerald-600/10 dark:bg-emerald-400/10 border border-emerald-600/25 dark:border-emerald-400/25 px-2 py-0.5 rounded-full">
                      Auto-sync
                    </span>
                  )}
                </div>
                {canManage && (
                  <div className="flex gap-4 shrink-0">
                    <button
                      onClick={() => syncNow(r)}
                      disabled={syncingId === r.id}
                      className="text-[10px] font-bold uppercase tracking-wider text-blue-600 dark:text-blue-400 hover:brightness-110 disabled:opacity-50 flex items-center gap-1.5"
                    >
                      {syncingId === r.id ? (
                        "Syncing…"
                      ) : (
                        <>
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M23 4v6h-6" /><path d="M1 20v-6h6" /><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" /></svg>
                          Sync Now
                        </>
                      )}
                    </button>
                    <button
                      onClick={() => openEditForm(r)}
                      className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 hover:text-blue-600 dark:hover:text-blue-400"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => removeRepo(r)}
                      className="text-[10px] font-bold uppercase tracking-wider text-red-600 dark:text-red-400 hover:brightness-125"
                    >
                      Remove
                    </button>
                  </div>
                )}
              </div>

              <p className="text-xs text-slate-500 dark:text-slate-400 font-mono">
                {r.repo_url} @ {r.branch} — {r.template_path}
              </p>

              {r.last_sync_error && (
                <p className="text-xs text-red-600 dark:text-red-400 bg-red-600/10 dark:bg-red-400/10 border border-red-600/25 dark:border-red-400/25 rounded-md px-3 py-2">{r.last_sync_error}</p>
              )}

              <div className="flex items-center justify-between flex-wrap gap-3 text-[11px] text-slate-400 dark:text-slate-500">
                <span>
                  {r.last_synced_at
                    ? `Last synced ${new Date(r.last_synced_at).toLocaleString()}${r.last_synced_commit ? ` @ ${r.last_synced_commit.slice(0, 8)}` : ""}`
                    : "Never synced"}
                </span>
                <span className="flex items-center gap-3">
                  <span className={r.has_access_token ? "text-emerald-600 dark:text-emerald-400" : "text-slate-400 dark:text-slate-500"}>
                    {r.has_access_token ? "✓" : "–"} Access token
                  </span>
                  <span className={r.has_webhook_secret ? "text-emerald-600 dark:text-emerald-400" : "text-slate-400 dark:text-slate-500"}>
                    {r.has_webhook_secret ? "✓" : "–"} Webhook secret
                  </span>
                </span>
              </div>

              {(r.direction === "push" || r.direction === "bidirectional") && (
                <div className="pt-1">
                  <CopyField label="Push webhook URL (point the repo's push webhook here)" value={`${API_BASE}/gitops/webhook/${r.id}`} />
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {showForm && (
        <form onSubmit={submit} className="p-5 border-t border-slate-200 dark:border-slate-700 flex flex-col gap-3 bg-slate-50/60 dark:bg-slate-800/60">
          <p className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
            {editingId ? "Edit repo" : "New repo"}
          </p>
          {saveError && <p className="text-red-600 dark:text-red-400 text-xs">{saveError}</p>}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <input
              required
              placeholder="Name (e.g. network-configs)"
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
            />
            <input
              required
              placeholder="Repo URL (https://github.com/org/repo.git)"
              value={form.repo_url}
              onChange={(e) => setForm((f) => ({ ...f, repo_url: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
            />
            <input
              placeholder="Branch"
              value={form.branch}
              onChange={(e) => setForm((f) => ({ ...f, branch: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
            />
            <input
              placeholder="Template path (templates/)"
              value={form.template_path}
              onChange={(e) => setForm((f) => ({ ...f, template_path: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
            />
            <select
              value={form.direction}
              onChange={(e) => setForm((f) => ({ ...f, direction: e.target.value as GitRepoConfig["direction"] }))}
              className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
            >
              <option value="pull">Pull (repo → NetGuard review queue)</option>
              <option value="push">Push (NetGuard → repo mirror)</option>
              <option value="bidirectional">Bidirectional</option>
            </select>
            <label className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400">
              <input
                type="checkbox"
                checked={form.auto_sync_enabled}
                onChange={(e) => setForm((f) => ({ ...f, auto_sync_enabled: e.target.checked }))}
                className="accent-blue-600 dark:accent-blue-400"
              />
              Auto-sync (periodic safety-net pull)
            </label>
            <div>
              <input
                type="password"
                placeholder={editingId ? "New access token (leave blank to keep current)" : "Access token (optional for public read-only repos)"}
                value={form.access_token}
                onChange={(e) => setForm((f) => ({ ...f, access_token: e.target.value }))}
                className="w-full border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
              />
              {editingId && (
                <p className="text-[11px] text-slate-400 dark:text-slate-500 mt-1">
                  Currently {repos.find((r) => r.id === editingId)?.has_access_token ? "set" : "not set"} — leave blank to keep it unchanged.
                </p>
              )}
            </div>
            <div>
              <input
                type="password"
                placeholder={editingId ? "New webhook secret (leave blank to keep current)" : "Webhook secret (needed for push-triggered sync)"}
                value={form.webhook_secret}
                onChange={(e) => setForm((f) => ({ ...f, webhook_secret: e.target.value }))}
                className="w-full border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
              />
              {editingId && (
                <p className="text-[11px] text-slate-400 dark:text-slate-500 mt-1">
                  Currently {repos.find((r) => r.id === editingId)?.has_webhook_secret ? "set" : "not set"} — leave blank to keep it unchanged.
                </p>
              )}
            </div>
          </div>
          <div className="flex gap-2 justify-end">
            <button
              type="button"
              onClick={() => {
                setShowForm(false);
                setEditingId(null);
              }}
              className="px-4 py-2 text-xs font-bold text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="bg-blue-600 dark:bg-blue-400 text-slate-50 dark:text-slate-950 rounded-md px-5 py-2 text-xs font-bold hover:brightness-110 transition disabled:opacity-50"
            >
              {saving ? (editingId ? "Saving…" : "Adding…") : editingId ? "Save changes" : "Add Repo"}
            </button>
          </div>
        </form>
      )}
    </Panel>
  );
}

// =========================== Email Digests ==================================
// Surfaces app.api.tenant_digest -- a full per-tenant scheduled digest
// (Alert/Incident/AuditLog rollup, daily or weekly, its own recipient
// list and severity floor) that previously had no UI at all despite the
// backend, model, and dispatcher (app.services.tenant_digest_service)
// being fully built. A scoped (non-MSP) admin manages exactly one
// subscription for their own tenant; MSP staff can create/manage one per
// tenant they oversee.

function DigestsSection({ canManage }: { canManage: boolean }) {
  const { user } = useAuth();
  const toast = useToast();
  const confirm = useConfirm();

  const [subs, setSubs] = useState<DigestSubscription[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // MSP staff can target any tenant, so they get a picker; a scoped
  // admin's own tenant is inferred server-side (see
  // TenantDigestSubscriptionCreate.tenant_id), so the form never even
  // shows the field for them.
  const [tenants, setTenants] = useState<TenantOption[]>([]);

  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState(emptyDigestForm);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [sendingId, setSendingId] = useState<string | null>(null);
  const [togglingId, setTogglingId] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    api
      .get<DigestSubscription[]>("/tenant-digest-subscriptions")
      .then((res) => {
        setSubs(res.data);
        setError(null);
      })
      .catch((err) => setError(err?.response?.data?.detail || "Failed to load digest subscriptions."))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  useEffect(() => {
    if (!user?.is_msp_staff) return;
    api
      .get<TenantOption[]>("/tenants")
      .then((res) => setTenants(res.data))
      .catch(() => {});
  }, [user?.is_msp_staff]);

  const tenantName = (id: string) => tenants.find((t) => t.id === id)?.name || id.slice(0, 8);

  const openCreateForm = () => {
    setEditingId(null);
    setForm({ ...emptyDigestForm, tenant_id: tenants[0]?.id || "" });
    setSaveError(null);
    setShowForm(true);
  };

  const openEditForm = (s: DigestSubscription) => {
    setEditingId(s.id);
    setForm({
      tenant_id: s.tenant_id,
      cadence: s.cadence,
      hour_utc: s.hour_utc,
      day_of_week: s.day_of_week ?? 0,
      recipients: s.recipients,
      severity_floor: s.severity_floor,
      is_active: s.is_active,
    });
    setSaveError(null);
    setShowForm(true);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setSaveError(null);
    try {
      const payload: Record<string, unknown> = {
        cadence: form.cadence,
        hour_utc: form.hour_utc,
        day_of_week: form.cadence === "weekly" ? form.day_of_week : null,
        recipients: form.recipients,
        severity_floor: form.severity_floor,
        is_active: form.is_active,
      };
      if (editingId) {
        await api.put(`/tenant-digest-subscriptions/${editingId}`, payload);
        toast.success("Digest subscription updated.");
      } else {
        if (user?.is_msp_staff) payload.tenant_id = form.tenant_id;
        await api.post("/tenant-digest-subscriptions", payload);
        toast.success("Digest subscription created.");
      }
      setShowForm(false);
      setEditingId(null);
      setForm(emptyDigestForm);
      load();
    } catch (err: any) {
      setSaveError(err?.response?.data?.detail || `Failed to ${editingId ? "update" : "create"} digest subscription.`);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (s: DigestSubscription) => {
    if (!(await confirm(`Remove this digest subscription? ${s.recipients} will stop receiving it.`, { confirmLabel: "Remove" }))) return;
    try {
      await api.delete(`/tenant-digest-subscriptions/${s.id}`);
      toast.success("Digest subscription removed.");
      load();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to remove digest subscription.");
    }
  };

  const toggleActive = async (s: DigestSubscription) => {
    setTogglingId(s.id);
    try {
      await api.put(`/tenant-digest-subscriptions/${s.id}`, { is_active: !s.is_active });
      load();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to update digest subscription.");
    } finally {
      setTogglingId(null);
    }
  };

  const sendNow = async (s: DigestSubscription) => {
    setSendingId(s.id);
    try {
      await api.post(`/tenant-digest-subscriptions/${s.id}/send-now`);
      toast.success(`Digest sent to ${s.recipients}.`);
      load();
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Failed to send digest.");
    } finally {
      setSendingId(null);
    }
  };

  const cadenceLabel = (s: DigestSubscription) =>
    s.cadence === "weekly"
      ? `Weekly, ${DAY_NAMES[s.day_of_week ?? 0]} ${String(s.hour_utc).padStart(2, "0")}:00 UTC`
      : `Daily, ${String(s.hour_utc).padStart(2, "0")}:00 UTC`;

  return (
    <Panel>
      <div className="p-5 flex items-start justify-between gap-4 flex-wrap border-b border-slate-200 dark:border-slate-700">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-lg bg-amber-600/10 dark:bg-amber-400/10 border border-amber-600/25 dark:border-amber-400/25 text-amber-600 dark:text-amber-400 flex items-center justify-center shrink-0">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 8l9 6 9-6" />
            </svg>
          </div>
          <div>
            <h2 className="text-lg font-bold text-navy dark:text-white">Email Digests</h2>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-1 max-w-2xl leading-relaxed">
              A recurring Alert / Incident / Audit Log rollup, emailed on its own daily or weekly schedule —
              the antidote to real-time email for low-priority noise. Set a severity floor and anything below
              it stops paging live and only shows up here instead; in-app, Slack, Teams, and webhook delivery
              stay real-time regardless.
            </p>
          </div>
        </div>
        {canManage && (
          <button
            onClick={openCreateForm}
            disabled={user?.is_msp_staff && tenants.length === 0}
            className="bg-blue-600 dark:bg-blue-400 text-slate-50 dark:text-slate-950 rounded-md px-4 py-2 text-xs font-bold hover:brightness-110 transition shrink-0 disabled:opacity-50"
          >
            + Add Digest
          </button>
        )}
      </div>

      {error && <p className="text-red-600 dark:text-red-400 text-sm p-5">{error}</p>}

      {loading ? (
        <p className="text-xs text-slate-400 dark:text-slate-500 p-5">Loading digest subscriptions…</p>
      ) : subs.length === 0 ? (
        <div className="p-8 text-center">
          <p className="text-sm text-slate-500 dark:text-slate-400">No email digests configured yet.</p>
          <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">
            Add one to get a scheduled rollup instead of chasing every alert in real time.
          </p>
        </div>
      ) : (
        <div className="divide-y divide-slate-200 dark:divide-slate-700">
          {subs.map((s) => (
            <div key={s.id} className="p-5 flex flex-col gap-2">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-2 flex-wrap">
                  {user?.is_msp_staff && (
                    <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 px-2 py-0.5 rounded-full">
                      {tenantName(s.tenant_id)}
                    </span>
                  )}
                  <span className="text-sm font-bold text-navy dark:text-white">{cadenceLabel(s)}</span>
                  <span
                    className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full ${
                      s.is_active
                        ? "bg-emerald-600/10 dark:bg-emerald-400/10 text-emerald-600 dark:text-emerald-400 border border-emerald-600/25 dark:border-emerald-400/25"
                        : "bg-slate-50 dark:bg-slate-800 text-slate-500 dark:text-slate-400 border border-slate-200 dark:border-slate-700"
                    }`}
                  >
                    {s.is_active ? "Active" : "Paused"}
                  </span>
                </div>
                {canManage && (
                  <div className="flex gap-4 shrink-0">
                    <button
                      onClick={() => sendNow(s)}
                      disabled={sendingId === s.id}
                      className="text-[10px] font-bold uppercase tracking-wider text-blue-600 dark:text-blue-400 hover:brightness-110 disabled:opacity-50"
                    >
                      {sendingId === s.id ? "Sending…" : "Send Now"}
                    </button>
                    <button
                      onClick={() => toggleActive(s)}
                      disabled={togglingId === s.id}
                      className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 hover:text-blue-600 dark:hover:text-blue-400 disabled:opacity-50"
                    >
                      {s.is_active ? "Pause" : "Resume"}
                    </button>
                    <button
                      onClick={() => openEditForm(s)}
                      className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 hover:text-blue-600 dark:hover:text-blue-400"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => remove(s)}
                      className="text-[10px] font-bold uppercase tracking-wider text-red-600 dark:text-red-400 hover:brightness-125"
                    >
                      Remove
                    </button>
                  </div>
                )}
              </div>
              <p className="text-xs text-slate-500 dark:text-slate-400 font-mono">{s.recipients}</p>
              <div className="flex items-center justify-between flex-wrap gap-3 text-[11px] text-slate-400 dark:text-slate-500">
                <span>{severityFloorCopy[s.severity_floor]}</span>
                <span>{s.last_sent_at ? `Last sent ${new Date(s.last_sent_at).toLocaleString()}` : "Never sent"}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {showForm && (
        <form onSubmit={submit} className="p-5 border-t border-slate-200 dark:border-slate-700 flex flex-col gap-3 bg-slate-50/60 dark:bg-slate-800/60">
          <p className="text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
            {editingId ? "Edit digest" : "New digest"}
          </p>
          {saveError && <p className="text-red-600 dark:text-red-400 text-xs">{saveError}</p>}

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {!editingId && user?.is_msp_staff && (
              <select
                value={form.tenant_id}
                onChange={(e) => setForm((f) => ({ ...f, tenant_id: e.target.value }))}
                className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
              >
                {tenants.map((t) => (
                  <option key={t.id} value={t.id}>{t.name}</option>
                ))}
              </select>
            )}
            <input
              required
              type="text"
              placeholder="Recipients (comma-separated emails)"
              value={form.recipients}
              onChange={(e) => setForm((f) => ({ ...f, recipients: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 placeholder:text-slate-400 dark:text-slate-500 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400 sm:col-span-2"
            />
            <select
              value={form.cadence}
              onChange={(e) => setForm((f) => ({ ...f, cadence: e.target.value as DigestSubscription["cadence"] }))}
              className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
            >
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
            </select>
            {form.cadence === "weekly" && (
              <select
                value={form.day_of_week}
                onChange={(e) => setForm((f) => ({ ...f, day_of_week: Number(e.target.value) }))}
                className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
              >
                {DAY_NAMES.map((d, i) => (
                  <option key={d} value={i}>{d}</option>
                ))}
              </select>
            )}
            <select
              value={form.hour_utc}
              onChange={(e) => setForm((f) => ({ ...f, hour_utc: Number(e.target.value) }))}
              className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400"
            >
              {Array.from({ length: 24 }, (_, h) => (
                <option key={h} value={h}>{String(h).padStart(2, "0")}:00 UTC</option>
              ))}
            </select>
            <select
              value={form.severity_floor}
              onChange={(e) => setForm((f) => ({ ...f, severity_floor: e.target.value as DigestSubscription["severity_floor"] }))}
              className="border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 text-slate-900 dark:text-slate-100 rounded-md px-3 py-2 text-sm outline-none focus:border-blue-600 dark:border-blue-400 sm:col-span-2"
            >
              <option value="all">No live suppression — digest is a rollup only</option>
              <option value="warning">Suppress warning/info from live email</option>
              <option value="critical">Suppress everything from live email</option>
            </select>
            <label className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400 sm:col-span-2">
              <input
                type="checkbox"
                checked={form.is_active}
                onChange={(e) => setForm((f) => ({ ...f, is_active: e.target.checked }))}
                className="accent-blue-600 dark:accent-blue-400"
              />
              Active
            </label>
          </div>

          <div className="flex gap-2 justify-end">
            <button
              type="button"
              onClick={() => {
                setShowForm(false);
                setEditingId(null);
              }}
              className="px-4 py-2 text-xs font-bold text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving || !form.recipients || (!editingId && !!user?.is_msp_staff && !form.tenant_id)}
              className="bg-blue-600 dark:bg-blue-400 text-slate-50 dark:text-slate-950 rounded-md px-5 py-2 text-xs font-bold hover:brightness-110 transition disabled:opacity-50"
            >
              {saving ? (editingId ? "Saving…" : "Adding…") : editingId ? "Save changes" : "Add Digest"}
            </button>
          </div>
        </form>
      )}
    </Panel>
  );
}

type Provider = "ntfy" | "pushover" | "browser";

interface FormState {
  label: string;
  provider: Provider;
  target: string;
  include_non_critical: boolean;
  include_actions: string[];
}

const EMPTY_FORM: FormState = {
  label: "My Phone",
  provider: "ntfy",
  target: "",
  include_non_critical: false,
  include_actions: [],
};

function targetPlaceholder(provider: Provider): string {
  if (provider === "browser") return "Browser WebPush (Not Editable)";
  return provider === "ntfy" ? "https://ntfy.sh/your-private-topic" : "Pushover user key";
}

function targetHint(provider: Provider): string {
  if (provider === "browser") return "Browser push token automatically populated by the browser.";
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



function PushNotificationsPanel() {

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
  const [testingForm, setTestingForm] = useState(false);
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
      include_actions: sub.include_actions || [],
    });
    setFormError(null);
    setShowForm(true);
  };

  const closeForm = () => {
    setShowForm(false);
    setEditingId(null);
    setFormError(null);
  };

  const toggleFormAction = (action: string) => {
    setForm((f) => ({
      ...f,
      include_actions: f.include_actions.includes(action)
        ? f.include_actions.filter((a) => a !== action)
        : [...f.include_actions, action],
    }));
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

  const handleTestForm = async () => {
    setFormError(null);
    setTestingForm(true);
    try {
      const res = await api.post<{ sent: boolean; message: string }>("/push-subscriptions/test-target", {
        provider: form.provider,
        target: form.target,
      });
      if (res.data.sent) {
        toast.success(res.data.message);
      } else {
        toast.error(res.data.message);
      }
    } catch (err) {
      setFormError(extractErrorMessage(err, "Failed to send test push."));
    } finally {
      setTestingForm(false);
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
    <div className="p-5 border-t border-slate-200 dark:border-slate-700">
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
                  {sub.include_actions && sub.include_actions.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-1.5">
                      {sub.include_actions.map((a) => (
                        <span key={a} className="text-[10px] font-bold uppercase tracking-wider text-brandblue bg-brandblue/10 px-2 py-0.5 rounded-full">
                          {a === "run_runbook" ? "Run Runbook" : a === "acknowledge" ? "Acknowledge" : "Escalate"}
                        </span>
                      ))}
                    </div>
                  )}
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

              {form.provider === "ntfy" && (
                <div>
                  <label className="block text-xs font-medium text-slate-500 dark:text-slate-400 mb-1.5">
                    Action buttons on the push itself
                  </label>
                  <div className="flex flex-wrap gap-2">
                    {[
                      { id: "acknowledge", label: "Acknowledge" },
                      { id: "escalate", label: "Escalate" },
                      { id: "run_runbook", label: "Run Runbook" },
                    ].map((a) => (
                      <label
                        key={a.id}
                        className={`px-2.5 py-1.5 rounded-lg text-xs font-medium border cursor-pointer transition-colors ${
                          form.include_actions.includes(a.id)
                            ? "border-brandblue bg-brandblue/10 text-brandblue"
                            : "border-slate-200 dark:border-noc-borderlit text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-white/5"
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={form.include_actions.includes(a.id)}
                          onChange={() => toggleFormAction(a.id)}
                          className="sr-only"
                        />
                        {a.label}
                      </label>
                    ))}
                  </div>
                  <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">
                    Adds up to 3 tap targets under the notification (ntfy's native action buttons) that open NetGuard straight to that action — not yet supported on Pushover or browser push.
                  </p>
                </div>
              )}

              {formError && <p className="text-sm text-red-600 dark:text-red-400">{formError}</p>}

              <div className="flex justify-between items-center gap-2 pt-2">
                {form.provider !== "browser" ? (
                  <button
                    type="button"
                    onClick={handleTestForm}
                    disabled={testingForm || !form.target}
                    className="px-3 py-2 rounded-lg text-sm font-medium text-brandblue border border-brandblue/30 hover:bg-brandblue/5 disabled:opacity-50 transition-colors"
                  >
                    {testingForm ? "Sending…" : "Send test"}
                  </button>
                ) : (
                  <span />
                )}
                <div className="flex gap-2">
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
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}