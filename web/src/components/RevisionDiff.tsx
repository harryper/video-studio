/**
 * RevisionDiff — paragraph-by-paragraph diff between the current draft
 * and the candidate rewrite (spec §10.4).
 *
 * Three kinds of changes:
 *   * ``modified`` — paragraph exists in both, text differs
 *   * ``removed``  — paragraph only in current
 *   * ``added``    — paragraph only in candidate
 *
 * Each modified paragraph renders side-by-side old/new with a
 * "保留修改 / 拒收修改" toggle. The bottom "采用此版本" button adopts
 * the candidate as the new head — note that the candidate already
 * exists as a draft artifact because triggerRewrite created it; the
 * ``onAccept`` callback only acknowledges the preview (see
 * DraftReviewPage.handleAcceptDiff). It does NOT call approveDraft —
 * that endpoint produces a separate ``approved_script`` artifact per
 * spec §10.6, which is exposed via the dedicated "定稿本版本" button
 * elsewhere in the workspace. If any paragraph is rejected the button
 * stays disabled; the brief does not expose partial-accept semantics
 * (the UI surfaces a banner asking the user to re-issue with revised
 * comments instead).
 */

import { useMemo, useState } from "react";

import type { DraftParagraph, DraftRevision } from "../api/types";

import styles from "./RevisionDiff.module.css";

export interface RevisionDiffProps {
  current: DraftRevision;
  candidate: DraftRevision;
  onAccept: () => void;
  onDiscard: () => void;
}

interface DiffEntry {
  id: string;
  kind: "modified" | "removed" | "added";
  before?: string;
  after?: string;
}

function diff(
  current: DraftRevision,
  candidate: DraftRevision,
): DiffEntry[] {
  const map = new Map<string, { before?: string; after?: string }>();
  for (const para of current.paragraphs) {
    map.set(para.id, { before: para.text });
  }
  for (const para of candidate.paragraphs) {
    const existing = map.get(para.id) ?? {};
    existing.after = para.text;
    map.set(para.id, existing);
  }
  const entries: DiffEntry[] = [];
  for (const [id, entry] of map) {
    const before = entry.before;
    const after = entry.after;
    if (before !== undefined && after === undefined) {
      entries.push({ id, kind: "removed", before });
    } else if (before === undefined && after !== undefined) {
      entries.push({ id, kind: "added", after });
    } else if (before !== undefined && after !== undefined) {
      if (before === after) continue;
      entries.push({ id, kind: "modified", before, after });
    }
  }
  return entries;
}

export function RevisionDiff({
  current,
  candidate,
  onAccept,
  onDiscard,
}: RevisionDiffProps): React.ReactElement {
  const entries = useMemo(() => diff(current, candidate), [current, candidate]);
  const [accepted, setAccepted] = useState<Record<string, boolean>>(
    () => Object.fromEntries(entries.map((e) => [e.id, true])),
  );

  const anyRejected = Object.values(accepted).some((v) => !v);

  return (
    <section className={styles.panel} aria-label="改写预览">
      <h2 className={styles.heading}>改写预览</h2>
      <p className={styles.summary}>
        候选版本与当前版本相比有 {entries.length} 处差异
      </p>
      {entries.length === 0 ? (
        <p className={styles.empty}>无可比较的段落差异</p>
      ) : (
        <ul className={styles.list}>
          {entries.map((entry) => {
            const acceptedFlag = accepted[entry.id] ?? false;
            return (
              <li key={entry.id} className={styles.entry}>
                <header className={styles.entryHeader}>
                  <span className={styles.paragraphId}>{entry.id}</span>
                  <span
                    className={
                      entry.kind === "modified"
                        ? styles.badgeModified
                        : entry.kind === "added"
                          ? styles.badgeAdded
                          : styles.badgeRemoved
                    }
                  >
                    {entry.kind === "modified"
                      ? "改写"
                      : entry.kind === "added"
                        ? "新增"
                        : "删除"}
                  </span>
                </header>
                <div className={styles.body}>
                  {entry.before !== undefined ? (
                    <div className={styles.col}>
                      <span className={styles.colLabel}>原版</span>
                      <p className={styles.before}>{entry.before}</p>
                    </div>
                  ) : null}
                  {entry.after !== undefined ? (
                    <div className={styles.col}>
                      <span className={styles.colLabel}>候选</span>
                      <p className={styles.after}>{entry.after}</p>
                    </div>
                  ) : null}
                </div>
                {entry.kind !== "removed" ? (
                  <button
                    type="button"
                    className={acceptedFlag ? styles.acceptToggleOn : styles.acceptToggleOff}
                    onClick={() =>
                      setAccepted((prev) => ({ ...prev, [entry.id]: !prev[entry.id] }))
                    }
                    aria-pressed={acceptedFlag}
                  >
                    {acceptedFlag ? "✓ 保留修改" : "拒收修改"}
                  </button>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}

      <div className={styles.actions}>
        <button
          type="button"
          className={styles.accept}
          onClick={onAccept}
          disabled={anyRejected || entries.length === 0}
        >
          采用此版本
        </button>
        <button type="button" className={styles.discard} onClick={onDiscard}>
          丢弃改写
        </button>
      </div>
      {anyRejected ? (
        <p className={styles.banner}>
          如需拒收部分修改，请通过批注重新提交
        </p>
      ) : null}
    </section>
  );
}

export function sameParagraphIds(
  current: DraftRevision,
  candidate: DraftRevision,
): boolean {
  const a = new Set(current.paragraphs.map((p) => p.id));
  const b = new Set(candidate.paragraphs.map((p) => p.id));
  if (a.size !== b.size) return false;
  for (const id of a) if (!b.has(id)) return false;
  return true;
}

export function applyAccepted(
  current: DraftRevision,
  candidate: DraftRevision,
  accepted: Record<string, boolean>,
): DraftRevision {
  const map = new Map<string, DraftParagraph>();
  for (const para of current.paragraphs) map.set(para.id, para);
  for (const para of candidate.paragraphs) {
    if (accepted[para.id] !== false) {
      map.set(para.id, para);
    }
  }
  return {
    ...candidate,
    paragraphs: Array.from(map.values()),
  };
}