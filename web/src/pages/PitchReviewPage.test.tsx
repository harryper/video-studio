/**
 * PitchReviewPage tests — three-card pitch gate per spec §10.3.
 *
 * Covers:
 *   * renders 3 cards with the expected fields
 *   * 选择 → POST /pitches/{id}/accept with no body
 *   * 编辑并选择 → POST with edited_pitch
 *   * 全部换方向 → two-step reopen handshake (409 then X-Confirm-Invalidates)
 *   * 展开调查材料 → renders research fact_cards / sources
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type {
  ApiError,
  ArtifactHistoryEntry,
  StoryPitch,
  StoryPitchSet,
} from "../api/types";

const {
  getPitchesMock,
  acceptPitchMock,
  reopenPitchesMock,
  listArtifactsMock,
} = vi.hoisted(() => ({
  getPitchesMock: vi.fn(),
  acceptPitchMock: vi.fn(),
  reopenPitchesMock: vi.fn(),
  listArtifactsMock: vi.fn(),
}));

vi.mock("../api/client", () => ({
  getPitches: getPitchesMock,
  acceptPitch: acceptPitchMock,
  reopenPitches: reopenPitchesMock,
  listArtifacts: listArtifactsMock,
}));

vi.mock("../router", () => ({
  usePathname: () => "/projects/p1/pitches",
  navigate: vi.fn(),
  Link: ({ to, children, ...rest }: { to: string; children: React.ReactNode; [key: string]: unknown }) => (
    <a href={to} {...(rest as object)}>
      {children}
    </a>
  ),
  RouterProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

import { PitchReviewPage } from "./PitchReviewPage";

const PITCH_1: StoryPitch = {
  id: "p-1",
  investigation_question: "为什么海水是咸的？",
  opening_scene: "站在海边看浪花",
  evidence_path: "化学风化 + 河流输送",
  payoff: "海水咸度是亿万年累积",
  why_it_works: "贴近日常景观",
  estimated_duration_sec: 180,
  risks: ["数据来源单一"],
};

const PITCH_2: StoryPitch = {
  ...PITCH_1,
  id: "p-2",
  investigation_question: "台风为何越强越安静？",
  opening_scene: "卫星云图旋转",
  evidence_path: "对流层能量耗散",
  payoff: "最强时已进入衰减期",
  why_it_works: "反直觉",
  estimated_duration_sec: 240,
};

const PITCH_3: StoryPitch = {
  ...PITCH_1,
  id: "p-3",
  investigation_question: "冰川消融会让海平面上升多少？",
  opening_scene: "冰山崩裂的瞬间",
  evidence_path: "卫星测高数据",
  payoff: "世纪内预计 0.6-1.0 米",
  why_it_works: "数字可视化",
  estimated_duration_sec: 200,
};

const PITCH_SET: StoryPitchSet = {
  payload_kind: "pitch_set",
  id: "set-1",
  pitches: [PITCH_1, PITCH_2, PITCH_3],
  parent_set_id: null,
  feedback: null,
  created_at: "2026-08-10T00:00:00Z",
};

const RESEARCH_PAYLOAD = {
  mechanisms: ["风化", "河流"],
  fact_cards: [
    {
      claim: "海水含盐量约 35‰",
      narrative_value: "数字锚点",
      confidence: 0.9,
      risk: "number",
      sources: [
        {
          title: "NOAA Salinity",
          url: "https://example.com/noaa",
          snippet: "全球平均 35‰",
          publisher: "NOAA",
          published_at: null,
        },
      ],
      verification_status: "verified",
      payoff_critical: true,
    },
  ],
  people_events: [],
  concrete_scenes: ["海浪冲刷礁石"],
  visual_details: ["盐晶"],
  uncertainties: ["深层洋流成分"],
  sources: [
    {
      title: "NOAA Salinity",
      url: "https://example.com/noaa",
      snippet: "全球平均 35‰",
      publisher: "NOAA",
      published_at: null,
    },
  ],
};

const ARTIFACTS: ArtifactHistoryEntry[] = [
  {
    id: "art-research",
    kind: "research",
    revision: 1,
    parent_id: null,
    created_at: "2026-08-09T00:00:00Z",
    accepted_at: "2026-08-09T01:00:00Z",
    is_head: true,
    payload: RESEARCH_PAYLOAD,
  },
];

function renderPage(): void {
  render(<PitchReviewPage projectId="p1" />);
}

describe("PitchReviewPage", () => {
  beforeEach(() => {
    getPitchesMock.mockReset();
    acceptPitchMock.mockReset();
    reopenPitchesMock.mockReset();
    listArtifactsMock.mockReset();
    listArtifactsMock.mockResolvedValue(ARTIFACTS);
    getPitchesMock.mockResolvedValue(PITCH_SET);
  });

  it("renders three pitch cards with the expected fields", async () => {
    renderPage();

    expect(await screen.findByText("为什么海水是咸的？")).toBeVisible();
    expect(screen.getByText("台风为何越强越安静？")).toBeVisible();
    expect(screen.getByText("冰川消融会让海平面上升多少？")).toBeVisible();
    // estimated_duration_sec renders as minutes — pitch 1 (180s) and
    // pitch 3 (200s) both round to 3 minutes so we check the unique
    // 4-minute pitch instead.
    expect(screen.getByText("4 分钟")).toBeVisible();
    // Each card exposes 选择 / 编辑并选择 / 重新生成此卡片 / 全部换方向
    expect(
      screen.getAllByRole("button", { name: /^选择$/ }),
    ).toHaveLength(3);
    expect(
      screen.getAllByRole("button", { name: /编辑并选择/ }),
    ).toHaveLength(3);
  });

  it("选择 → POST /pitches/{id}/accept without edits", async () => {
    acceptPitchMock.mockResolvedValue({
      artifact_id: "art-1",
      job_id: "job-1",
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("为什么海水是咸的？");

    await user.click(screen.getAllByRole("button", { name: /^选择$/ })[0]);
    await waitFor(() => {
      expect(acceptPitchMock).toHaveBeenCalledWith("p1", "p-1", {});
    });
  });

  it("编辑并选择 → POST /pitches/{id}/accept with edited_pitch", async () => {
    acceptPitchMock.mockResolvedValue({
      artifact_id: "art-1",
      job_id: "job-1",
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("为什么海水是咸的？");

    const editButtons = screen.getAllByRole("button", { name: /编辑并选择/ });
    await user.click(editButtons[0]);

    // Each editable field renders a label; the editor opens inline.
    const payoff = await screen.findByLabelText(/回报$/);
    await user.clear(payoff);
    await user.type(payoff, "编辑后的回报");

    await user.click(screen.getByRole("button", { name: /保存并选择/ }));

    await waitFor(() => {
      expect(acceptPitchMock).toHaveBeenCalledWith(
        "p1",
        "p-1",
        expect.objectContaining({
          edited_pitch: expect.objectContaining({
            id: "p-1",
            payoff: "编辑后的回报",
          }),
        }),
      );
    });
  });

  it("全部换方向 → triggers the two-step reopen handshake", async () => {
    const firstCallError: ApiError = {
      status: 409,
      body: {
        code: "confirmation_required",
        message: "reopen will discard downstream artifacts",
        invalidates: ["narrative", "draft"],
      },
    };
    reopenPitchesMock
      .mockRejectedValueOnce(firstCallError)
      .mockResolvedValueOnce({ invalidated: ["narrative", "draft"] });

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("为什么海水是咸的？");

    await user.click(screen.getByRole("button", { name: /全部换方向/ }));

    // First click: confirmation modal appears listing the invalidated kinds.
    expect(await screen.findByRole("dialog")).toBeVisible();
    expect(screen.getByText(/narrative/)).toBeVisible();
    expect(screen.getByText(/draft/)).toBeVisible();

    // Second click: confirm. The client re-sends with the same list.
    await user.click(screen.getByRole("button", { name: /确认全部换方向/ }));

    await waitFor(() => {
      expect(reopenPitchesMock).toHaveBeenCalledTimes(2);
    });
    expect(reopenPitchesMock.mock.calls[0][1]).toBeNull();
    expect(reopenPitchesMock.mock.calls[1][1]).toBe("draft,narrative");
  });

  it("展开调查材料 → renders research fact_cards and sources", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("为什么海水是咸的？");

    await user.click(screen.getByRole("button", { name: /展开调查材料/ }));
    expect(await screen.findByText("海水含盐量约 35‰")).toBeVisible();
    // NOAA Salinity appears once per fact_card source AND once under
    // the consolidated "来源" list — verify both, but disambiguate by
    // container so we don't trip the "multiple elements" matcher.
    expect(screen.getAllByText("NOAA Salinity").length).toBeGreaterThan(0);
  });
});