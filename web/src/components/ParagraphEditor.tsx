/**
 * ParagraphEditor — read-only paragraph renderer with selection-aware
 * comment insertion (spec §10.4).
 *
 * Each paragraph carries a stable id (``DraftParagraph.id``). The editor
 * does not mutate the draft text directly; the only way to change a
 * paragraph is via the rewrite flow. The "添加批注" affordance is a
 * hover/focus button on each paragraph; clicking it opens the comment
 * form for that paragraph, seeded with the current text selection's
 * Unicode code-point offsets.
 *
 * Offsets are computed with :func:`selectionOffsetsWithin` so emoji and
 * surrogate pairs don't blow up against the backend validator
 * (``validate_offsets`` measures by ``Array.from(text).length``).
 */

import { useCallback, useState } from "react";

import type { CreateCommentInput, DraftParagraph } from "../api/types";

import styles from "./ParagraphEditor.module.css";

export interface ParagraphEditorProps {
  paragraphs: DraftParagraph[];
  onAddComment: (paragraphId: string, draft: CreateCommentInput) => void;
}

export function selectionOffsetsWithin(
  target: HTMLElement,
): { start: number; end: number } | null {
  const selection = typeof window === "undefined" ? null : window.getSelection();
  if (!selection || selection.rangeCount === 0) return null;
  const range = selection.getRangeAt(0);
  if (!target.contains(range.startContainer) || !target.contains(range.endContainer)) {
    return null;
  }
  if (range.collapsed) return null;

  const start = offsetWithin(target, range.startContainer, range.startOffset);
  const end = offsetWithin(target, range.endContainer, range.endOffset);
  if (start === null || end === null) return null;
  return start <= end
    ? { start, end }
    : { start: end, end: start };
}

function offsetWithin(
  root: HTMLElement,
  node: Node,
  nodeOffset: number,
): number | null {
  // The paragraph carries a single text node as its first child followed by
  // an "add comment" button — offsets are measured into the paragraph text
  // only, never into the button's own text. Walk the direct children of
  // the root in document order and accumulate Unicode code-point counts
  // (Array.from(text).length) rather than UTF-16 code units so emoji and
  // surrogate pairs round-trip cleanly to ``validate_offsets``.
  let total = 0;
  for (let i = 0; i < root.childNodes.length; i++) {
    const child = root.childNodes[i];
    if (child.nodeType === Node.TEXT_NODE) {
      if (child === node) {
        const text = (child.textContent ?? "").slice(0, nodeOffset);
        return total + Array.from(text).length;
      }
      total += Array.from(child.textContent ?? "").length;
    }
  }
  // Element-node anchor (e.g. ``range.selectNodeContents(root)``): the
  // nodeOffset is a child index, not a character count — convert it by
  // summing the text-node descendants up to that index.
  if (node === root && node.nodeType === Node.ELEMENT_NODE) {
    let childTotal = 0;
    for (let i = 0; i < nodeOffset && i < root.childNodes.length; i++) {
      const child = root.childNodes[i];
      if (child.nodeType === Node.TEXT_NODE) {
        childTotal += Array.from(child.textContent ?? "").length;
      }
    }
    return childTotal;
  }
  return null;
}

export function ParagraphEditor({
  paragraphs,
  onAddComment,
}: ParagraphEditorProps): React.ReactElement {
  const [openParagraphId, setOpenParagraphId] = useState<string | null>(null);
  const [body, setBody] = useState<string>("");
  const [aiAction, setAiAction] = useState<"rewrite" | "note">("rewrite");
  const [offsetDraft, setOffsetDraft] = useState<{ start: number; end: number } | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const closeForm = useCallback((): void => {
    setOpenParagraphId(null);
    setBody("");
    setAiAction("rewrite");
    setOffsetDraft(null);
    setFormError(null);
  }, []);

  const handleOpen = useCallback(
    (
      paragraphId: string,
      capturedOffsets: { start: number; end: number } | null = null,
    ): void => {
      const para = paragraphs.find((p) => p.id === paragraphId);
      if (!para) return;
      // Prefer the offsets captured at mousedown time (when the user's
      // text selection is still alive); fall back to a live selection
      // read if the click handler invokes us.
      const offsets =
        capturedOffsets ?? (() => {
          const target = document.getElementById(paragraphId);
          return target ? selectionOffsetsWithin(target) : null;
        })();
      // Non-empty selection ⇒ pre-check the protected-span box; pre-fill
      // offsets. Empty selection ⇒ comment anchors to paragraph only.
      if (offsets && offsets.end > offsets.start) {
        setOffsetDraft(offsets);
        setAiAction("rewrite");
      } else {
        setOffsetDraft(null);
        setAiAction("note");
      }
      setOpenParagraphId(paragraphId);
      setBody("");
      setFormError(null);
    },
    [paragraphs],
  );

  const handleSubmit = (paragraphId: string): void => {
    if (!body.trim()) {
      setFormError("批注正文不能为空");
      return;
    }
    const start = offsetDraft?.start ?? 0;
    const end = offsetDraft?.end ?? 0;
    onAddComment(paragraphId, {
      paragraph_id: paragraphId,
      start_offset: start,
      end_offset: end,
      kind: aiAction === "rewrite" ? "rewrite" : "note",
      body: body.trim(),
      ai_action: aiAction,
    });
    closeForm();
  };

  return (
    <div className={styles.editor} aria-label="初稿正文">
      <p className={styles.hint}>正文只读 — 通过批注触发改写</p>
      {paragraphs.map((paragraph) => {
        const isOpen = openParagraphId === paragraph.id;
        return (
          <div key={paragraph.id} className={styles.paragraphBlock}>
            <p id={paragraph.id} className={styles.paragraph}>
              {paragraph.text}
              <button
                type="button"
                className={styles.addButton}
                aria-label="添加批注"
                onMouseDown={(e) => {
                  // Capture the current selection before focus shifts to
                  // the button (which would clear the user's text
                  // selection). mousedown fires before click / focus, so
                  // we can read window.getSelection() synchronously here.
                  const para = document.getElementById(paragraph.id);
                  if (!para) return;
                  e.preventDefault();
                  const offsets = selectionOffsetsWithin(para);
                  handleOpen(paragraph.id, offsets);
                }}
              >
                + 批注
              </button>
            </p>
            {isOpen ? (
              <form
                className={styles.commentForm}
                onSubmit={(e) => {
                  e.preventDefault();
                  handleSubmit(paragraph.id);
                }}
              >
                <label className={styles.field}>
                  <span>批注正文</span>
                  <textarea
                    value={body}
                    onChange={(e) => setBody(e.target.value)}
                    aria-label="批注正文"
                    rows={3}
                  />
                </label>
                <label className={styles.checkbox}>
                  <input
                    type="checkbox"
                    checked={aiAction === "rewrite"}
                    onChange={(e) => setAiAction(e.target.checked ? "rewrite" : "note")}
                  />
                  <span>交给 AI（取消勾选则只作为备注）</span>
                </label>
                {offsetDraft ? (
                  <p className={styles.range}>
                    选区：Unicode 码点 {offsetDraft.start} – {offsetDraft.end}
                  </p>
                ) : (
                  <p className={styles.range}>未选中文字 — 批注将附加到整段</p>
                )}
                {formError ? (
                  <p className={styles.formError} role="alert">
                    {formError}
                  </p>
                ) : null}
                <div className={styles.formActions}>
                  <button type="submit" className={styles.primary}>
                    提交批注
                  </button>
                  <button type="button" onClick={closeForm}>
                    取消
                  </button>
                </div>
              </form>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}