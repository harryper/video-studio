/**
 * DraftReviewPage — desktop three-column / mobile three-tab review (spec §10.4).
 *
 * Columns:
 *   * 路线图 (left) — NarrativePlan.beats
 *   * 正文 (center) — DraftRevision.paragraphs (read-only; mutate via comments)
 *   * 批注 + 检查 (right) — comments grouped by paragraph + QualityPanel
 *
 * The page owns the rewrite handshake:
 *   1. Editor seeds comments via the inline form.
 *   2. Editor clicks "预览本轮修改" — the panel renders what will and
 *      won't be rewritten.
 *   3. Editor clicks "接受全部" — POST /drafts/{id}/rewrite runs server-side
 *      (only paragraphs whose comments carry ``ai_action="rewrite"`` are
 *      sent to the model) and returns the new draft artifact id.
 *   4. The new draft replaces the current draft; the editor can re-review.
 *
 * Mobile breakpoint: < 768 px collapses the columns to three tabs at the
 * bottom of the viewport (路线图 / 正文 / 批注). The layout uses
 * ``display: none`` toggling, no nested route.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  listArtifacts,
  listComments,
  postComment,
  reopenPitches,
  triggerRewrite,
} from "../api/client";
import type {
  ApiError,
  ArtifactHistoryEntry,
  CreateCommentInput,
  DraftParagraph,
  DraftRevision,
  EditorialComment,
  NarrativePlan,
} from "../api/types";
import { CommentPanel } from "../components/CommentPanel";
import { errorMessage } from "../components/errorMessage";
import { ParagraphEditor } from "../components/ParagraphEditor";
import { RevisionDiff } from "../components/RevisionDiff";
import { Link } from "../router";

import styles from "./DraftReviewPage.module.css";

export interface DraftReviewPageProps {
  projectId: string;
  draftArtifactId: string;
  forceMobile?: boolean;
}

function extractInvalidates(err: ApiError): string[] {
  const value = err.body.invalidates;
  if (Array.isArray(value)) {
    return value.filter((entry): entry is string => typeof entry === "string");
  }
  return [];
}

function sortKinds(kinds: string[]): string {
  return [...kinds].sort().join(",");
}

type MobileTab = "roadmap" | "text" | "comments";

export function DraftReviewPage({
  projectId,
  draftArtifactId,
  forceMobile,
}: DraftReviewPageProps): React.ReactElement {
  const [draft, setDraft] = useState<DraftRevision | null>(null);
  const [plan, setPlan] = useState<NarrativePlan | null>(null);
  const [comments, setComments] = useState<EditorialComment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [conflictNewer, setConflictNewer] = useState<DraftRevision | null>(null);
  const [diffCandidate, setDiffCandidate] = useState<DraftRevision | null>(null);
  const [acceptedNotice, setAcceptedNotice] = useState<boolean>(false);
  const [reopenPending, setReopenPending] = useState<boolean>(false);
  const [reopenInvalidates, setReopenInvalidates] = useState<string[] | null>(null);
  const [activeTab, setActiveTab] = useState<MobileTab>("text");
  const [busy, setBusy] = useState<boolean>(false);

  const refresh = useCallback(async (): Promise<void> => {
    setError(null);
    try {
      const [artifacts, commentList] = await Promise.all([
        listArtifacts(projectId),
        listComments(projectId, draftArtifactId),
      ]);
      const draftArtifact = artifacts.find(
        (a: ArtifactHistoryEntry) => a.id === draftArtifactId && a.kind === "draft",
      );
      if (draftArtifact && draftArtifact.payload) {
        setDraft(draftArtifact.payload as DraftRevision);
      } else {
        setDraft(null);
      }
      const narrativeHead = artifacts.find(
        (a: ArtifactHistoryEntry) => a.kind === "narrative" && a.is_head,
      );
      if (narrativeHead && narrativeHead.payload) {
        setPlan(narrativeHead.payload as NarrativePlan);
      }
      setComments(commentList);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [projectId, draftArtifactId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleAddComment = useCallback(
    (_paragraphId: string, draftInput: CreateCommentInput): void => {
      void postComment(projectId, draftArtifactId, draftInput)
        .then((newComment) => {
          setComments((prev) => [...prev, newComment]);
        })
        .catch(async (err: unknown) => {
          const apiErr = err as ApiError;
          if (apiErr && apiErr.body && apiErr.status === 409) {
            // Newer draft exists — fetch artifacts and surface the
            // newer revision so the editor can see both side-by-side.
            try {
              const artifacts = await listArtifacts(projectId);
              const candidates = artifacts
                .filter((a: ArtifactHistoryEntry) => a.kind === "draft")
                .sort((a, b) => b.revision - a.revision);
              const newer = candidates.find((a) => a.id !== draftArtifactId && a.payload);
              if (newer && newer.payload) {
                setConflictNewer(newer.payload as DraftRevision);
              }
            } catch {
              // ignore secondary fetch failures
            }
          }
          setError(errorMessage(err));
        });
    },
    [projectId, draftArtifactId],
  );

  const handlePreviewRewrite = useCallback(
    (targets: string[], _untouched: string[]): void => {
      if (!draft) return;
      if (targets.length === 0) {
        setError("请至少添加一条标记为「交给 AI」的批注");
        return;
      }
      setBusy(true);
      setError(null);
      triggerRewrite(projectId, draftArtifactId)
        .then(async (response) => {
          const artifacts = await listArtifacts(projectId);
          const next = artifacts.find(
            (a: ArtifactHistoryEntry) => a.id === response.artifact_id,
          );
          if (next && next.payload) {
            setDiffCandidate(next.payload as DraftRevision);
            setAcceptedNotice(false);
          } else {
            setError("新版本未找到");
          }
        })
        .catch((err: unknown) => {
          setError(errorMessage(err));
        })
        .finally(() => {
          setBusy(false);
        });
    },
    [draft, projectId, draftArtifactId],
  );

  const handleAcceptDiff = useCallback(async (): Promise<void> => {
    if (!diffCandidate) return;
    setBusy(true);
    setError(null);
    try {
      // The backend already produced the artifact when we triggered the
      // rewrite — "accept" here is a UI acknowledgement that the diff
      // view was reviewed. Refresh comments / artifacts so any follow-up
      // view shows the new draft head.
      setAcceptedNotice(true);
      setDiffCandidate(null);
      await refresh();
    } finally {
      setBusy(false);
    }
  }, [diffCandidate, refresh]);

  const handleDiscardDiff = useCallback((): void => {
    setDiffCandidate(null);
  }, []);

  const triggerReopen = useCallback(async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await reopenPitches(projectId, sortKinds(reopenInvalidates ?? []));
      setReopenPending(false);
      setReopenInvalidates(null);
    } catch (err) {
      const apiErr = err as ApiError;
      if (apiErr && apiErr.body && apiErr.body.code === "confirmation_required") {
        setReopenInvalidates(extractInvalidates(apiErr));
        setReopenPending(true);
      } else {
        setError(errorMessage(err));
      }
    } finally {
      setBusy(false);
    }
  }, [projectId, reopenInvalidates]);

  const handleStartReopen = useCallback((): void => {
    void triggerReopen();
  }, [triggerReopen]);

  const handleConfirmReopen = useCallback((): void => {
    void triggerReopen();
  }, [triggerReopen]);

  const handleDismissReopen = useCallback((): void => {
    setReopenPending(false);
    setReopenInvalidates(null);
  }, []);

  const paragraphs: DraftParagraph[] = useMemo(
    () => (draft?.paragraphs ?? []) as DraftParagraph[],
    [draft],
  );

  if (!draft && !error) {
    return (
      <main className={styles.page}>
        <p className={styles.info}>加载稿件…</p>
      </main>
    );
  }

  if (!draft) {
    return (
      <main className={styles.page}>
        <p className={styles.error} role="alert">
          {error ?? "稿件不可用"}
        </p>
        <p>
          <Link to={`/projects/${projectId}`}>返回工作区</Link>
        </p>
      </main>
    );
  }

  const aiLabeledComments = comments.filter((c) => c.ai_action === "rewrite");
  const cursorParagraphId =
    aiLabeledComments[aiLabeledComments.length - 1]?.paragraph_id ?? null;

  const layoutClass = forceMobile ? styles.mobile : styles.desktop;

  return (
    <main className={styles.page}>
      <nav className={styles.breadcrumb}>
        <Link to={`/projects/${projectId}`}>← 工作区</Link>
      </nav>

      <header className={styles.header}>
        <h1 className={styles.title}>初稿审阅</h1>
        <p className={styles.subtitle}>通过批注触发定向改写，正文不可直接编辑。</p>
      </header>

      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : null}

      {acceptedNotice ? (
        <p className={styles.acceptedNotice} role="status">
          新版本已采纳
        </p>
      ) : null}

      {conflictNewer ? (
        <section className={styles.conflict} aria-label="存在新版本">
          <h2 className={styles.conflictTitle}>存在新版本</h2>
          <p>服务侧检测到一份新的稿件版本；下方并排展示两个版本，请基于新版本继续审阅。</p>
          <div className={styles.conflictBody}>
            <div className={styles.conflictCol}>
              <h3>当前版本</h3>
              {draft.paragraphs.map((p) => (
                <p key={p.id} className={styles.conflictText}>
                  {p.text}
                </p>
              ))}
            </div>
            <div className={styles.conflictCol}>
              <h3>新版本</h3>
              {conflictNewer.paragraphs.map((p) => (
                <p key={p.id} className={styles.conflictText}>
                  {p.text}
                </p>
              ))}
            </div>
          </div>
        </section>
      ) : null}

      <div
        className={`${styles.layout} ${layoutClass}`}
        data-testid="draft-layout"
        data-tab={activeTab}
      >
        <section
          className={styles.roadmap}
          aria-label="路线图"
          data-pane="roadmap"
        >
          <h2 className={styles.paneHeading}>路线图</h2>
          {plan ? (
            <ol className={styles.beats}>
              {plan.beats.map((beat) => {
                const isActive = cursorParagraphId === beat.id;
                return (
                  <li
                    key={beat.id}
                    className={isActive ? styles.beatActive : styles.beat}
                    aria-current={isActive ? "true" : undefined}
                  >
                    <span className={styles.beatId}>{beat.id}</span>
                    <span className={styles.beatPurpose}>{beat.purpose}</span>
                    <p className={styles.beatNewInfo}>{beat.new_information}</p>
                  </li>
                );
              })}
            </ol>
          ) : (
            <p className={styles.info}>暂无结构</p>
          )}
        </section>

        <section className={styles.text} aria-label="正文" data-pane="text">
          {diffCandidate ? (
            <RevisionDiff
              current={draft}
              candidate={diffCandidate}
              onAccept={() => {
                void handleAcceptDiff();
              }}
              onDiscard={handleDiscardDiff}
            />
          ) : (
            <ParagraphEditor
              paragraphs={draft.paragraphs}
              onAddComment={handleAddComment}
            />
          )}
        </section>

        <section
          className={styles.comments}
          aria-label="批注"
          data-pane="comments"
        >
          <CommentPanel
            comments={comments}
            paragraphs={paragraphs.map((p) => ({ id: p.id }))}
            onPreviewRewrite={handlePreviewRewrite}
            onReopenConfirm={handleStartReopen}
          />
        </section>
      </div>

      {forceMobile ? (
        <nav className={styles.tabBar} role="tablist" aria-label="稿件视图">
          {(["roadmap", "text", "comments"] as MobileTab[]).map((tab) => (
            <button
              key={tab}
              type="button"
              role="tab"
              aria-selected={activeTab === tab}
              className={activeTab === tab ? styles.tabActive : styles.tab}
              onClick={() => setActiveTab(tab)}
            >
              {tab === "roadmap" ? "路线图" : tab === "text" ? "正文" : "批注"}
            </button>
          ))}
        </nav>
      ) : null}

      {reopenPending && reopenInvalidates ? (
        <div className={styles.modalBackdrop} role="dialog" aria-modal="true">
          <div className={styles.modal}>
            <h2 className={styles.modalTitle}>确认改回上游</h2>
            <p>改回上游将丢弃以下阶段的产物：</p>
            <ul className={styles.modalList}>
              {reopenInvalidates.map((kind) => (
                <li key={kind}>{kind}</li>
              ))}
            </ul>
            <div className={styles.modalActions}>
              <button
                type="button"
                className={styles.primary}
                onClick={handleConfirmReopen}
                disabled={busy}
              >
                确认改回上游
              </button>
              <button type="button" onClick={handleDismissReopen}>
                取消
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </main>
  );
}