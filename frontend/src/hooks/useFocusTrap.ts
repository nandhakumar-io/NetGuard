/**
 * useFocusTrap
 *
 * When `enabled` is true, traps keyboard focus inside `containerRef`.
 * On mount it focuses the first focusable descendant; Tab/Shift-Tab cycle
 * within the container; Escape calls `onEscape` (if provided).
 * On cleanup it returns focus to whichever element had focus before the
 * trap was activated -- so a modal trigger button regains focus
 * automatically when the modal closes.
 *
 * Usage:
 *   const ref = useRef<HTMLDivElement>(null);
 *   useFocusTrap(ref, isOpen, () => setIsOpen(false));
 */
import { useEffect, useRef } from "react";

const FOCUSABLE_SELECTORS = [
  "a[href]",
  "area[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export function useFocusTrap(
  containerRef: React.RefObject<HTMLElement | null>,
  enabled: boolean,
  onEscape?: () => void,
) {
  // Remember the previously-focused element to restore on cleanup.
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!enabled) return;

    previouslyFocused.current = document.activeElement as HTMLElement | null;

    const container = containerRef.current;
    if (!container) return;

    const getFocusable = () =>
      Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTORS)).filter(
        (el) => !el.closest("[aria-hidden='true']"),
      );

    // Focus the first focusable element on trap activation.
    const first = getFocusable()[0];
    if (first) {
      // Defer one tick so the element is painted before receiving focus
      // (relevant when the container is rendered the same render cycle).
      const raf = requestAnimationFrame(() => first.focus());
      return () => cancelAnimationFrame(raf);
    }
  }, [enabled, containerRef]);

  useEffect(() => {
    if (!enabled) return;

    const container = containerRef.current;
    if (!container) return;

    const getFocusable = () =>
      Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTORS)).filter(
        (el) => !el.closest("[aria-hidden='true']"),
      );

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onEscape?.();
        return;
      }
      if (e.key !== "Tab") return;

      const focusable = getFocusable();
      if (focusable.length === 0) {
        e.preventDefault();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement as HTMLElement;

      if (e.shiftKey) {
        // Shift+Tab: wrap from first → last
        if (active === first || !container.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else {
        // Tab: wrap from last → first
        if (active === last || !container.contains(active)) {
          e.preventDefault();
          first.focus();
        }
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      // Restore focus to the element that had it before the trap.
      previouslyFocused.current?.focus();
    };
  }, [enabled, containerRef, onEscape]);
}
