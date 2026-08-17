# Video Pipeline Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the legacy four-daemon media pipeline with a recoverable pipeline that consumes immutable `ApprovedScript` and `SpeechPlan` artifacts without changing editorial text.

**Architecture:** Extend the modular monolith and leased worker created by the Content Studio plan. Each media stage creates immutable artifacts and local files under a project/revision-specific media root; adapters isolate TTS, alignment, image generation, rendering, FFmpeg, and publishing. The Web UI exposes explicit previews and approvals without writing content state directly.

**Tech Stack:** Existing Content Studio stack, FFmpeg/ffprobe, stable-ts, provider adapters, Hyperframes pinned in `web-render/package-lock.json`, pytest, Playwright, Docker/systemd worker deployment.

## Global Constraints

- This plan starts only after the Content Studio stop gate passes.
- `ApprovedScript.editorial_text` is immutable; media stages may never rewrite, truncate, append, or reorder it.
- `SpeechPlan` is the only input for TTS and subtitle timing.
- Every subprocess has a finite timeout and captured diagnostic tail.
- Every external provider has a fake adapter and offline contract tests.
- Every output path includes project ID and source artifact revision.
- Re-running a stage creates a new artifact and never overwrites an accepted artifact.
- Do not delete legacy code or production media during this plan.

---

### Task 1: Media contracts and revision-scoped storage

**Files:**
- Create: `studio/media/schemas.py`
- Create: `studio/media/storage.py`
- Create: `tests/media/test_storage.py`

**Interfaces:**
- Produces: `VisualPlan`, `VoiceAsset`, `Alignment`, `Storyboard`, `RenderAsset`, `PublishedAsset`
- Produces: `MediaStorage.path(project_id, source_revision, artifact_kind, filename) -> Path`

- [ ] Write tests proving path traversal is rejected, different revisions never collide, and writes use unique temporary files plus fsync/replace.

```python
def test_revision_paths_never_collide(storage):
    a = storage.path("p1", 3, "voice", "voice.mp3")
    b = storage.path("p1", 4, "voice", "voice.mp3")
    assert a != b
    with pytest.raises(UnsafeMediaPath):
        storage.path("p1", 3, "voice", "../../secret")
```
- [ ] Run `uv run pytest tests/media/test_storage.py -v`; expect missing-module failure.
- [ ] Implement typed schemas and revision-scoped atomic storage rooted at `CONTENT_STUDIO_MEDIA_DIR`.
- [ ] Run the test again; expect PASS.
- [ ] Commit with `git add studio/media tests/media/test_storage.py && git commit -m "feat: add revisioned media artifacts"`.

### Task 2: TTS and forced-alignment adapters

**Files:**
- Create: `studio/media/tts.py`
- Create: `studio/media/alignment.py`
- Create: `studio/providers/tts.py`
- Test: `tests/media/test_tts_alignment.py`

**Interfaces:**
- Produces: `synthesize(plan: SpeechPlan, voice: VoiceConfig) -> VoiceAsset`
- Produces: `align(plan: SpeechPlan, voice: VoiceAsset) -> Alignment`

- [ ] Write tests asserting TTS input equals `SpeechPlan.spoken_text`, alignment covers every semantic block monotonically, decimal punctuation is preserved, and provider timeouts become stable error codes.

```python
def test_tts_receives_only_approved_spoken_text(tts, speech_plan, provider):
    tts.synthesize(speech_plan, VoiceConfig(id="voice-1"))
    assert provider.requests == [speech_plan.spoken_text]
```
- [ ] Run `uv run pytest tests/media/test_tts_alignment.py -v`; expect failure.
- [ ] Implement MiniMax and fake TTS adapters plus stable-ts and fake alignment adapters; configure 300-second TTS and 900-second alignment timeouts.
- [ ] Run the tests; expect PASS without network or binaries through fakes.
- [ ] Commit with `git add studio/media studio/providers/tts.py tests/media/test_tts_alignment.py && git commit -m "feat: synthesize and align approved speech"`.

### Task 3: Storyboard and visual-plan generation

**Files:**
- Create: `studio/media/storyboard.py`
- Create: `studio/media/visuals.py`
- Test: `tests/media/test_storyboard.py`

**Interfaces:**
- Produces: `build_storyboard(approved, speech, alignment, provider) -> Storyboard`
- Produces: stable scene IDs referencing editorial paragraph IDs and alignment spans

- [ ] Write tests that every scene references existing text, all time spans cover the voice without overlap, visual prompts contain no unapproved factual text, and pad scenes contain no generated claims.

```python
def test_storyboard_scenes_trace_to_approved_paragraphs(storyboard, approved):
    paragraph_ids = {p.id for p in approved.paragraphs}
    assert all(set(scene.paragraph_ids) <= paragraph_ids for scene in storyboard.scenes)
    assert all(a.end <= b.start for a, b in pairwise(storyboard.scenes))
```
- [ ] Run `uv run pytest tests/media/test_storyboard.py -v`; expect failure.
- [ ] Implement visual planning from approved content and measured alignment; keep visuals descriptive and never feed visual copy back into the script.
- [ ] Run tests; expect PASS.
- [ ] Commit with `git add studio/media/storyboard.py studio/media/visuals.py tests/media/test_storyboard.py && git commit -m "feat: derive storyboards from approved scripts"`.

### Task 4: Image generation with deterministic fallback

**Files:**
- Create: `studio/providers/images.py`
- Create: `studio/media/images.py`
- Create: `studio/media/cards.py`
- Test: `tests/media/test_images.py`

**Interfaces:**
- Produces: `materialize_scene(scene: StoryboardScene) -> SceneMedia`

- [ ] Write tests for provider success, bounded retry, annotation removal on the final retry, deterministic local card fallback, correct aspect ratio, and no stock-service calls.

```python
def test_three_failures_use_local_card(service, failing_provider, scene):
    media = service.materialize_scene(scene)
    assert failing_provider.call_count == 3
    assert media.source == "local_card"
    assert media.width / media.height == pytest.approx(16 / 9)
```
- [ ] Run `uv run pytest tests/media/test_images.py -v`; expect failure.
- [ ] Implement fake/MiniMax adapters and a local Pillow fallback renderer with bundled CJK font validation.
- [ ] Run tests; expect PASS.
- [ ] Commit with `git add studio/providers/images.py studio/media/images.py studio/media/cards.py tests/media/test_images.py && git commit -m "feat: render storyboard scene media"`.

### Task 5: Composition and subtitle preview

**Files:**
- Create: `web-render/package.json`
- Create: `web-render/src/composition.ts`
- Create: `studio/media/composition.py`
- Test: `tests/media/test_composition.py`
- Test: `web-render/src/composition.test.ts`

**Interfaces:**
- Produces: versioned composition manifest and preview MP4

- [ ] Write tests for scene/alignment mapping, cover timing, subtitle semantic blocks, aspect ratios, missing media fallback, and preview generation from faked subprocess results.

```python
def test_manifest_uses_measured_alignment(builder, alignment):
    manifest = builder.build(storyboard_fixture(), alignment)
    assert manifest.scenes[0].start == alignment.blocks[0].start
    assert manifest.scenes[-1].end == alignment.blocks[-1].end
```
- [ ] Run `uv run pytest tests/media/test_composition.py -v && npm --prefix web-render test`; expect failure.
- [ ] Implement a data-driven composition package and pin Hyperframes in `web-render/package-lock.json`; never install packages with `npx --yes` at runtime.
- [ ] Run backend/frontend render tests and `npm --prefix web-render run build`; expect PASS.
- [ ] Commit with `git add web-render studio/media/composition.py tests/media/test_composition.py && git commit -m "feat: build versioned video compositions"`.

### Task 6: Final render, mux, validation, and publishing

**Files:**
- Create: `studio/media/rendering.py`
- Create: `studio/media/publishing.py`
- Create: `studio/providers/object_store.py`
- Test: `tests/media/test_rendering.py`

**Interfaces:**
- Produces: `render(composition, voice, alignment) -> RenderAsset`
- Produces: `publish(render_asset) -> PublishedAsset`

- [ ] Write tests asserting finite subprocess timeouts, ffprobe validation, cover audio delay, exact final-duration calculation, atomic final placement, upload retry bounds, and redacted URLs in logs.

```python
def test_unreadable_render_is_never_published(renderer, failed_probe, publisher):
    with pytest.raises(InvalidVideo):
        renderer.render(composition_fixture(), voice_fixture(), alignment_fixture())
    publisher.publish.assert_not_called()
```
- [ ] Run `uv run pytest tests/media/test_rendering.py -v`; expect failure.
- [ ] Implement command builders with argument arrays, 3600-second render cap, 600-second FFmpeg cap, 60-second ffprobe cap, and configurable object-store adapter.
- [ ] Run tests; expect PASS through fake commands/providers.
- [ ] Commit with `git add studio/media/rendering.py studio/media/publishing.py studio/providers/object_store.py tests/media/test_rendering.py && git commit -m "feat: render and publish approved videos"`.

### Task 7: Media workflow orchestration and recovery

**Files:**
- Create: `studio/media/workflow.py`
- Modify: `studio/worker.py`
- Test: `tests/media/test_media_workflow.py`

**Interfaces:**
- Consumes: leased jobs from the Content Studio plan
- Produces: stage chain `speech → voice → alignment → storyboard → scene_media → preview → render → publish`

- [ ] Write a crash-recovery test that expires each media-stage lease and proves a stale worker cannot publish or overwrite the new attempt.

```python
def test_stale_render_attempt_cannot_commit(queue, expired_render_job):
    old = queue.claim_next("old", expired_render_job.started_at)
    queue.recover_expired(expired_render_job.expired_at)
    new = queue.claim_next("new", expired_render_job.expired_at)
    with pytest.raises(StaleLease):
        queue.finish(old.id, old.token, "old-render")
    queue.finish(new.id, new.token, "new-render")
```
- [ ] Run `uv run pytest tests/media/test_media_workflow.py -v`; expect failure.
- [ ] Register media handlers, stage-specific lease lengths, heartbeats around long subprocesses, and explicit user-triggered reruns.
- [ ] Run queue and media workflow suites; expect PASS.
- [ ] Commit with `git add studio/media/workflow.py studio/worker.py tests/media/test_media_workflow.py && git commit -m "feat: orchestrate recoverable media jobs"`.

### Task 8: Web previews and production controls

**Files:**
- Create: `web/src/pages/ProductionPage.tsx`
- Create: `web/src/components/{SpeechPreview,StoryboardPreview,VoicePreview,RenderProgress,PublishPanel}.tsx`
- Test: `web/src/pages/ProductionPage.test.tsx`
- Test: `web/e2e/video-production.spec.ts`

**Interfaces:**
- Consumes: immutable media artifact APIs and SSE progress

- [ ] Write tests for speech/subtitle display, scene-to-paragraph traceability, audio preview, explicit rerun confirmation, stale-artifact badges, final download, and “return to review” invalidation warning.

```tsx
it('warns before returning an approved script to review', async () => {
  render(<ProductionPage />, { wrapper: productionFixture() })
  await userEvent.click(screen.getByRole('button', { name: '退回审稿' }))
  expect(screen.getByText('口播、分镜和渲染版本将失效')).toBeVisible()
})
```
- [ ] Run `npm --prefix web test -- ProductionPage`; expect failure.
- [ ] Implement responsive previews and production controls; do not expose raw status mutation.
- [ ] Run all frontend tests, build, and Playwright media flow; expect PASS.
- [ ] Commit with `git add web/src web/e2e/video-production.spec.ts && git commit -m "feat: add video production workspace"`.

### Task 9: Offline and real end-to-end acceptance

**Files:**
- Create: `tests/e2e/test_video_pipeline.py`
- Create: `scripts/run_video_acceptance.sh`
- Create: `docs/operations/video-pipeline.md`
- Modify: `docker-compose.next.yml`
- Modify: `systemd/video-studio-next-worker.service`

**Interfaces:**
- Produces: one verified final MP4 and a JSON manifest tracing every source revision

- [ ] Write an offline test running an approved script through fake TTS, alignment, images, render, and publish, asserting editorial-text hash equality at every boundary.

```python
def test_media_pipeline_preserves_editorial_hash(pipeline, approved):
    result = pipeline.run(approved)
    assert result.manifest.editorial_sha256 == sha256_text(approved.editorial_text)
    assert result.final_mp4.exists()
    assert result.probe.video_packets > 0
```
- [ ] Run `uv run pytest tests/e2e/test_video_pipeline.py -v`; expect failure until all wiring is present.
- [ ] Complete worker deployment, media mounts, finite systemd timeout, resource limits, health reporting, and acceptance script.
- [ ] Run `uv run pytest -q && npm --prefix web test && npm --prefix web-render test && bash scripts/run_video_acceptance.sh --offline`; expect PASS.
- [ ] With explicit external-provider authorization, run one real topic and inspect voice, subtitle, storyboard, final MP4, recovery manifest, and immutable text hash.
- [ ] Commit with `git add tests/e2e/test_video_pipeline.py scripts/run_video_acceptance.sh docs/operations/video-pipeline.md docker-compose.next.yml systemd/video-studio-next-worker.service && git commit -m "test: verify rebuilt video pipeline"`.

## Video Pipeline Stop Gate

Do not execute the cutover plan until the user approves one real end-to-end video and both the Content Studio and video acceptance bundles are retained outside directories scheduled for deletion.
