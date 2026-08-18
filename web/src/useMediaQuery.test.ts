/**
 * useMediaQuery — React hook backed by window.matchMedia.
 *
 *  * Default: subscribes to window.matchMedia(query) and returns
 *    ``matches`` reactively.
 *  * Override: callers can pin the value (tests, storybooks) so the
 *    hook ignores the live viewport.
 *  * Cleans up its listener on unmount.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useMediaQuery } from "./useMediaQuery";

interface ListenerHandle {
  matches: boolean;
  listeners: Array<EventListener>;
}

function installMatchMedia(initial: boolean): {
  setMatches: (next: boolean) => void;
} {
  const handles = new Map<string, ListenerHandle>();
  const matchMedia = vi.fn().mockImplementation(
    (query: string): MediaQueryList => {
      const handle: ListenerHandle = handles.get(query) ?? {
        matches: initial,
        listeners: [],
      };
      handles.set(query, handle);
      const onAdd: EventListener = () => undefined;
      void onAdd;
      const mql: MediaQueryList = {
        matches: handle.matches,
        media: query,
        onchange: null,
        addEventListener: (
          _type: keyof MediaQueryListEventMap | string,
          cb: EventListenerOrEventListenerObject,
        ): void => {
          const listener: EventListener =
            typeof cb === "function" ? cb : cb.handleEvent;
          handle.listeners.push(listener);
        },
        removeEventListener: (
          _type: keyof MediaQueryListEventMap | string,
          cb: EventListenerOrEventListenerObject,
        ): void => {
          const listener: EventListener =
            typeof cb === "function" ? cb : cb.handleEvent;
          handle.listeners = handle.listeners.filter((l) => l !== listener);
        },
        addListener: (cb: (this: MediaQueryList, ev: MediaQueryListEvent) => void): void => {
          const listener: EventListener = (event): void => {
            cb.call({} as MediaQueryList, event as MediaQueryListEvent);
          };
          handle.listeners.push(listener);
        },
        removeListener: (cb: (this: MediaQueryList, ev: MediaQueryListEvent) => void): void => {
          const listener: EventListener = (event): void => {
            cb.call({} as MediaQueryList, event as MediaQueryListEvent);
          };
          handle.listeners = handle.listeners.filter((l) => l !== listener);
        },
        dispatchEvent: (): boolean => true,
      };
      return mql;
    },
  );
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: matchMedia,
  });
  return {
    setMatches(next: boolean): void {
      for (const handle of handles.values()) {
        handle.matches = next;
        const event = { matches: next, media: "" } as unknown as Event;
        for (const listener of handle.listeners) {
          listener(event);
        }
      }
    },
  };
}

describe("useMediaQuery", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns the override when one is provided (test pin)", () => {
    installMatchMedia(true);
    const { result, unmount } = renderHook(() => useMediaQuery(true));
    expect(result.current).toBe(true);
    unmount();
  });

  it("returns false override when override is false even if viewport is narrow", () => {
    installMatchMedia(true);
    const { result, unmount } = renderHook(() => useMediaQuery(false));
    expect(result.current).toBe(false);
    unmount();
  });

  it("queries window.matchMedia with the supplied query string", () => {
    const matcher = installMatchMedia(false);
    const { unmount } = renderHook(() =>
      useMediaQuery(undefined, "(min-width: 1024px)"),
    );
    expect(window.matchMedia).toHaveBeenCalledWith("(min-width: 1024px)");
    unmount();
    matcher.setMatches(true);
  });

  it("reacts to viewport changes by re-rendering with the new value", () => {
    const matcher = installMatchMedia(false);
    const { result, unmount } = renderHook(() =>
      useMediaQuery(undefined, "(max-width: 768px)"),
    );
    expect(result.current).toBe(false);
    act(() => {
      matcher.setMatches(true);
    });
    expect(result.current).toBe(true);
    unmount();
  });

  it("removes its listener on unmount so the hook does not leak", () => {
    const matcher = installMatchMedia(false);
    const removeSpy = vi.fn();
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: vi.fn().mockImplementation((query: string) => {
        const mql = {
          matches: false,
          media: query,
          onchange: null,
          addEventListener: () => undefined,
          removeEventListener: removeSpy,
          addListener: () => undefined,
          removeListener: () => undefined,
          dispatchEvent: () => true,
        };
        return mql as unknown as MediaQueryList;
      }),
    });
    const { unmount } = renderHook(() =>
      useMediaQuery(undefined, "(max-width: 768px)"),
    );
    unmount();
    expect(removeSpy).toHaveBeenCalled();
    matcher.setMatches(true);
  });
});