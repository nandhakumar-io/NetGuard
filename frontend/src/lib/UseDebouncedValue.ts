import { useEffect, useState } from "react";

/**
 * Returns a debounced copy of `value` that only updates after `delay`ms of
 * no further changes. Use for search/filter inputs so every keystroke
 * doesn't trigger a fetch or a full client-side re-filter.
 *
 *   const [query, setQuery] = useState("");
 *   const debouncedQuery = useDebouncedValue(query, 300);
 *   useEffect(() => { load(debouncedQuery); }, [debouncedQuery]);
 */
export function useDebouncedValue<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);

  return debounced;
}