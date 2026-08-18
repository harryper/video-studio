/**
 * PitchReviewPage — three-card pitch gate (spec §10.3).
 *
 * Renders the current pitch set as 3 PitchCard instances plus a research
 * packet <details> and the global "全部换方向" control. The page is
 * responsible for:
 *   * Loading the current pitch set + research packet (Task 11's artifact
 *     history endpoint returns the research payload).
 *   * The two-step reopen handshake: first click shows a confirmation
 *     modal listing the kinds that will be invalidated, second click
 *     re-sends with the ``X-Confirm-Invalidates`` header.
 *   * Accepting a pitch (with or without edits) and navigating the
 *     editor onward to the draft review once the narrative job is queued.
 */

import { useCallback, useEffect, useState } from "react";

import {
  acceptPitch,
  getPitches,
  listArtifacts,
  reopenPitches,
} from "../api/client";
import type {
  ApiError,
  ArtifactHistoryEntry,
  ResearchPacket,
  StoryPitch,
  StoryPitchSet,
} from "../api/types";
import { errorMessage } from "../components/errorMessage";
import { PitchCard } from "../components/PitchCard";
import { Link, navigate } from "../router";

import styles from "./PitchReviewPage.module.css";

export interface PitchReviewPageProps {
  projectId: string;
}

function sortKinds(kinds: string[]): string {
  return [...kinds].sort().join(",");
}

function extractInvalidates(err: ApiError): string[] {
  const value = err.body.invalidates;
  if (Array.isArray(value)) {
    return value.filter((entry): entry is string => typeof entry === "string");
  }
  return [];
}

export function PitchReviewPage({
  projectId,
}: PitchReviewPageProps): React.ReactElement {
  const [pitchSet, setPitchSet] = useState<StoryPitchSet | null>(null);
  const [research, setResearch] = useState<ResearchPacket | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<boolean>(false);
  const [reopenPending, setReopenPending] = useState<boolean>(false);
  const [reopenInvalidates, setReopenInvalidates] = useState<string[] | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    setError(null);
    try {
      const [set, artifacts] = await Promise.all([
        getPitches(projectId).catch((err: unknown) => {
          if (err instanceof Object && "status" in err && (err as ApiError).status === 404) {
            return null;
          }
          throw err;
        }),
        listArtifacts(projectId),
      ]);
      setPitchSet(set);
      const researchArtifact = artifacts.find((a: ArtifactHistoryEntry) => a.kind === "research" && a.is_head);
      if (researchArtifact && researchArtifact.payload) {
        setResearch(researchArtifact.payload as ResearchPacket);
      }
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onAccept = useCallback(
    async (pitch: StoryPitch, editedPitch?: StoryPitch): Promise<void> => {
      setBusy(true);
      setError(null);
      try {
        const response = await acceptPitch(projectId, pitch.id, {
          ...(editedPitch ? { edited_pitch: editedPitch } : {}),
        });
        navigate(`/projects/${projectId}/drafts/${response.artifact_id}`);
      } catch (err) {
        setError(errorMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [projectId],
  );

  const triggerReopen = useCallback(async (): Promise<void> => {
    setBusy(true);
    setError(null);
    const confirm = reopenInvalidates ? sortKinds(reopenInvalidates) : null;
    try {
      await reopenPitches(projectId, confirm);
      setReopenPending(false);
      setReopenInvalidates(null);
      await refresh();
    } catch (err) {
      const apiErr = err as ApiError;
      if (apiErr && apiErr.body && apiErr.body.code === "confirmation_required") {
        setReopenInvalidates(extractInvalidates(apiErr));
        setReopenPending(true);
      } else {
        setError(errorMessage(err));
      }
    } finally {
      setBusy(false);
    }
  }, [projectId, reopenInvalidates, refresh]);

  const handleAcceptClick = useCallback(
    (pitch: StoryPitch) => {
      void onAccept(pitch);
    },
    [onAccept],
  );

  const handleEditedAccept = useCallback(
    (pitch: StoryPitch) => {
      void onAccept(pitch, pitch);
    },
    [onAccept],
  );

  const handleStartReopen = useCallback((): void => {
    void triggerReopen();
  }, [triggerReopen]);

  const handleConfirmReopen = useCallback((): void => {
    void triggerReopen();
  }, [triggerReopen]);

  const handleDismissReopen = useCallback((): void => {
    setReopenPending(false);
    setReopenInvalidates(null);
  }, []);

  if (error && !pitchSet) {
    return (
      <main className={styles.page}>
        <p className={styles.error} role="alert">
          {error}
        </p>
        <p>
          <Link to={`/projects/${projectId}`}>返回工作区</Link>
        </p>
      </main>
    );
  }

  if (!pitchSet) {
    return (
      <main className={styles.page}>
        <p className={styles.info}>加载切口…</p>
      </main>
    );
  }

  return (
    <main className={styles.page}>
      <nav className={styles.breadcrumb}>
        <Link to={`/projects/${projectId}`}>← 工作区</Link>
      </nav>

      <header className={styles.header}>
        <h1 className={styles.title}>选择切口</h1>
        <p className={styles.subtitle}>
          从三个候选中挑一个作为本期脚本的起点；也可以先微调文字再提交。
        </p>
      </header>

      {error ? (
        <p className={styles.error} role="alert">
          {error}
        </p>
      ) : null}

      <section className={styles.cards} aria-label="候选切口">
        {pitchSet.pitches.map((pitch) => (
          <PitchCard
            key={pitch.id}
            pitch={pitch}
            onAccept={(p) => {
              if (p === pitch) handleAcceptClick(p);
              else handleEditedAccept(p);
            }}
          />
        ))}
      </section>

      <div className={styles.actions}>
        <button
          type="button"
          className={styles.regenerate}
          onClick={handleStartReopen}
          disabled={busy}
        >
          全部换方向
        </button>
      </div>

      <ResearchSection research={research} />

      {reopenPending && reopenInvalidates ? (
        <div className={styles.modalBackdrop} role="dialog" aria-modal="true">
          <div className={styles.modal}>
            <h2 className={styles.modalTitle}>确认全部换方向</h2>
            <p>重新生成切口会丢弃以下阶段的产物：</p>
            <ul className={styles.modalList}>
              {reopenInvalidates.map((kind) => (
                <li key={kind}>{kind}</li>
              ))}
            </ul>
            <p className={styles.modalHint}>
              再次点击将携带确认头部重新提交，并立即触发一次切口重新生成。
            </p>
            <div className={styles.modalActions}>
              <button
                type="button"
                className={styles.primary}
                onClick={handleConfirmReopen}
                disabled={busy}
              >
                确认全部换方向
              </button>
              <button type="button" onClick={handleDismissReopen}>
                取消
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </main>
  );
}

interface ResearchSectionProps {
  research: ResearchPacket | null;
}

function ResearchSection({ research }: ResearchSectionProps): React.ReactElement {
  const [open, setOpen] = useState<boolean>(false);
  const facts = research?.fact_cards ?? [];
  const sources = research?.sources ?? [];

  return (
    <section className={styles.research}>
      <button
        type="button"
        className={styles.researchToggle}
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-controls="research-body"
      >
        {open ? "收起调查材料" : "展开调查材料"}
      </button>
      {open ? (
        <div className={styles.researchBody} id="research-body">
          <h3 className={styles.researchHeading}>事实卡片</h3>
          {facts.length === 0 ? (
            <p className={styles.info}>暂无事实卡片</p>
          ) : (
            <ul className={styles.researchList}>
              {facts.map((card) => (
                <li key={card.claim} className={styles.researchItem}>
                  <strong>{card.claim}</strong>
                  <span className={styles.researchMeta}>
                    {card.risk} · {Math.round(card.confidence * 100)}% · {card.verification_status}
                  </span>
                  {card.sources.length > 0 ? (
                    <ul className={styles.researchSources}>
                      {card.sources.map((source) => (
                        <li key={source.url}>{source.title}</li>
                      ))}
                    </ul>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
          {sources.length > 0 ? (
            <>
              <h3 className={styles.researchHeading}>来源</h3>
              <ul className={styles.researchSources}>
                {sources.map((source) => (
                  <li key={source.url}>{source.title}</li>
                ))}
              </ul>
            </>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}