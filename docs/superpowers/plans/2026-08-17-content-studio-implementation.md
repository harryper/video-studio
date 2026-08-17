# Content Studio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a working, independently deployable Content Studio that turns a topic into researched story pitches, an editable investigative-science draft, reviewed revisions, and an immutable approved script.

**Architecture:** Add the new system beside the legacy application until acceptance. Use a FastAPI modular monolith, SQLite/SQLAlchemy transactional persistence, a leased database job worker, stage-specific model/search adapters, and a React/TypeScript editing UI. Stage artifacts are immutable revisions; projects only point at the accepted revision for each stage.

**Tech Stack:** Python 3.12, FastAPI 0.141.1, SQLAlchemy 2.0.51, Alembic, Pydantic v2, SQLite WAL, pytest, React 19.2, TypeScript, Vite 8.1, Vitest, Testing Library, Playwright, Docker Compose.

## Global Constraints

- Content type is investigative-story science for 18–35-year-old general-knowledge viewers.
- Duration is inferred from content, normally 60–360 seconds; never pad or truncate merely to hit a target.
- Verify story-bearing conclusions and high-risk claims; ordinary background knowledge does not require citations.
- Preserve exactly two human gates: pitch selection and draft approval/review.
- Do not use mandatory five-act structure, mandatory reversal, or canned phrases such as “你以为……其实……”, “这就有意思了”, “离谱的是”, “说白了”, “关键是”, or “没了”.
- Model calls use fresh, stage-specific context and validated structured output.
- Re-generation creates a revision and never overwrites an earlier artifact.
- Default tests are offline, use fake providers, write only to pytest temporary paths, and never read production credentials.
- Keep the legacy application runnable throughout this plan; deletion belongs only to the cutover plan.
- Run all Python commands through `uv run`; commit `uv.lock` and `web/package-lock.json`.

---

## File Structure

```text
pyproject.toml                    Python dependencies and tool configuration
uv.lock                           Locked Python dependency graph
alembic.ini                       Migration configuration
migrations/                      SQLite schema migrations
studio/
  api/app.py                      FastAPI assembly, middleware, route mounting
  api/dependencies.py             DB/provider dependency injection
  api/routes/{projects,stages,comments,events}.py
  config.py                       Environment-backed settings
  db.py                           Engine, session factory, WAL setup
  models.py                       SQLAlchemy persistence models
  schemas.py                      Shared Pydantic content and API contracts
  artifacts.py                    Immutable artifact repository
  workflow.py                     Allowed transitions and downstream invalidation
  jobs.py                         Queue, lease, heartbeat, commit and recovery
  providers/{base,fake,anthropic,search}.py
  content/{diagnosis,research,pitches,narratives,writing,review,speech}.py
  worker.py                       Stage dispatcher process
  cli.py                          Migration, worker and evaluation commands
web/
  package.json                    Frontend dependencies and scripts
  src/api/                        Typed HTTP and SSE client
  src/components/                 Shared progress, error, diff and comment UI
  src/pages/                      Projects, pitches, editor and approval pages
  src/state/                      Query hooks and draft autosave state
tests/                            Offline backend tests
web/src/**/*.test.tsx             Frontend unit/component tests
web/e2e/                          Playwright user-flow tests
```

### Task 1: Reproducible application skeleton

**Files:**
- Create: `pyproject.toml`
- Create: `studio/__init__.py`
- Create: `studio/config.py`
- Create: `studio/api/app.py`
- Create: `tests/test_health.py`
- Create: `web/package.json`
- Create: `web/src/main.tsx`
- Create: `web/src/App.tsx`
- Modify: `.gitignore`
- Create: `Dockerfile.next`
- Create: `docker-compose.next.yml`

**Interfaces:**
- Produces: `studio.api.app:create_app() -> FastAPI`
- Produces: `studio.config:Settings` with `database_url`, `artifact_dir`, `provider_name`, `search_provider_name`, `lease_seconds`

- [ ] **Step 1: Write the failing health test**

```python
from fastapi.testclient import TestClient
from studio.api.app import create_app

def test_health_reports_content_studio():
    response = TestClient(create_app()).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "app": "content-studio"}
```

- [ ] **Step 2: Run the test and verify import failure**

Run: `uv run pytest tests/test_health.py -v`
Expected: FAIL because `studio.api.app` does not exist.

- [ ] **Step 3: Add the minimal FastAPI app and locked toolchains**

```python
from fastapi import FastAPI

def create_app() -> FastAPI:
    app = FastAPI(title="Content Studio")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "app": "content-studio"}

    return app

app = create_app()
```

Declare Python `>=3.12,<3.13`, FastAPI `0.141.1`, SQLAlchemy `2.0.51`, and the test/lint dependencies in `pyproject.toml`. Initialize the React 19.2/Vite 8.1 TypeScript app manually so no generated demo files remain. Configure the new container on port `10000`; do not change the legacy `9998` service.

Add `.venv/`, `node_modules/`, `*.db`, `*.db-wal`, `*.db-shm`, `media/`, and frontend build directories to `.gitignore` before installing dependencies.

- [ ] **Step 4: Lock and verify both applications**

Run: `uv lock && uv run pytest tests/test_health.py -v && npm --prefix web install && npm --prefix web run build`
Expected: backend test PASS and frontend production build succeeds.

- [ ] **Step 5: Commit**

```bash
git add .gitignore pyproject.toml uv.lock studio tests/test_health.py web/package.json web/package-lock.json web/src web/index.html web/tsconfig.json web/vite.config.ts Dockerfile.next docker-compose.next.yml
git commit -m "build: scaffold content studio"
```

### Task 2: Transactional database and immutable artifacts

**Files:**
- Create: `studio/db.py`
- Create: `studio/models.py`
- Create: `studio/schemas.py`
- Create: `studio/artifacts.py`
- Create: `migrations/env.py`
- Create: `migrations/versions/0001_content_core.py`
- Create: `alembic.ini`
- Test: `tests/test_artifacts.py`

**Interfaces:**
- Produces: `Project`, `Artifact`, `EditorialComment`, `StageJob` ORM models
- Produces: `ArtifactRepository.create(project_id, kind, payload, parent_id=None, created_by="system") -> Artifact`
- Produces: `ArtifactRepository.get(artifact_id) -> Artifact`
- Produces: `ArtifactRepository.current(project_id, kind) -> Artifact | None`
- Produces: `ArtifactRepository.accept(project_id, artifact_id) -> Artifact`
- Produces: `ArtifactRepository.list_revisions(project_id, kind) -> list[Artifact]`

- [ ] **Step 1: Write artifact immutability and revision tests**

```python
def test_artifacts_are_append_only(repo, project):
    first = repo.create(project.id, "story_pitch_set", {"pitches": [1, 2, 3]})
    second = repo.create(project.id, "story_pitch_set", {"pitches": [4, 5, 6]}, parent_id=first.id)
    assert (first.revision, second.revision) == (1, 2)
    assert repo.get(first.id).payload == {"pitches": [1, 2, 3]}

def test_accept_sets_only_current_pointer(repo, project):
    artifact = repo.create(project.id, "story_pitch_set", {"pitches": []})
    repo.accept(project.id, artifact.id)
    assert repo.current(project.id, "story_pitch_set").id == artifact.id
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_artifacts.py -v`
Expected: FAIL because the models and repository are undefined.

- [ ] **Step 3: Implement the schema and repository**

Create tables `projects`, `artifacts`, `project_artifact_heads`, `editorial_comments`, and `stage_jobs`. Store artifact payloads as validated JSON, use UUID strings, add unique `(project_id, kind, revision)`, foreign keys, and timestamps. Configure `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, and `busy_timeout=5000` on every connection.

Reject ORM updates to artifact `payload`, `kind`, `project_id`, and `revision` after insert. `accept()` updates only `project_artifact_heads` inside one transaction.

- [ ] **Step 4: Run migrations and tests**

Run: `CONTENT_STUDIO_DB=/tmp/content-studio-plan.db uv run alembic upgrade head && uv run pytest tests/test_artifacts.py -v`
Expected: migration succeeds and all artifact tests PASS.

- [ ] **Step 5: Commit**

```bash
git add alembic.ini migrations studio/db.py studio/models.py studio/schemas.py studio/artifacts.py tests/test_artifacts.py
git commit -m "feat: add immutable content revisions"
```

### Task 3: Leased job queue and recovery

**Files:**
- Create: `studio/jobs.py`
- Create: `studio/worker.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Produces: `enqueue(project_id: str, stage: Stage, input_artifact_ids: list[str]) -> StageJob`
- Produces: `claim_next(worker_id: str, now: datetime) -> ClaimedJob | None`
- Produces: `heartbeat(job_id: str, token: str, now: datetime) -> None`
- Produces: `finish(job_id: str, token: str, output_artifact_id: str) -> None`
- Produces: `fail(job_id: str, token: str, code: str, message: str) -> None`
- Produces: `recover_expired(now: datetime) -> list[str]`

- [ ] **Step 1: Write ownership and expiry tests**

```python
def test_stale_worker_cannot_finish(queue, queued_job, clock):
    first = queue.claim_next("worker-a", clock.now)
    clock.advance(seconds=901)
    queue.recover_expired(clock.now)
    second = queue.claim_next("worker-b", clock.now)
    with pytest.raises(StaleLease):
        queue.finish(first.id, first.token, "artifact-old")
    queue.finish(second.id, second.token, "artifact-new")
```

Also test FIFO claim, different-project claims, idempotent recovery, heartbeat extension, maximum three attempts, cancellation, and one running stage per project.

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/test_jobs.py -v`
Expected: FAIL because `studio.jobs` is missing.

- [ ] **Step 3: Implement transactional claims**

Use `BEGIN IMMEDIATE` for claims, a cryptographically random token, a default 900-second lease, and token checks on every mutation. Recovery changes expired running jobs back to queued unless attempts reached three, in which case it marks them failed. Worker handlers return artifact IDs; they never write project state directly.

- [ ] **Step 4: Verify queue behavior**

Run: `uv run pytest tests/test_jobs.py -v`
Expected: all queue and recovery tests PASS.

- [ ] **Step 5: Commit**

```bash
git add studio/jobs.py studio/worker.py tests/test_jobs.py
git commit -m "feat: add recoverable stage jobs"
```

### Task 4: Provider contracts and deterministic fakes

**Files:**
- Create: `studio/providers/base.py`
- Create: `studio/providers/fake.py`
- Create: `studio/providers/anthropic.py`
- Create: `studio/providers/search.py`
- Test: `tests/test_providers.py`

**Interfaces:**
- Produces: `ModelProvider.generate(schema: type[T], system: str, prompt: str, *, operation: str) -> T`
- Produces: `SearchProvider.search(query: str, *, limit: int = 5) -> list[SourceDocument]`
- Produces: `FakeModelProvider.responses: dict[str, list[BaseModel]]`

- [ ] **Step 1: Write contract tests**

```python
def test_fake_provider_returns_operation_fixture(fake_provider):
    result = fake_provider.generate(TopicDiagnosis, "system", "topic", operation="diagnosis")
    assert result.core_question == "测试问题"

def test_provider_repairs_format_only_once(broken_client):
    provider = AnthropicProvider(client=broken_client)
    provider.generate(TopicDiagnosis, "s", "p", operation="diagnosis")
    assert broken_client.call_count == 2
    assert broken_client.calls[1].metadata["mode"] == "schema_repair"
```

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/test_providers.py -v`
Expected: FAIL because provider classes do not exist.

- [ ] **Step 3: Implement adapters**

Keep prompts out of adapters. Parse model output directly into the requested Pydantic schema. The second call receives the invalid payload and validation errors and may only repair structure. Redact authorization headers and response bodies from logs. Implement search behind a configurable HTTP JSON endpoint and normalize title, URL, snippet, publisher, and publication date.

- [ ] **Step 4: Verify offline adapters**

Run: `uv run pytest tests/test_providers.py -v`
Expected: PASS without network or credentials.

- [ ] **Step 5: Commit**

```bash
git add studio/providers tests/test_providers.py
git commit -m "feat: define model and search providers"
```

### Task 5: Topic diagnosis and research packet

**Files:**
- Create: `studio/content/diagnosis.py`
- Create: `studio/content/research.py`
- Test: `tests/content/test_diagnosis.py`
- Test: `tests/content/test_research.py`

**Interfaces:**
- Produces: `diagnose_topic(topic: str, provider: ModelProvider) -> TopicDiagnosis`
- Produces: `build_research_packet(diagnosis: TopicDiagnosis, model: ModelProvider, search: SearchProvider) -> ResearchPacket`

- [ ] **Step 1: Write failing content-policy tests**

```python
def test_research_flags_high_risk_claims(packet):
    assert packet.fact_cards[0].risk in {"number", "date", "superlative", "absolute"}
    assert packet.fact_cards[0].verification_status == "verified"

def test_unverified_central_claim_is_rejected(service):
    with pytest.raises(UnverifiedCentralClaim):
        service.finalize(packet_with_unverified_payoff())
```

Also assert diagnosis contains scope/exclusions and contains no draft hook; assert ordinary background facts may omit sources.

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/content/test_diagnosis.py tests/content/test_research.py -v`
Expected: FAIL because content services are missing.

- [ ] **Step 3: Implement focused prompts and validators**

Diagnosis asks only for the central investigative question, audience prior knowledge, tension, misconceptions, scope, and exclusions. Research first expands candidate material, then searches only central/high-risk claims, attaches normalized sources, and removes or softens unsupported absolutes.

- [ ] **Step 4: Verify content contracts**

Run: `uv run pytest tests/content/test_diagnosis.py tests/content/test_research.py -v`
Expected: PASS with fake providers.

- [ ] **Step 5: Commit**

```bash
git add studio/content/diagnosis.py studio/content/research.py tests/content
git commit -m "feat: build scoped research packets"
```

### Task 6: Diverse story pitches and selection workflow

**Files:**
- Create: `studio/content/pitches.py`
- Modify: `studio/workflow.py`
- Create: `studio/api/routes/stages.py`
- Test: `tests/content/test_pitches.py`
- Test: `tests/api/test_pitch_routes.py`

**Interfaces:**
- Produces: `generate_pitches(diagnosis, research, provider) -> StoryPitchSet`
- Produces: `regenerate_pitch(pitch_set, pitch_id, feedback, provider) -> StoryPitchSet`
- Produces: `accept_pitch(project_id, pitch_set_id, pitch_id, edited_pitch=None) -> Artifact`

- [ ] **Step 1: Write diversity and revision tests**

```python
def test_pitches_use_distinct_questions_or_evidence_paths(service):
    result = service.generate(diagnosis_fixture(), research_fixture())
    assert len(result.pitches) == 3
    assert service.effective_difference_rate(result.pitches) == 1.0

def test_single_pitch_regeneration_preserves_other_ids(service):
    revised = service.regenerate(original, original.pitches[1].id, "不要历史路线")
    assert revised.pitches[0] == original.pitches[0]
    assert revised.pitches[2] == original.pitches[2]
```

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/content/test_pitches.py tests/api/test_pitch_routes.py -v`
Expected: FAIL because pitch services/routes are missing.

- [ ] **Step 3: Implement pitch generation and human gate**

Require three stable IDs and all approved fields. Reject pairs that share both normalized investigation question and evidence-path signature; regenerate only colliding entries. `POST /projects/{id}/pitches/{pitch_id}/accept` creates an accepted pitch artifact and queues narrative planning.

- [ ] **Step 4: Verify services and API**

Run: `uv run pytest tests/content/test_pitches.py tests/api/test_pitch_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add studio/content/pitches.py studio/workflow.py studio/api/routes/stages.py tests/content/test_pitches.py tests/api/test_pitch_routes.py
git commit -m "feat: add editorial pitch selection"
```

### Task 7: Narrative plan, free draft, and anti-template checks

**Files:**
- Create: `studio/content/narratives.py`
- Create: `studio/content/writing.py`
- Create: `studio/content/fingerprints.py`
- Test: `tests/content/test_narratives.py`
- Test: `tests/content/test_writing.py`

**Interfaces:**
- Produces: `plan_narrative(pitch, research, provider) -> NarrativePlan`
- Produces: `write_draft(plan, research, provider) -> DraftRevision`
- Produces: `analyze_repetition(draft, recent_drafts) -> RepetitionReport`

- [ ] **Step 1: Write narrative invariants**

```python
def test_every_beat_advances_investigation(plan):
    assert all(beat.purpose and beat.new_information for beat in plan.beats)
    assert all(beat.fact_card_ids for beat in plan.beats if beat.purpose != "question")

def test_canned_phrase_is_reported_not_reworded():
    report = analyze_repetition(draft("这就有意思了"), [])
    assert report.must_replan is True
    assert report.rewrite_suggestions == []
```

Also test duration derives from spoken-character estimate, range warnings occur outside 60–360 seconds, and no prompt contains mandatory five-act/reversal instructions.

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/content/test_narratives.py tests/content/test_writing.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement planning, writing, and fingerprints**

Use paragraph beats with stable IDs. Writing receives only accepted research and plan. Compute fingerprints for opening syntax, transition distribution, reveal position, ending shape, comparisons, and misconception-correction pattern against the latest 50 approved drafts. If similarity exceeds the tested threshold, mark `must_replan`; do not perform synonym substitution.

- [ ] **Step 4: Verify narrative quality gates**

Run: `uv run pytest tests/content/test_narratives.py tests/content/test_writing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add studio/content/narratives.py studio/content/writing.py studio/content/fingerprints.py tests/content
git commit -m "feat: write free-form investigative drafts"
```

### Task 8: Paragraph comments, targeted rewrite, and immutable approval

**Files:**
- Create: `studio/content/review.py`
- Create: `studio/api/routes/comments.py`
- Modify: `studio/api/routes/stages.py`
- Test: `tests/content/test_review.py`
- Test: `tests/api/test_review_routes.py`

**Interfaces:**
- Produces: `create_comment(draft_id, paragraph_id, start, end, kind, body, ai_action) -> EditorialComment`
- Produces: `rewrite_with_comments(draft, comments, provider) -> DraftRevision`
- Produces: `approve_draft(project_id, draft_id) -> ApprovedScript`

- [ ] **Step 1: Write protected-text and diff tests**

```python
def test_protected_span_survives_rewrite_exactly(review_service):
    revised = review_service.rewrite(draft, [protect("真正的原因在供应链")])
    assert "真正的原因在供应链" in revised.text

def test_rewrite_touches_only_selected_paragraphs(review_service):
    revised = review_service.rewrite(draft, [comment_on("p2", "这里没讲懂")])
    assert revised.paragraph("p1") == draft.paragraph("p1")
    assert revised.paragraph("p3") == draft.paragraph("p3")
```

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/content/test_review.py tests/api/test_review_routes.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement anchored comments and approval**

Validate offsets against the referenced immutable revision. Send only selected paragraphs, necessary neighboring context, comments, and protected spans to the rewrite provider. Reject output if any protected span differs. Approval creates an `approved_script` artifact and prevents downstream code from updating its payload.

- [ ] **Step 4: Verify review behavior**

Run: `uv run pytest tests/content/test_review.py tests/api/test_review_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add studio/content/review.py studio/api/routes/comments.py studio/api/routes/stages.py tests/content/test_review.py tests/api/test_review_routes.py
git commit -m "feat: add targeted editorial revisions"
```

### Task 9: Speech-plan derivation without semantic edits

**Files:**
- Create: `studio/content/speech.py`
- Test: `tests/content/test_speech.py`

**Interfaces:**
- Produces: `build_speech_plan(approved: ApprovedScript, provider: ModelProvider) -> SpeechPlan`
- Produces: `assert_semantic_identity(approved: ApprovedScript, plan: SpeechPlan) -> None`

- [ ] **Step 1: Write semantic identity tests**

```python
def test_speech_plan_preserves_normalized_words(service, approved):
    plan = service.build(approved)
    assert normalize_spoken(plan.spoken_text) == normalize_spoken(approved.editorial_text)
    assert plan.source_revision_id == approved.revision_id

def test_added_fact_is_rejected(service, approved):
    with pytest.raises(SemanticMutation):
        service.validate(approved, speech_plan_with_added_number())
```

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/content/test_speech.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement deterministic segmentation plus constrained pronunciation metadata**

Build subtitle semantic blocks deterministically from punctuation and syntax. Permit provider output only for pronunciation, emphasis, and pause metadata keyed to existing spans. Compute duration from the configured voice rate; outside-range results create an editorial warning and never mutate text.

- [ ] **Step 4: Verify speech contract**

Run: `uv run pytest tests/content/test_speech.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add studio/content/speech.py tests/content/test_speech.py
git commit -m "feat: derive immutable speech plans"
```

### Task 10: Project, stage, progress, and SSE APIs

**Files:**
- Create: `studio/api/dependencies.py`
- Create: `studio/api/auth.py`
- Create: `studio/api/routes/projects.py`
- Create: `studio/api/routes/events.py`
- Modify: `studio/api/app.py`
- Test: `tests/api/test_projects.py`
- Test: `tests/api/test_events.py`

**Interfaces:**
- Produces REST endpoints under `/api/projects`
- Produces `GET /api/projects/{id}/events` as `text/event-stream`

- [ ] **Step 1: Write API lifecycle tests**

```python
def test_create_project_queues_diagnosis(client):
    response = client.post("/api/projects", json={"topic": "糖为什么是战略物资"})
    assert response.status_code == 201
    assert response.json()["stage"] == "diagnosis_queued"

def test_upstream_change_reports_invalidated_artifacts(client, drafted_project):
    response = client.post(f"/api/projects/{drafted_project.id}/pitch/reopen")
    assert response.status_code == 409
    assert response.json()["invalidates"] == ["narrative_plan", "draft"]
```

Also cover project list filters, artifact history, retry, cancel, actionable error codes, and SSE progress replay via `Last-Event-ID`.

```python
def test_mutating_api_requires_session_and_csrf(client):
    assert client.post("/api/projects", json={"topic": "台风"}).status_code == 401
    login = client.post("/api/session", json={"password": "test-password"})
    assert login.status_code == 204
    assert client.post("/api/projects", json={"topic": "台风"}).status_code == 403
```

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/api -v`
Expected: FAIL for missing routes.

- [ ] **Step 3: Implement APIs and event stream**

Use explicit transition commands instead of a generic PATCH status endpoint. Publish job progress rows transactionally and stream them through SSE with heartbeat comments. Return stable `{code, message, details}` errors. Upstream reopening requires a second confirmation request containing the listed artifact IDs. Add a single-user session using an environment-only password, random environment-only signing key, `HttpOnly`/`SameSite=Strict` cookies, `Secure` in production, and a per-session CSRF token for mutating requests. Only `/api/health` and the login endpoint are public; media downloads require the same session.

- [ ] **Step 4: Verify API suite**

Run: `uv run pytest tests/api -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add studio/api tests/api
git commit -m "feat: expose content workflow api"
```

### Task 11: Projects dashboard and live workflow shell

**Files:**
- Create: `web/src/api/client.ts`
- Create: `web/src/api/types.ts`
- Create: `web/src/pages/ProjectsPage.tsx`
- Create: `web/src/pages/ProjectWorkspace.tsx`
- Create: `web/src/components/StageStepper.tsx`
- Create: `web/src/components/JobProgress.tsx`
- Test: `web/src/pages/ProjectsPage.test.tsx`
- Test: `web/src/components/JobProgress.test.tsx`

**Interfaces:**
- Consumes: project REST API and SSE event endpoint from Task 10
- Produces: `/` project dashboard and `/projects/:id` workspace routes

- [ ] **Step 1: Write component tests**

```tsx
it('shows editorial statuses instead of backend state names', async () => {
  render(<ProjectsPage />, { wrapper: testAppWith([{ id: 'p1', topic: '台风', stage: 'pitch_review' }]) })
  expect(await screen.findByText('等待选切口')).toBeVisible()
  expect(screen.queryByText('pitch_review')).not.toBeInTheDocument()
})
```

Test new-topic minimal form, status filters, stepper, reconnecting SSE, cancel/retry actions, and detailed failure disclosure.

- [ ] **Step 2: Verify frontend failures**

Run: `npm --prefix web test -- ProjectsPage JobProgress`
Expected: FAIL because the components do not exist.

- [ ] **Step 3: Implement dashboard and workspace shell**

Use accessible semantic controls, keyboard focus states, and responsive CSS. Show precise progress text from the event payload. Do not poll or reload the page. Keep advanced creation settings collapsed.

- [ ] **Step 4: Verify components and build**

Run: `npm --prefix web test -- ProjectsPage JobProgress && npm --prefix web run build`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src
git commit -m "feat: add live content dashboard"
```

### Task 12: Pitch cards and editorial review workspace

**Files:**
- Create: `web/src/pages/PitchReviewPage.tsx`
- Create: `web/src/pages/DraftReviewPage.tsx`
- Create: `web/src/components/PitchCard.tsx`
- Create: `web/src/components/ParagraphEditor.tsx`
- Create: `web/src/components/CommentPanel.tsx`
- Create: `web/src/components/RevisionDiff.tsx`
- Create: `web/src/components/QualityPanel.tsx`
- Test: `web/src/pages/PitchReviewPage.test.tsx`
- Test: `web/src/pages/DraftReviewPage.test.tsx`

**Interfaces:**
- Consumes: pitch, comment, rewrite, artifact-history, and approval endpoints
- Produces: three-card pitch gate and desktop-three-column/mobile-tab review UI

- [ ] **Step 1: Write user-interaction tests**

```tsx
it('rewrites only comments explicitly marked for AI', async () => {
  const user = userEvent.setup()
  render(<DraftReviewPage />, { wrapper: reviewFixture() })
  await user.click(screen.getByLabelText('交给 AI：这里没讲懂'))
  await user.click(screen.getByRole('button', { name: '预览本轮修改' }))
  expect(screen.getByText('将修改：段落 2')).toBeVisible()
  expect(screen.getByText('不会修改：段落 1、3')).toBeVisible()
})
```

Test edit-and-accept pitch, regenerate one/all, autosave conflict, text-selection comment offsets, protect selection, accept/reject diff hunks, ignored-quality reason, mobile tabs, and explicit reopen confirmation.

- [ ] **Step 2: Verify failures**

Run: `npm --prefix web test -- PitchReviewPage DraftReviewPage`
Expected: FAIL.

- [ ] **Step 3: Implement editorial interactions**

Render paragraph blocks with stable IDs. Autosave with artifact revision preconditions; on conflict show both versions rather than overwriting. Selection comments store Unicode code-point offsets. Diff decisions create another revision. Quality warnings link to paragraph IDs and require a reason when ignored.

- [ ] **Step 4: Verify UI and accessibility**

Run: `npm --prefix web test && npm --prefix web run build`
Expected: all frontend tests and build PASS.

- [ ] **Step 5: Commit**

```bash
git add web/src
git commit -m "feat: add pitch and draft review ui"
```

### Task 13: Offline end-to-end workflow and evaluation harness

**Files:**
- Create: `tests/fixtures/provider_responses/*.json`
- Create: `tests/e2e/test_content_workflow.py`
- Create: `studio/evaluation.py`
- Create: `evaluation/topics.yaml`
- Create: `evaluation/rubric.yaml`
- Create: `web/e2e/content-workflow.spec.ts`
- Modify: `studio/cli.py`

**Interfaces:**
- Produces: `uv run content-studio evaluate --dataset evaluation/topics.yaml --output <dir>`
- Produces: machine-readable `results.json` and human review `ballot.csv`

- [ ] **Step 1: Write the offline end-to-end test**

```python
def test_topic_to_approved_script(app, fake_providers, worker):
    project = create_project(app, "西瓜为什么不用来制糖")
    worker.drain()
    accept_pitch(app, project.id, pitch_index=1)
    worker.drain()
    add_comment(app, project.id, paragraph="p2", body="把成本因果讲清楚")
    rewrite_selected(app, project.id)
    approved = approve_current_draft(app, project.id)
    assert approved.kind == "approved_script"
    assert approved.payload["editorial_text"]
```

- [ ] **Step 2: Verify the end-to-end test fails**

Run: `uv run pytest tests/e2e/test_content_workflow.py -v`
Expected: FAIL until dispatcher wiring and fixtures are complete.

- [ ] **Step 3: Wire the dispatcher and 24-topic evaluation dataset**

Map every stage enum to its service, persist operation progress, and add recorded valid/invalid fixtures. Evaluation output must include eight approved rubric fields, blind randomized labels, pitch-difference decisions, claim-verification coverage, canned-phrase flags, and recent-structure similarity.

- [ ] **Step 4: Run all offline quality gates**

Run: `uv run pytest -q && npm --prefix web test && npm --prefix web run build && npm --prefix web run e2e`
Expected: all commands PASS without network.

- [ ] **Step 5: Commit**

```bash
git add studio tests evaluation web/e2e web/package.json web/package-lock.json
git commit -m "test: cover content studio end to end"
```

### Task 14: Deployment, operator docs, and Content Studio acceptance gate

**Files:**
- Create: `README.next.md`
- Create: `docs/operations/content-studio.md`
- Modify: `docker-compose.next.yml`
- Create: `systemd/video-studio-next-worker.service`
- Create: `scripts/run_content_acceptance.sh`
- Test: `tests/test_deployment_contract.py`

**Interfaces:**
- Produces: new Web service on port `10000` and one recoverable worker service
- Produces: acceptance bundle under `evaluation/results/<run-id>/`

- [ ] **Step 1: Write deployment contract tests**

```python
def test_next_services_use_finite_timeouts(compose, worker_unit):
    assert compose["services"]["content-studio-web"]["healthcheck"]
    assert compose["services"]["content-studio-web"]["read_only"] is True
    assert "TimeoutStartSec=" in worker_unit
    assert "Restart=on-failure" in worker_unit
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/test_deployment_contract.py -v`
Expected: FAIL until deployment files are complete.

- [ ] **Step 3: Complete side-by-side deployment and acceptance script**

Document environment variables, migrations, backup-free test data reset, provider setup, worker recovery, and logs. The acceptance script runs offline tests, one explicitly authorized online 24-topic generation, produces blind ballots, and calculates only the approved thresholds: 75% preference, under 10% obvious canned phrasing, 90% pitch distinction, 100% verified central claims, and 100% protected-text preservation.

- [ ] **Step 4: Run final Content Studio verification**

Run: `uv run pytest -q && npm --prefix web test && npm --prefix web run build && docker compose -f docker-compose.next.yml config && bash scripts/run_content_acceptance.sh --offline`
Expected: all checks PASS and no legacy file changes appear in `git status --short`.

- [ ] **Step 5: Commit**

```bash
git add README.next.md docs/operations docker-compose.next.yml systemd/video-studio-next-worker.service scripts/run_content_acceptance.sh tests/test_deployment_contract.py
git commit -m "docs: prepare content studio acceptance"
```

## Content Studio Stop Gate

Do not start the video-pipeline plan until the user has reviewed real generated drafts and the acceptance bundle demonstrates the approved thresholds. A passing automated suite alone is insufficient.
