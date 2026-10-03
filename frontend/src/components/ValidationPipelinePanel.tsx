import { CombinedValidationReport } from "../lib/types";

// Renders the Section 15 validation pipeline breakdown (syntax → OPA →
// Batfish → risk → blast radius → combined decision) for a Change
// Request. Fed by GET/POST /change-requests/{id}/validation|validate,
// which both return the same CombinedValidationReport shape produced by
// app.services.change_validation_service.validate_change. This panel
// only renders what it's given -- it doesn't call the API itself, so it
// can be reused for a stale "last known" report and a fresh re-run alike.

const DECISION_STYLE: Record<string, { border: string; badge: string; label: string }> = {
  pass: { border: "border-emerald-200 dark:border-emerald-900", badge: "bg-emerald-50 text-emerald-700 border-emerald-200", label: "✓ Pass" },
  review: { border: "border-amber-200 dark:border-amber-900", badge: "bg-amber-50 text-riskmed border-amber-200", label: "⚠ Requires Review" },
  block: { border: "border-red-200 dark:border-red-900", badge: "bg-red-50 text-red-700 border-red-200", label: "⛔ Blocked" },
};

function StepBadge({ status, label }: { status: "pass" | "warn" | "block" | "unavailable" | "pending"; label: string }) {
  const style: Record<string, string> = {
    pass: "bg-emerald-50 text-emerald-700 border-emerald-200",
    warn: "bg-amber-50 text-riskmed border-amber-200",
    block: "bg-red-50 text-red-700 border-red-200",
    unavailable: "bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400 border-slate-200 dark:border-slate-700",
    pending: "bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400 border-slate-200 dark:border-slate-700",
  };
  const icon: Record<string, string> = { pass: "✓", warn: "⚠", block: "⛔", unavailable: "—", pending: "…" };
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wide border ${style[status]}`}>
      {icon[status]} {label}
    </span>
  );
}

function severityToStepStatus(decision: string | null | undefined): "pass" | "warn" | "block" | "unavailable" {
  if (!decision) return "unavailable";
  const d = decision.toLowerCase();
  if (d === "deny" || d === "block" || d === "critical") return "block";
  if (d === "review" || d === "warn" || d === "warning") return "warn";
  if (d === "allow" || d === "pass" || d === "accept") return "pass";
  if (d.includes("unavailable") || d.includes("unsupported")) return "unavailable";
  return "unavailable";
}

export default function ValidationPipelinePanel({
  report,
  loading,
  onRerun,
  rerunning,
}: {
  report: CombinedValidationReport | null;
  loading: boolean;
  onRerun?: () => void;
  rerunning?: boolean;
}) {
  if (loading && !report) {
    return (
      <div className="border border-slate-200 dark:border-slate-700 rounded-lg p-3 text-xs bg-slate-50 dark:bg-slate-900">
        <p className="font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-1">Policy &amp; Behavior Validation</p>
        <p className="text-slate-400">Running OPA policy checks and Batfish network simulation…</p>
      </div>
    );
  }

  if (!report) {
    return (
      <div className="border border-slate-200 dark:border-slate-700 rounded-lg p-3 text-xs bg-slate-50 dark:bg-slate-900 flex items-center justify-between gap-2">
        <p className="text-slate-400">This change hasn't been through the OPA + Batfish validation gate yet.</p>
        {onRerun && (
          <button
            onClick={onRerun}
            disabled={rerunning}
            className="shrink-0 text-[11px] font-bold uppercase tracking-wide text-brandblue border border-blue-200 bg-blue-50 hover:bg-blue-100 px-2.5 py-1 rounded-lg shadow-sm disabled:opacity-50"
          >
            {rerunning ? "Validating…" : "Run Validation"}
          </button>
        )}
      </div>
    );
  }

  const decisionStyle = DECISION_STYLE[report.decision] ?? DECISION_STYLE.review;
  const syntaxStatus: "pass" | "block" = report.syntax_passed ? "pass" : "block";
  const opaStatus = report.opa ? severityToStepStatus(report.opa.decision) : "unavailable";
  const batfishStatus = report.batfish ? severityToStepStatus(report.batfish.status) : "unavailable";
  const behaviorChanges = report.batfish?.findings.filter((f) => f.behavior_changed) ?? [];

  return (
    <div className={`border rounded-lg p-3 text-xs ${decisionStyle.border}`}>
      <div className="flex items-center justify-between mb-2">
        <p className="font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Policy &amp; Behavior Validation</p>
        <div className="flex items-center gap-2">
          <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold border ${decisionStyle.badge}`}>{decisionStyle.label}</span>
          {onRerun && (
            <button
              onClick={onRerun}
              disabled={rerunning}
              className="text-[11px] font-bold uppercase tracking-wide text-brandblue border border-blue-200 bg-blue-50 hover:bg-blue-100 px-2 py-0.5 rounded-lg shadow-sm disabled:opacity-50"
            >
              {rerunning ? "Re-running…" : "↻ Re-run"}
            </button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-2">
        <div className="flex flex-col gap-1">
          <span className="text-slate-500 dark:text-slate-400">Config Syntax</span>
          <StepBadge status={syntaxStatus} label={report.syntax_passed ? "Pass" : "Fail"} />
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-slate-500 dark:text-slate-400">OPA Policy</span>
          <StepBadge
            status={opaStatus}
            label={report.opa ? `${report.opa.decision} · ${report.opa.matched_policies.length} policies` : "Not run"}
          />
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-slate-500 dark:text-slate-400">Batfish Simulation</span>
          <StepBadge
            status={batfishStatus}
            label={report.batfish ? `${report.batfish.status} · ${report.batfish.behavior_changes} change${report.batfish.behavior_changes === 1 ? "" : "s"}` : "Not run"}
          />
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-slate-500 dark:text-slate-400">Risk / Blast Radius</span>
          <span className="font-mono text-slate-700 dark:text-slate-200">
            {report.risk_classification ?? report.risk_score ?? "—"}
            {report.blast_radius_devices != null ? ` · ${report.blast_radius_devices} devices` : ""}
          </span>
        </div>
      </div>

      {report.syntax_errors.length > 0 && (
        <ul className="list-disc list-inside text-red-700 space-y-0.5 mb-1">
          {report.syntax_errors.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      )}

      {report.opa && report.opa.violations.length > 0 && (
        <div className="mt-1">
          <p className="text-slate-500 dark:text-slate-400 font-semibold mb-1">OPA policy violations:</p>
          <ul className="space-y-1">
            {report.opa.violations.map((v, i) => (
              <li key={i} className="border border-slate-200 dark:border-slate-700 rounded px-2 py-1 bg-white dark:bg-slate-900/40">
                <span className="font-bold uppercase text-[10px] tracking-wide text-red-700">{v.severity}</span>{" "}
                <span className="font-mono text-slate-600 dark:text-slate-300">{v.policy}</span>
                <p className="text-slate-700 dark:text-slate-200">{v.message}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {report.opa?.error && (
        <p className="text-[11px] text-slate-500 dark:text-slate-400 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg px-2.5 py-1.5 mt-1">
          OPA: {report.opa.error}
        </p>
      )}

      {report.batfish?.reason && (
        <p className="text-[11px] text-slate-500 dark:text-slate-400 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg px-2.5 py-1.5 mt-1">
          Batfish: {report.batfish.reason}
        </p>
      )}

      {behaviorChanges.length > 0 && (
        <div className="mt-2">
          <p className="text-slate-500 dark:text-slate-400 font-semibold mb-1">Network behavior changes found by Batfish:</p>
          <ul className="space-y-1">
            {behaviorChanges.map((f, i) => (
              <li key={i} className="border border-slate-200 dark:border-slate-700 rounded px-2 py-1 bg-white dark:bg-slate-900/40">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-slate-700 dark:text-slate-200">
                    {f.source} → {f.destination}
                    {f.protocol ? ` (${f.protocol}${f.port ? `/${f.port}` : ""})` : ""}
                  </span>
                  <span className="font-bold uppercase text-[10px] tracking-wide text-red-700">{f.severity}</span>
                </div>
                <p className="text-slate-600 dark:text-slate-300">
                  {f.before} → {f.after}
                  {f.affected_device ? ` on ${f.affected_device}` : ""}
                </p>
                <p className="text-slate-500 dark:text-slate-400">{f.explanation}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {report.reasons.length > 0 && (
        <div className="mt-2">
          <p className="text-slate-500 dark:text-slate-400 font-semibold mb-1">Decision reasons:</p>
          <ul className="list-disc list-inside text-slate-600 dark:text-slate-300 space-y-0.5">
            {report.reasons.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}