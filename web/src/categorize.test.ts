import { describe, expect, it } from "vitest";

import type { ProjectSummary } from "./api/types";
import {
  BUCKET_LABEL,
  BUCKET_ORDER,
  categorizeProject,
  type BucketName,
} from "./categorize";

function makeSummary(
  overrides: Partial<ProjectSummary> = {},
): ProjectSummary {
  return {
    id: "p",
    title: "t",
    topic: "topic",
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    latest_stage: null,
    latest_job_status: null,
    ...overrides,
  };
}

describe("categorizeProject", () => {
  it("categorizes null stage as 草稿", () => {
    expect(categorizeProject(makeSummary())).toBe("draft");
  });

  it("categorizes diagnosis stage as 草稿", () => {
    for (const status of ["queued", "running", "finished"] as const) {
      expect(
        categorizeProject(makeSummary({ latest_stage: "diagnosis", latest_job_status: status })),
      ).toBe("draft");
    }
  });

  it("categorizes research stage as 草稿", () => {
    for (const status of ["queued", "running", "finished"] as const) {
      expect(
        categorizeProject(makeSummary({ latest_stage: "research", latest_job_status: status })),
      ).toBe("draft");
    }
  });

  it("categorizes pitches stage as 等待选切口", () => {
    for (const status of ["queued", "running", "finished"] as const) {
      expect(
        categorizeProject(makeSummary({ latest_stage: "pitches", latest_job_status: status })),
      ).toBe("awaiting_pitch");
    }
  });

  it("categorizes narrative queued as 等待选切口 (no narrative progress yet)", () => {
    expect(
      categorizeProject(makeSummary({ latest_stage: "narrative", latest_job_status: "queued" })),
    ).toBe("awaiting_pitch");
  });

  it("categorizes narrative running or finished as 等待审稿", () => {
    for (const status of ["running", "finished"] as const) {
      expect(
        categorizeProject(makeSummary({ latest_stage: "narrative", latest_job_status: status })),
      ).toBe("awaiting_review");
    }
  });

  it("categorizes draft stage as 等待审稿", () => {
    for (const status of ["queued", "running", "finished", "failed", "cancelled"] as const) {
      expect(
        categorizeProject(makeSummary({ latest_stage: "draft", latest_job_status: status })),
      ).toBe("awaiting_review");
    }
  });

  it("categorizes rewrite stage as 等待审稿", () => {
    for (const status of ["queued", "running", "finished", "failed", "cancelled"] as const) {
      expect(
        categorizeProject(makeSummary({ latest_stage: "rewrite", latest_job_status: status })),
      ).toBe("awaiting_review");
    }
  });

  it("categorizes approval stage queued or running as 已定稿", () => {
    for (const status of ["queued", "running"] as const) {
      expect(
        categorizeProject(makeSummary({ latest_stage: "approval", latest_job_status: status })),
      ).toBe("finalized");
    }
  });

  it("categorizes speech stage as 制作中 regardless of status", () => {
    for (const status of ["queued", "running", "finished", "failed", "cancelled"] as const) {
      expect(
        categorizeProject(makeSummary({ latest_stage: "speech", latest_job_status: status })),
      ).toBe("in_production");
    }
  });

  it("categorizes approval finished as 已完成", () => {
    expect(
      categorizeProject(makeSummary({ latest_stage: "approval", latest_job_status: "finished" })),
    ).toBe("completed");
  });
});

describe("BUCKET_LABEL / BUCKET_ORDER", () => {
  it("exposes the 6 spec §10.1 buckets in order", () => {
    expect(BUCKET_ORDER).toEqual<BucketName[]>([
      "draft",
      "awaiting_pitch",
      "awaiting_review",
      "finalized",
      "in_production",
      "completed",
    ]);
  });

  it("maps every bucket to its Chinese label from spec §10.1", () => {
    expect(BUCKET_LABEL.draft).toBe("草稿");
    expect(BUCKET_LABEL.awaiting_pitch).toBe("等待选切口");
    expect(BUCKET_LABEL.awaiting_review).toBe("等待审稿");
    expect(BUCKET_LABEL.finalized).toBe("已定稿");
    expect(BUCKET_LABEL.in_production).toBe("制作中");
    expect(BUCKET_LABEL.completed).toBe("已完成");
  });
});