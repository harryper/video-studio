import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { ProjectSummary } from "../api/types";

const { listProjectsMock, createProjectMock, listArtifactsMock } = vi.hoisted(() => {
  return {
    listProjectsMock: vi.fn(),
    createProjectMock: vi.fn(),
    listArtifactsMock: vi.fn(),
  };
});

vi.mock("../api/client", () => ({
  listProjects: listProjectsMock,
  createProject: createProjectMock,
  listArtifacts: listArtifactsMock,
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

const DRAFT_PROJECT: ProjectSummary = {
  id: "p-draft",
  title: "草稿项目",
  topic: "台风",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-02T00:00:00Z",
  latest_stage: "research",
  latest_job_status: "running",
};

const AWAITING_PITCH_PROJECT: ProjectSummary = {
  id: "p1",
  title: "台风科普",
  topic: "台风",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-02T00:00:00Z",
  latest_stage: "pitches",
  latest_job_status: "finished",
};

const AWAITING_REVIEW_PROJECT: ProjectSummary = {
  id: "p2",
  title: "海洋之声",
  topic: "海浪",
  created_at: "2026-08-03T00:00:00Z",
  updated_at: "2026-08-04T00:00:00Z",
  latest_stage: "draft",
  latest_job_status: "running",
};

const FINALIZED_PROJECT: ProjectSummary = {
  id: "p-finalized",
  title: "已定稿项目",
  topic: "冰川",
  created_at: "2026-08-05T00:00:00Z",
  updated_at: "2026-08-06T00:00:00Z",
  latest_stage: "approval",
  latest_job_status: "running",
};

const IN_PRODUCTION_PROJECT: ProjectSummary = {
  id: "p3",
  title: "海洋之声 失败",
  topic: "潮汐",
  created_at: "2026-08-05T00:00:00Z",
  updated_at: "2026-08-06T00:00:00Z",
  latest_stage: "speech",
  latest_job_status: "failed",
};

const COMPLETED_PROJECT: ProjectSummary = {
  id: "p-completed",
  title: "完成项目",
  topic: "极光",
  created_at: "2026-08-07T00:00:00Z",
  updated_at: "2026-08-08T00:00:00Z",
  latest_stage: "approval",
  latest_job_status: "finished",
};

const SAMPLE: ProjectSummary[] = [
  AWAITING_PITCH_PROJECT,
  AWAITING_REVIEW_PROJECT,
  IN_PRODUCTION_PROJECT,
];

function bucketSection(label: string): HTMLElement {
  // Each bucket is rendered as a <section> with aria-label={BUCKET_LABEL[...]+"项目"} (or similar).
  // Look for the heading text since aria-label may vary.
  const heading = screen.getByRole("heading", { name: label });
  return heading.closest("section") as HTMLElement;
}

describe("ProjectsPage", () => {
  beforeEach(() => {
    listProjectsMock.mockReset();
    createProjectMock.mockReset();
    listArtifactsMock.mockReset();
    listArtifactsMock.mockResolvedValue([]);
  });

  it("renders project with editorial label inside the awaiting-pitch bucket", async () => {
    listProjectsMock.mockResolvedValue([AWAITING_PITCH_PROJECT]);
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
    expect(screen.getByText("海洋之声 失败")).toBeVisible();
  });

  it("renders the 6 spec §10.1 bucket sections", async () => {
    listProjectsMock.mockResolvedValue(SAMPLE);
    render(<ProjectsPage />);
    await screen.findByText("台风科普");

    for (const heading of ["草稿", "等待选切口", "等待审稿", "已定稿", "制作中", "已完成"]) {
      expect(
        screen.getByRole("heading", { name: heading }),
        `expected bucket heading "${heading}" to be present`,
      ).toBeVisible();
    }
  });

  it("places each project in its editorial bucket", async () => {
    listProjectsMock.mockResolvedValue([
      DRAFT_PROJECT,
      AWAITING_PITCH_PROJECT,
      AWAITING_REVIEW_PROJECT,
      FINALIZED_PROJECT,
      IN_PRODUCTION_PROJECT,
      COMPLETED_PROJECT,
    ]);
    render(<ProjectsPage />);
    await screen.findByText("完成项目");

    const draft = within(bucketSection("草稿"));
    expect(draft.getByText("草稿项目")).toBeVisible();

    const pitch = within(bucketSection("等待选切口"));
    expect(pitch.getByText("台风科普")).toBeVisible();

    const review = within(bucketSection("等待审稿"));
    expect(review.getByText("海洋之声")).toBeVisible();

    const finalized = within(bucketSection("已定稿"));
    expect(finalized.getByText("已定稿项目")).toBeVisible();

    const production = within(bucketSection("制作中"));
    expect(production.getByText("海洋之声 失败")).toBeVisible();

    const completed = within(bucketSection("已完成"));
    expect(completed.getByText("完成项目")).toBeVisible();
  });

  it("rows in the awaiting-pitch bucket link directly to the pitch gate", async () => {
    listProjectsMock.mockResolvedValue([AWAITING_PITCH_PROJECT]);
    render(<ProjectsPage />);
    const link = await screen.findByRole("link", { name: /台风科普/ });
    expect(link).toHaveAttribute("href", "/projects/p1/pitches");
  });

  it("rows in awaiting-review / finalized buckets link to the draft head", async () => {
    listArtifactsMock.mockResolvedValue([
      {
        id: "draft-head",
        kind: "draft",
        revision: 1,
        parent_id: null,
        created_at: "2026-08-05T00:00:00Z",
        accepted_at: "2026-08-05T01:00:00Z",
        is_head: true,
        payload: null,
      },
    ]);
    listProjectsMock.mockResolvedValue([AWAITING_REVIEW_PROJECT, FINALIZED_PROJECT]);
    render(<ProjectsPage />);
    const reviewLink = await screen.findByRole("link", { name: /海洋之声/ });
    expect(reviewLink).toHaveAttribute("href", "/projects/p2/drafts/draft-head");
    const finalizedLink = screen.getByRole("link", { name: /已定稿项目/ });
    expect(finalizedLink).toHaveAttribute("href", "/projects/p-finalized/drafts/draft-head");
  });

  describe("new-project form (collapsed advanced)", () => {
    it("hides the title input behind a collapsed <details> by default", async () => {
      listProjectsMock.mockResolvedValue([]);
      render(<ProjectsPage />);
      await screen.findByLabelText("主题");

      const details = document.querySelector("details");
      expect(details).not.toBeNull();
      expect(details?.open).toBe(false);
      // The title input lives inside the (still-closed) <details>, so the
      // user only sees it after expanding the advanced section.
      const titleInput = document.querySelector("details input");
      expect(titleInput).not.toBeNull();
    });

    it("topic input is required without expanding details", async () => {
      listProjectsMock.mockResolvedValue([]);
      render(<ProjectsPage />);
      const topic = await screen.findByLabelText("主题");
      expect(topic).toBeRequired();
    });

    it("submits with title derived from topic when advanced is collapsed", async () => {
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

      await user.type(screen.getByLabelText("主题"), "量子通信");
      await user.click(screen.getByRole("button", { name: "创建" }));

      await waitFor(() => {
        expect(createProjectMock).toHaveBeenCalledWith({
          title: "量子通信",
          topic: "量子通信",
        });
      });
      expect(await screen.findByText("量子通信")).toBeInTheDocument();
    });

    it("submits with user-entered title when advanced is expanded", async () => {
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

      await user.type(screen.getByLabelText("主题"), "量子通信");
      // Open the advanced section and enter a custom title.
      const advanced = await screen.findByText(/高级选项|高级|advanced/i);
      await user.click(advanced);
      await user.type(screen.getByLabelText("标题"), "量子纠缠");
      await user.click(screen.getByRole("button", { name: "创建" }));

      await waitFor(() => {
        expect(createProjectMock).toHaveBeenCalledWith({
          title: "量子纠缠",
          topic: "量子通信",
        });
      });
    });
  });
});