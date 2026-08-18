/** Shared CSV export helpers -- factored out of the page-local `toCsv`
 * implementation that AuditLog.tsx (and, before this file existed, several
 * other pages with their own copy-pasted versions) used, so every "Export
 * CSV" button across the app escapes/quotes/downloads the same way instead
 * of N slightly-different one-off implementations.
 */

export interface CsvColumn<T> {
  header: string;
  value: (row: T) => string | number | boolean | null | undefined;
}

function escapeCsvCell(raw: string): string {
  // Always quote -- simplest correct behavior, and avoids having to detect
  // which cells "need" it (commas, quotes, or embedded newlines all do).
  return `"${raw.replace(/"/g, '""')}"`;
}

/** Builds a CSV string (header row + one row per item) from column
 * definitions. Values are stringified (null/undefined become an empty
 * cell) and quote-escaped -- callers pass already-formatted display
 * strings (dates as ISO strings, booleans as "Yes"/"No", etc.) rather
 * than raw values, same convention the column defs at each call site
 * already follow.
 */
export function toCsv<T>(rows: T[], columns: CsvColumn<T>[]): string {
  const header = columns.map((c) => escapeCsvCell(c.header)).join(",");
  const lines = rows.map((row) =>
    columns
      .map((c) => {
        const v = c.value(row);
        return escapeCsvCell(v === null || v === undefined ? "" : String(v));
      })
      .join(",")
  );
  return [header, ...lines].join("\n");
}

/** Triggers a browser download of a CSV string under the given filename. */
export function downloadCsv(filename: string, csv: string): void {
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/** YYYY-MM-DD for today, for consistent export filenames
 * (`netguard-<thing>-2026-08-18.csv`) across every page that exports. */
export function todayStamp(): string {
  return new Date().toISOString().slice(0, 10);
}

/** Shared Tailwind classes for every page's "Export CSV" button, so they
 * look identical without each page re-typing the same class string. */
export const exportButtonClass =
  "bg-white border border-slate-300 text-navy rounded-lg px-4 py-2 text-sm font-semibold hover:bg-slate-50 flex items-center gap-1.5";