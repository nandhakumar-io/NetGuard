import React, { useCallback, useEffect, useRef, useState } from "react";
import { WebTerminal } from "./WebTerminal";
import { useTerminalSessions } from "../lib/terminalSessions";

const DEFAULT_SIZE = { width: 900, height: 560 };
const MIN_SIZE = { width: 480, height: 320 };

function clampToViewport(pos: { x: number; y: number }, size: { width: number; height: number }) {
  const maxX = Math.max(0, window.innerWidth - 80);
  const maxY = Math.max(0, window.innerHeight - 40);
  return {
    x: Math.min(Math.max(pos.x, -size.width + 120), maxX),
    y: Math.min(Math.max(pos.y, 0), maxY),
  };
}

/** Single floating window holding every open terminal session as a tab.
 * Sessions stay mounted (just hidden) while inactive so switching tabs
 * doesn't drop the WebSocket -- only closing a tab tears its session down. */
export function TerminalWindowManager() {
  const { sessions, activeId, closeTerminal, setActive } = useTerminalSessions();
  const [pos, setPos] = useState(() => ({
    x: Math.max(40, window.innerWidth - DEFAULT_SIZE.width - 60),
    y: 90,
  }));
  const [size, setSize] = useState(DEFAULT_SIZE);
  const [minimized, setMinimized] = useState(false);
  const dragState = useRef<{ startX: number; startY: number; origX: number; origY: number } | null>(null);
  const resizeState = useRef<{ startX: number; startY: number; origW: number; origH: number } | null>(null);

  const onDragMove = useCallback((e: MouseEvent) => {
    if (!dragState.current) return;
    const { startX, startY, origX, origY } = dragState.current;
    setPos(clampToViewport({ x: origX + (e.clientX - startX), y: origY + (e.clientY - startY) }, size));
  }, [size]);

  const onDragEnd = useCallback(() => {
    dragState.current = null;
    window.removeEventListener("mousemove", onDragMove);
    window.removeEventListener("mouseup", onDragEnd);
  }, [onDragMove]);

  const startDrag = (e: React.MouseEvent) => {
    // Don't start a window drag from the tab bar's own controls.
    if ((e.target as HTMLElement).closest("[data-no-drag]")) return;
    dragState.current = { startX: e.clientX, startY: e.clientY, origX: pos.x, origY: pos.y };
    window.addEventListener("mousemove", onDragMove);
    window.addEventListener("mouseup", onDragEnd);
  };

  const onResizeMove = useCallback((e: MouseEvent) => {
    if (!resizeState.current) return;
    const { startX, startY, origW, origH } = resizeState.current;
    setSize({
      width: Math.max(MIN_SIZE.width, origW + (e.clientX - startX)),
      height: Math.max(MIN_SIZE.height, origH + (e.clientY - startY)),
    });
  }, []);

  const onResizeEnd = useCallback(() => {
    resizeState.current = null;
    window.removeEventListener("mousemove", onResizeMove);
    window.removeEventListener("mouseup", onResizeEnd);
  }, [onResizeMove]);

  const startResize = (e: React.MouseEvent) => {
    e.stopPropagation();
    resizeState.current = { startX: e.clientX, startY: e.clientY, origW: size.width, origH: size.height };
    window.addEventListener("mousemove", onResizeMove);
    window.addEventListener("mouseup", onResizeEnd);
  };

  // Keep the window on-screen if the browser is resized.
  useEffect(() => {
    const onResize = () => setPos((p) => clampToViewport(p, size));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [size]);

  // Dragging the corner handle resizes this panel but isn't a browser
  // window resize, so it wouldn't otherwise reach WebTerminal's own
  // 'resize' listener (which is what tells its FitAddon to re-measure).
  // Nudge the same listener so the active terminal re-fits to the new
  // panel dimensions.
  useEffect(() => {
    window.dispatchEvent(new Event("resize"));
  }, [size.width, size.height, minimized]);

  useEffect(() => {
    return () => {
      window.removeEventListener("mousemove", onDragMove);
      window.removeEventListener("mouseup", onDragEnd);
      window.removeEventListener("mousemove", onResizeMove);
      window.removeEventListener("mouseup", onResizeEnd);
    };
  }, [onDragMove, onDragEnd, onResizeMove, onResizeEnd]);

  if (sessions.length === 0) return null;

  return (
    <div
      className="fixed z-[100] bg-slate-900 border border-slate-700 rounded-xl shadow-2xl flex flex-col overflow-hidden select-none"
      style={{
        left: pos.x,
        top: pos.y,
        width: size.width,
        height: minimized ? "auto" : size.height,
      }}
    >
      {/* Title bar: drag handle + tab strip */}
      <div
        onMouseDown={startDrag}
        className="flex items-stretch bg-slate-800 border-b border-slate-700 cursor-move"
      >
        <div className="flex items-end gap-1 px-2 pt-2 overflow-x-auto flex-1 min-w-0">
          {sessions.map((s) => (
            <div
              key={s.id}
              data-no-drag
              onMouseDown={(e) => e.stopPropagation()}
              onClick={() => setActive(s.id)}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-t-md text-xs font-mono cursor-pointer whitespace-nowrap ${
                s.id === activeId
                  ? "bg-slate-900 text-slate-100 border border-b-0 border-slate-700"
                  : "bg-slate-800 text-slate-400 hover:text-slate-200 border border-b-0 border-transparent"
              }`}
              title={s.hostname}
            >
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 shrink-0" />
              {s.hostname}
              <button
                data-no-drag
                onClick={(e) => {
                  e.stopPropagation();
                  closeTerminal(s.id);
                }}
                className="text-slate-500 hover:text-red-400 leading-none bg-transparent border-0 px-0.5"
                aria-label={`Close terminal for ${s.hostname}`}
              >
                ×
              </button>
            </div>
          ))}
        </div>
        <div className="flex items-center gap-1 px-2" data-no-drag onMouseDown={(e) => e.stopPropagation()}>
          <button
            onClick={() => setMinimized((m) => !m)}
            className="text-slate-400 hover:text-white text-xs font-bold bg-transparent border-0 px-2 py-1"
            title={minimized ? "Restore" : "Minimize"}
          >
            {minimized ? "▢" : "—"}
          </button>
        </div>
      </div>

      {!minimized && (
        <div className="relative flex-grow overflow-hidden p-1">
          {sessions.map((s) => (
            <div
              key={s.id}
              style={{ display: s.id === activeId ? "block" : "none", width: "100%", height: "100%" }}
            >
              <WebTerminal deviceId={s.deviceId} active={s.id === activeId} />
            </div>
          ))}
          {/* Resize handle, bottom-right corner */}
          <div
            onMouseDown={startResize}
            className="absolute bottom-0 right-0 w-4 h-4 cursor-nwse-resize"
            title="Resize"
          >
            <svg viewBox="0 0 16 16" className="w-full h-full text-slate-600">
              <path d="M14 2 L2 14 M14 8 L8 14 M14 14 L14 14" stroke="currentColor" strokeWidth="1.5" fill="none" />
            </svg>
          </div>
        </div>
      )}
    </div>
  );
}