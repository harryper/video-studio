/**
 * Wire types mirroring the Content Studio REST + SSE contract from Task 10.
 *
 * Hand-written to keep the build dependency-free; the contract is stable
 * and the schema is small. Updates here MUST be paired with a contract
 * change on the API side.
 */

export type StageStatus =
  | "queued"
  | "running"
  | "finished"
  | "failed"
  | "cancelled";

export type StageName =
  | "diagnosis"
  | "research"
  | "pitches"
  | "narrative"
  | "draft"
  | "rewrite"
  | "speech"
  | "approval";

export interface ProjectSummary {
  id: string;
  title: string;
  topic: string;
  created_at: string;
  updated_at: string;
  latest_stage: StageName | null;
  latest_job_status: StageStatus | null;
}

export interface ProjectCreated {
  id: string;
  title: string;
  topic: string;
  created_at: string;
  updated_at: string;
  stage: string;
  job_id: string;
}

export interface ArtifactHistoryEntry {
  id: string;
  kind: string;
  revision: number;
  parent_id: string | null;
  created_at: string;
  accepted_at: string | null;
  is_head: boolean;
  payload?: unknown;
}

// ---------------------------------------------------------------------------
// Task 12 — editorial review wire types (mirror studio/schemas.py)
// ---------------------------------------------------------------------------

export interface StoryPitch {
  id: string;
  investigation_question: string;
  opening_scene: string;
  evidence_path: string;
  payoff: string;
  why_it_works: string;
  estimated_duration_sec: number;
  risks: string[];
}

export interface StoryPitchSet {
  payload_kind: "pitch_set";
  id: string;
  pitches: StoryPitch[];
  parent_set_id: string | null;
  feedback: string | null;
  created_at: string;
}

export interface SourceDocument {
  title: string;
  url: string;
  snippet: string;
  publisher: string;
  published_at: string | null;
}

export interface FactCard {
  claim: string;
  narrative_value: string;
  confidence: number;
  risk: "number" | "date" | "superlative" | "absolute" | "ordinary";
  sources: SourceDocument[];
  verification_status: "verified" | "softened" | "dropped" | "unverified";
  payoff_critical: boolean;
}

export interface ResearchPacket {
  mechanisms: string[];
  fact_cards: FactCard[];
  people_events: string[];
  concrete_scenes: string[];
  visual_details: string[];
  uncertainties: string[];
  sources: SourceDocument[];
}

export interface NarrativeBeat {
  id: string;
  purpose: string;
  fact_card_ids: string[];
  new_information: string;
  next_question: string;
  withheld_information: string;
}

export interface NarrativePlan {
  payload_kind: "narrative_plan";
  id: string;
  pitch_id: string;
  beats: NarrativeBeat[];
  created_at: string;
}

export interface DraftParagraph {
  id: string;
  text: string;
}

export interface DraftRevision {
  payload_kind: "draft";
  id: string;
  narrative_plan_id: string;
  paragraphs: DraftParagraph[];
  editorial_text: string;
  parent_id: string | null;
  change_source: string;
  author_note: string;
  created_at: string;
}

export interface EditorialComment {
  id: string;
  draft_artifact_id: string;
  paragraph_id: string;
  start_offset: number;
  end_offset: number;
  kind: string;
  body: string;
  ai_action: "rewrite" | "note" | "none";
  processed_in_revision: string | null;
  created_at: string;
}

export interface CreateCommentInput {
  paragraph_id: string;
  start_offset: number;
  end_offset: number;
  kind: string;
  body: string;
  ai_action: "rewrite" | "note" | "none";
}

export interface AcceptPitchInput {
  edited_pitch?: StoryPitch;
}

export interface JobProgressEvent {
  id: string;
  job_id: string;
  stage: StageName;
  status: StageStatus;
  ts: string;
  attempt: number;
  error_code: string | null;
  error_message: string | null;
}

export interface JobMutation {
  job_id: string;
  status: StageStatus;
}

export interface LoginResponse {
  csrf_token: string;
}

export interface ProjectListFilters {
  stage?: StageName | null;
  status?: StageStatus | null;
}

export interface CreateProjectInput {
  title: string;
  topic: string;
}

/**
 * The API's error envelope shape. ``code`` is a stable machine identifier;
 * any additional keys (e.g. ``invalidates``) live alongside it because
 * Task 10 flattens the ``details`` payload to the top level.
 */
export interface ApiError {
  status: number;
  body: {
    code: string;
    message: string;
    [key: string]: unknown;
  };
}

export interface EventStreamHandlers {
  onProgress: (event: JobProgressEvent) => void;
  onError?: (err: ApiError | Error) => void;
  onOpen?: () => void;
  onClose?: () => void;
}