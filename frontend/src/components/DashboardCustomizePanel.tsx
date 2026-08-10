import { useState } from "react";
import { DashboardLayoutEntry, DashboardWidgetInfo } from "../lib/types";

/** Customize-dashboard modal: toggle which widgets show and drag/reorder
 * them (via up/down buttons, not full drag-and-drop -- keeps this
 * dependency-free and accessible via keyboard) before saving to
 * PUT /dashboard/preferences. Operates on a local copy of `layout` so
 * Cancel doesn't touch the live dashboard until Save is pressed. */
export default function DashboardCustomizePanel({
  layout,
  availableWidgets,
  saving,
  onClose,
  onSave,
}: {
  layout: DashboardLayoutEntry[];
  availableWidgets: DashboardWidgetInfo[];
  saving: boolean;
  onClose: () => void;
  onSave: (next: DashboardLayoutEntry[]) => void;
}) {
  const [local, setLocal] = useState<DashboardLayoutEntry[]>(layout);

  const titleFor = (id: string) => availableWidgets.find((w) => w.id === id)?.title || id;

  const toggle = (id: string) =>
    setLocal((prev) => prev.map((e) => (e.id === id ? { ...e, visible: !e.visible } : e)));

  const move = (index: number, dir: -1 | 1) => {
    setLocal((prev) => {
      const next = [...prev];
      const target = index + dir;
      if (target < 0 || target >= next.length) return prev;
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  const resetToDefaults = () =>
    setLocal(availableWidgets.map((w) => ({ id: w.id, visible: w.default_visible })));

  const visibleCount = local.filter((e) => e.visible).length;

  return (
    <div className="fixed inset-0 bg-navy dark:bg-slate-950/40 flex items-center justify-center z-50 px-4">
      <div className="bg-white dark:bg-slate-800 rounded-xl shadow-xl max-w-lg w-full p-5 max-h-[85vh] flex flex-col">
        <div className="flex items-start justify-between shrink-0">
          <div>
            <h3 className="font-semibold text-navy dark:text-white">Customize Dashboard</h3>
            <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
              Choose which widgets show on your dashboard and drag their order with the arrows.
              This only changes your own view.
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 dark:text-slate-500 hover:text-slate-600 dark:text-slate-300 text-lg leading-none"
          >
            ✕
          </button>
        </div>

        <div className="mt-4 flex-1 overflow-y-auto -mx-1 px-1 space-y-1.5">
          {local.map((entry, i) => (
            <div
              key={entry.id}
              className={`flex items-center gap-2 rounded-lg border px-3 py-2 ${
                entry.visible
                  ? "border-slate-200 dark:border-slate-600 bg-white dark:bg-slate-800"
                  : "border-slate-100 dark:border-slate-700 bg-slate-50 dark:bg-slate-900/50 opacity-60"
              }`}
            >
              <div className="flex flex-col -space-y-1 shrink-0">
                <button
                  onClick={() => move(i, -1)}
                  disabled={i === 0}
                  className="text-slate-400 hover:text-brandblue disabled:opacity-25 disabled:hover:text-slate-400 text-xs leading-none py-0.5"
                  title="Move up"
                >
                  ▲
                </button>
                <button
                  onClick={() => move(i, 1)}
                  disabled={i === local.length - 1}
                  className="text-slate-400 hover:text-brandblue disabled:opacity-25 disabled:hover:text-slate-400 text-xs leading-none py-0.5"
                  title="Move down"
                >
                  ▼
                </button>
              </div>

              <span className="flex-1 text-sm font-medium text-navy dark:text-white truncate">
                {titleFor(entry.id)}
              </span>

              <label className="flex items-center gap-1.5 shrink-0 cursor-pointer">
                <input
                  type="checkbox"
                  checked={entry.visible}
                  onChange={() => toggle(entry.id)}
                  className="accent-brandblue"
                />
                <span className="text-[11px] text-slate-400 dark:text-slate-500 w-10">
                  {entry.visible ? "Shown" : "Hidden"}
                </span>
              </label>
            </div>
          ))}
        </div>

        <div className="mt-4 shrink-0 flex items-center justify-between gap-2 pt-3 border-t border-slate-100 dark:border-slate-700">
          <div className="flex items-center gap-3">
            <button
              onClick={resetToDefaults}
              className="text-xs text-slate-400 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300 underline"
            >
              Reset to defaults
            </button>
            <span className="text-[11px] text-slate-400 dark:text-slate-500">{visibleCount} shown</span>
          </div>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="px-3 py-2 text-xs font-semibold text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
            >
              Cancel
            </button>
            <button
              onClick={() => onSave(local)}
              disabled={saving}
              className="px-4 py-2 text-xs font-semibold text-white bg-brandblue rounded-lg hover:bg-navy dark:bg-slate-950 disabled:opacity-50"
            >
              {saving ? "Saving…" : "Save Layout"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}