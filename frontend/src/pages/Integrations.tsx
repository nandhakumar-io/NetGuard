import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";

// --- Types (kept local -- these two features are small enough not to
// warrant new entries in lib/types.ts's shared type set) --------------

type ChatOpsLink = {
  user_id: string;
  user_email: string;
  full_name: string;
  slack_user_id: string | null;
  msteams_user_id: string | null;
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

const syncStatusStyle: Record<string, string> = {
  never_synced: "bg-slate-100 text-slate-600",
  syncing: "bg-blue-100 text-blue-700",
  succeeded: "bg-emerald-100 text-emerald-700",
  failed: "bg-red-100 text-red-700",
};

export default function IntegrationsPage() {
  const { user } = useAuth();
  const canManage = user?.role === "network_admin";

  return (
    <div className="pb-16 max-w-6xl mx-auto flex flex-col gap-8 pt-2">
      <div>
        <h1 className="text-3xl font-bold text-navy dark:text-white">Integrations</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1 font-medium">
          Two-way ChatOps (approve, reject, and roll back straight from Slack/Teams) and Git-backed config-as-code
          sync for the template library.
        </p>
      </div>

      <ChatOpsSection canManage={canManage} />
      <GitOpsSection canManage={canManage} />
    </div>
  );
}

function ChatOpsSection({ canManage }: { canManage: boolean }) {
  const [links, setLinks] = useState<ChatOpsLink[]>([]);
  const [loading, setLoading] = useState(true);
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

  useEffect(load, []);

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
    if (!window.confirm(`Unlink ${platform} from ${link.user_email}?`)) return;
    try {
      await api.delete(`/chatops/links/${link.user_id}`, { params: { platform } });
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to unlink account.");
    }
  };

  return (
    <section className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl overflow-hidden">
      <div className="p-5 flex items-start justify-between gap-4 flex-wrap border-b border-slate-100 dark:border-slate-700">
        <div>
          <h2 className="text-lg font-bold text-navy dark:text-white">ChatOps</h2>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-1 max-w-2xl">
            Linked users can run <code className="font-mono">/netguard approve &lt;id&gt;</code>,{" "}
            <code className="font-mono">reject</code>, <code className="font-mono">rollback</code>, and{" "}
            <code className="font-mono">status &lt;hostname&gt;</code> from Slack or Teams. Set up the Slack slash
            command / Teams outgoing webhook to point at{" "}
            <code className="font-mono">/api/v1/chatops/slack/commands</code> and{" "}
            <code className="font-mono">/api/v1/chatops/teams/commands</code>, then link accounts here.
          </p>
        </div>
        {canManage && (
          <button
            onClick={() => {
              setForm(emptyLinkForm);
              setSaveError(null);
              setShowForm(true);
            }}
            className="bg-brandblue text-white rounded-full px-4 py-2 text-xs font-bold shadow-md hover:bg-navy transition-colors shrink-0"
          >
            + Link Account
          </button>
        )}
      </div>

      {error && <p className="text-riskcrit text-sm p-5">{error}</p>}

      {loading ? (
        <p className="text-xs text-slate-400 dark:text-slate-500 p-5">Loading links…</p>
      ) : links.length === 0 ? (
        <p className="text-xs text-slate-400 dark:text-slate-500 italic p-5">
          No Slack or Teams accounts linked yet.
        </p>
      ) : (
        <div className="divide-y divide-slate-100 dark:divide-slate-800">
          {links.map((l) => (
            <div key={l.user_id} className="p-4 flex items-center justify-between gap-3 flex-wrap">
              <div>
                <p className="font-bold text-navy dark:text-white text-sm">{l.full_name}</p>
                <p className="text-xs text-slate-500 dark:text-slate-400">{l.user_email}</p>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                {l.slack_user_id && (
                  <span className="text-[11px] font-mono bg-purple-50 dark:bg-purple-950/40 text-purple-700 dark:text-purple-300 px-2 py-1 rounded-full flex items-center gap-2">
                    Slack: {l.slack_user_id}
                    {canManage && (
                      <button onClick={() => unlink(l, "slack")} className="font-bold hover:text-red-600">
                        ×
                      </button>
                    )}
                  </span>
                )}
                {l.msteams_user_id && (
                  <span className="text-[11px] font-mono bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300 px-2 py-1 rounded-full flex items-center gap-2">
                    Teams: {l.msteams_user_id}
                    {canManage && (
                      <button onClick={() => unlink(l, "teams")} className="font-bold hover:text-red-600">
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
        <form onSubmit={submit} className="p-5 border-t border-slate-100 dark:border-slate-700 flex flex-col gap-3 bg-slate-50 dark:bg-slate-900/40">
          {saveError && <p className="text-riskcrit text-xs">{saveError}</p>}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <select
              value={form.platform}
              onChange={(e) => setForm((f) => ({ ...f, platform: e.target.value as "slack" | "teams" }))}
              className="border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 rounded-lg px-3 py-2 text-sm"
            >
              <option value="slack">Slack</option>
              <option value="teams">Microsoft Teams</option>
            </select>
            <input
              required
              placeholder="External user ID (e.g. Slack U0123ABC)"
              value={form.external_user_id}
              onChange={(e) => setForm((f) => ({ ...f, external_user_id: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 rounded-lg px-3 py-2 text-sm"
            />
            <input
              required
              type="email"
              placeholder="NetGuard user email"
              value={form.user_email}
              onChange={(e) => setForm((f) => ({ ...f, user_email: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 rounded-lg px-3 py-2 text-sm"
            />
          </div>
          <div className="flex gap-2 justify-end">
            <button type="button" onClick={() => setShowForm(false)} className="px-4 py-2 text-xs font-bold text-slate-500">
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="bg-brandblue text-white rounded-full px-5 py-2 text-xs font-bold shadow-md hover:bg-navy transition-colors disabled:opacity-50"
            >
              {saving ? "Linking…" : "Link Account"}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

function GitOpsSection({ canManage }: { canManage: boolean }) {
  const [repos, setRepos] = useState<GitRepoConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(emptyRepoForm);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [syncingId, setSyncingId] = useState<string | null>(null);
  const [syncResult, setSyncResult] = useState<string | null>(null);

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

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setSaveError(null);
    try {
      await api.post("/gitops/repos", {
        ...form,
        access_token: form.access_token || null,
        webhook_secret: form.webhook_secret || null,
      });
      setShowForm(false);
      setForm(emptyRepoForm);
      load();
    } catch (err: any) {
      setSaveError(err?.response?.data?.detail || "Failed to add repo.");
    } finally {
      setSaving(false);
    }
  };

  const removeRepo = async (r: GitRepoConfig) => {
    if (!window.confirm(`Remove Git repo config '${r.name}'? This does not delete anything from the repo itself.`)) return;
    try {
      await api.delete(`/gitops/repos/${r.id}`);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to remove repo.");
    }
  };

  const syncNow = async (r: GitRepoConfig) => {
    setSyncingId(r.id);
    setSyncResult(null);
    try {
      const res = await api.post(`/gitops/repos/${r.id}/sync`);
      const { created, updated, unchanged, errors } = res.data;
      setSyncResult(
        `${r.name}: +${created} created, ${updated} updated, ${unchanged} unchanged` +
          (errors?.length ? `, ${errors.length} error(s): ${errors.join("; ")}` : "")
      );
      load();
    } catch (err: any) {
      setSyncResult(err?.response?.data?.detail || `Sync failed for ${r.name}.`);
    } finally {
      setSyncingId(null);
    }
  };

  return (
    <section className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl overflow-hidden">
      <div className="p-5 flex items-start justify-between gap-4 flex-wrap border-b border-slate-100 dark:border-slate-700">
        <div>
          <h2 className="text-lg font-bold text-navy dark:text-white">GitOps / Config-as-Code</h2>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-1 max-w-2xl">
            Pull direction reads <code className="font-mono">*.j2</code> files under the template path and queues
            them as new template versions for review (never auto-published). Push direction mirrors published
            versions back out to the repo. Point the repo's push webhook at{" "}
            <code className="font-mono">/api/v1/gitops/webhook/&lt;repo id&gt;</code>.
          </p>
        </div>
        {canManage && (
          <button
            onClick={() => {
              setForm(emptyRepoForm);
              setSaveError(null);
              setShowForm(true);
            }}
            className="bg-brandblue text-white rounded-full px-4 py-2 text-xs font-bold shadow-md hover:bg-navy transition-colors shrink-0"
          >
            + Add Repo
          </button>
        )}
      </div>

      {error && <p className="text-riskcrit text-sm p-5">{error}</p>}
      {syncResult && <p className="text-xs text-slate-600 dark:text-slate-300 px-5 pt-4">{syncResult}</p>}

      {loading ? (
        <p className="text-xs text-slate-400 dark:text-slate-500 p-5">Loading repos…</p>
      ) : repos.length === 0 ? (
        <p className="text-xs text-slate-400 dark:text-slate-500 italic p-5">No Git repos configured yet.</p>
      ) : (
        <div className="divide-y divide-slate-100 dark:divide-slate-800">
          {repos.map((r) => (
            <div key={r.id} className="p-4 flex items-center justify-between gap-3 flex-wrap">
              <div>
                <div className="flex items-center gap-2 flex-wrap">
                  <p className="font-bold text-navy dark:text-white text-sm">{r.name}</p>
                  <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500 bg-slate-100 dark:bg-slate-700 dark:text-slate-300 px-2 py-0.5 rounded-full">
                    {r.direction}
                  </span>
                  <span className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full ${syncStatusStyle[r.last_sync_status] || syncStatusStyle.never_synced}`}>
                    {r.last_sync_status.replace("_", " ")}
                  </span>
                </div>
                <p className="text-xs text-slate-500 dark:text-slate-400 mt-1 font-mono">
                  {r.repo_url} @ {r.branch} — {r.template_path}
                </p>
                {r.last_sync_error && <p className="text-xs text-riskcrit mt-1">{r.last_sync_error}</p>}
                {r.last_synced_at && (
                  <p className="text-[11px] text-slate-400 dark:text-slate-500 mt-1">
                    Last synced {new Date(r.last_synced_at).toLocaleString()}
                    {r.last_synced_commit ? ` @ ${r.last_synced_commit.slice(0, 8)}` : ""}
                  </p>
                )}
              </div>
              {canManage && (
                <div className="flex gap-3 shrink-0">
                  <button
                    onClick={() => syncNow(r)}
                    disabled={syncingId === r.id}
                    className="text-[10px] font-bold uppercase tracking-wider text-brandblue hover:text-navy disabled:opacity-50"
                  >
                    {syncingId === r.id ? "Syncing…" : "Sync Now"}
                  </button>
                  <button
                    onClick={() => removeRepo(r)}
                    className="text-[10px] font-bold uppercase tracking-wider text-riskcrit hover:text-red-800"
                  >
                    Remove
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {showForm && (
        <form onSubmit={submit} className="p-5 border-t border-slate-100 dark:border-slate-700 flex flex-col gap-3 bg-slate-50 dark:bg-slate-900/40">
          {saveError && <p className="text-riskcrit text-xs">{saveError}</p>}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <input
              required
              placeholder="Name (e.g. network-configs)"
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 rounded-lg px-3 py-2 text-sm"
            />
            <input
              required
              placeholder="Repo URL (https://github.com/org/repo.git)"
              value={form.repo_url}
              onChange={(e) => setForm((f) => ({ ...f, repo_url: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 rounded-lg px-3 py-2 text-sm"
            />
            <input
              placeholder="Branch"
              value={form.branch}
              onChange={(e) => setForm((f) => ({ ...f, branch: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 rounded-lg px-3 py-2 text-sm"
            />
            <input
              placeholder="Template path (templates/)"
              value={form.template_path}
              onChange={(e) => setForm((f) => ({ ...f, template_path: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 rounded-lg px-3 py-2 text-sm"
            />
            <select
              value={form.direction}
              onChange={(e) => setForm((f) => ({ ...f, direction: e.target.value as GitRepoConfig["direction"] }))}
              className="border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 rounded-lg px-3 py-2 text-sm"
            >
              <option value="pull">Pull (repo → NetGuard review queue)</option>
              <option value="push">Push (NetGuard → repo mirror)</option>
              <option value="bidirectional">Bidirectional</option>
            </select>
            <label className="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
              <input
                type="checkbox"
                checked={form.auto_sync_enabled}
                onChange={(e) => setForm((f) => ({ ...f, auto_sync_enabled: e.target.checked }))}
              />
              Auto-sync (periodic safety-net pull)
            </label>
            <input
              type="password"
              placeholder="Access token (optional for public read-only repos)"
              value={form.access_token}
              onChange={(e) => setForm((f) => ({ ...f, access_token: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 rounded-lg px-3 py-2 text-sm"
            />
            <input
              type="password"
              placeholder="Webhook secret (needed for push-triggered sync)"
              value={form.webhook_secret}
              onChange={(e) => setForm((f) => ({ ...f, webhook_secret: e.target.value }))}
              className="border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 rounded-lg px-3 py-2 text-sm"
            />
          </div>
          <div className="flex gap-2 justify-end">
            <button type="button" onClick={() => setShowForm(false)} className="px-4 py-2 text-xs font-bold text-slate-500">
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="bg-brandblue text-white rounded-full px-5 py-2 text-xs font-bold shadow-md hover:bg-navy transition-colors disabled:opacity-50"
            >
              {saving ? "Adding…" : "Add Repo"}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}