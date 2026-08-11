import React from "react";

/**
 * Standard "nothing here" placeholder for tables/lists, so an empty result
 * set reads as "no results" rather than looking like the page is broken.
 *
 *   {items.length === 0 ? (
 *     <EmptyState title="No devices found" message="Try adjusting your filters." />
 *   ) : ( ... )}
 */
export function EmptyState({
  title,
  message,
  icon,
  action,
  className = "",
}: {
  title: string;
  message?: string;
  icon?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={`flex flex-col items-center justify-center text-center py-12 px-6 ${className}`}>
      <div className="flex items-center justify-center w-12 h-12 rounded-full bg-slate-100 dark:bg-white/5 text-slate-400 dark:text-slate-500 mb-3">
        {icon ?? (
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75}>
            <circle cx="11" cy="11" r="7" />
            <path d="M21 21l-4.3-4.3" />
          </svg>
        )}
      </div>
      <p className="text-sm font-medium text-slate-700 dark:text-slate-200">{title}</p>
      {message && <p className="mt-1 text-sm text-slate-500 dark:text-slate-400 max-w-sm">{message}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

/**
 * Standard skeleton rows shown while a table/list is loading, so pages don't
 * flash an empty table before data arrives.
 *
 *   {loading ? <LoadingRows rows={5} /> : <table>...</table>}
 */
export function LoadingRows({ rows = 5, className = "" }: { rows?: number; className?: string }) {
  return (
    <div className={`flex flex-col gap-2 py-2 ${className}`} aria-busy="true" aria-live="polite">
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="h-10 rounded-lg bg-slate-100 dark:bg-white/5 animate-pulse"
          style={{ animationDelay: `${i * 60}ms` }}
        />
      ))}
    </div>
  );
}