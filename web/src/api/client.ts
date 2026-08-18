/**
 * Fetch wrappers + SSE client for the Content Studio REST + event stream.
 *
 * Surfaces API errors as ``ApiError`` so the UI can branch on the stable
 * ``code`` field rather than parsing free-form detail strings. CSRF is
 * attached on mutating requests after a successful ``login``.
 *
 * SSE uses ``fetch`` + ``ReadableStream`` so the client can pass
 * ``Last-Event-ID`` on reconnect and so the connection can be cancelled
 * via ``AbortController`` — both of which the native ``EventSource``
 * constructor does not support.
 */

import type {
  AcceptPitchInput,
  ApiError,
  ArtifactHistoryEntry,
  CreateCommentInput,
  CreateProjectInput,
  EditorialComment,
  EventStreamHandlers,
  JobMutation,
  JobProgressEvent,
  LoginResponse,
  ProjectCreated,
  ProjectListFilters,
  ProjectSummary,
  StoryPitchSet,
} from "./types";

const DEFAULT_BASE = "/api";

let csrfToken: string | null = null;
let baseUrl = DEFAULT_BASE;

export function configureClient(opts: { baseUrl?: string; csrfToken?: string | null }): void {
  if (opts.baseUrl !== undefined) baseUrl = opts.baseUrl;
  csrfToken = opts.csrfToken ?? null;
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

function url(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) return path;
  return `${baseUrl}${path}`;
}

async function parseError(response: Response): Promise<ApiError> {
  let body: Record<string, unknown> = {};
  try {
    body = (await response.json()) as Record<string, unknown>;
  } catch {
    body = {};
  }
  const code = typeof body.code === "string" ? body.code : "error";
  const message =
    typeof body.message === "string" ? body.message : response.statusText;
  return { status: response.status, body: { ...body, code, message } };
}

interface RequestOpts {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  csrf?: boolean;
  headers?: Record<string, string>;
}

async function request<T>(path: string, opts: RequestOpts = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...opts.headers };
  let body: BodyInit | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  if (opts.csrf && csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }
  const init: RequestInit = { method: opts.method ?? "GET", headers };
  if (body !== undefined) init.body = body;
  if (opts.signal) init.signal = opts.signal;

  const response = await fetch(url(path), init);
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function login(password: string): Promise<LoginResponse> {
  return request<LoginResponse>("/session", {
    method: "POST",
    body: { password },
  }).then((res) => {
    csrfToken = res.csrf_token;
    return res;
  });
}

export function listProjects(
  filters: ProjectListFilters = {},
): Promise<ProjectSummary[]> {
  const params = new URLSearchParams();
  if (filters.stage) params.set("stage", filters.stage);
  if (filters.status) params.set("status", filters.status);
  const qs = params.toString();
  return request<ProjectSummary[]>(`/projects${qs ? `?${qs}` : ""}`);
}

export function createProject(input: CreateProjectInput): Promise<ProjectCreated> {
  return request<ProjectCreated>("/projects", {
    method: "POST",
    body: input,
    csrf: true,
  });
}

export function getProject(id: string): Promise<ProjectSummary> {
  return request<ProjectSummary>(`/projects/${encodeURIComponent(id)}`);
}

export function listArtifacts(id: string): Promise<ArtifactHistoryEntry[]> {
  return request<ArtifactHistoryEntry[]>(`/projects/${encodeURIComponent(id)}/artifacts`);
}

export function retryJob(projectId: string, jobId: string): Promise<JobMutation> {
  return request<JobMutation>(
    `/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(jobId)}/retry`,
    { method: "POST", csrf: true },
  );
}

export function cancelJob(projectId: string, jobId: string): Promise<JobMutation> {
  return request<JobMutation>(
    `/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(jobId)}/cancel`,
    { method: "POST", csrf: true },
  );
}

// ---------------------------------------------------------------------------
// Task 12 — pitch + draft review endpoints
// ---------------------------------------------------------------------------

export function getPitches(projectId: string): Promise<StoryPitchSet> {
  return request<StoryPitchSet>(
    `/projects/${encodeURIComponent(projectId)}/pitches`,
  );
}

export function generatePitches(projectId: string): Promise<{ job_id: string }> {
  return request<{ job_id: string }>(
    `/projects/${encodeURIComponent(projectId)}/pitches/generate`,
    { method: "POST", csrf: true },
  );
}

export function acceptPitch(
  projectId: string,
  pitchId: string,
  input: AcceptPitchInput = {},
): Promise<{ artifact_id: string; job_id: string }> {
  return request<{ artifact_id: string; job_id: string }>(
    `/projects/${encodeURIComponent(projectId)}/pitches/${encodeURIComponent(pitchId)}/accept`,
    { method: "POST", body: input, csrf: true },
  );
}

export function reopenPitches(
  projectId: string,
  invalidates: string | null,
): Promise<{ invalidated: string[] }> {
  const headers: Record<string, string> = {};
  if (invalidates !== null) headers["X-Confirm-Invalidates"] = invalidates;
  return request<{ invalidated: string[] }>(
    `/projects/${encodeURIComponent(projectId)}/pitch/reopen`,
    { method: "POST", csrf: true, headers },
  );
}

export function regeneratePitches(
  projectId: string,
): Promise<{ job_id: string }> {
  return request<{ job_id: string }>(
    `/projects/${encodeURIComponent(projectId)}/pitch/regenerate`,
    { method: "POST", csrf: true },
  );
}

export function listComments(
  projectId: string,
  draftArtifactId: string,
): Promise<EditorialComment[]> {
  return request<EditorialComment[]>(
    `/projects/${encodeURIComponent(projectId)}/drafts/${encodeURIComponent(draftArtifactId)}/comments`,
  );
}

export function postComment(
  projectId: string,
  draftArtifactId: string,
  input: CreateCommentInput,
): Promise<EditorialComment> {
  return request<EditorialComment>(
    `/projects/${encodeURIComponent(projectId)}/drafts/${encodeURIComponent(draftArtifactId)}/comments`,
    { method: "POST", body: input, csrf: true },
  );
}

export function triggerRewrite(
  projectId: string,
  draftArtifactId: string,
): Promise<{ artifact_id: string }> {
  return request<{ artifact_id: string }>(
    `/projects/${encodeURIComponent(projectId)}/drafts/${encodeURIComponent(draftArtifactId)}/rewrite`,
    { method: "POST", csrf: true },
  );
}

export function approveDraft(
  projectId: string,
  draftArtifactId: string,
): Promise<{ artifact_id: string }> {
  return request<{ artifact_id: string }>(
    `/projects/${encodeURIComponent(projectId)}/drafts/${encodeURIComponent(draftArtifactId)}/approve`,
    { method: "POST", csrf: true },
  );
}

// ---------------------------------------------------------------------------
// SSE
// ---------------------------------------------------------------------------

interface ParseFrameState {
  event?: string;
  id?: string;
  data: string[];
}

function dispatchFrame(state: ParseFrameState, handlers: EventStreamHandlers): void {
  if (state.data.length === 0) return;
  const raw = state.data.join("\n");
  if (state.event === "progress") {
    try {
      const parsed = JSON.parse(raw) as JobProgressEvent;
      handlers.onProgress(parsed);
    } catch {
      // malformed payload; ignore (the server will eventually send a fresh one)
    }
  }
}

interface OpenStreamOptions {
  signal?: AbortSignal;
  lastEventId?: string | null;
}

export function openEventStream(
  projectId: string,
  handlers: EventStreamHandlers,
  options: OpenStreamOptions = {},
): () => void {
  const controller = new AbortController();
  if (options.signal) {
    if (options.signal.aborted) controller.abort();
    else options.signal.addEventListener("abort", () => controller.abort(), { once: true });
  }

  let cancelled = false;
  let lastId: string | null = options.lastEventId ?? null;
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;

  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (lastId !== null) headers["Last-Event-ID"] = lastId;

  const connect = async (): Promise<void> => {
    if (cancelled) return;
    try {
      const response = await fetch(
        url(`/projects/${encodeURIComponent(projectId)}/events`),
        { headers, signal: controller.signal },
      );
      if (!response.ok) {
        throw await parseError(response);
      }
      handlers.onOpen?.();
      if (!response.body) {
        handlers.onClose?.();
        return;
      }
      reader = response.body.getReader();
      while (!cancelled) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          const raw = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const state: ParseFrameState = { data: [] };
          for (const line of raw.split("\n")) {
            if (line.startsWith(":")) continue; // comment
            const sep = line.indexOf(":");
            if (sep === -1) continue;
            const field = line.slice(0, sep);
            let value = line.slice(sep + 1);
            if (value.startsWith(" ")) value = value.slice(1);
            if (field === "data") state.data.push(value);
            else if (field === "id") {
              state.id = value;
              lastId = value;
            } else if (field === "event") state.event = value;
          }
          dispatchFrame(state, handlers);
        }
      }
      handlers.onClose?.();
    } catch (err) {
      if (cancelled) return;
      if (controller.signal.aborted) {
        handlers.onClose?.();
        return;
      }
      handlers.onError?.(
        err instanceof Error
          ? err
          : new Error(typeof err === "string" ? err : "stream failed"),
      );
    }
  };

  void connect();

  return () => {
    cancelled = true;
    controller.abort();
    if (reader) {
      reader.cancel().catch(() => {
        /* best-effort */
      });
      reader = null;
    }
  };
}