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