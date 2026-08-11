import React, { createContext, useCallback, useContext, useRef, useState } from "react";

export type ToastKind = "success" | "error" | "info";

interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastContextType {
  /** Show a toast. Returns nothing — fire and forget. */
  toast: (message: string, kind?: ToastKind) => void;
  success: (message: string) => void;
  error: (message: string) => void;
  info: (message: string) => void;
}

const ToastContext = createContext<ToastContextType | undefined>(undefined);

const ICONS: Record<ToastKind, string> = {
  success: "✓",
  error: "✕",
  info: "ℹ",
};

const STYLES: Record<ToastKind, string> = {
  success: "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800/60 dark:bg-emerald-950/60 dark:text-emerald-300",
  error: "border-red-200 bg-red-50 text-red-800 dark:border-red-800/60 dark:bg-red-950/60 dark:text-red-300",
  info: "border-slate-200 bg-white text-slate-800 dark:border-noc-borderlit dark:bg-noc-panel2 dark:text-slate-200",
};

const ICON_STYLES: Record<ToastKind, string> = {
  success: "bg-emerald-500 text-white",
  error: "bg-red-500 text-white",
  info: "bg-brandblue text-white",
};

let idCounter = 0;

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const timers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());

  const dismiss = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
    const timer = timers.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
  }, []);

  const toast = useCallback(
    (message: string, kind: ToastKind = "info") => {
      const id = ++idCounter;
      setItems((prev) => [...prev, { id, kind, message }]);
      const timer = setTimeout(() => dismiss(id), kind === "error" ? 7000 : 4500);
      timers.current.set(id, timer);
    },
    [dismiss]
  );

  const value: ToastContextType = {
    toast,
    success: (message: string) => toast(message, "success"),
    error: (message: string) => toast(message, "error"),
    info: (message: string) => toast(message, "info"),
  };

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        aria-live="polite"
        className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2 w-full max-w-sm pointer-events-none"
      >
        {items.map((t) => (
          <div
            key={t.id}
            role="status"
            className={`pointer-events-auto flex items-start gap-3 rounded-xl border shadow-lg px-4 py-3 text-sm animate-toast-in ${STYLES[t.kind]}`}
          >
            <span
              className={`flex-shrink-0 flex items-center justify-center w-5 h-5 rounded-full text-[11px] font-bold mt-0.5 ${ICON_STYLES[t.kind]}`}
            >
              {ICONS[t.kind]}
            </span>
            <p className="flex-1 leading-snug break-words">{t.message}</p>
            <button
              onClick={() => dismiss(t.id)}
              aria-label="Dismiss notification"
              className="flex-shrink-0 opacity-50 hover:opacity-100 transition-opacity leading-none text-base"
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/**
 * Reports errors and confirmations consistently in-page instead of native
 * alert()/confirm() dialogs, which block the JS thread, can't be styled,
 * are frequently blocked/muted by browsers, and are unusable by automated
 * or screen-reader-driven flows.
 */
export function useToast(): ToastContextType {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within a ToastProvider");
  return ctx;
}

/** Convenience: extract a readable message from an axios-style error. */
export function errorMessage(err: unknown, fallback: string): string {
  const anyErr = err as { response?: { data?: { detail?: string } }; message?: string };
  return anyErr?.response?.data?.detail || anyErr?.message || fallback;
}