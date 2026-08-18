/**
 * useMediaQuery — React hook backed by window.matchMedia.
 *
 *  * When ``override`` is provided (test pin, storybook fixture, etc.)
 *    the hook returns that value verbatim and does NOT touch the
 *    viewport — letting callers simulate a breakpoint without
 *    resizing jsdom.
 *  * Otherwise the hook subscribes to ``window.matchMedia(query)``,
 *    returns ``matches`` reactively, and cleans up its listener on
 *    unmount.
 *
 * The query argument is a real CSS media query string so any caller
 * can drive the breakpoint they care about. The default for the draft
 * review page is ``(max-width: 768px)`` to match the spec §10.4
 * mobile breakpoint.
 */

import { useEffect, useState } from "react";

export function useMediaQuery(
  override?: boolean,
  query: string = "(max-width: 768px)",
): boolean {
  const [matches, setMatches] = useState<boolean>(() => {
    if (override !== undefined) return override;
    if (typeof window === "undefined") return false;
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    if (override !== undefined) {
      setMatches(override);
      return;
    }
    if (typeof window === "undefined") return;
    const mql = window.matchMedia(query);
    setMatches(mql.matches);
    const handler = (event: MediaQueryListEvent): void => {
      setMatches(event.matches);
    };
    mql.addEventListener("change", handler);
    return () => {
      mql.removeEventListener("change", handler);
    };
  }, [override, query]);

  return matches;
}