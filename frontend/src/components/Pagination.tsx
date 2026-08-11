import React from "react";

/**
 * Simple offset pager: "Showing X-Y of Z" plus Prev/Next.
 * Purely presentational — the caller owns `page`/`pageSize` state and
 * re-fetches (or re-slices) on change.
 *
 *   const [page, setPage] = useState(1);
 *   const pageSize = 25;
 *   ...
 *   <Pagination page={page} pageSize={pageSize} total={total} onPageChange={setPage} />
 */
export function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  className = "",
}: {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  className?: string;
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const start = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const end = Math.min(page * pageSize, total);

  if (total <= pageSize) return null;

  return (
    <div className={`flex items-center justify-between gap-3 text-sm text-slate-500 dark:text-slate-400 ${className}`}>
      <span>
        Showing <span className="font-medium text-slate-700 dark:text-slate-200">{start}-{end}</span> of{" "}
        <span className="font-medium text-slate-700 dark:text-slate-200">{total}</span>
      </span>
      <div className="flex items-center gap-1">
        <button
          onClick={() => onPageChange(page - 1)}
          disabled={page <= 1}
          className="px-2.5 py-1 rounded-lg border border-slate-200 dark:border-noc-borderlit disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-50 dark:hover:bg-white/5 transition-colors"
        >
          Prev
        </button>
        <span className="px-2 tabular-nums">
          {page} / {totalPages}
        </span>
        <button
          onClick={() => onPageChange(page + 1)}
          disabled={page >= totalPages}
          className="px-2.5 py-1 rounded-lg border border-slate-200 dark:border-noc-borderlit disabled:opacity-40 disabled:cursor-not-allowed hover:bg-slate-50 dark:hover:bg-white/5 transition-colors"
        >
          Next
        </button>
      </div>
    </div>
  );
}