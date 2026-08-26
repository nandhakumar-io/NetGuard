import { useState, useEffect } from "react";
import { useAuth } from "../lib/auth";
import { api } from "../lib/api";

interface Tenant {
  id: string;
  name: string;
  slug: string;
}

interface DeviceGroup {
  id: string;
  name: string;
}

type Format = "pdf" | "csv";
type Period = 7 | 30;

type ReportKind = "uptime-incident" | "compliance";

interface Card {
  id: ReportKind;
  title: string;
  description: string;
  supportsScope: boolean;
  icon: JSX.Element;
}

const iconProps = { width: 20, height: 20, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8 } as const;

const CARDS: Card[] = [
  {
    id: "uptime-incident",
    title: "Uptime & Incident Report",
    description: "Device availability percentages, outage counts, MTTA/MTTR, and a full incident list for the selected window.",
    supportsScope: true,
    icon: <svg {...iconProps}><path d="M3 3v18h18" /><path d="M7 16l4-7 3 4 3-5 3 3" /></svg>,
  },
  {
    id: "compliance",
    title: "Compliance Report",
    description: "Baseline drift summary, configuration compliance scores, and remediation tracking across your fleet.",
    supportsScope: false,
    icon: <svg {...iconProps}><path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" /></svg>,
  },
];

export default function Reports() {
  const { user } = useAuth();

  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [groups, setGroups] = useState<DeviceGroup[]>([]);

  // per-card state
  const [format, setFormat] = useState<Record<ReportKind, Format>>({ "uptime-incident": "pdf", compliance: "pdf" });
  const [period, setPeriod] = useState<Record<ReportKind, Period>>({ "uptime-incident": 30, compliance: 30 });
  const [tenant, setTenant] = useState<Record<ReportKind, string>>({ "uptime-incident": "", compliance: "" });
  const [group, setGroup] = useState<Record<ReportKind, string>>({ "uptime-incident": "", compliance: "" });
  const [loading, setLoading] = useState<Record<ReportKind, boolean>>({ "uptime-incident": false, compliance: false });
  const [error, setError] = useState<Record<ReportKind, string | null>>({ "uptime-incident": null, compliance: null });

  useEffect(() => {
    if (user?.is_msp_staff) {
      api.get("/tenants/public-list").then((r: { data: Tenant[] }) => setTenants(r.data)).catch(() => {});
    }
    api.get("/device-groups").then((r: { data: DeviceGroup[] }) => setGroups(r.data ?? [])).catch(() => {});
  }, [user]);

  async function download(kind: ReportKind) {
    setLoading((p) => ({ ...p, [kind]: true }));
    setError((p) => ({ ...p, [kind]: null }));
    try {
      const params: Record<string, string | number> = { format: format[kind], days: period[kind] };
      if (kind === "uptime-incident") {
        if (tenant[kind]) params.tenant_id = tenant[kind];
        if (group[kind]) params.device_group_id = group[kind];
      }

      const endpoint = kind === "uptime-incident" ? "/reports/uptime-incident" : "/reports/compliance";
      const res = await api.get(endpoint, { params, responseType: "blob" });

      const disposition: string = res.headers["content-disposition"] ?? "";
      const fnMatch = disposition.match(/filename="([^"]+)"/);
      const fileName = fnMatch ? fnMatch[1] : `netguard-${kind}-report.${format[kind]}`;

      const url = URL.createObjectURL(new Blob([res.data]));
      const a = document.createElement("a");
      a.href = url;
      a.download = fileName;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      const detail =
        e?.response?.data instanceof Blob
          ? await e.response.data.text().then((t: string) => { try { return JSON.parse(t).detail; } catch { return t; } })
          : e?.response?.data?.detail ?? "Download failed";
      setError((p) => ({ ...p, [kind]: detail }));
    } finally {
      setLoading((p) => ({ ...p, [kind]: false }));
    }
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-display font-bold tracking-tight">Reports</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-0.5">
          Download on-demand PDF or CSV reports. Scheduled weekly &amp; monthly delivery is automatic.
        </p>
      </div>

      {CARDS.map((card) => (
        <div
          key={card.id}
          className="bg-white dark:bg-noc-panel rounded-xl border border-slate-200 dark:border-noc-border shadow-sm p-6"
        >
          {/* Card header */}
          <div className="flex items-start gap-4 mb-5">
            <div className="w-10 h-10 shrink-0 flex items-center justify-center rounded-lg bg-brandblue/10 dark:bg-noc-cyan/10 text-brandblue dark:text-noc-cyan">
              {card.icon}
            </div>
            <div>
              <h2 className="text-base font-semibold">{card.title}</h2>
              <p className="text-sm text-slate-500 dark:text-noc-muted mt-0.5">{card.description}</p>
            </div>
          </div>

          {/* Controls */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-4">
            {/* Period */}
            <div>
              <label className="block text-xs font-medium text-slate-500 dark:text-noc-muted mb-1.5">Period</label>
              <select
                value={period[card.id]}
                onChange={(e) => setPeriod((p) => ({ ...p, [card.id]: Number(e.target.value) as Period }))}
                className="w-full px-3 py-2 text-sm rounded-lg border border-slate-200 dark:border-noc-border bg-slate-50 dark:bg-noc-bg focus:outline-none focus:ring-2 focus:ring-brandblue dark:focus:ring-noc-cyan"
              >
                <option value={7}>Last 7 days</option>
                <option value={30}>Last 30 days</option>
                <option value={90}>Last 90 days</option>
              </select>
            </div>

            {/* Format */}
            <div>
              <label className="block text-xs font-medium text-slate-500 dark:text-noc-muted mb-1.5">Format</label>
              <select
                value={format[card.id]}
                onChange={(e) => setFormat((p) => ({ ...p, [card.id]: e.target.value as Format }))}
                className="w-full px-3 py-2 text-sm rounded-lg border border-slate-200 dark:border-noc-border bg-slate-50 dark:bg-noc-bg focus:outline-none focus:ring-2 focus:ring-brandblue dark:focus:ring-noc-cyan"
              >
                <option value="pdf">PDF</option>
                <option value="csv">CSV</option>
              </select>
            </div>

            {/* Tenant scope — MSP staff only, uptime-incident only */}
            {card.supportsScope && user?.is_msp_staff && (
              <div>
                <label className="block text-xs font-medium text-slate-500 dark:text-noc-muted mb-1.5">Tenant</label>
                <select
                  value={tenant[card.id]}
                  onChange={(e) => setTenant((p) => ({ ...p, [card.id]: e.target.value }))}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-slate-200 dark:border-noc-border bg-slate-50 dark:bg-noc-bg focus:outline-none focus:ring-2 focus:ring-brandblue dark:focus:ring-noc-cyan"
                >
                  <option value="">All tenants</option>
                  {tenants.map((t) => (
                    <option key={t.id} value={t.id}>{t.name}</option>
                  ))}
                </select>
              </div>
            )}

            {/* Device group scope */}
            {card.supportsScope && groups.length > 0 && (
              <div>
                <label className="block text-xs font-medium text-slate-500 dark:text-noc-muted mb-1.5">Device Group</label>
                <select
                  value={group[card.id]}
                  onChange={(e) => setGroup((p) => ({ ...p, [card.id]: e.target.value }))}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-slate-200 dark:border-noc-border bg-slate-50 dark:bg-noc-bg focus:outline-none focus:ring-2 focus:ring-brandblue dark:focus:ring-noc-cyan"
                >
                  <option value="">All groups</option>
                  {groups.map((g) => (
                    <option key={g.id} value={g.id}>{g.name}</option>
                  ))}
                </select>
              </div>
            )}
          </div>

          {/* Error */}
          {error[card.id] && (
            <p className="text-sm text-red-500 bg-red-50 dark:bg-red-900/20 px-3 py-2 rounded-lg mb-4">
              {error[card.id]}
            </p>
          )}

          {/* Download button */}
          <button
            onClick={() => download(card.id)}
            disabled={loading[card.id]}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-brandblue text-white text-sm font-medium hover:bg-brandblue/90 disabled:opacity-60 transition-colors"
          >
            {loading[card.id] ? (
              <>
                <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                Generating…
              </>
            ) : (
              <>
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <path d="M12 3v12M8 11l4 4 4-4M4 19h16" />
                </svg>
                Download {format[card.id].toUpperCase()}
              </>
            )}
          </button>
        </div>
      ))}

      {/* Scheduled delivery note */}
      <div className="rounded-xl border border-slate-200 dark:border-noc-border bg-slate-50 dark:bg-noc-panel2 p-4 flex gap-3 items-start">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="shrink-0 text-slate-400 mt-0.5">
          <circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 3" /><path d="M8 2l-2 2M18 2l2 2" />
        </svg>
        <p className="text-sm text-slate-500 dark:text-noc-muted">
          <strong className="text-slate-700 dark:text-noc-text">Automatic delivery:</strong> Uptime &amp; compliance reports are emailed
          to <code className="text-xs bg-slate-200 dark:bg-slate-700 px-1 rounded">NOTIFY_EMAIL_RECIPIENTS</code> every{" "}
          <strong>Monday at 07:00 UTC</strong> (7-day) and on the <strong>1st of each month</strong> (30-day). Configure
          recipients in your environment file.
        </p>
      </div>
    </div>
  );
}
