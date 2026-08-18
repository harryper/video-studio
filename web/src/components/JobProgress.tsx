/**
 * Live progress panel for a single project.
 *
 * Subscribes to ``GET /api/projects/{id}/events`` and re-renders on every
 * progress event. Surfaces failure details (error_code + truncated
 * error_message) so the user can diagnose without leaving the page.
 *
 * Reconnects on demand — the SSE stream can drop for any number of reasons
 * (proxy timeout, backend restart, transient network blip). A reconnect
 * button keeps the user in control rather than silently retrying forever.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { openEventStream } from "../api/client";
import type { JobProgressEvent } from "../api/types";
import { stageLabel, truncateMessage } from "../labels";

import styles from "./JobProgress.module.css";

export interface JobProgressProps {
  projectId: string;
  onCancel?: (jobId: string) => void;
  onRetry?: (jobId: string) => void;
}

export function JobProgress({
  projectId,
  onCancel,
  onRetry,
}: JobProgressProps): React.ReactElement {
  const [event, setEvent] = useState<JobProgressEvent | null>(null);
  const [connected, setConnected] = useState<boolean>(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const closeRef = useRef<(() => void) | null>(null);

  const connect = useCallback((): void => {
    if (closeRef.current) {
      closeRef.current();
      closeRef.current = null;
    }
    setConnected(true);
    setErrorMessage(null);
    closeRef.current = openEventStream(projectId, {
      onProgress: (next) => setEvent(next),
      onOpen: () => setConnected(true),
      onError: (err) => {
        const message = err instanceof Error ? err.message : "事件流失败";
        setErrorMessage(message);
        setConnected(false);
      },
      onClose: () => setConnected(false),
    });
  }, [projectId]);

  useEffect(() => {
    connect();
    return () => {
      closeRef.current?.();
      closeRef.current = null;
    };
  }, [connect]);

  if (!event) {
    return (
      <div className={styles.panel} data-testid="job-progress">
        <p className={styles.empty}>暂无进度事件</p>
        {!connected ? (
          <button type="button" className={styles.reconnect} onClick={connect}>
            重连事件流
          </button>
        ) : null}
        {errorMessage ? (
          <p className={styles.error} role="alert">
            {errorMessage}
          </p>
        ) : null}
      </div>
    );
  }

  const rawTag = `${event.stage}:${event.status}`;
  const editorial = stageLabel(event.stage, event.status, {
    errorCode: event.error_code,
    errorMessage: event.error_message,
  });
  const detail = truncateMessage(event.error_message);

  const canCancel = event.status === "queued";
  const canRetry = event.status === "failed";
  const showEditorial = event.status !== "failed" && editorial !== rawTag;

  return (
    <div className={styles.panel} data-testid="job-progress">
      <p className={styles.status} data-testid="job-progress-status">
        <span className={styles.code}>{rawTag}</span>
        {showEditorial ? (
          <span className={styles.editorial}> · {editorial}</span>
        ) : null}
      </p>
      {event.status === "failed" ? (
        <p className={styles.error} role="alert" data-testid="job-progress-error">
          {event.error_code ? <code>{event.error_code}</code> : null}
          {detail ? <span className={styles.message}> · {detail}</span> : null}
        </p>
      ) : null}
      <p className={styles.meta}>
        第 {event.attempt} 次 · {new Date(event.ts).toLocaleString()}
      </p>
      <div className={styles.actions}>
        {canCancel && onCancel ? (
          <button type="button" onClick={() => onCancel(event.job_id)}>
            取消任务
          </button>
        ) : null}
        {canRetry && onRetry ? (
          <button type="button" onClick={() => onRetry(event.job_id)}>
            重试任务
          </button>
        ) : null}
        {!connected ? (
          <button type="button" className={styles.reconnect} onClick={connect}>
            重连事件流
          </button>
        ) : null}
      </div>
      {errorMessage ? (
        <p className={styles.error} role="alert">
          {errorMessage}
        </p>
      ) : null}
    </div>
  );
}