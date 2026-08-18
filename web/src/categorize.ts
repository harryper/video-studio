/**
 * Editorial bucketing for the projects dashboard.
 *
 * Spec §10.1 mandates six buckets: 草稿 / 等待选切口 / 等待审稿 /
 * 已定稿 / 制作中 / 已完成. The dashboard renders only the
 * ``ProjectSummary`` shape returned by ``GET /api/projects`` — the
 * full artifact history lives behind ``/projects/:id`` — so the
 * bucketing is keyed on the (latest_stage, latest_job_status) pair.
 *
 * The pipeline order is ``diagnosis → research → pitches → narrative
 * → draft → rewrite → speech → approval``. ``narrative`` straddles
 * the pitch-selection and review phases: while it is queued the user
 * is still picking a pitch; once it starts running the script work
 * has begun.
 */

import type { ProjectSummary, StageName, StageStatus } from "./api/types";

export type BucketName =
  | "draft"
  | "awaiting_pitch"
  | "awaiting_review"
  | "finalized"
  | "in_production"
  | "completed";

export const BUCKET_ORDER: readonly BucketName[] = [
  "draft",
  "awaiting_pitch",
  "awaiting_review",
  "finalized",
  "in_production",
  "completed",
] as const;

export const BUCKET_LABEL: Record<BucketName, string> = {
  draft: "草稿",
  awaiting_pitch: "等待选切口",
  awaiting_review: "等待审稿",
  finalized: "已定稿",
  in_production: "制作中",
  completed: "已完成",
};

export function categorizeProject(summary: ProjectSummary): BucketName {
  const stage = summary.latest_stage;
  const status = summary.latest_job_status;
  return bucketFor(stage, status);
}

function bucketFor(stage: StageName | null, status: StageStatus | null): BucketName {
  if (stage === null) return "draft";

  switch (stage) {
    case "diagnosis":
    case "research":
      return "draft";
    case "pitches":
      return "awaiting_pitch";
    case "narrative":
      // Queued narrative means the user is choosing a pitch; once it
      // starts running the script work has begun.
      return status === "queued" ? "awaiting_pitch" : "awaiting_review";
    case "draft":
    case "rewrite":
      return "awaiting_review";
    case "speech":
      return "in_production";
    case "approval":
      return status === "finished" ? "completed" : "finalized";
  }
}