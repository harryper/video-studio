/**
 * Editorial Chinese labels for (latest_stage, latest_job_status) pairs.
 *
 * The dashboard surfaces a Chinese editorial reading rather than raw
 * pipeline identifiers; the mapping lives here so it can be tested in
 * isolation and reused wherever the label is rendered.
 *
 * The failure label embeds the error_code; the error_message is appended
 * when present, truncated to 60 characters so an LLM payload can never
 * leak into the UI.
 */

export interface StageLabelOptions {
  errorCode?: string | null;
  errorMessage?: string | null;
}

const ERROR_MESSAGE_LIMIT = 60;

const MAPPINGS: Record<string, Record<string, string>> = {
  diagnosis: {
    queued: "等待排版",
    running: "主题诊断中",
  },
  research: {
    queued: "等待研究",
    running: "调研中",
    finished: "调研完成",
  },
  pitches: {
    queued: "等待生成切口",
    running: "切口生成中",
    finished: "等待选切口",
  },
  narrative: {
    queued: "等待结构",
    running: "结构编辑中",
  },
  draft: {
    queued: "等待初稿",
    running: "初稿撰写中",
  },
  rewrite: {
    queued: "等待改写",
    running: "定向改写中",
  },
  speech: {
    queued: "等待配音",
    running: "配音准备中",
  },
  approval: {
    queued: "等待终审",
    running: "终审中",
  },
};

function truncateMessage(message: string | null | undefined): string | null {
  if (!message) return null;
  if (message.length <= ERROR_MESSAGE_LIMIT) return message;
  return `${message.slice(0, ERROR_MESSAGE_LIMIT)}…`;
}

export function stageLabel(
  stage: string | null,
  status: string | null,
  opts: StageLabelOptions = {},
): string {
  if (stage === null) return "空闲";

  if (status === "cancelled") return "已取消";

  if (status === "failed") {
    const code = opts.errorCode ?? "";
    const base = code ? `${stage}失败：${code}` : `${stage}失败`;
    const message = truncateMessage(opts.errorMessage ?? null);
    return message ? `${base}（${message}）` : base;
  }

  const stageMap = MAPPINGS[stage];
  if (stageMap) {
    const label = status === null ? undefined : stageMap[status];
    if (label) return label;
  }

  return `${stage}:${status ?? ""}`;
}