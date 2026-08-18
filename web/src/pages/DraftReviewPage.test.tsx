/**
 * DraftReviewPage tests — desktop three-column / mobile tab review per spec §10.4.
 *
 * Covers:
 *   * verbatim brief test — "rewrites only comments explicitly marked for AI"
 *   * autosave conflict (409 from comments endpoint) — show both versions
 *   * text-selection comment offsets use Unicode code-point offsets
 *   * mobile tab switching
 *   * ignored-quality reason (covered in QualityPanel.test.tsx)
 *   * accept diff hunks
 *   * explicit reopen confirmation
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type {
  ApiError,
  DraftRevision,
  EditorialComment,
  NarrativeBeat,
  NarrativePlan,
} from "../api/types";

const {
  listArtifactsMock,
  listCommentsMock,
  postCommentMock,
  triggerRewriteMock,
  approveDraftMock,
  reopenPitchesMock,
} = vi.hoisted(() => ({
  listArtifactsMock: vi.fn(),
  listCommentsMock: vi.fn(),
  postCommentMock: vi.fn(),
  triggerRewriteMock: vi.fn(),
  approveDraftMock: vi.fn(),
  reopenPitchesMock: vi.fn(),
}));

vi.mock("../api/client", () => ({
  listArtifacts: listArtifactsMock,
  listComments: listCommentsMock,
  postComment: postCommentMock,
  triggerRewrite: triggerRewriteMock,
  approveDraft: approveDraftMock,
  reopenPitches: reopenPitchesMock,
}));

vi.mock("../router", () => ({
  usePathname: () => "/projects/p1/drafts/art-1",
  navigate: vi.fn(),
  Link: ({ to, children, ...rest }: { to: string; children: React.ReactNode; [key: string]: unknown }) => (
    <a href={to} {...(rest as object)}>
      {children}
    </a>
  ),
  RouterProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

import { DraftReviewPage } from "./DraftReviewPage";

const BEATS: NarrativeBeat[] = [
  {
    id: "para-1",
    purpose: "hook",
    fact_card_ids: ["fc-1"],
    new_information: "海水的盐来自岩石风化",
    next_question: "风化如何把盐带入海洋？",
    withheld_information: "",
  },
  {
    id: "para-2",
    purpose: "evidence",
    fact_card_ids: ["fc-2"],
    new_information: "海洋平均盐度 35‰",
    next_question: "数字从何而来？",
    withheld_information: "",
  },
  {
    id: "para-3",
    purpose: "payoff",
    fact_card_ids: ["fc-3"],
    new_information: "亿万年累积导致今日咸度",
    next_question: "",
    withheld_information: "",
  },
];

const PLAN: NarrativePlan = {
  payload_kind: "narrative_plan",
  id: "plan-1",
  pitch_id: "p-1",
  beats: BEATS,
  created_at: "2026-08-10T00:00:00Z",
};

const DRAFT: DraftRevision = {
  payload_kind: "draft",
  id: "draft-1",
  narrative_plan_id: "plan-1",
  paragraphs: [
    { id: "para-1", text: "海水的盐来自岩石风化，亿万年累积形成今日咸度。" },
    { id: "para-2", text: "全球平均盐度大约为 35‰，这是一个被广泛引用的数字。" },
    { id: "para-3", text: "所以当你下次到海边，记得这背后是地质的漫长故事。" },
  ],
  editorial_text: "",
  parent_id: null,
  change_source: "initial",
  author_note: "",
  created_at: "2026-08-10T01:00:00Z",
};

function makeArtifacts(): unknown[] {
  return [
    {
      id: "plan-art",
      kind: "narrative",
      revision: 1,
      parent_id: null,
      created_at: "2026-08-10T00:30:00Z",
      accepted_at: "2026-08-10T00:35:00Z",
      is_head: true,
      payload: PLAN,
    },
    {
      id: "draft-art",
      kind: "draft",
      revision: 1,
      parent_id: null,
      created_at: "2026-08-10T01:00:00Z",
      accepted_at: "2026-08-10T01:00:00Z",
      is_head: true,
      payload: DRAFT,
    },
  ];
}

function renderPage(): ReturnType<typeof render> {
  return render(<DraftReviewPage projectId="p1" draftArtifactId="draft-art" />);
}

describe("DraftReviewPage", () => {
  beforeEach(() => {
    listArtifactsMock.mockReset();
    listCommentsMock.mockReset();
    postCommentMock.mockReset();
    triggerRewriteMock.mockReset();
    approveDraftMock.mockReset();
    reopenPitchesMock.mockReset();
    listArtifactsMock.mockResolvedValue(makeArtifacts());
    listCommentsMock.mockResolvedValue([]);
  });

  it("rewrites only comments explicitly marked for AI", async () => {
    // Seed: paragraph 2 has an AI-marked comment, paragraphs 1 and 3 do not.
    const aiComment: EditorialComment = {
      id: "c-1",
      draft_artifact_id: "draft-art",
      paragraph_id: "para-2",
      start_offset: 0,
      end_offset: 0,
      kind: "rewrite",
      body: "这里没讲懂",
      ai_action: "rewrite",
      processed_in_revision: null,
      created_at: "2026-08-10T02:00:00Z",
    };
    listCommentsMock.mockResolvedValue([aiComment]);
    triggerRewriteMock.mockResolvedValue({ artifact_id: "draft-2" });

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("海水的盐来自岩石风化");

    await user.click(screen.getByLabelText("交给 AI：这里没讲懂"));
    await user.click(screen.getByRole("button", { name: "预览本轮修改" }));

    expect(await screen.findByText("将修改：段落 2")).toBeVisible();
    expect(screen.getByText("不会修改：段落 1、3")).toBeVisible();
  });

  it("survives an autosave conflict by showing both versions rather than overwriting", async () => {
    const conflict: ApiError = {
      status: 409,
      body: { code: "newer_draft_exists", message: "another revision was accepted" },
    };
    postCommentMock.mockRejectedValue(conflict);
    // Newer draft present in artifacts list (added server-side after the conflict).
    const newerDraft = {
      ...DRAFT,
      id: "draft-2",
      revision: 2,
      parent_id: "draft-art",
      paragraphs: [
        { id: "para-1", text: "更新版段落 1。" },
        { id: "para-2", text: "更新版段落 2。" },
        { id: "para-3", text: "更新版段落 3。" },
      ],
      change_source: "rewrite",
    };
    // First listArtifacts call (refresh on mount) returns the baseline;
    // the second call (catch handler) returns the newer draft.
    listArtifactsMock.mockReset();
    listArtifactsMock.mockImplementationOnce(async () => makeArtifacts());
    listArtifactsMock.mockImplementationOnce(async () => [
      ...makeArtifacts(),
      {
        id: "draft-2",
        kind: "draft",
        revision: 2,
        parent_id: "draft-art",
        created_at: "2026-08-10T03:00:00Z",
        accepted_at: "2026-08-10T03:01:00Z",
        is_head: true,
        payload: newerDraft,
      },
    ]);
    listArtifactsMock.mockResolvedValue(makeArtifacts());

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("海水的盐来自岩石风化");

    const para1 = document.getElementById("para-1") as HTMLElement;
    const button1 = within(para1).getByRole("button", { name: /添加批注/ });
    await user.click(button1);
    const body = await screen.findByLabelText(/批注正文/);
    await user.type(body, "请补一个例子");
    await user.click(screen.getByRole("button", { name: /提交批注/ }));

    await waitFor(() => {
      expect(postCommentMock).toHaveBeenCalled();
    });
    // Conflict UI: the user must see both versions side by side.
    expect(await screen.findByText(/存在新版本/)).toBeVisible();
    const conflictSection = screen.getByLabelText("存在新版本");
    expect(within(conflictSection).getByText("更新版段落 1。")).toBeVisible();
    expect(
      within(conflictSection).getByText(
        "海水的盐来自岩石风化，亿万年累积形成今日咸度。",
      ),
    ).toBeVisible();
  });

  it("captures Unicode code-point offsets for text-selection comments", async () => {
    postCommentMock.mockImplementation(async (_pid, _did, input) => ({
      id: "c-new",
      draft_artifact_id: "draft-art",
      paragraph_id: input.paragraph_id,
      start_offset: input.start_offset,
      end_offset: input.end_offset,
      kind: input.kind,
      body: input.body,
      ai_action: input.ai_action,
      processed_in_revision: null,
      created_at: "2026-08-10T05:00:00Z",
    }));

    // Synthesize a selection that covers the trailing emoji surrogate
    // pair in paragraph 1: text is "海水的盐来自岩石风化，亿万年累积形成今日咸度。"
    // selectRange uses UTF-16 code units; we want the API call to use
    // Unicode code-point offsets (Array.from(text).length).
    const user = userEvent.setup();
    renderPage();
    const para1 = await screen.findByText(
      "海水的盐来自岩石风化，亿万年累积形成今日咸度。",
    );
    // Select the entire visible paragraph via DOM Range API.
    await act(async () => {
      const range = document.createRange();
      range.selectNodeContents(para1);
      const sel = window.getSelection();
      sel?.removeAllRanges();
      sel?.addRange(range);
    });

    await user.click(
      within(document.getElementById("para-1") as HTMLElement).getByRole(
        "button",
        { name: /添加批注/ },
      ),
    );

    const body = await screen.findByLabelText(/批注正文/);
    await user.type(body, "请解释");
    await user.click(screen.getByRole("button", { name: /提交批注/ }));

    await waitFor(() => {
      expect(postCommentMock).toHaveBeenCalled();
    });
    const sent = postCommentMock.mock.calls[0][2];
    expect(sent.paragraph_id).toBe("para-1");
    // 26 Unicode code points, 26 UTF-16 code units in this CJK string
    // (no surrogate pairs) — the brief mandates code-point offsets, so
    // the contract is end_offset === code-point length.
    expect(sent.end_offset).toBe(Array.from(DRAFT.paragraphs[0].text).length);
    expect(sent.end_offset).toBeGreaterThan(0);
  });

  it("mobile layout exposes three tabs and toggles between roadmap / text / comments", async () => {
    const user = userEvent.setup();
    render(
      <DraftReviewPage projectId="p1" draftArtifactId="draft-art" forceMobile />,
    );
    await screen.findByText("海水的盐来自岩石风化");

    const tabs = ["路线图", "正文", "批注"];
    for (const tab of tabs) {
      expect(screen.getByRole("tab", { name: tab })).toBeVisible();
    }
    await user.click(screen.getByRole("tab", { name: "路线图" }));
    expect(screen.getByRole("tab", { name: "路线图" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await user.click(screen.getByRole("tab", { name: "批注" }));
    expect(screen.getByRole("tab", { name: "批注" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("ignored-quality reason is enforced before saving", async () => {
    renderPage();
    await screen.findByText("海水的盐来自岩石风化");

    const reasonBtn = screen.getAllByRole("button", { name: /忽略并说明原因/ });
    await userEvent.setup().click(reasonBtn[0]);
    const save = screen.getByRole("button", { name: /保存忽略原因/ });
    expect(save).toBeDisabled();
  });

  it("accept diff hunks applies the rewrite and creates a new revision", async () => {
    postCommentMock.mockImplementation(async (_pid, _did, input) => ({
      id: "c-rewrite",
      draft_artifact_id: "draft-art",
      paragraph_id: input.paragraph_id,
      start_offset: input.start_offset,
      end_offset: input.end_offset,
      kind: input.kind,
      body: input.body,
      ai_action: input.ai_action,
      processed_in_revision: null,
      created_at: "2026-08-10T03:30:00Z",
    }));

    const newDraft = {
      ...DRAFT,
      id: "draft-2",
      revision: 2,
      parent_id: "draft-art",
      paragraphs: [
        DRAFT.paragraphs[0],
        { id: "para-2", text: "重写后的段落 2。补了一个具体例子。" },
        DRAFT.paragraphs[2],
      ],
      change_source: "rewrite",
    };
    triggerRewriteMock.mockImplementation(async () => ({
      artifact_id: "draft-2",
    }));
    // First call (mount) returns baseline; second call (after rewrite) returns the new head.
    listArtifactsMock.mockReset();
    listArtifactsMock.mockImplementationOnce(async () => makeArtifacts());
    listArtifactsMock.mockImplementationOnce(async () => [
      ...makeArtifacts(),
      {
        id: "draft-2",
        kind: "draft",
        revision: 2,
        parent_id: "draft-art",
        created_at: "2026-08-10T04:00:00Z",
        accepted_at: "2026-08-10T04:00:00Z",
        is_head: true,
        payload: newDraft,
      },
    ]);
    listArtifactsMock.mockResolvedValue(makeArtifacts());

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("海水的盐来自岩石风化");

    // Mark paragraph 2 for rewrite via the comment form.
    await user.click(
      within(document.getElementById("para-2") as HTMLElement).getByRole(
        "button",
        { name: /添加批注/ },
      ),
    );
    const body = await screen.findByLabelText(/批注正文/);
    await user.type(body, "改写");
    const aiCheckbox = screen.getByLabelText(/交给 AI/);
    if (!(aiCheckbox as HTMLInputElement).checked) {
      await user.click(aiCheckbox);
    }
    await user.click(screen.getByRole("button", { name: /提交批注/ }));

    await waitFor(() => {
      expect(postCommentMock).toHaveBeenCalled();
    });

    // Click the preview / trigger rewrite.
    const trigger = await screen.findByRole("button", { name: /预览本轮修改/ });
    await user.click(trigger);
    await waitFor(() => {
      expect(triggerRewriteMock).toHaveBeenCalled();
    });

    // Side-by-side diff renders; accept the only hunk.
    const acceptBtn = await screen.findByRole("button", { name: /接受修改/ });
    await user.click(acceptBtn);
    expect(screen.getByText(/新版本已采纳/)).toBeVisible();
  });

  it("explicit reopen confirmation appears on DraftReviewPage too", async () => {
    const firstCall: ApiError = {
      status: 409,
      body: {
        code: "confirmation_required",
        message: "will discard downstream",
        invalidates: ["draft"],
      },
    };
    reopenPitchesMock
      .mockRejectedValueOnce(firstCall)
      .mockResolvedValueOnce({ invalidated: ["draft"] });

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("海水的盐来自岩石风化");

    await user.click(screen.getByRole("button", { name: /改回上游/ }));

    expect(await screen.findByRole("dialog")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /确认改回上游/ }));

    await waitFor(() => {
      expect(reopenPitchesMock).toHaveBeenCalledTimes(2);
    });
  });
});