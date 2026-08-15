import { ImpactSimulationPreview } from "../lib/types";
import { classificationBorderClass, classificationBadgeClass, ImpactClassification } from "./ImpactClassificationBadge";

// Pre-deployment impact simulation panel ("what-if" dry run). Originally
// shared between the New Change Request form (live, as the operator
// types) and the CR detail view (re-run against the topology as it
// stands now); also used by the firmware upgrade form, where the
// "proposed config" is synthesized as a reboot (every confirmed
// interface going down for the reload window) rather than typed by the
// operator -- see app.services.impact_simulation_service.simulate_reboot_impact.
// This component just renders whatever ImpactSimulationPreview it's given.
export default function ImpactSimulationPanel({
  sim,
  loading,
  title = "Pre-Change Impact Simulation",
  loadingLabel = "Simulating routing/reachability impact…",
}: {
  sim: ImpactSimulationPreview | null;
  loading: boolean;
  title?: string;
  loadingLabel?: string;
}) {
  if (loading && !sim) {
    return (
      <div className="border border-slate-200 dark:border-slate-700 rounded-lg p-3 text-xs bg-slate-50 dark:bg-slate-900">
        <p className="font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 mb-1">{title}</p>
        <p className="text-slate-400">{loadingLabel}</p>
      </div>
    );
  }
  if (!sim) return null;

  const badgeLabel: Record<string, string> = {
    danger: "⛔ Would break reachability",
    caution: "⚠ Review before deploying",
    safe: "✓ No reachability impact",
  };
  const classification = (sim.classification as ImpactClassification) in classificationBorderClass ? (sim.classification as ImpactClassification) : "safe";

  const allImpacted = [...sim.isolated_devices, ...sim.degraded_devices];

  return (
    <div className={`border rounded-lg p-3 text-xs ${classificationBorderClass[classification]}`}>
      <div className="flex items-center justify-between mb-1">
        <p className="font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">{title}</p>
        <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${classificationBadgeClass[classification]}`}>
          {badgeLabel[classification]}
        </span>
      </div>
      <p className="text-slate-700 dark:text-slate-200">{sim.summary}</p>

      {sim.removed_links.length > 0 && (
        <div className="mt-2">
          <p className="text-slate-500 dark:text-slate-400 font-semibold mb-1">Links this change would take down:</p>
          <ul className="space-y-0.5">
            {sim.removed_links.map((link, i) => (
              <li key={i} className="text-slate-600 dark:text-slate-300">
                <span className="font-mono">{link.interface}</span>
                {link.neighbor_hostname && (
                  <>
                    {" → "}
                    <span className="font-medium">{link.neighbor_hostname}</span>
                    {link.neighbor_port && <span className="font-mono opacity-70"> ({link.neighbor_port})</span>}
                  </>
                )}
                <span className="opacity-70"> — {link.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {allImpacted.length > 0 && (
        <div className="mt-2">
          <p className="text-slate-500 dark:text-slate-400 font-semibold mb-1">Devices affected:</p>
          <ul className="space-y-0.5">
            {allImpacted.map((d) => (
              <li key={d.device_id} className="text-slate-600 dark:text-slate-300">
                <span className={`inline-block w-2 h-2 rounded-full mr-1.5 ${d.status === "isolated" ? "bg-red-500" : "bg-amber-500"}`} />
                <span className="font-medium">{d.hostname}</span>
                {d.device_role && <span className="opacity-60"> · {d.device_role}</span>}
                {d.status === "isolated" ? (
                  <span className="ml-1 text-red-600 dark:text-red-400">would lose all reachability — no alternate path</span>
                ) : (
                  <span className="ml-1 text-amber-700 dark:text-amber-400">
                    still reachable, but via a longer path ({d.before_hop_count} → {d.after_hop_count} hops)
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}