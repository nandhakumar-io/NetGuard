import { createContext, useCallback, useContext, useMemo, useState, ReactNode } from "react";

export interface TerminalSession {
  id: string; // unique per opened window, NOT the device id -- opening the
  // same device twice (e.g. two engineers' tabs, or "I need a second
  // shell on this box") gets two independent sessions/tabs rather than
  // silently reusing one.
  deviceId: string;
  hostname: string;
}

interface TerminalSessionsContextValue {
  sessions: TerminalSession[];
  activeId: string | null;
  openTerminal: (deviceId: string, hostname: string) => void;
  closeTerminal: (id: string) => void;
  setActive: (id: string) => void;
}

const TerminalSessionsContext = createContext<TerminalSessionsContextValue | null>(null);

export function TerminalSessionsProvider({ children }: { children: ReactNode }) {
  const [sessions, setSessions] = useState<TerminalSession[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);

  const openTerminal = useCallback((deviceId: string, hostname: string) => {
    const id = `${deviceId}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    setSessions((prev) => [...prev, { id, deviceId, hostname }]);
    setActiveId(id);
  }, []);

  const closeTerminal = useCallback((id: string) => {
    setSessions((prev) => {
      const next = prev.filter((s) => s.id !== id);
      setActiveId((current) => {
        if (current !== id) return current;
        return next.length ? next[next.length - 1].id : null;
      });
      return next;
    });
  }, []);

  const setActive = useCallback((id: string) => setActiveId(id), []);

  const value = useMemo(
    () => ({ sessions, activeId, openTerminal, closeTerminal, setActive }),
    [sessions, activeId, openTerminal, closeTerminal, setActive]
  );

  return <TerminalSessionsContext.Provider value={value}>{children}</TerminalSessionsContext.Provider>;
}

export function useTerminalSessions(): TerminalSessionsContextValue {
  const ctx = useContext(TerminalSessionsContext);
  if (!ctx) throw new Error("useTerminalSessions must be used within a TerminalSessionsProvider");
  return ctx;
}