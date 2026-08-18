/**
 * Tiny hand-rolled router.
 *
 * Supported routes:
 *
 *   * ``/`` — projects dashboard
 *   * ``/projects/:id`` — single project workspace
 *   * ``/projects/:id/pitches`` — pitch selection
 *   * ``/projects/:id/drafts/:draftArtifactId`` — draft review
 *
 * Anything else redirects to ``/``. Navigation uses real ``<a>`` links so
 * the browser back / forward stack keeps working without JS — a full
 * client-side router is YAGNI for these routes.
 */

import { useEffect, useState } from "react";

export const DEFAULT_PATH = "/";

export type RouteName = "dashboard" | "workspace" | "pitches" | "draft";

export interface RouteMatch {
  pathname: string;
  name: RouteName;
  projectId: string | null;
  draftArtifactId: string | null;
}

export function matchPath(pathname: string): RouteMatch {
  const cleaned = pathname.replace(/\/+$/, "") || "/";
  const segments = cleaned.split("/").filter(Boolean);
  if (segments.length === 0) {
    return { pathname: cleaned, name: "dashboard", projectId: null, draftArtifactId: null };
  }
  if (segments[0] !== "projects") {
    return { pathname: cleaned, name: "dashboard", projectId: null, draftArtifactId: null };
  }
  if (segments.length < 2) {
    return { pathname: cleaned, name: "dashboard", projectId: null, draftArtifactId: null };
  }
  const projectId = decodeURIComponent(segments[1]);
  if (segments.length === 2) {
    return { pathname: cleaned, name: "workspace", projectId, draftArtifactId: null };
  }
  if (segments[2] === "pitches" && segments.length === 3) {
    return { pathname: cleaned, name: "pitches", projectId, draftArtifactId: null };
  }
  if (segments[2] === "drafts" && segments.length === 4) {
    return {
      pathname: cleaned,
      name: "draft",
      projectId,
      draftArtifactId: decodeURIComponent(segments[3]),
    };
  }
  return { pathname: cleaned, name: "dashboard", projectId: null, draftArtifactId: null };
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

export function useRoute(): RouteMatch {
  return matchPath(usePathname());
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