import { useEffect } from "react";

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
  if (match.projectId !== null) {
    return <ProjectWorkspace projectId={match.projectId} />;
  }
  return <ProjectsPage />;
}