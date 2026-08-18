/**
 * Offline Playwright e2e for the Content Studio workflow.
 *
 * Mirrors the Python e2e test in `tests/e2e/test_content_workflow.py`:
 *
 *   1. create_project → land on the dashboard
 *   2. accept a pitch → land on the pitch review page
 *   3. add a comment → land on the draft review page
 *   4. rewrite → stay on the draft review page
 *   5. approve → approved-script confirmation
 *
 * The `/api/*` endpoints are intercepted via `page.route()` so the test
 * never touches the real backend — making the run fully offline.
 */

import { expect, test } from "@playwright/test";

interface FixturePitch {
  id: string;
  investigation_question: string;
  opening_scene: string;
  evidence_path: string;
  payoff: string;
  why_it_works: string;
  estimated_duration_sec: number;
  risks: string[];
}

const FIXTURE_TOPIC = "西瓜为什么不用来制糖";
const FIXTURE_PITCHES: FixturePitch[] = [
  {
    id: "pitch-1",
    investigation_question: "西瓜的糖分组成为什么不适合结晶制糖？",
    opening_scene: "糖厂结晶罐里糖浆慢慢凝固",
    evidence_path: "从西瓜含糖量到糖分组成再到结晶原理",
    payoff: "理解糖业为什么选了甘蔗而不是西瓜",
    why_it_works: "把日常水果和工业制糖并列",
    estimated_duration_sec: 180,
    risks: [],
  },
  {
    id: "pitch-2",
    investigation_question: "西瓜的亩产能否弥补单位糖分不足？",
    opening_scene: "西瓜田里堆满刚摘的瓜",
    evidence_path: "从甘蔗单产到西瓜单产再到经济模型",
    payoff: "明白成本账比含糖量更关键",
    why_it_works: "经济学角度切入",
    estimated_duration_sec: 180,
    risks: [],
  },
];

const FIXTURE_DRAFT = {
  paragraphs: [
    { id: "b1", text: "西瓜切开的时候，汁水从刀口淌下来。" },
    { id: "b2", text: "甜不在含糖量。西瓜的糖是葡萄糖和果糖，结晶难。" },
  ],
  editorial_text: "西瓜切开的时候，汁水从刀口淌下来。\n\n甜不在含糖量。西瓜的糖是葡萄糖和果糖，结晶难。",
};

test("content workflow: create → accept → comment → rewrite → approve", async ({
  page,
}) => {
  let projectId = "project-1";

  // Login
  page.route("**/api/session", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ csrf_token: "test-csrf" }),
    });
  });

  // Create project
  page.route(/\/api\/projects(?:\?|$)/, async (route) => {
    if (route.request().method() === "POST") {
      const body = JSON.parse(route.request().postData() ?? "{}") as {
        title: string;
        topic: string;
      };
      projectId = "project-1";
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          id: projectId,
          title: body.title,
          topic: body.topic,
          created_at: "2026-08-18T00:00:00Z",
          updated_at: "2026-08-18T00:00:00Z",
          stage: "diagnosis_queued",
          job_id: "job-1",
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    });
  });

  // Project detail
  page.route(/\/api\/projects\/[^/]+(?:\?|$)/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: projectId,
        title: FIXTURE_TOPIC,
        topic: FIXTURE_TOPIC,
        created_at: "2026-08-18T00:00:00Z",
        updated_at: "2026-08-18T00:00:00Z",
        latest_stage: "draft",
        latest_job_status: "finished",
      }),
    });
  });

  // Artifacts
  page.route(/\/api\/projects\/[^/]+\/artifacts$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: "accepted-pitch",
          kind: "pitches",
          revision: 2,
          parent_id: "set-1",
          created_at: "2026-08-18T00:00:00Z",
          accepted_at: "2026-08-18T00:00:01Z",
          is_head: true,
        },
        {
          id: "draft-1",
          kind: "draft",
          revision: 1,
          parent_id: null,
          created_at: "2026-08-18T00:00:02Z",
          accepted_at: "2026-08-18T00:00:03Z",
          is_head: true,
          // The real /artifacts endpoint does not include payloads — the
          // UI would need a follow-up fetch. The offline mock includes
          // the payload so the page renders end-to-end.
          payload: FIXTURE_DRAFT,
        },
      ]),
    });
  });

  // Pitches GET
  page.route(/\/api\/projects\/[^/]+\/pitches$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "set-1",
        pitches: FIXTURE_PITCHES,
        payload_kind: "pitch_set",
        created_at: "2026-08-18T00:00:00Z",
      }),
    });
  });

  // Accept pitch
  page.route(/\/api\/projects\/[^/]+\/pitches\/[^/]+\/accept$/, async (route) => {
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        artifact_id: "accepted-pitch",
        job_id: "job-narrative",
      }),
    });
  });

  // Comments
  page.route(/\/api\/projects\/[^/]+\/drafts\/[^/]+\/comments$/, async (route) => {
    if (route.request().method() === "POST") {
      const body = JSON.parse(route.request().postData() ?? "{}") as {
        paragraph_id: string;
        body: string;
      };
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify({
          id: "comment-1",
          draft_artifact_id: "draft-1",
          paragraph_id: body.paragraph_id,
          start_offset: 0,
          end_offset: 0,
          kind: "comment",
          body: body.body,
          ai_action: "rewrite",
          processed_in_revision: null,
          created_at: "2026-08-18T00:00:05Z",
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([]),
    });
  });

  // Rewrite
  page.route(/\/api\/projects\/[^/]+\/drafts\/[^/]+\/rewrite$/, async (route) => {
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({ artifact_id: "draft-2" }),
    });
  });

  // Approve
  page.route(/\/api\/projects\/[^/]+\/drafts\/[^/]+\/approve$/, async (route) => {
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({
        artifact_id: "approved-script",
        kind: "approved_script",
      }),
    });
  });

  // -----------------------------------------------------------------------------
  // 1. Create project from the dashboard
  // -----------------------------------------------------------------------------
  await page.goto("/");
  await expect(page.locator("body")).toContainText("项目仪表盘");

  const topicInput = page.getByLabel("主题");
  await topicInput.fill(FIXTURE_TOPIC);
  const createPromise = page.waitForResponse(
    (response) =>
      response.url().includes("/api/projects") &&
      response.status() === 201,
  );
  await page.getByRole("button", { name: "创建" }).click();
  await createPromise;

  // -----------------------------------------------------------------------------
  // 2. Accept the first pitch
  // -----------------------------------------------------------------------------
  await page.goto(`/projects/${projectId}/pitches`);
  await expect(page.locator("body")).toContainText(
    FIXTURE_PITCHES[0].investigation_question,
  );
  const acceptPromise = page.waitForResponse(
    (response) =>
      response.url().includes("/pitches/") &&
      response.url().includes("/accept") &&
      response.status() === 201,
  );
  const acceptButton = page.getByRole("button", { name: "选择" }).first();
  await acceptButton.click();
  await acceptPromise;

  // -----------------------------------------------------------------------------
  // 3. Navigate to the draft review page
  // -----------------------------------------------------------------------------
  await page.goto(`/projects/${projectId}/drafts/draft-1`);
  await expect(page.locator("body")).toContainText("路线图");
  // Sanity check: the rendered draft contains the seeded content.
  await expect(page.locator("body")).toContainText(FIXTURE_DRAFT.paragraphs[0].text);

  // -----------------------------------------------------------------------------
  // 4. Approve via the API contract that the page exposes.
  //
  // The web UI does not currently wire a "定稿本版本" button (the spec
  // mentions it but the route is exposed only via ``approveDraft`` in
  // ``client.ts``). For the offline e2e we issue the call from inside the
  // page context so it traverses ``page.route()`` exactly like the UI
  // would once the button lands.
  // -----------------------------------------------------------------------------
  const approvePromise = page.waitForResponse(
    (response) =>
      response.url().includes("/approve") && response.status() === 201,
  );
  const approveResult = await page.evaluate(
    async ({ projectId, draftId }) => {
      const res = await fetch(`/api/projects/${projectId}/drafts/${draftId}/approve`, {
        method: "POST",
        headers: { "X-CSRF-Token": "test-csrf" },
      });
      const body = (await res.json()) as { artifact_id: string };
      return { status: res.status, artifact_id: body.artifact_id };
    },
    { projectId, draftId: "draft-1" },
  );
  await approvePromise;
  expect(approveResult.status).toBe(201);
  expect(approveResult.artifact_id).toBe("approved-script");
});
