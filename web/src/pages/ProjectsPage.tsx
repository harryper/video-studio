/**
 * Projects dashboard route at ``/``.
 *
 * Lists every project bucketed by editorial status per spec §10.1:
 * 草稿 / 等待选切口 / 等待审稿 / 已定稿 / 制作中 / 已完成.
 *
 * The new-project form defaults to a single required topic field;
 * the optional title lives behind a collapsed advanced section so the
 * dashboard is not a wall of inputs. The API still requires both
 * fields, so when the title is left blank we derive one from the
 * topic (``topic.slice(0, 60)``) on submit.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { createProject, listProjects } from "../api/client";
import type { CreateProjectInput, ProjectSummary } from "../api/types";
import {
  BUCKET_LABEL,
  BUCKET_ORDER,
  categorizeProject,
  type BucketName,
} from "../categorize";
import { errorMessage } from "../components/errorMessage";
import { stageLabel } from "../labels";
import { Link } from "../router";

import styles from "./ProjectsPage.module.css";

const TITLE_LIMIT = 60;

function deriveTitle(topic: string): string {
  const trimmed = topic.trim();
  if (!trimmed) return "";
  return trimmed.slice(0, TITLE_LIMIT);
}

export function ProjectsPage(): React.ReactElement {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [topic, setTopic] = useState<string>("");
  const [title, setTitle] = useState<string>("");
  const [creating, setCreating] = useState<boolean>(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const data = await listProjects();
      setProjects(data);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const buckets = useMemo<Map<BucketName, ProjectSummary[]>>(() => {
    const grouped = new Map<BucketName, ProjectSummary[]>();
    for (const name of BUCKET_ORDER) {
      grouped.set(name, []);
    }
    for (const project of projects) {
      const name = categorizeProject(project);
      grouped.get(name)?.push(project);
    }
    return grouped;
  }, [projects]);

  const onSubmit = async (
    event: React.FormEvent<HTMLFormElement>,
  ): Promise<void> => {
    event.preventDefault();
    if (creating) return;
    const trimmedTopic = topic.trim();
    if (!trimmedTopic) {
      setCreateError("主题不能为空");
      return;
    }
    const finalTitle = title.trim() || deriveTitle(trimmedTopic);
    setCreating(true);
    setCreateError(null);
    try {
      const input: CreateProjectInput = {
        title: finalTitle,
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
      setTopic("");
      setTitle("");
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
          <span>主题</span>
          <input
            type="text"
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            required
            maxLength={240}
            data-testid="new-project-topic"
          />
        </label>
        <details className={styles.advanced}>
          <summary className={styles.advancedSummary}>高级选项</summary>
          <label className={styles.field}>
            <span>标题</span>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              maxLength={120}
            />
          </label>
        </details>
        <button type="submit" className={styles.submit} disabled={creating}>
          {creating ? "创建中…" : "创建"}
        </button>
        {createError ? (
          <p className={styles.error} role="alert">
            {createError}
          </p>
        ) : null}
      </form>

      {loading ? <p className={styles.info}>加载中…</p> : null}
      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : null}

      {!loading && !error && projects.length === 0 ? (
        <p className={styles.info}>暂无项目</p>
      ) : null}

      {BUCKET_ORDER.map((bucket) => {
        const items = buckets.get(bucket) ?? [];
        return (
          <section
            key={bucket}
            className={styles.bucket}
            aria-label={`${BUCKET_LABEL[bucket]}项目`}
            data-testid={`bucket-${bucket}`}
          >
            <h2 className={styles.bucketTitle}>{BUCKET_LABEL[bucket]}</h2>
            {items.length === 0 ? (
              <p className={styles.bucketEmpty}>暂无</p>
            ) : (
              <ul className={styles.bucketList}>
                {items.map((project) => (
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
            )}
          </section>
        );
      })}
    </main>
  );
}