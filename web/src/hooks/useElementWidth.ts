import { useLayoutEffect, useRef, useState } from 'react';

/**
 * Width of an element: measured before first paint, then tracked with ResizeObserver
 * (falls back to `initial` where layout is unavailable, e.g. jsdom).
 */
export function useElementWidth<T extends HTMLElement>(initial = 720) {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(initial);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = (w: number) => {
      if (w > 0) setWidth(Math.round(w));
    };
    update(el.getBoundingClientRect().width);
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      update(entries[0]?.contentRect.width ?? 0);
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
    };
  }, []);
  return [ref, width] as const;
}
