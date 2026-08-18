/**
 * CommentPanel — list editorial comments grouped by paragraph, plus
 * the "preview rewrite" controls (spec §10.4 right column).
 *
 * The panel renders:
 *   * 每条批注 with a small action badge (rewrite → AI / note → 人类)
 *   * a paragraph-grouped list keyed off ``paragraph_id``
 *   * a "预览本轮修改" / "改写全部已选段" trigger that calls
 *     ``onPreviewRewrite(rewriteTargets, untouched)`` so the parent can
 *     render the RevisionDiff view.
 */

import { useMemo } from "react";

import type { EditorialComment } from "../api/types";
import { QualityPanel } from "./QualityPanel";

import styles from "./CommentPanel.module.css";

export interface CommentPanelProps {
  comments: EditorialComment[];
  paragraphs: { id: string }[];
  onPreviewRewrite: (
    targets: string[],
    untouched: string[],
  ) => void;
  onReopenConfirm: () => void;
}

function paragraphNumber(id: string): string {
  const match = id.match(/(\d+)$/);
  return match ? match[1] : id;
}

function joinParagraphs(ids: string[]): string {
  if (ids.length === 0) return "";
  const numbers = ids.map(paragraphNumber);
  return `段落 ${numbers.join("、")}`;
}

export function CommentPanel({
  comments,
  paragraphs,
  onPreviewRewrite,
  onReopenConfirm,
}: CommentPanelProps): React.ReactElement {
  const grouped = useMemo(() => {
    const map = new Map<string, EditorialComment[]>();
    for (const comment of comments) {
      const list = map.get(comment.paragraph_id) ?? [];
      list.push(comment);
      map.set(comment.paragraph_id, list);
    }
    return map;
  }, [comments]);

  const rewriteParagraphs = useMemo(() => {
    const set = new Set<string>();
    for (const comment of comments) {
      if (comment.ai_action === "rewrite") set.add(comment.paragraph_id);
    }
    return set;
  }, [comments]);

  const allParagraphIds = useMemo(
    () => paragraphs.map((p) => p.id),
    [paragraphs],
  );

  const targets = useMemo(
    () => allParagraphIds.filter((id) => rewriteParagraphs.has(id)),
    [allParagraphIds, rewriteParagraphs],
  );
  const untouched = useMemo(
    () => allParagraphIds.filter((id) => !rewriteParagraphs.has(id)),
    [allParagraphIds, rewriteParagraphs],
  );

  const targetsLabel = targets.length > 0
    ? `将修改：${joinParagraphs(targets)}`
    : "将修改：暂无";
  const untouchedLabel =
    untouched.length > 0
      ? `不会修改：${joinParagraphs(untouched)}`
      : "不会修改：全部段落都会被改写";

  const triggerPreview = (): void => {
    onPreviewRewrite(targets, untouched);
  };

  return (
    <aside className={styles.panel} aria-label="批注与检查">
      <section>
        <h2 className={styles.heading}>批注</h2>
        {comments.length === 0 ? (
          <p className={styles.empty}>尚无批注</p>
        ) : (
          <ul className={styles.list}>
            {Array.from(grouped.entries()).map(([paragraphId, items]) => (
              <li key={paragraphId} className={styles.group}>
                <h3 className={styles.groupTitle}>
                  {`段落 ${paragraphNumber(paragraphId)}`}
                </h3>
                <ul className={styles.commentList}>
                  {items.map((comment) => (
                    <li
                      key={comment.id}
                      className={styles.comment}
                      data-ai-action={comment.ai_action}
                      aria-label={
                        comment.ai_action === "rewrite"
                          ? `交给 AI：${comment.body}`
                          : `交给人类：${comment.body}`
                      }
                    >
                      <span
                        className={
                          comment.ai_action === "rewrite"
                            ? styles.badgeRewrite
                            : styles.badgeNote
                        }
                      >
                        {comment.ai_action === "rewrite" ? "→ AI" : "→ 人类"}
                      </span>
                      <span className={styles.commentBody}>{comment.body}</span>
                      <span className={styles.commentMeta}>
                        {comment.start_offset === 0 && comment.end_offset === 0
                          ? "整段"
                          : `选区 ${comment.start_offset}-${comment.end_offset}`}
                      </span>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h2 className={styles.heading}>本轮改写预览</h2>
        <p className={styles.preview}>{targetsLabel}</p>
        <p className={styles.preview}>{untouchedLabel}</p>
        <button
          type="button"
          className={styles.previewButton}
          onClick={triggerPreview}
        >
          预览本轮修改
        </button>
      </section>

      <section>
        <h2 className={styles.heading}>上游回退</h2>
        <p className={styles.hint}>如需回到结构 / 切口阶段，先丢弃下游产物。</p>
        <button type="button" className={styles.reopen} onClick={onReopenConfirm}>
          改回上游
        </button>
      </section>

      <QualityPanel />
    </aside>
  );
}