/**
 * PitchCard — one of three pitch options.
 *
 * Renders the pitch's investigation_question, opening_scene, evidence_path,
 * payoff, estimated_duration (as minutes), and risks. Editors can:
 *   * 选择 → hand the pitch back to the parent (no edits)
 *   * 编辑并选择 → toggle an inline editor; on save the parent receives
 *     the edited pitch
 *   * 重新生成此卡片 → disabled placeholder (Task 14)
 *   * 全部换方向 → handled by the parent page (lives outside the card)
 *
 * The card itself does not own the action wiring; the parent passes a
 * callback so this component stays dumb and testable.
 */

import { useState } from "react";

import type { StoryPitch } from "../api/types";

import styles from "./PitchCard.module.css";

export interface PitchCardProps {
  pitch: StoryPitch;
  onAccept: (pitch: StoryPitch) => void;
}

function minutes(durationSec: number): string {
  return `${Math.round(durationSec / 60)} 分钟`;
}

interface EditDraft {
  investigation_question: string;
  opening_scene: string;
  evidence_path: string;
  payoff: string;
}

export function PitchCard({ pitch, onAccept }: PitchCardProps): React.ReactElement {
  const [editing, setEditing] = useState<boolean>(false);
  const [draft, setDraft] = useState<EditDraft>({
    investigation_question: pitch.investigation_question,
    opening_scene: pitch.opening_scene,
    evidence_path: pitch.evidence_path,
    payoff: pitch.payoff,
  });

  const startEdit = (): void => {
    setEditing(true);
  };

  const cancelEdit = (): void => {
    setDraft({
      investigation_question: pitch.investigation_question,
      opening_scene: pitch.opening_scene,
      evidence_path: pitch.evidence_path,
      payoff: pitch.payoff,
    });
    setEditing(false);
  };

  const saveAndAccept = (): void => {
    const edited: StoryPitch = {
      ...pitch,
      investigation_question: draft.investigation_question,
      opening_scene: draft.opening_scene,
      evidence_path: draft.evidence_path,
      payoff: draft.payoff,
    };
    onAccept(edited);
    setEditing(false);
  };

  return (
    <article className={styles.card} aria-label={`切口 ${pitch.id}`}>
      {editing ? (
        <div className={styles.editForm}>
          <label className={styles.field}>
            <span>调查问题</span>
            <textarea
              value={draft.investigation_question}
              onChange={(e) =>
                setDraft({ ...draft, investigation_question: e.target.value })
              }
              rows={2}
            />
          </label>
          <label className={styles.field}>
            <span>开场场景</span>
            <textarea
              value={draft.opening_scene}
              onChange={(e) =>
                setDraft({ ...draft, opening_scene: e.target.value })
              }
              rows={2}
            />
          </label>
          <label className={styles.field}>
            <span>证据路线</span>
            <textarea
              value={draft.evidence_path}
              onChange={(e) =>
                setDraft({ ...draft, evidence_path: e.target.value })
              }
              rows={2}
            />
          </label>
          <label className={styles.field}>
            <span>回报</span>
            <textarea
              value={draft.payoff}
              onChange={(e) => setDraft({ ...draft, payoff: e.target.value })}
              rows={2}
            />
          </label>
          <div className={styles.actions}>
            <button type="button" className={styles.primary} onClick={saveAndAccept}>
              保存并选择
            </button>
            <button type="button" onClick={cancelEdit}>
              取消
            </button>
          </div>
        </div>
      ) : (
        <>
          <h3 className={styles.title}>{pitch.investigation_question}</h3>
          <dl className={styles.fields}>
            <dt>开场场景</dt>
            <dd>{pitch.opening_scene}</dd>
            <dt>证据路线</dt>
            <dd>{pitch.evidence_path}</dd>
            <dt>回报</dt>
            <dd>{pitch.payoff}</dd>
            <dt>预计时长</dt>
            <dd>{minutes(pitch.estimated_duration_sec)}</dd>
          </dl>
          {pitch.risks.length > 0 ? (
            <div className={styles.risks}>
              <span className={styles.risksLabel}>风险</span>
              <ul className={styles.risksList}>
                {pitch.risks.map((risk) => (
                  <li key={risk}>{risk}</li>
                ))}
              </ul>
            </div>
          ) : null}
          <div className={styles.actions}>
            <button
              type="button"
              className={styles.primary}
              onClick={() => onAccept(pitch)}
            >
              选择
            </button>
            <button type="button" onClick={startEdit}>
              编辑并选择
            </button>
            <button
              type="button"
              className={styles.disabled}
              disabled
              title="单卡重做将在 Task 14 提供"
            >
              重新生成此卡片（Task 14 提供）
            </button>
          </div>
        </>
      )}
    </article>
  );
}