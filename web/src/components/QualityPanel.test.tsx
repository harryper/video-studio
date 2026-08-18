/**
 * QualityPanel renders the 7 spec §10.5 checks; ignoring any one of them
 * requires a non-empty reason (no fake composite score).
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { QualityPanel } from "./QualityPanel";

describe("QualityPanel", () => {
  it("renders all seven §10.5 checks with Chinese headings", () => {
    render(<QualityPanel />);
    const expected = [
      "可疑数字",
      "因果跳跃",
      "跨稿件重复",
      "未推进调查的段落",
      "难以口播的句子",
      "预计时长",
      "建议拆题位置",
    ];
    for (const heading of expected) {
      expect(
        screen.getByRole("heading", { name: heading }),
        `expected "${heading}" to be rendered as a heading`,
      ).toBeVisible();
    }
  });

  it("does NOT render a fake composite score", () => {
    render(<QualityPanel />);
    // The brief forbids composite scores — no element should claim an
    // overall quality metric.
    expect(screen.queryByText(/综合评分|总分|composite/i)).not.toBeInTheDocument();
  });

  it("requires a non-empty reason when ignoring a check", async () => {
    const user = userEvent.setup();
    render(<QualityPanel />);

    const ignoreButtons = screen.getAllByRole("button", { name: /忽略并说明原因/ });
    expect(ignoreButtons.length).toBeGreaterThan(0);

    await user.click(ignoreButtons[0]);

    // Without a reason, the ignore cannot be saved.
    const save = screen.getByRole("button", { name: /保存忽略原因/ });
    expect(save).toBeDisabled();

    const reason = screen.getByLabelText(/忽略原因/);
    await user.type(reason, "本段落已经多次交叉验证");
    expect(save).toBeEnabled();
    await user.click(save);

    expect(
      screen.getByText(/已忽略：本段落已经多次交叉验证/),
    ).toBeVisible();
  });
});