import { diffLines, Change } from "diff";

interface Props {
  currentConfig: string | null | undefined;
  proposedConfig: string | null | undefined;
}

interface Row {
  left: string | null;
  right: string | null;
  kind: "same" | "removed" | "added" | "changed";
}

/** Turns a diff.js line-diff (a flat list of added/removed/unchanged
 * chunks) into aligned left/right rows for a side-by-side table. Adjacent
 * removed+added chunks of equal line count are paired up as "changed"
 * rows (same line replaced) rather than shown as an unrelated
 * delete-block followed by an add-block, which is what a reviewer
 * actually wants to compare during peer review. */
function toRows(changes: Change[]): Row[] {
  const rows: Row[] = [];
  for (let i = 0; i < changes.length; i++) {
    const part = changes[i];
    const lines = part.value.replace(/\n$/, "").split("\n");

    if (!part.added && !part.removed) {
      lines.forEach((line) => rows.push({ left: line, right: line, kind: "same" }));
      continue;
    }

    if (part.removed) {
      const next = changes[i + 1];
      if (next && next.added) {
        const addedLines = next.value.replace(/\n$/, "").split("\n");
        const max = Math.max(lines.length, addedLines.length);
        for (let j = 0; j < max; j++) {
          rows.push({
            left: lines[j] ?? null,
            right: addedLines[j] ?? null,
            kind: "changed",
          });
        }
        i++; // consumed the paired "added" chunk
        continue;
      }
      lines.forEach((line) => rows.push({ left: line, right: null, kind: "removed" }));
      continue;
    }

    // Unpaired "added" chunk (no preceding "removed" chunk consumed above).
    lines.forEach((line) => rows.push({ left: null, right: line, kind: "added" }));
  }
  return rows;
}

const ROW_CLASS: Record<Row["kind"], string> = {
  same: "",
  removed: "bg-red-950/40",
  added: "bg-green-950/40",
  changed: "bg-amber-950/30",
};

export default function SideBySideDiff({ currentConfig, proposedConfig }: Props) {
  if (!currentConfig && !proposedConfig) {
    return <p className="text-sm text-slate-400 italic">No configuration to compare.</p>;
  }

  const rows = toRows(diffLines(currentConfig || "", proposedConfig || ""));

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-700">
      <table className="w-full text-xs font-mono border-collapse">
        <thead>
          <tr className="bg-slate-800 text-slate-400">
            <th className="text-left px-3 py-1.5 font-semibold uppercase tracking-wide w-1/2">Current</th>
            <th className="text-left px-3 py-1.5 font-semibold uppercase tracking-wide w-1/2">Proposed</th>
          </tr>
        </thead>
        <tbody className="bg-slate-900">
          {rows.map((row, i) => (
            <tr key={i} className={ROW_CLASS[row.kind]}>
              <td
                className={`px-3 py-0.5 align-top whitespace-pre-wrap break-all border-r border-slate-800 ${
                  row.kind === "removed" || row.kind === "changed" ? "text-riskcrit" : "text-slate-300"
                }`}
              >
                {row.left ?? ""}
              </td>
              <td
                className={`px-3 py-0.5 align-top whitespace-pre-wrap break-all ${
                  row.kind === "added" || row.kind === "changed" ? "text-risklow" : "text-slate-300"
                }`}
              >
                {row.right ?? ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}