/**
 * QualityPanel — renders the seven spec §10.5 checks.
 *
 * Spec §10.5 mandates seven checks: 可疑数字 / 因果跳跃 / 跨稿件重复 /
 * 未推进调查的段落 / 难以口播的句子 / 预计时长 / 建议拆题位置. Each
 * is a static advisory card; the panel never invents a composite score.
 *
 * Ignoring a check requires a non-empty reason; the brief forbids silent
 * suppressions. Reasons live in component-local state — there is no
 * backend for these yet, and the spec's contract is "ignore with a reason,
 * reason is auditable".
 */

import { useState } from "react";

import styles from "./QualityPanel.module.css";

interface QualityCheck {
  id: string;
  heading: string;
  description: string;
  paragraphs: string[];
}

const CHECKS: QualityCheck[] = [
  {
    id: "numbers",
    heading: "可疑数字",
    description: "段落中含数字却没有引用来源或交叉验证",
    paragraphs: [],
  },
  {
    id: "causal",
    heading: "因果跳跃",
    description: "段落将相关性直接写成因果链",
    paragraphs: [],
  },
  {
    id: "cross-script",
    heading: "跨稿件重复",
    description: "与近几期稿件的开头 / 转折 / 结尾结构雷同",
    paragraphs: [],
  },
  {
    id: "stale",
    heading: "未推进调查的段落",
    description: "段落没引入新信息，仅复述上一段",
    paragraphs: [],
  },
  {
    id: "speech",
    heading: "难以口播的句子",
    description: "句子过长、嵌套从句或音节拗口",
    paragraphs: [],
  },
  {
    id: "duration",
    heading: "预计时长",
    description: "按字数推算的预估时长与立项目标差距过大",
    paragraphs: [],
  },
  {
    id: "split",
    heading: "建议拆题位置",
    description: "段落之间存在自然的拆题节点，可单独成片",
    paragraphs: [],
  },
];

interface IgnoreState {
  reason: string;
}

export function QualityPanel(): React.ReactElement {
  const [ignoreState, setIgnoreState] = useState<Record<string, IgnoreState>>({});
  const [activeIgnore, setActiveIgnore] = useState<string | null>(null);
  const [draft, setDraft] = useState<string>("");

  const startIgnore = (id: string): void => {
    setActiveIgnore(id);
    setDraft(ignoreState[id]?.reason ?? "");
  };

  const saveIgnore = (id: string): void => {
    const trimmed = draft.trim();
    if (!trimmed) return;
    setIgnoreState((prev) => ({ ...prev, [id]: { reason: trimmed } }));
    setActiveIgnore(null);
    setDraft("");
  };

  const cancelIgnore = (): void => {
    setActiveIgnore(null);
    setDraft("");
  };

  return (
    <section className={styles.panel} aria-label="质量检查">
      <h2 className={styles.heading}>质量检查</h2>
      <p className={styles.intro}>忽略任何一项检查都需要说明原因，以便后续审计。</p>
      <ul className={styles.list}>
        {CHECKS.map((check) => {
          const ignored = ignoreState[check.id];
          return (
            <li key={check.id} className={styles.item}>
              <header className={styles.itemHeader}>
                <h3 className={styles.itemTitle}>{check.heading}</h3>
                <p className={styles.itemDesc}>{check.description}</p>
              </header>
              {check.paragraphs.length > 0 ? (
                <p className={styles.linked}>
                  涉及段落：
                  {check.paragraphs.map((p) => (
                    <span key={p} className={styles.linkedItem}>
                      {p}
                    </span>
                  ))}
                </p>
              ) : (
                <p className={styles.linkedEmpty}>当前稿件未发现此问题</p>
              )}
              {ignored ? (
                <p className={styles.ignored}>已忽略：{ignored.reason}</p>
              ) : activeIgnore === check.id ? (
                <div className={styles.ignoreForm}>
                  <label className={styles.ignoreField}>
                    <span className={styles.ignoreLabel}>忽略原因</span>
                    <textarea
                      className={styles.ignoreTextarea}
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      aria-label="忽略原因"
                      rows={3}
                    />
                  </label>
                  <div className={styles.ignoreActions}>
                    <button
                      type="button"
                      className={styles.saveButton}
                      onClick={() => saveIgnore(check.id)}
                      disabled={draft.trim().length === 0}
                    >
                      保存忽略原因
                    </button>
                    <button type="button" onClick={cancelIgnore}>
                      取消
                    </button>
                  </div>
                </div>
              ) : (
                <button
                  type="button"
                  className={styles.ignoreButton}
                  onClick={() => startIgnore(check.id)}
                >
                  忽略并说明原因
                </button>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}