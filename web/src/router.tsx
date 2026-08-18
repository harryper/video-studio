/**
 * Tiny hand-rolled router.
 *
 * Two routes:
 *
 *   * ``/`` — projects dashboard
 *   * ``/projects/:id`` — single project workspace
 *
 * Anything else redirects to ``/``. Navigation uses real ``<a>`` links so
 * the browser back / forward stack keeps working without JS — a full
 * client-side router is YAGNI for two routes.
 */

import { useEffect, useState } from "react";

export const DEFAULT_PATH = "/";

export interface RouteMatch {
  pathname: string;
  projectId: string | null;
}

export function matchPath(pathname: string): RouteMatch {
  const cleaned = pathname.replace(/\/+$/, "") || "/";
  const segments = cleaned.split("/").filter(Boolean);
  if (segments.length === 0) return { pathname: cleaned, projectId: null };
  if (segments[0] === "projects" && segments.length >= 2) {
    return { pathname: cleaned, projectId: decodeURIComponent(segments[1]) };
  }
  return { pathname: cleaned, projectId: null };
}

export function normalizePath(pathname: string): string {
  return matchPath(pathname).pathname;
}

export function navigate(pathname: string): void {
  const target = normalizePath(pathname);
  if (typeof window !== "undefined" && window.location.pathname !== target) {
    window.history.pushState({}, "", target);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }
}

export function usePathname(): string {
  const [pathname, setPathname] = useState<string>(() =>
    typeof window === "undefined" ? DEFAULT_PATH : window.location.pathname,
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onPop = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  return normalizePath(pathname);
}

export interface LinkProps {
  to: string;
  children: React.ReactNode;
  className?: string;
}

export function Link({ to, children, className }: LinkProps): React.ReactElement {
  const target = normalizePath(to);
  return (
    <a href={target} className={className}>
      {children}
    </a>
  );
}

export interface RouterProviderProps {
  children: React.ReactNode;
}

export function RouterProvider({ children }: RouterProviderProps): React.ReactElement {
  return <>{children}</>;
}