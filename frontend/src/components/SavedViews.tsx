import { useEffect, useRef, useState } from "react";

/** Saved filter presets for a list page ("My Core Switches", "Critical
 * Only") -- every NOC engineer re-applies the same handful of filters
 * every shift, so let them save the current filter combination under a
 * name and re-apply it in one click instead of re-picking 3 dropdowns
 * every time they load the page.
 *
 * Stored client-side (localStorage, namespaced per `storageKey`) since
 * there's no per-user saved-view backend yet -- same tradeoff as the
 * command palette's recents. `T` is whatever shape of filter state the
 * calling page uses; this component only serializes/restores it, it
 * never interprets the fields.
 */

export interface SavedView<T> {
  id: string;
  name: string;
  filters: T;
  createdAt: number;
}

function loadViews<T>(storageKey: string): SavedView<T>[] {
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function persistViews<T>(storageKey: string, views: SavedView<T>[]) {
  try {
    localStorage.setItem(storageKey, JSON.stringify(views));
  } catch {
    // storage full/unavailable -- saved views are a nice-to-have, fail silently
  }
}

export default function SavedViews<T>({
  storageKey,
  currentFilters,
  onApply,
  isDefault,
}: {
  /** localStorage namespace for this page, e.g. "netguard_saved_views_devices". */
  storageKey: string;
  /** The filter state as it stands right now -- captured verbatim on save. */
  currentFilters: T;
  /** Called with a saved view's filters when the user picks it from the list. */
  onApply: (filters: T) => void;
  /** Optional: lets the "Save current filters" button gray out when
   * every filter is already at its default (nothing meaningful to save). */
  isDefault?: (filters: T) => boolean;
}) {
  const [views, setViews] = useState<SavedView<T>[]>([]);
  const [open, setOpen] = useState(false);
  const [naming, setNaming] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setViews(loadViews<T>(storageKey));
  }, [storageKey]);

  useEffect(() => {
    if (!open) return;
    const onClickAway = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
        setNaming(false);
      }
    };
    document.addEventListener("mousedown", onClickAway);
    return () => document.removeEventListener("mousedown", onClickAway);
  }, [open]);

  const nothingToSave = isDefault ? isDefault(currentFilters) : false;

  const saveView = () => {
    const name = nameDraft.trim();
    if (!name) return;
    const next = [
      ...views,
      { id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, name, filters: currentFilters, createdAt: Date.now() },
    ];
    setViews(next);
    persistViews(storageKey, next);
    setNaming(false);
    setNameDraft("");
  };

  const deleteView = (id: string) => {
    const next = views.filter((v) => v.id !== id);
    setViews(next);
    persistViews(storageKey, next);
  };

  return (
    <div className="relative" ref={rootRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 border border-slate-300 dark:border-slate-600 shadow-sm rounded-full px-4 py-1.5 text-sm text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700/50 transition-colors"
        title="Saved views"
      >
        ⭐ Views {views.length > 0 && <span className="text-[10px] text-slate-400">({views.length})</span>}
      </button>

      {open && (
        <div className="absolute z-20 mt-1 w-64 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg shadow-xl overflow-hidden">
          <div className="max-h-64 overflow-y-auto">
            {views.length === 0 && (
              <p className="px-3 py-3 text-xs text-slate-400">No saved views yet — set your filters, then save.</p>
            )}
            {views.map((v) => (
              <div
                key={v.id}
                className="flex items-center justify-between gap-1 px-3 py-2 hover:bg-slate-50 dark:hover:bg-slate-700/50 group"
              >
                <button
                  type="button"
                  onClick={() => {
                    onApply(v.filters);
                    setOpen(false);
                  }}
                  className="flex-1 text-left text-sm text-navy dark:text-white truncate"
                >
                  {v.name}
                </button>
                <button
                  type="button"
                  onClick={() => deleteView(v.id)}
                  title="Delete this saved view"
                  className="text-slate-300 dark:text-slate-500 hover:text-riskcrit opacity-0 group-hover:opacity-100 transition-opacity text-xs font-bold px-1"
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
          <div className="border-t border-slate-200 dark:border-slate-700 p-2">
            {!naming ? (
              <button
                type="button"
                disabled={nothingToSave}
                onClick={() => setNaming(true)}
                className="w-full text-xs font-bold text-brandblue disabled:text-slate-300 disabled:cursor-not-allowed hover:underline px-1 py-1 text-left"
              >
                + Save current filters as view
              </button>
            ) : (
              <div className="flex items-center gap-1">
                <input
                  autoFocus
                  value={nameDraft}
                  onChange={(e) => setNameDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") saveView();
                    if (e.key === "Escape") {
                      setNaming(false);
                      setNameDraft("");
                    }
                  }}
                  placeholder='e.g. "My Core Switches"'
                  className="flex-1 border border-slate-300 dark:border-slate-600 rounded px-2 py-1 text-xs outline-none focus:ring-1 focus:ring-brandblue"
                />
                <button
                  type="button"
                  onClick={saveView}
                  disabled={!nameDraft.trim()}
                  className="text-xs font-bold text-brandblue disabled:text-slate-300 px-1"
                >
                  Save
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}