import { DeploymentRecord } from "../lib/types";

export type StageState = "pending" | "running" | "passed" | "failed" | "skipped";

export interface Stage {
  key: string;
  label: string;
  state: StageState;
}

const STATE_STYLES: Record<StageState, { box: string; text: string; dot: string }> = {
  pending: { box: "bg-slate-50 border-slate-200", text: "text-slate-400", dot: "bg-slate-300" },
  running: { box: "bg-blue-50 border-brandblue", text: "text-brandblue", dot: "bg-brandblue animate-pulse" },
  passed: { box: "bg-green-50 border-green-400", text: "text-green-700", dot: "bg-green-500" },
  failed: { box: "bg-red-50 border-riskcrit", text: "text-riskcrit", dot: "bg-riskcrit" },
  skipped: { box: "bg-amber-50 border-riskmed", text: "text-riskmed", dot: "bg-riskmed" },
};

const STAGE_LABELS: { key: string; label: string }[] = [
  { key: "SNAPSHOT", label: "Snapshot" },
  { key: "DEPLOY", label: "Deploy" },
  { key: "VERIFY", label: "Verify" },
  { key: "COMPLETE", label: "Complete" },
];

/** Derives Jenkins-style stage states from a deployment's terminal/live
 * status. This is a coarse view -- e.g. `failed` doesn't know whether the
 * failure happened at Deploy or Verify without also fetching logs -- built
 * so a compact pipeline strip can render in list contexts (the
 * Deployments table) without an extra fetch per row. `DeploymentDetails`
 * (the per-deployment expanded panel with live logs) already renders the
 * precise, log-driven step-by-step timeline; this complements it with the
 * "which stage failed" story where full log data isn't loaded.
 */
export function stagesFromStatus(status: DeploymentRecord["status"], hasHealthChecks: boolean): Stage[] {
  if (status === "queued") {
    return STAGE_LABELS.map((s) => ({ ...s, state: "pending" as StageState }));
  }
  if (status === "in_progress") {
    return STAGE_LABELS.map((s, i) => ({
      ...s,
      state: i === 0 ? "passed" : i === 1 ? "running" : "pending",
    }));
  }
  if (status === "succeeded") {
    return STAGE_LABELS.map((s) => ({ ...s, state: "passed" as StageState }));
  }
  if (status === "rolled_back") {
    // Deploy landed and passed initial checks, but post-deploy monitoring
    // (Verify) caught a regression and it was rolled back -- Complete
    // shows amber ("skipped" style, reused for rollback) rather than red,
    // since the network is back to a known-good state, not left broken.
    return [
      { key: "SNAPSHOT", label: "Snapshot", state: "passed" },
      { key: "DEPLOY", label: "Deploy", state: "passed" },
      { key: "VERIFY", label: "Verify", state: "failed" },
      { key: "COMPLETE", label: "Rolled Back", state: "skipped" },
    ];
  }
  // failed -- Verify only ran if health checks were actually recorded;
  // otherwise Deploy itself is where it died.
  if (hasHealthChecks) {
    return [
      { key: "SNAPSHOT", label: "Snapshot", state: "passed" },
      { key: "DEPLOY", label: "Deploy", state: "passed" },
      { key: "VERIFY", label: "Verify", state: "failed" },
      { key: "COMPLETE", label: "Complete", state: "pending" },
    ];
  }
  return [
    { key: "SNAPSHOT", label: "Snapshot", state: "passed" },
    { key: "DEPLOY", label: "Deploy", state: "failed" },
    { key: "VERIFY", label: "Verify", state: "pending" },
    { key: "COMPLETE", label: "Complete", state: "pending" },
  ];
}

export default function PipelineStages({ stages, compact = false }: { stages: Stage[]; compact?: boolean }) {
  return (
    <div className="flex items-center">
      {stages.map((stage, idx) => {
        const style = STATE_STYLES[stage.state];
        return (
          <div key={stage.key} className="flex items-center">
            <div
              className={`flex items-center gap-1.5 border rounded-md shadow-sm font-semibold whitespace-nowrap ${style.box} ${style.text} ${
                compact ? "px-2 py-1 text-[10px]" : "px-3 py-1.5 text-xs"
              }`}
              title={`${stage.label}: ${stage.state}`}
            >
              <span className={`rounded-full shrink-0 ${style.dot} ${compact ? "w-1.5 h-1.5" : "w-2 h-2"}`} />
              {stage.label}
            </div>
            {idx < stages.length - 1 && (
              <div
                className={`shrink-0 ${compact ? "w-3 h-[2px]" : "w-5 h-[2px]"} ${
                  stage.state === "passed" ? "bg-green-400" : "bg-slate-200"
                }`}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}