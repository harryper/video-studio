# Clean Cutover and History Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the legacy deployment and repository history with the accepted rebuilt application, deleting old code, runtime data, media, and Git history while preserving only explicitly listed secrets and approved reusable assets.

**Architecture:** This is a gated destructive cutover, not a migration layer. First create machine-readable keep/delete manifests and verify the new application from a staged clean tree; then stop old services, remove exact approved targets, move the new application into final paths, initialize a single-root Git history, and force-push only after local and deployed verification.

**Tech Stack:** POSIX shell with strict mode, Git plumbing, Docker Compose, systemd, pytest, npm, FFmpeg smoke validation.

## Global Constraints

- Start only after both prior stop gates have explicit user approval.
- No compatibility code, old prompt, old test, old document, old job, old run, old archive, old log, old trigger, or old Git commit may remain.
- Preserve only paths listed in `cutover/keep-manifest.txt`; secrets remain untracked.
- Never use `$HOME`, `~`, `/`, workspace-root globs, or unresolved variables as recursive deletion targets.
- Every destructive target must resolve under `/root/opc/video-studio` and match the reviewed manifest.
- The new root commit must contain only rebuilt source, current docs, tests, lockfiles, deployment definitions, and approved non-secret assets.
- Force push occurs only after local verification, deployment smoke testing, and a displayed commit/file manifest.

---

### Task 1: Exact keep/delete manifests and validator

**Files:**
- Create: `cutover/keep-manifest.txt`
- Create: `cutover/delete-manifest.txt`
- Create: `scripts/validate_cutover_manifest.py`
- Test: `tests/cutover/test_manifest.py`

**Interfaces:**
- Produces: `uv run python scripts/validate_cutover_manifest.py --root /root/opc/video-studio --keep cutover/keep-manifest.txt --delete cutover/delete-manifest.txt`

- [ ] Write tests rejecting absolute targets outside the repository, symlink escapes, missing targets, overlapping keep/delete paths, `.git` in the file-deletion manifest, unresolved globs, and workspace root itself.

```python
@pytest.mark.parametrize("entry", ["/etc/passwd", "../outside", ".", ".git", "runs/*"])
def test_delete_manifest_rejects_unsafe_entries(tmp_path, entry):
    with pytest.raises(UnsafeCutoverTarget):
        validate_entries(tmp_path, [entry], keep=[])
```
- [ ] Run `uv run pytest tests/cutover/test_manifest.py -v`; expect failure.
- [ ] Implement `Path.resolve()` containment checks and manifests containing one literal relative path per line. List secret files and approved BGM/font/voice assets in keep; list every legacy source/runtime/deployment path explicitly in delete.
- [ ] Run the tests and validator; expect PASS and a sorted summary with byte/file counts.
- [ ] Commit with `git add cutover scripts/validate_cutover_manifest.py tests/cutover/test_manifest.py && git commit -m "chore: define cutover boundaries"`.

### Task 2: Build a clean release tree before deletion

**Files:**
- Create: `scripts/build_release_tree.py`
- Create: `release-manifest.sha256`
- Test: `tests/cutover/test_release_tree.py`

**Interfaces:**
- Produces: `uv run python scripts/build_release_tree.py --output <mktemp-dir>`

- [ ] Write tests proving the release tree contains all tracked rebuilt files, excludes every delete-manifest path and secret, contains no symlink escaping the tree, and has reproducible SHA-256 entries.

```python
def test_release_tree_excludes_secrets(builder, tmp_path):
    release = builder.build(tmp_path / "release")
    assert not (release / "llm_config.json").exists()
    assert verify_sha256_manifest(release / "release-manifest.sha256", release)
```
- [ ] Run `uv run pytest tests/cutover/test_release_tree.py -v`; expect failure.
- [ ] Implement copy-by-explicit-manifest into a caller-provided empty directory; refuse a non-empty output directory.
- [ ] Build into a `mktemp -d` path and run backend, frontend, renderer, migration, and offline acceptance commands from that tree; expect all PASS.
- [ ] Commit with `git add scripts/build_release_tree.py tests/cutover/test_release_tree.py release-manifest.sha256 && git commit -m "build: create clean release tree"`.

### Task 3: Stop and disable the legacy deployment

**Files:**
- Create: `scripts/cutover_services.sh`
- Test: `tests/cutover/test_service_script.py`

**Interfaces:**
- Produces: `bash scripts/cutover_services.sh inspect|stop-legacy|start-next|verify`

- [ ] Write static tests requiring exact legacy/new unit names, `set -euo pipefail`, finite command timeouts, state inspection before mutation, and no wildcard unit operations.

```python
def test_service_script_has_no_wildcard_units(script_text):
    assert "set -euo pipefail" in script_text
    assert "video-studio-*" not in script_text
    assert script_text.index("systemctl is-active") < script_text.index("systemctl stop")
```
- [ ] Run `uv run pytest tests/cutover/test_service_script.py -v`; expect failure.
- [ ] Implement inspect-only default behavior and explicit subcommands. `stop-legacy` stops/disables the four old path/service units and old Web container; `start-next` enables the new worker and Web service; `verify` checks API health and worker recovery.
- [ ] Run static tests and `bash scripts/cutover_services.sh inspect`; review the exact affected units without changing them.
- [ ] Commit with `git add scripts/cutover_services.sh tests/cutover/test_service_script.py && git commit -m "ops: script controlled service cutover"`.

### Task 4: Destructive file cleanup with a final dry run

**Files:**
- Create: `scripts/delete_legacy_paths.py`
- Test: `tests/cutover/test_delete_legacy.py`

**Interfaces:**
- Produces: `uv run python scripts/delete_legacy_paths.py --root /root/opc/video-studio --manifest cutover/delete-manifest.txt --dry-run`
- Produces: same command with `--execute --confirmation <manifest-sha256>`

- [ ] Write tests proving dry-run is default, execute requires the current manifest digest, changed manifests invalidate confirmation, keep paths are untouched, and every removed item was printed before execution.

```python
def test_execute_requires_current_digest(runner, manifest):
    result = runner.invoke(["--execute", "--confirmation", "stale-digest"])
    assert result.exit_code != 0
    assert manifest.first_target.exists()
```
- [ ] Run `uv run pytest tests/cutover/test_delete_legacy.py -v`; expect failure.
- [ ] Implement literal-path deletion without shell expansion; use unlink/rmdir traversal only after containment and symlink checks. Do not delete `.git` in this task.
- [ ] Stop legacy services, run dry-run, save its exact output, compare byte/file totals to Task 1, and obtain the user's final confirmation for that resolved list.
- [ ] Run execute with the reviewed digest, then rerun the validator; expect every delete target absent and every keep target present.
- [ ] Commit the surviving rebuilt tree changes with explicit paths; never use `git add .`.

### Task 5: Final application placement and deployment verification

**Files:**
- Modify: rebuilt deployment files to final names
- Replace: `README.md` with rebuilt-system documentation
- Create: `scripts/verify_release.sh`
- Test: `tests/cutover/test_final_tree.py`

**Interfaces:**
- Produces: final application on port `9998` and one new worker deployment

- [ ] Write tests asserting no legacy filename/state/prompt phrase remains, no runtime artifact is tracked, required lockfiles exist, README describes only the new workflow, and all service paths point to final locations.

```python
def test_final_tree_contains_no_legacy_markers(repo):
    tracked = repo.tracked_text()
    for marker in [".video-script-trigger", "ready_shotlist", "NARRATIVE_SKELETON"]:
        assert marker not in tracked
    assert repo.exists("uv.lock") and repo.exists("web/package-lock.json")
```
- [ ] Run `uv run pytest tests/cutover/test_final_tree.py -v`; expect failures listing remaining legacy references.
- [ ] Rename next deployment files to final names, update the port after old service removal, install the new unit, run migrations, and start the rebuilt Web/worker services.
- [ ] Run `bash scripts/verify_release.sh`; it must run all offline suites, API health, SQLite integrity check, queue recovery smoke, one fake end-to-end project, and ffprobe on the accepted real output.
- [ ] Commit with explicit rebuilt file paths and message `release: prepare clean video studio root`.

### Task 6: Replace Git history with one root commit

**Files:**
- No source files created; operates on the verified final tree and `.git`

**Interfaces:**
- Produces: branch `master` with exactly one root commit

- [ ] Record `git status --short`, `git ls-files`, secret scan results, `release-manifest.sha256`, and all verification output outside `/root/opc/video-studio` in a `mktemp -d` audit directory.
- [ ] Verify `git status --short` contains only intentional final-tree changes and no secrets, runtime media, database, logs, or user-owned untracked files.
- [ ] Capture the current remote SHA with `git rev-parse origin/master` in the audit directory. Create an orphan branch with `git checkout --orphan clean-master`, immediately clear only the index with `git rm -r --cached --ignore-unmatch .`, explicitly stage only paths listed by `release-manifest.sha256`, and commit `release: rebuild video studio`. Do not use `git clean`, `git rm` without `--cached`, or a workspace-wide deletion.
- [ ] Verify `git rev-list --count HEAD` prints `1`, `git fsck --no-reflogs --unreachable` is reviewed, `git ls-files` matches the release manifest, and all release checks still PASS.
- [ ] Delete the old local `master` ref only after all checks pass, rename `clean-master` to `master`, and verify `git rev-list --count master` prints `1`. Do not expire reflogs, prune objects, or force push in this task.

### Task 7: Force-push and post-cutover audit

**Files:**
- No source changes

**Interfaces:**
- Produces: remote `origin/master` pointing to the verified single root commit

- [ ] Display the exact local root commit, remote URL, `git log --oneline --all`, tracked-file count, and release-manifest digest to the user; obtain explicit force-push confirmation.
- [ ] Run `git push --force-with-lease=refs/heads/master:<captured-old-origin-sha> origin master`, substituting the exact SHA captured in Task 6; never use plain `--force` or an implicit lease.
- [ ] Fetch origin and assert local/remote commit equality plus `git rev-list --count origin/master` equals `1`.
- [ ] Run deployed health, create a fresh real project through both human gates, produce and ffprobe its final video, and verify no old job/media appears in UI or filesystem.
- [ ] After remote equality and production verification pass, confirm `git for-each-ref` shows no tag, local branch, or remote-tracking ref pointing to old commits. Then run `git reflog expire --expire=now --all` and `git gc --prune=now`, and verify `git fsck --unreachable` does not report the old commits recorded in the audit directory.
- [ ] Report deleted path/file/byte totals, preserved secret/asset path names without contents, final commit hash, acceptance results, and the fact that old history is no longer available from `origin/master`.

## Irreversibility Notice

Tasks 4, 6, and 7 intentionally remove runtime data and reachable Git history. Claude Code must stop at each explicit confirmation gate even if earlier approval exists, because the resolved path list, manifest digest, and remote lease are only known at execution time.
