import { describe, expect, it } from "vitest";

import type { ApiError } from "../api/types";
import { errorMessage } from "./errorMessage";

describe("errorMessage", () => {
  it("returns Error.message when err is an Error instance", () => {
    expect(errorMessage(new Error("boom"))).toBe("boom");
  });

  it("returns ApiError.body.message when err matches the envelope shape", () => {
    const apiErr: ApiError = {
      status: 400,
      body: { code: "validation", message: "字段缺失" },
    };
    expect(errorMessage(apiErr)).toBe("字段缺失");
  });

  it("returns the fallback label for unknown errors", () => {
    expect(errorMessage(null)).toBe("操作失败");
    expect(errorMessage(undefined)).toBe("操作失败");
    expect(errorMessage("string error")).toBe("操作失败");
    expect(errorMessage({ body: { message: 123 } })).toBe("操作失败");
    expect(errorMessage({})).toBe("操作失败");
  });
});