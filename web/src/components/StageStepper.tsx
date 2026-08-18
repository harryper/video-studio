/**
 * Horizontal pipeline stepper showing the 8 editorial stages.
 *
 * The current stage is highlighted; stages whose artifact head has been
 * accepted are marked completed with a check. Stages in the future are
 * rendered but muted.
 */

import type { StageName } from "../api/types";

import styles from "./StageStepper.module.css";

export const STAGES: StageName[] = [
  "diagnosis",
  "research",
  "pitches",
  "narrative",
  "draft",
  "rewrite",
  "speech",
  "approval",
];

export interface StageStepperProps {
  currentStage: string | null;
  completedStages: ReadonlySet<string>;
}

const STAGE_LABELS: Record<StageName, string> = {
  diagnosis: "主题诊断",
  research: "调研",
  pitches: "切口",
  narrative: "结构",
  draft: "初稿",
  rewrite: "改写",
  speech: "配音",
  approval: "终审",
};

function stageIndex(name: string): number {
  const idx = STAGES.indexOf(name as StageName);
  return idx === -1 ? STAGES.length : idx;
}

export function StageStepper({
  currentStage,
  completedStages,
}: StageStepperProps): React.ReactElement {
  const currentIdx = currentStage === null ? -1 : stageIndex(currentStage);
  return (
    <ol className={styles.list} aria-label="内容流水线阶段">
      {STAGES.map((stage, idx) => {
        const isCompleted = completedStages.has(stage);
        const isCurrent = idx === currentIdx;
        const stateClass = isCompleted
          ? styles.completed
          : isCurrent
            ? styles.current
            : styles.upcoming;
        return (
          <li
            key={stage}
            className={`${styles.item} ${stateClass}`}
            aria-current={isCurrent ? "step" : undefined}
            data-stage={stage}
            data-state={isCompleted ? "completed" : isCurrent ? "current" : "upcoming"}
          >
            <span className={styles.marker} aria-hidden="true">
              {isCompleted ? "✓" : idx + 1}
            </span>
            <span className={styles.label}>{STAGE_LABELS[stage]}</span>
          </li>
        );
      })}
    </ol>
  );
}