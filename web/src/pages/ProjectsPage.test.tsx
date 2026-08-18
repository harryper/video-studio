import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { ProjectSummary } from "../api/types";

const { listProjectsMock, createProjectMock } = vi.hoisted(() => {
  return {
    listProjectsMock: vi.fn(),
    createProjectMock: vi.fn(),
  };
});

vi.mock("../api/client", () => ({
  listProjects: listProjectsMock,
  createProject: createProjectMock,
}));

vi.mock("../router", () => ({
  usePathname: () => "/",
  navigate: vi.fn(),
  Link: ({ to, children, ...rest }: { to: string; children: React.ReactNode; [key: string]: unknown }) => (
    <a href={to} {...(rest as object)}>
      {children}
    </a>
  ),
  RouterProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

import { ProjectsPage } from "./ProjectsPage";

const SAMPLE: ProjectSummary[] = [
  {
    id: "p1",
    title: "台风科普",
    topic: "台风",
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-02T00:00:00Z",
    latest_stage: "pitches",
    latest_job_status: "finished",
  },
  {
    id: "p2",
    title: "海洋之声",
    topic: "海浪",
    created_at: "2026-08-03T00:00:00Z",
    updated_at: "2026-08-04T00:00:00Z",
    latest_stage: "draft",
    latest_job_status: "running",
  },
  {
    id: "p3",
    title: "海洋之声 失败",
    topic: "潮汐",
    created_at: "2026-08-05T00:00:00Z",
    updated_at: "2026-08-06T00:00:00Z",
    latest_stage: "speech",
    latest_job_status: "failed",
  },
];

describe("ProjectsPage", () => {
  beforeEach(() => {
    listProjectsMock.mockReset();
    createProjectMock.mockReset();
  });

  it("renders project with editorial label", async () => {
    listProjectsMock.mockResolvedValue([SAMPLE[0]]);
    render(<ProjectsPage />);

    expect(await screen.findByText("等待选切口")).toBeVisible();
    expect(screen.queryByText("pitches")).not.toBeInTheDocument();
  });

  it("lists each project's title and topic", async () => {
    listProjectsMock.mockResolvedValue(SAMPLE);
    render(<ProjectsPage />);

    expect(await screen.findByText("台风科普")).toBeVisible();
    expect(screen.getByText("台风")).toBeVisible();
    expect(screen.getByText("海洋之声")).toBeVisible();
  });

  it("creates project via form", async () => {
    listProjectsMock.mockResolvedValue([]);
    const created: ProjectSummary = {
      id: "new-1",
      title: "量子纠缠",
      topic: "量子通信",
      created_at: "2026-08-10T00:00:00Z",
      updated_at: "2026-08-10T00:00:00Z",
      latest_stage: "diagnosis",
      latest_job_status: "queued",
    };
    createProjectMock.mockResolvedValue(created);

    const user = userEvent.setup();
    render(<ProjectsPage />);

    await user.type(screen.getByLabelText("标题"), "量子纠缠");
    await user.type(screen.getByLabelText("主题"), "量子通信");
    await user.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() => {
      expect(createProjectMock).toHaveBeenCalledWith({
        title: "量子纠缠",
        topic: "量子通信",
      });
    });
    expect(await screen.findByText("量子纠缠")).toBeInTheDocument();
    expect(screen.getByText("量子通信")).toBeInTheDocument();
  });

  it("filters by stage", async () => {
    listProjectsMock.mockResolvedValue(SAMPLE);
    render(<ProjectsPage />);

    await screen.findByText("台风科普");

    const stageSelect = screen.getByLabelText("阶段") as HTMLSelectElement;
    await userEvent.setup().selectOptions(stageSelect, "draft");

    expect(listProjectsMock).toHaveBeenLastCalledWith({
      stage: "draft",
      status: null,
    });
  });

  it("filters by status", async () => {
    listProjectsMock.mockResolvedValue(SAMPLE);
    render(<ProjectsPage />);

    await screen.findByText("台风科普");

    const statusSelect = screen.getByLabelText("状态") as HTMLSelectElement;
    await userEvent.setup().selectOptions(statusSelect, "running");

    expect(listProjectsMock).toHaveBeenLastCalledWith({
      stage: null,
      status: "running",
    });
  });

  it("navigates to workspace on row click", async () => {
    listProjectsMock.mockResolvedValue([SAMPLE[0]]);
    render(<ProjectsPage />);
    const link = await screen.findByRole("link", { name: /台风科普/ });
    expect(link).toHaveAttribute("href", "/projects/p1");
  });
});