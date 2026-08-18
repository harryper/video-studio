import { useEffect } from "react";

import { DraftReviewPage } from "./pages/DraftReviewPage";
import { PitchReviewPage } from "./pages/PitchReviewPage";
import { ProjectWorkspace } from "./pages/ProjectWorkspace";
import { ProjectsPage } from "./pages/ProjectsPage";
import { matchPath, navigate, usePathname } from "./router";

export default function App(): React.ReactElement {
  const pathname = usePathname();

  useEffect(() => {
    const match = matchPath(pathname);
    if (match.projectId === null && pathname !== "/") {
      navigate("/");
    }
  }, [pathname]);

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