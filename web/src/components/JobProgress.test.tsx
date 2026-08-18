import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { EventStreamHandlers, JobProgressEvent } from "../api/types";

let activeHandlers: EventStreamHandlers | null = null;
let activeClose: (() => void) | null = null;

const openEventStreamMock = vi.fn(
  (
    _projectId: string,
    handlers: EventStreamHandlers,
    _options: { lastEventId?: string | null } = {},
  ): (() => void) => {
    activeHandlers = handlers;
    const close = () => {
      activeHandlers = null;
      activeClose = null;
      handlers.onClose?.();
    };
    activeClose = close;
    return close;
  },
);

vi.mock("../api/client", () => ({
  openEventStream: (...args: unknown[]) =>
    (openEventStreamMock as unknown as (...a: unknown[]) => () => void)(...args),
}));

import { JobProgress } from "./JobProgress";

const EVT_QUEUED: JobProgressEvent = {
  id: "job-1",
  job_id: "job-1",
  stage: "pitches",
  status: "queued",
  ts: "2026-08-10T00:00:00Z",
  attempt: 0,
  error_code: null,
  error_message: null,
};

const EVT_RUNNING: JobProgressEvent = {
  ...EVT_QUEUED,
  status: "running",
  attempt: 1,
};

const EVT_FAILED: JobProgressEvent = {
  ...EVT_QUEUED,
  status: "failed",
  error_code: "llm_timeout",
  error_message: "boom",
};

describe("JobProgress", () => {
  beforeEach(() => {
    openEventStreamMock.mockClear();
    activeHandlers = null;
    activeClose = null;
  });

  it("renders initial state from first SSE event", async () => {
    render(<JobProgress projectId="p1" />);
    expect(openEventStreamMock).toHaveBeenCalledWith(
      "p1",
      expect.any(Object),
    );
    act(() => {
      activeHandlers?.onProgress(EVT_QUEUED);
    });
    expect(await screen.findByText("pitches:queued")).toBeVisible();
  });

  it("updates when a new progress event arrives", async () => {
    render(<JobProgress projectId="p1" />);
    act(() => {
      activeHandlers?.onProgress(EVT_QUEUED);
    });
    await screen.findByText("pitches:queued");
    act(() => {
      activeHandlers?.onProgress(EVT_RUNNING);
    });
    expect(await screen.findByText("pitches:running")).toBeVisible();
  });

  it("reconnect button appears after stream close", async () => {
    render(<JobProgress projectId="p1" />);
    act(() => {
      activeHandlers?.onProgress(EVT_QUEUED);
    });
    await screen.findByText("pitches:queued");

    await act(async () => {
      activeClose?.();
    });

    expect(await screen.findByRole("button", { name: /重连/ })).toBeVisible();
  });

  it("clicking reconnect opens a new stream", async () => {
    render(<JobProgress projectId="p1" />);
    act(() => {
      activeHandlers?.onProgress(EVT_QUEUED);
    });
    await screen.findByText("pitches:queued");

    await act(async () => {
      activeClose?.();
    });

    const btn = await screen.findByRole("button", { name: /重连/ });
    await userEvent.setup().click(btn);
    await waitFor(() => {
      expect(openEventStreamMock).toHaveBeenCalledTimes(2);
    });
  });

  it("shows error_code on failure event", async () => {
    render(<JobProgress projectId="p1" />);
    act(() => {
      activeHandlers?.onProgress(EVT_FAILED);
    });
    expect(await screen.findByText(/pitches:failed/)).toBeVisible();
    expect(screen.getByText(/llm_timeout/)).toBeInTheDocument();
  });
});