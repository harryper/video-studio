import { useEffect, useState } from "react";

import { getCsrfToken } from "./api/client";
import { DraftReviewPage } from "./pages/DraftReviewPage";
import { LoginPage } from "./pages/LoginPage";
import { PitchReviewPage } from "./pages/PitchReviewPage";
import { ProjectWorkspace } from "./pages/ProjectWorkspace";
import { ProjectsPage } from "./pages/ProjectsPage";
import { matchPath, navigate, usePathname } from "./router";

export default function App(): React.ReactElement {
  const pathname = usePathname();
  // Auth gate: the SPA's API client stores the CSRF token in module state
  // after a successful login; we mirror it as a render trigger so a
  // session refresh doesn't require a manual reload.
  const [, setAuthTick] = useState<number>(0);

  useEffect(() => {
    const match = matchPath(pathname);
    if (match.projectId === null && pathname !== "/") {
      navigate("/");
    }
  }, [pathname]);

  if (getCsrfToken() === null) {
    return <LoginPage onLoggedIn={() => setAuthTick((n) => n + 1)} />;
  }

  const match = matchPath(pathname);
  if (match.name === "pitches" && match.projectId !== null) {
    return <PitchReviewPage projectId={match.projectId} />;
  }
  if (match.name === "draft" && match.projectId !== null && match.draftArtifactId !== null) {
    return (
      <DraftReviewPage
        projectId={match.projectId}
        draftArtifactId={match.draftArtifactId}
      />
    );
  }
  if (match.projectId !== null) {
    return <ProjectWorkspace projectId={match.projectId} />;
  }
  return <ProjectsPage />;
}