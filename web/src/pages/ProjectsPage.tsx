/**
 * Projects dashboard route at ``/``.
 *
 * Lists every project with its editorial status label, exposes stage and
 * status filters, and provides a minimal new-project form (title + topic
 * both required by the API). Each row is a link to the per-project
 * workspace so the browser back / forward stack keeps working.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { createProject, listProjects } from "../api/client";
import type {
  ApiError,
  CreateProjectInput,
  ProjectListFilters,
  ProjectSummary,
  StageName,
  StageStatus,
} from "../api/types";
import { stageLabel } from "../labels";
import { Link } from "../router";

import styles from "./ProjectsPage.module.css";

const STAGE_SELECT: StageName[] = [
  "diagnosis",
  "research",
  "pitches",
  "narrative",
  "draft",
  "rewrite",
  "speech",
  "approval",
];

const STATUS_SELECT: StageStatus[] = [
  "queued",
  "running",
  "finished",
  "failed",
  "cancelled",
];

const STAGE_LABEL_FOR_SELECT: Record<StageName, string> = {
  diagnosis: "主题诊断",
  research: "调研",
  pitches: "切口",
  narrative: "结构",
  draft: "初稿",
  rewrite: "改写",
  speech: "配音",
  approval: "终审",
};

const STATUS_LABEL_FOR_SELECT: Record<StageStatus, string> = {
  queued: "排队",
  running: "进行中",
  finished: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

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

export function ProjectsPage(): React.ReactElement {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [stage, setStage] = useState<StageName | "">("");
  const [status, setStatus] = useState<StageStatus | "">("");
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [title, setTitle] = useState<string>("");
  const [topic, setTopic] = useState<string>("");
  const [creating, setCreating] = useState<boolean>(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const filters = useMemo<ProjectListFilters>(
    () => ({
      stage: stage === "" ? null : stage,
      status: status === "" ? null : status,
    }),
    [stage, status],
  );

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const data = await listProjects(filters);
      setProjects(data);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onSubmit = async (event: React.FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (creating) return;
    const trimmedTitle = title.trim();
    const trimmedTopic = topic.trim();
    if (!trimmedTitle || !trimmedTopic) {
      setCreateError("标题和主题均不能为空");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const input: CreateProjectInput = {
        title: trimmedTitle,
        topic: trimmedTopic,
      };
      const created = await createProject(input);
      setProjects((prev) => [
        {
          id: created.id,
          title: created.title,
          topic: created.topic,
          created_at: created.created_at,
          updated_at: created.updated_at,
          latest_stage: "diagnosis",
          latest_job_status: "queued",
        },
        ...prev,
      ]);
      setTitle("");
      setTopic("");
    } catch (err) {
      setCreateError(errorMessage(err));
    } finally {
      setCreating(false);
    }
  };

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>项目仪表盘</h1>
        <p className={styles.subtitle}>查看所有内容项目并启动新主题</p>
      </header>

      <form className={styles.form} onSubmit={onSubmit} aria-label="创建项目">
        <label className={styles.field}>
          <span>标题</span>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
            maxLength={120}
          />
        </label>
        <label className={styles.field}>
          <span>主题</span>
          <input
            type="text"
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            required
            maxLength={240}
          />
        </label>
        <button type="submit" className={styles.submit} disabled={creating}>
          {creating ? "创建中…" : "创建"}
        </button>
        {createError ? (
          <p className={styles.error} role="alert">
            {createError}
          </p>
        ) : null}
      </form>

      <section className={styles.filters} aria-label="筛选项目">
        <label className={styles.filterField}>
          <span>阶段</span>
          <select
            value={stage}
            onChange={(e) => setStage(e.target.value as StageName | "")}
          >
            <option value="">全部阶段</option>
            {STAGE_SELECT.map((value) => (
              <option key={value} value={value}>
                {STAGE_LABEL_FOR_SELECT[value]}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.filterField}>
          <span>状态</span>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value as StageStatus | "")}
          >
            <option value="">全部状态</option>
            {STATUS_SELECT.map((value) => (
              <option key={value} value={value}>
                {STATUS_LABEL_FOR_SELECT[value]}
              </option>
            ))}
          </select>
        </label>
      </section>

      {loading ? <p className={styles.info}>加载中…</p> : null}
      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : null}

      {!loading && !error && projects.length === 0 ? (
        <p className={styles.info}>暂无项目</p>
      ) : null}

      <ul className={styles.list}>
        {projects.map((project) => (
          <li key={project.id} className={styles.item}>
            <Link
              to={`/projects/${project.id}`}
              className={styles.itemLink}
            >
              <span className={styles.itemTitle}>{project.title}</span>
              <span className={styles.itemTopic}>{project.topic}</span>
              <span className={styles.itemStatus}>
                {stageLabel(project.latest_stage, project.latest_job_status)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </main>
  );
}