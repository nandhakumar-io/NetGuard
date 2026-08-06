import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { GlobalSearchResponse, GlobalSearchResultItem } from "../lib/types";

/** Cmd+K (or Ctrl+K on non-Mac) global command palette -- unifies device
 * lookup, device groups, alerts, change requests, and config search (grep
 * across running configs) behind one shortcut instead of a separate
 * search box per page. Mounted once in Layout so it's available from
 * anywhere in the app. */

const SECTION_META: Array<{ key: keyof Omit<GlobalSearchResponse, "query">; label: string; icon: string }> = [
  { key: "devices", label: "Devices", icon: "🖧" },
  { key: "groups", label: "Groups", icon: "🗂️" },
  { key: "alerts", label: "Alerts", icon: "🚨" },
  { key: "change_requests", label: "Change Requests", icon: "📝" },
  { key: "configs", label: "Config matches", icon: "🔎" },
];

export default function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<GlobalSearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  // Open/close on Cmd+K / Ctrl+K from anywhere; Escape closes; also
  // opens on a synthetic "open-command-palette" event so the header
  // search button (for anyone who doesn't know/use the shortcut) can
  // trigger the exact same palette instead of duplicating it.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      } else if (e.key === "Escape") {
        setOpen(false);
      }
    };
    const onCustomOpen = () => setOpen(true);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("open-command-palette", onCustomOpen);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("open-command-palette", onCustomOpen);
    };
  }, []);

  useEffect(() => {
    if (open) {
      setQuery("");
      setResults(null);
      setActiveIndex(0);
      setTimeout(() => inputRef.current?.focus(), 10);
    }
  }, [open]);

  // Debounced search -- fires ~200ms after typing stops so every
  // keystroke doesn't trigger a fleet-wide config grep.
  useEffect(() => {
    if (!open || !query.trim()) {
      setResults(null);
      return;
    }
    setLoading(true);
    const handle = setTimeout(() => {
      api
        .get<GlobalSearchResponse>(`/search?query=${encodeURIComponent(query.trim())}`)
        .then((res) => setResults(res.data))
        .catch(() => setResults(null))
        .finally(() => setLoading(false));
    }, 200);
    return () => clearTimeout(handle);
  }, [query, open]);

  const flatResults: GlobalSearchResultItem[] = results
    ? SECTION_META.flatMap((s) => results[s.key])
    : [];

  const goTo = (item: GlobalSearchResultItem) => {
    setOpen(false);
    navigate(item.url);
  };

  const onKeyDownInput = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, flatResults.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter" && flatResults[activeIndex]) {
      e.preventDefault();
      goTo(flatResults[activeIndex]);
    }
  };

  if (!open) return null;

  let runningIndex = -1;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[12vh] bg-black/40 backdrop-blur-sm"
      onClick={() => setOpen(false)}
    >
      <div
        className="w-full max-w-xl bg-white dark:bg-slate-800 rounded-xl shadow-2xl border border-slate-200 dark:border-slate-700 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 px-4 py-3 border-b border-slate-200 dark:border-slate-700">
          <span className="text-slate-400">⌘K</span>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={onKeyDownInput}
            placeholder="Search devices, groups, alerts, change requests, configs…"
            className="flex-1 bg-transparent outline-none text-sm text-navy dark:text-white placeholder:text-slate-400"
          />
          {loading && <span className="text-[10px] text-slate-400">Searching…</span>}
        </div>

        <div className="max-h-[60vh] overflow-y-auto py-2">
          {!query.trim() && (
            <p className="px-4 py-6 text-center text-xs text-slate-400">
              Start typing to search across devices, groups, alerts, change requests, and configs.
            </p>
          )}
          {query.trim() && results && flatResults.length === 0 && !loading && (
            <p className="px-4 py-6 text-center text-xs text-slate-400">No matches for "{query}".</p>
          )}
          {results &&
            SECTION_META.map((section) => {
              const items = results[section.key];
              if (items.length === 0) return null;
              return (
                <div key={section.key} className="mb-1">
                  <p className="px-4 pt-2 pb-1 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                    {section.icon} {section.label}
                  </p>
                  {items.map((item) => {
                    runningIndex += 1;
                    const isActive = runningIndex === activeIndex;
                    return (
                      <button
                        key={`${section.key}-${item.id}`}
                        onClick={() => goTo(item)}
                        onMouseEnter={() => setActiveIndex(runningIndex)}
                        className={`w-full text-left px-4 py-2 flex flex-col gap-0.5 transition-colors ${
                          isActive ? "bg-brandblue/10 dark:bg-brandblue/20" : "hover:bg-slate-50 dark:hover:bg-slate-700/50"
                        }`}
                      >
                        <span className="text-sm font-medium text-navy dark:text-white truncate">{item.title}</span>
                        {item.subtitle && (
                          <span className="text-xs text-slate-400 dark:text-slate-500 truncate">{item.subtitle}</span>
                        )}
                      </button>
                    );
                  })}
                </div>
              );
            })}
        </div>

        <div className="px-4 py-2 border-t border-slate-200 dark:border-slate-700 text-[10px] text-slate-400 flex items-center gap-3">
          <span>↑↓ navigate</span>
          <span>↵ open</span>
          <span>esc close</span>
        </div>
      </div>
    </div>
  );
}