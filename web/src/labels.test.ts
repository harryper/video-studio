import { describe, expect, it } from "vitest";

import { stageLabel } from "./labels";

describe("stageLabel", () => {
  it("returns '等待选切口' for pitches+finished (verbatim brief test)", () => {
    expect(stageLabel("pitches", "finished")).toBe("等待选切口");
  });

  it("maps diagnosis queued/running", () => {
    expect(stageLabel("diagnosis", "queued")).toBe("等待排版");
    expect(stageLabel("diagnosis", "running")).toBe("主题诊断中");
  });

  it("maps research queued/running/finished", () => {
    expect(stageLabel("research", "queued")).toBe("等待研究");
    expect(stageLabel("research", "running")).toBe("调研中");
    expect(stageLabel("research", "finished")).toBe("调研完成");
  });

  it("maps pitches queued/running/finished", () => {
    expect(stageLabel("pitches", "queued")).toBe("等待生成切口");
    expect(stageLabel("pitches", "running")).toBe("切口生成中");
    expect(stageLabel("pitches", "finished")).toBe("等待选切口");
  });

  it("maps narrative queued/running", () => {
    expect(stageLabel("narrative", "queued")).toBe("等待结构");
    expect(stageLabel("narrative", "running")).toBe("结构编辑中");
  });

  it("maps draft queued/running", () => {
    expect(stageLabel("draft", "queued")).toBe("等待初稿");
    expect(stageLabel("draft", "running")).toBe("初稿撰写中");
  });

  it("maps rewrite queued/running", () => {
    expect(stageLabel("rewrite", "queued")).toBe("等待改写");
    expect(stageLabel("rewrite", "running")).toBe("定向改写中");
  });

  it("maps speech queued/running", () => {
    expect(stageLabel("speech", "queued")).toBe("等待配音");
    expect(stageLabel("speech", "running")).toBe("配音准备中");
  });

  it("maps approval queued/running", () => {
    expect(stageLabel("approval", "queued")).toBe("等待终审");
    expect(stageLabel("approval", "running")).toBe("终审中");
  });

  it("formats failure with error code", () => {
    expect(
      stageLabel("pitches", "failed", { errorCode: "llm_timeout" }),
    ).toBe("pitches失败：llm_timeout");
  });

  it("formats failure without error code", () => {
    expect(stageLabel("draft", "failed")).toBe("draft失败");
  });

  it("truncates long error messages in failure context", () => {
    const long = "x".repeat(200);
    const out = stageLabel("draft", "failed", {
      errorCode: "boom",
      errorMessage: long,
    });
    expect(out).toContain("draft失败：boom");
    expect(out.length).toBeLessThan(80);
  });

  it("returns '已取消' for any cancelled status", () => {
    expect(stageLabel("speech", "cancelled")).toBe("已取消");
    expect(stageLabel("draft", "cancelled")).toBe("已取消");
  });

  it("returns '空闲' when stage is null", () => {
    expect(stageLabel(null, null)).toBe("空闲");
  });

  it("falls back to <stage>:<status> when no mapping exists", () => {
    expect(stageLabel("totally_new_stage", "running")).toBe(
      "totally_new_stage:running",
    );
  });

  it("treats unknown status for known stage as fallback", () => {
    expect(stageLabel("approval", "finished")).toBe("approval:finished");
  });
});