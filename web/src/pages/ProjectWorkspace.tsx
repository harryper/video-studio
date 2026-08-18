/**
 * Project workspace route at ``/projects/:id``.
 *
 * Renders the project header, pipeline stepper, live SSE progress panel,
 * and artifact history. Editorial review cards (pitches / draft /
 * comments) are owned by Task 12.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  cancelJob,
  getProject,
  listArtifacts,
  retryJob,
} from "../api/client";
import type {
  ApiError,
  ArtifactHistoryEntry,
  ProjectSummary,
} from "../api/types";
import { JobProgress } from "../components/JobProgress";
import { StageStepper } from "../components/StageStepper";
import { stageLabel } from "../labels";
import { Link } from "../router";

import styles from "./ProjectWorkspace.module.css";

function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (err && typeof err === "object" && "body" in err) {
    const apiErr = err as ApiError;
    if (apiErr.body && typeof apiErr.body.message === "string") {
      return apiErr.body.message;
    }
  }
  return "操作失败";
}

const ARTIFACT_KIND_LABEL: Record<string, string> = {
  diagnosis: "主题诊断",
  research: "调研",
  pitches: "切口集",
  accepted_pitch: "选中切口",
  narrative: "结构",
  draft: "初稿",
  rewrite: "改写",
  speech_plan: "配音脚本",
  approved_script: "终审脚本",
};

export interface ProjectWorkspaceProps {
  projectId: string;
}

export function ProjectWorkspace({
  projectId,
}: ProjectWorkspaceProps): React.ReactElement {
  const [project, setProject] = useState<ProjectSummary | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactHistoryEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<boolean>(false);

  const refresh = useCallback(async (): Promise<void> => {
    setError(null);
    try {
      const [next, history] = await Promise.all([
        getProject(projectId),
        listArtifacts(projectId),
      ]);
      setProject(next);
      setArtifacts(history);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const completedStages = useMemo<Set<string>>(() => {
    const set = new Set<string>();
    for (const entry of artifacts) {
      if (entry.is_head) set.add(entry.kind);
    }
    return set;
  }, [artifacts]);

  const onCancel = useCallback(
    async (jobId: string): Promise<void> => {
      setBusy(true);
      try {
        await cancelJob(projectId, jobId);
        await refresh();
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [projectId, refresh],
  );

  const onRetry = useCallback(
    async (jobId: string): Promise<void> => {
      setBusy(true);
      try {
        await retryJob(projectId, jobId);
        await refresh();
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [projectId, refresh],
  );

  if (error && !project) {
    return (
      <main className={styles.page}>
        <p className={styles.error} role="alert">
          {error}
        </p>
        <p>
          <Link to="/">返回项目仪表盘</Link>
        </p>
      </main>
    );
  }

  if (!project) {
    return (
      <main className={styles.page}>
        <p className={styles.info}>加载项目…</p>
      </main>
    );
  }

  return (
    <main className={styles.page}>
      <nav className={styles.breadcrumb}>
        <Link to="/">← 项目仪表盘</Link>
      </nav>

      <header className={styles.header}>
        <h1 className={styles.title}>{project.title}</h1>
        <p className={styles.topic}>{project.topic}</p>
      </header>

      <section aria-label="流水线进度" className={styles.section}>
        <h2 className={styles.sectionTitle}>流水线进度</h2>
        <StageStepper
          currentStage={project.latest_stage}
          completedStages={completedStages}
        />
      </section>

      <section aria-label="实时任务进度" className={styles.section}>
        <h2 className={styles.sectionTitle}>实时任务进度</h2>
        <JobProgress
          projectId={project.id}
          onCancel={onCancel}
          onRetry={onRetry}
        />
      </section>

      <section aria-label="产物历史" className={styles.section}>
        <h2 className={styles.sectionTitle}>产物历史</h2>
        {artifacts.length === 0 ? (
          <p className={styles.info}>暂无产物</p>
        ) : (
          <ul className={styles.history}>
            {artifacts.map((artifact) => (
              <li
                key={artifact.id}
                className={artifact.is_head ? styles.historyHead : styles.historyItem}
                data-state={artifact.is_head ? "head" : "history"}
              >
                <span className={styles.kind}>
                  {ARTIFACT_KIND_LABEL[artifact.kind] ?? artifact.kind}
                </span>
                <span className={styles.revision}>第 {artifact.revision} 版</span>
                <span className={styles.timestamp}>
                  {new Date(artifact.created_at).toLocaleString()}
                </span>
                {artifact.accepted_at ? (
                  <span className={styles.accepted}>已采纳</span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="项目状态" className={styles.section}>
        <h2 className={styles.sectionTitle}>项目状态</h2>
        <p>
          当前阶段：
          <strong data-testid="workspace-stage-label">
            {stageLabel(project.latest_stage, project.latest_job_status)}
          </strong>
        </p>
        {busy ? <p className={styles.info}>任务操作中…</p> : null}
      </section>
    </main>
  );
}