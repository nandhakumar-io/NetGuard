import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { GlobalSearchResponse, GlobalSearchResultItem } from "../lib/types";

/** Cmd+K (or Ctrl+K on non-Mac) global command palette -- unifies device
 * lookup, device groups, alerts, change requests, and config search (grep
 * across running configs) behind one shortcut instead of a separate
 * search box per page. Mounted once in Layout so it's available from
 * anywhere in the app. */

const SECTION_META: Array<{ key: keyof Omit<GlobalSearchResponse, "query" | "is_ip_query">; label: string; icon: string }> = [
  { key: "devices", label: "Devices", icon: "🖧" },
  { key: "groups", label: "Groups", icon: "🗂️" },
  { key: "alerts", label: "Alerts", icon: "🚨" },
  { key: "change_requests", label: "Change Requests", icon: "📝" },
  { key: "templates", label: "Config Templates", icon: "📄" },
  { key: "incidents", label: "Incidents", icon: "🔥" },
  { key: "configs", label: "Config matches", icon: "🔎" },
];

// --- Recent/frequent selections -------------------------------------
// Cmd+K has no fixed command list to rank (this palette is search-only),
// so "recent history" means: remember what the person actually opened
// from here before, and surface those first when the box is empty --
// the same "most-used surfaces first" idea a command list would give,
// applied to search results instead. Stored client-side (localStorage)
// since there's no per-user backend preference store for this yet, and
// it's inherently per-browser/per-shift info anyway.
const RECENTS_KEY = "netguard_palette_recents_v1";
const MAX_RECENTS = 8;

type RecentEntry = GlobalSearchResultItem & {
  section: string;
  sectionLabel: string;
  icon: string;
  hitCount: number;
  lastUsedAt: number;
};

function loadRecents(): RecentEntry[] {
  try {
    const raw = localStorage.getItem(RECENTS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function recordRecent(item: GlobalSearchResultItem, section: string, sectionLabel: string, icon: string) {
  const existing = loadRecents();
  const idx = existing.findIndex((r) => r.section === section && r.id === item.id);
  if (idx >= 0) {
    existing[idx] = { ...existing[idx], ...item, hitCount: existing[idx].hitCount + 1, lastUsedAt: Date.now() };
  } else {
    existing.push({ ...item, section, sectionLabel, icon, hitCount: 1, lastUsedAt: Date.now() });
  }
  // Rank by frequency first, recency as tiebreaker -- a page you visit
  // every shift should outrank something you clicked into once
  // yesterday, but among equally-frequent items the most recent wins.
  existing.sort((a, b) => b.hitCount - a.hitCount || b.lastUsedAt - a.lastUsedAt);
  const trimmed = existing.slice(0, MAX_RECENTS);
  try {
    localStorage.setItem(RECENTS_KEY, JSON.stringify(trimmed));
  } catch {
    // storage full/unavailable -- recents are a nice-to-have, fail silently
  }
  return trimmed;
}

export default function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<GlobalSearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const [recents, setRecents] = useState<RecentEntry[]>([]);
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
      setRecents(loadRecents());
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

  const showingRecents = !query.trim() && recents.length > 0;

  const flatResults: Array<GlobalSearchResultItem & { section: string; sectionLabel: string; icon: string }> = useMemo(() => {
    if (showingRecents) return recents;
    if (!results) return [];
    return SECTION_META.flatMap((s) =>
      results[s.key].map((item) => ({ ...item, section: s.key, sectionLabel: s.label, icon: s.icon }))
    );
  }, [showingRecents, recents, results]);

  const goTo = (item: GlobalSearchResultItem, section: string, sectionLabel: string, icon: string) => {
    setRecents(recordRecent(item, section, sectionLabel, icon));
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
      const item = flatResults[activeIndex];
      goTo(item, item.section, item.sectionLabel, item.icon);
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
            placeholder="Search devices, groups, alerts, change requests, templates, incidents, configs, or an IP/CIDR…"
            className="flex-1 bg-transparent outline-none text-sm text-navy dark:text-white placeholder:text-slate-400"
          />
          {loading && <span className="text-[10px] text-slate-400">Searching…</span>}
        </div>

        <div className="max-h-[60vh] overflow-y-auto py-2">
          {!query.trim() && recents.length === 0 && (
            <p className="px-4 py-6 text-center text-xs text-slate-400">
              Start typing to search across devices, groups, alerts, change requests, templates, incidents, and
              configs — or paste an IP/CIDR (e.g. 10.20.0.0/24) to see what's using it.
            </p>
          )}
          {showingRecents && (
            <div className="mb-1">
              <p className="px-4 pt-2 pb-1 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                🕐 Recent &amp; frequent
              </p>
              {recents.map((item) => {
                runningIndex += 1;
                const isActive = runningIndex === activeIndex;
                return (
                  <button
                    key={`recent-${item.section}-${item.id}`}
                    onClick={() => goTo(item, item.section, item.sectionLabel, item.icon)}
                    onMouseEnter={() => setActiveIndex(runningIndex)}
                    className={`w-full text-left px-4 py-2 flex items-center justify-between gap-2 transition-colors ${
                      isActive ? "bg-brandblue/10 dark:bg-brandblue/20" : "hover:bg-slate-50 dark:hover:bg-slate-700/50"
                    }`}
                  >
                    <span className="flex flex-col gap-0.5 min-w-0">
                      <span className="text-sm font-medium text-navy dark:text-white truncate">
                        {item.icon} {item.title}
                      </span>
                      {item.subtitle && (
                        <span className="text-xs text-slate-400 dark:text-slate-500 truncate">{item.subtitle}</span>
                      )}
                    </span>
                    <span className="text-[9px] font-bold uppercase tracking-wide text-slate-300 dark:text-slate-500 shrink-0">
                      {item.sectionLabel}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
          {query.trim() && results && flatResults.length === 0 && !loading && (
            <p className="px-4 py-6 text-center text-xs text-slate-400">No matches for "{query}".</p>
          )}
          {!showingRecents && results && results.is_ip_query && (
            <p className="px-4 pt-2 pb-1 text-[10px] text-slate-400">
              Matched as an IP/CIDR range — showing devices with a management or interface address inside it.
            </p>
          )}
          {!showingRecents &&
            results &&
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
                        onClick={() => goTo(item, section.key, section.label, section.icon)}
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