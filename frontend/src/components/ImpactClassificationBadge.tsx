/** Shared visual language for "how risky is this destructive action"
 * previews, so an operator learns one color/badge pattern and trusts it
 * everywhere instead of re-learning it per page. Originally introduced
 * for ImpactSimulationPanel's danger/caution/safe reachability preview
 * (Change Requests, Firmware Upgrades) and now the single source of
 * truth for that palette -- ImpactSimulationPanel imports it too, so
 * there's exactly one place these colors are defined. Reused as-is by
 * any other destructive/elevating action that can classify its own
 * blast radius: bulk device delete (Devices), golden-config restore
 * (Drift), and JIT elevation to network_admin (JitAccess).
 */

export type ImpactClassification = "danger" | "caution" | "safe";

export const classificationBorderClass: Record<ImpactClassification, string> = {
  danger: "border-red-300 bg-red-50 dark:bg-red-950/20 dark:border-red-800",
  caution: "border-amber-300 bg-amber-50 dark:bg-amber-950/20 dark:border-amber-800",
  safe: "border-green-200 bg-green-50 dark:bg-green-950/20 dark:border-green-900",
};

export const classificationBadgeClass: Record<ImpactClassification, string> = {
  danger: "bg-red-100 text-red-700",
  caution: "bg-amber-100 text-amber-700",
  safe: "bg-green-100 text-green-700",
};

export const classificationDotClass: Record<ImpactClassification, string> = {
  danger: "bg-red-500",
  caution: "bg-amber-500",
  safe: "bg-green-500",
};

const DEFAULT_LABEL: Record<ImpactClassification, string> = {
  danger: "⛔ High impact",
  caution: "⚠ Review before proceeding",
  safe: "✓ Low impact",
};

/** Small pill badge -- the one visual element every destructive-action
 * confirmation should share. `label` overrides the default per-context
 * copy (e.g. "Would break reachability" vs "Blast radius: 12 devices")
 * while keeping the same color per classification everywhere. */
export function ImpactClassificationBadge({
  classification,
  label,
  className = "",
}: {
  classification: ImpactClassification;
  label?: string;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-bold ${classificationBadgeClass[classification]} ${className}`}
    >
      {label ?? DEFAULT_LABEL[classification]}
    </span>
  );
}