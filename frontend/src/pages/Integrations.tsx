import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useConfirm } from "../lib/confirm";
import { useToast } from "../lib/toast";

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

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1").replace(/\/$/, "");

const syncStatusStyle: Record<string, string> = {
  never_synced: "bg-noc-panel2 text-noc-muted border border-noc-border",
  syncing: "bg-noc-cyan/10 text-noc-cyan border border-noc-cyan/30",
  succeeded: "bg-noc-good/10 text-noc-good border border-noc-good/30",
  failed: "bg-noc-crit/10 text-noc-crit border border-noc-crit/30",
};

const directionCopy: Record<GitRepoConfig["direction"], string> = {
  pull: "Repo → Review Queue",
  push: "NetGuard → Repo Mirror",
  bidirectional: "Bidirectional",
};

const QUICK_COMMANDS = ["help", "fleet", "alerts", "drift"];

function Panel({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <section className={`noc-panel rounded-lg overflow-hidden ${className}`}>{children}</section>;
}

function CopyField({ label, value }: { label: string; value: string }) {
  const toast = useToast();
  return (
    <div>
      <p className="noc-label text-[10px] text-noc-muted mb-1">{label}</p>
      <button
        type="button"
        onClick={() => {
          navigator.clipboard?.writeText(value);
          toast.success("Copied to clipboard.");
        }}
        className="w-full flex items-center gap-2 bg-noc-panel2 border border-noc-border rounded-md px-3 py-2 text-left group hover:border-noc-cyan/40 transition-colors"
      >
        <code className="flex-1 font-mono text-[11px] text-noc-text truncate">{value}</code>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="shrink-0 text-noc-muted group-hover:text-noc-cyan">
          <rect x="9" y="9" width="13" height="13" rx="2" />
          <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
        </svg>
      </button>
    </div>
  );
}

export default function IntegrationsPage() {
  const { user } = useAuth();
  const canManage = user?.role === "network_admin";

  return (
    <div className="pb-16 max-w-6xl mx-auto flex flex-col gap-8 pt-2">
      <div>
        <div className="flex items-center gap-2 mb-1">
          <span className="noc-live-dot inline-block w-1.5 h-1.5 rounded-full bg-noc-good" />
          <p className="noc-label text-[10px] text-noc-cyan">External Integrations</p>
        </div>
        <h1 className="text-3xl font-bold text-navy dark:text-white font-display tracking-wide">Integrations</h1>
        <p className="text-sm text-slate-500 dark:text-noc-muted mt-1 font-medium max-w-2xl">
          Two-way ChatOps — approve, reject, and roll back straight from Slack or Teams — plus Git-backed
          config-as-code sync for the template library.
        </p>
      </div>

      <ChatOpsSection canManage={canManage} />
      <GitOpsSection canManage={canManage} />
    </div>
  );
}

// =============================== ChatOps ===================================

function ChatOpsSection({ canManage }: { canManage: boolean }) {
  const confirm = useConfirm();
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
      <div className="p-5 flex items-start justify-between gap-4 flex-wrap border-b border-noc-border">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-lg bg-noc-violet/10 border border-noc-violet/25 text-noc-violet flex items-center justify-center shrink-0">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M8 9h8M8 13h5" /><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
            </svg>
          </div>
          <div>
            <h2 className="text-lg font-bold text-navy dark:text-noc-text">ChatOps</h2>
            <p className="text-xs text-slate-500 dark:text-noc-muted mt-1 max-w-2xl leading-relaxed">
              Linked users can run <code className="font-mono text-noc-cyan">/netguard approve &lt;id&gt;</code>,{" "}
              <code className="font-mono text-noc-cyan">reject</code>, <code className="font-mono text-noc-cyan">rollback</code>,{" "}
              <code className="font-mono text-noc-cyan">status &lt;hostname&gt;</code>,{" "}
              <code className="font-mono text-noc-cyan">alerts</code>, <code className="font-mono text-noc-cyan">fleet</code>,{" "}
              <code className="font-mono text-noc-cyan">drift</code>, and more from Slack or Teams.
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
            className="bg-noc-cyan text-noc-bg rounded-md px-4 py-2 text-xs font-bold hover:brightness-110 transition shrink-0"
          >
            + Link Account
          </button>
        )}
      </div>

      {/* Webhook target endpoints -- copy-to-clipboard so wiring up the
          Slack slash command / Teams outgoing webhook doesn't require
          reading source or docs. */}
      <div className="p-5 border-b border-noc-border grid grid-cols-1 sm:grid-cols-2 gap-3 bg-noc-panel2/40">
        <CopyField label="Slack slash command URL" value={`${API_BASE}/chatops/slack/commands`} />
        <CopyField label="Teams outgoing webhook URL" value={`${API_BASE}/chatops/teams/commands`} />
      </div>

      {error && <p className="text-noc-crit text-sm p-5">{error}</p>}

      {loading ? (
        <p className="text-xs text-noc-faint p-5">Loading links…</p>
      ) : links.length === 0 ? (
        <p className="text-xs text-noc-faint italic p-5">No Slack or Teams accounts linked yet.</p>
      ) : (
        <div className="divide-y divide-noc-border">
          {links.map((l) => (
            <div key={l.user_id} className="p-4 flex items-center justify-between gap-3 flex-wrap">
              <div>
                <p className="font-bold text-navy dark:text-noc-text text-sm">{l.full_name}</p>
                <p className="text-xs text-slate-500 dark:text-noc-muted">{l.user_email}</p>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                {l.slack_user_id && (
                  <span className="text-[11px] font-mono bg-noc-violet/10 border border-noc-violet/25 text-noc-violet px-2 py-1 rounded-full flex items-center gap-2">
                    Slack: {l.slack_user_id}
                    {canManage && (
                      <button onClick={() => unlink(l, "slack")} className="font-bold hover:text-noc-crit">
                        ×
                      </button>
                    )}
                  </span>
                )}
                {l.msteams_user_id && (
                  <span className="text-[11px] font-mono bg-noc-cyan/10 border border-noc-cyan/25 text-noc-cyan px-2 py-1 rounded-full flex items-center gap-2">
                    Teams: {l.msteams_user_id}
                    {canManage && (
                      <button onClick={() => unlink(l, "teams")} className="font-bold hover:text-noc-crit">
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
        <form onSubmit={submit} className="p-5 border-t border-noc-border flex flex-col gap-3 bg-noc-panel2/60">
          {saveError && <p className="text-noc-crit text-xs">{saveError}</p>}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <select
              value={form.platform}
              onChange={(e) => setForm((f) => ({ ...f, platform: e.target.value as "slack" | "teams" }))}
              className="border border-noc-border bg-noc-panel2 text-noc-text rounded-md px-3 py-2 text-sm outline-none focus:border-noc-cyan"
            >
              <option value="slack">Slack</option>
              <option value="teams">Microsoft Teams</option>
            </select>
            <input
              required
              placeholder="External user ID (e.g. Slack U0123ABC)"
              value={form.external_user_id}
              onChange={(e) => setForm((f) => ({ ...f, external_user_id: e.target.value }))}
              className="border border-noc-border bg-noc-panel2 text-noc-text placeholder:text-noc-faint rounded-md px-3 py-2 text-sm outline-none focus:border-noc-cyan"
            />
            <input
              required
              type="email"
              placeholder="NetGuard user email"
              value={form.user_email}
              onChange={(e) => setForm((f) => ({ ...f, user_email: e.target.value }))}
              className="border border-noc-border bg-noc-panel2 text-noc-text placeholder:text-noc-faint rounded-md px-3 py-2 text-sm outline-none focus:border-noc-cyan"
            />
          </div>
          <div className="flex gap-2 justify-end">
            <button type="button" onClick={() => setShowForm(false)} className="px-4 py-2 text-xs font-bold text-noc-muted hover:text-noc-text">
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="bg-noc-cyan text-noc-bg rounded-md px-5 py-2 text-xs font-bold hover:brightness-110 transition disabled:opacity-50"
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
    <div className="p-5 border-t border-noc-border bg-noc-bg/40">
      <div className="flex items-center gap-2 mb-1">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-noc-cyan">
          <path d="M4 17l6-6-6-6" /><path d="M12 19h8" />
        </svg>
        <p className="noc-label text-[10px] text-noc-cyan">Command Tester</p>
      </div>
      <p className="text-xs text-noc-muted mb-3">
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
        <div className="flex-1 flex items-center bg-noc-panel2 border border-noc-border rounded-md px-3 focus-within:border-noc-cyan">
          <span className="font-mono text-noc-cyan text-sm mr-1.5 select-none">/netguard</span>
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="fleet"
            className="flex-1 bg-transparent py-2.5 text-sm text-noc-text placeholder:text-noc-faint outline-none font-mono"
          />
        </div>
        <button
          type="submit"
          disabled={running}
          className="bg-noc-cyan text-noc-bg rounded-md px-5 py-2.5 text-sm font-bold hover:brightness-110 transition disabled:opacity-50 shrink-0"
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
            className="text-[11px] font-mono bg-noc-panel2 border border-noc-border text-noc-muted hover:text-noc-cyan hover:border-noc-cyan/40 rounded-full px-2.5 py-1 transition-colors"
          >
            {c}
          </button>
        ))}
      </div>

      {(result || err) && (
        <div
          className={`mt-3 rounded-md border px-4 py-3 font-mono text-xs whitespace-pre-wrap leading-relaxed ${
            err || result?.ok === false
              ? "border-noc-crit/30 bg-noc-crit/10 text-noc-crit"
              : "border-noc-good/25 bg-noc-good/5 text-noc-text"
          }`}
        >
          {err || result?.text}
          {!!result?.items?.length && (
            <div className="mt-2 pt-2 border-t border-noc-border/60 space-y-1">
              {result.items.map((it, i) => (
                <div key={i} className="flex gap-3 text-[11px] text-noc-muted">
                  {it.hostname && <span className="text-noc-text">{it.hostname}</span>}
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
      <div className="p-5 flex items-start justify-between gap-4 flex-wrap border-b border-noc-border">
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-lg bg-noc-cyan/10 border border-noc-cyan/25 text-noc-cyan flex items-center justify-center shrink-0">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="18" cy="18" r="3" /><circle cx="6" cy="6" r="3" />
              <path d="M6 21V9a9 9 0 009 9" />
            </svg>
          </div>
          <div>
            <h2 className="text-lg font-bold text-navy dark:text-noc-text">GitOps / Config-as-Code</h2>
            <p className="text-xs text-slate-500 dark:text-noc-muted mt-1 max-w-2xl leading-relaxed">
              Pull direction reads <code className="font-mono text-noc-cyan">*.j2</code> files under the template
              path and queues them as new template versions for review — never auto-published. Push direction
              mirrors published versions back out to the repo.
            </p>
          </div>
        </div>
        {canManage && (
          <button
            onClick={() => {
              setForm(emptyRepoForm);
              setSaveError(null);
              setShowForm(true);
            }}
            className="bg-noc-cyan text-noc-bg rounded-md px-4 py-2 text-xs font-bold hover:brightness-110 transition shrink-0"
          >
            + Add Repo
          </button>
        )}
      </div>

      {error && <p className="text-noc-crit text-sm p-5">{error}</p>}

      {loading ? (
        <p className="text-xs text-noc-faint p-5">Loading repos…</p>
      ) : repos.length === 0 ? (
        <div className="p-8 text-center">
          <p className="text-sm text-noc-muted">No Git repos configured yet.</p>
          <p className="text-xs text-noc-faint mt-1">Add one to pull templates from a repo or mirror published versions out.</p>
        </div>
      ) : (
        <div className="divide-y divide-noc-border">
          {repos.map((r) => (
            <div key={r.id} className="p-5 flex flex-col gap-3">
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-2 flex-wrap">
                  <p className="font-bold text-navy dark:text-noc-text text-sm">{r.name}</p>
                  <span className="text-[10px] font-bold uppercase tracking-wider text-noc-muted bg-noc-panel2 border border-noc-border px-2 py-0.5 rounded-full">
                    {directionCopy[r.direction]}
                  </span>
                  <span className={`text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full ${syncStatusStyle[r.last_sync_status] || syncStatusStyle.never_synced}`}>
                    {r.last_sync_status.replace("_", " ")}
                  </span>
                  {r.auto_sync_enabled && (
                    <span className="text-[10px] font-bold uppercase tracking-wider text-noc-good bg-noc-good/10 border border-noc-good/25 px-2 py-0.5 rounded-full">
                      Auto-sync
                    </span>
                  )}
                </div>
                {canManage && (
                  <div className="flex gap-4 shrink-0">
                    <button
                      onClick={() => syncNow(r)}
                      disabled={syncingId === r.id}
                      className="text-[10px] font-bold uppercase tracking-wider text-noc-cyan hover:brightness-110 disabled:opacity-50 flex items-center gap-1.5"
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
                      onClick={() => removeRepo(r)}
                      className="text-[10px] font-bold uppercase tracking-wider text-noc-crit hover:brightness-125"
                    >
                      Remove
                    </button>
                  </div>
                )}
              </div>

              <p className="text-xs text-noc-muted font-mono">
                {r.repo_url} @ {r.branch} — {r.template_path}
              </p>

              {r.last_sync_error && (
                <p className="text-xs text-noc-crit bg-noc-crit/10 border border-noc-crit/25 rounded-md px-3 py-2">{r.last_sync_error}</p>
              )}

              <div className="flex items-center justify-between flex-wrap gap-3 text-[11px] text-noc-faint">
                <span>
                  {r.last_synced_at
                    ? `Last synced ${new Date(r.last_synced_at).toLocaleString()}${r.last_synced_commit ? ` @ ${r.last_synced_commit.slice(0, 8)}` : ""}`
                    : "Never synced"}
                </span>
                <span className="flex items-center gap-3">
                  <span className={r.has_access_token ? "text-noc-good" : "text-noc-faint"}>
                    {r.has_access_token ? "✓" : "–"} Access token
                  </span>
                  <span className={r.has_webhook_secret ? "text-noc-good" : "text-noc-faint"}>
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
        <form onSubmit={submit} className="p-5 border-t border-noc-border flex flex-col gap-3 bg-noc-panel2/60">
          {saveError && <p className="text-noc-crit text-xs">{saveError}</p>}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <input
              required
              placeholder="Name (e.g. network-configs)"
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              className="border border-noc-border bg-noc-panel2 text-noc-text placeholder:text-noc-faint rounded-md px-3 py-2 text-sm outline-none focus:border-noc-cyan"
            />
            <input
              required
              placeholder="Repo URL (https://github.com/org/repo.git)"
              value={form.repo_url}
              onChange={(e) => setForm((f) => ({ ...f, repo_url: e.target.value }))}
              className="border border-noc-border bg-noc-panel2 text-noc-text placeholder:text-noc-faint rounded-md px-3 py-2 text-sm outline-none focus:border-noc-cyan"
            />
            <input
              placeholder="Branch"
              value={form.branch}
              onChange={(e) => setForm((f) => ({ ...f, branch: e.target.value }))}
              className="border border-noc-border bg-noc-panel2 text-noc-text placeholder:text-noc-faint rounded-md px-3 py-2 text-sm outline-none focus:border-noc-cyan"
            />
            <input
              placeholder="Template path (templates/)"
              value={form.template_path}
              onChange={(e) => setForm((f) => ({ ...f, template_path: e.target.value }))}
              className="border border-noc-border bg-noc-panel2 text-noc-text placeholder:text-noc-faint rounded-md px-3 py-2 text-sm outline-none focus:border-noc-cyan"
            />
            <select
              value={form.direction}
              onChange={(e) => setForm((f) => ({ ...f, direction: e.target.value as GitRepoConfig["direction"] }))}
              className="border border-noc-border bg-noc-panel2 text-noc-text rounded-md px-3 py-2 text-sm outline-none focus:border-noc-cyan"
            >
              <option value="pull">Pull (repo → NetGuard review queue)</option>
              <option value="push">Push (NetGuard → repo mirror)</option>
              <option value="bidirectional">Bidirectional</option>
            </select>
            <label className="flex items-center gap-2 text-sm text-noc-muted">
              <input
                type="checkbox"
                checked={form.auto_sync_enabled}
                onChange={(e) => setForm((f) => ({ ...f, auto_sync_enabled: e.target.checked }))}
                className="accent-noc-cyan"
              />
              Auto-sync (periodic safety-net pull)
            </label>
            <input
              type="password"
              placeholder="Access token (optional for public read-only repos)"
              value={form.access_token}
              onChange={(e) => setForm((f) => ({ ...f, access_token: e.target.value }))}
              className="border border-noc-border bg-noc-panel2 text-noc-text placeholder:text-noc-faint rounded-md px-3 py-2 text-sm outline-none focus:border-noc-cyan"
            />
            <input
              type="password"
              placeholder="Webhook secret (needed for push-triggered sync)"
              value={form.webhook_secret}
              onChange={(e) => setForm((f) => ({ ...f, webhook_secret: e.target.value }))}
              className="border border-noc-border bg-noc-panel2 text-noc-text placeholder:text-noc-faint rounded-md px-3 py-2 text-sm outline-none focus:border-noc-cyan"
            />
          </div>
          <div className="flex gap-2 justify-end">
            <button type="button" onClick={() => setShowForm(false)} className="px-4 py-2 text-xs font-bold text-noc-muted hover:text-noc-text">
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="bg-noc-cyan text-noc-bg rounded-md px-5 py-2 text-xs font-bold hover:brightness-110 transition disabled:opacity-50"
            >
              {saving ? "Adding…" : "Add Repo"}
            </button>
          </div>
        </form>
      )}
    </Panel>
  );
}