/**
 * Best-effort formatter for the failure message of any thrown value.
 *
 * The API client throws ``ApiError`` envelopes (with a stable ``code``
 * and a localized ``message``) while other call sites may surface a
 * plain ``Error`` or any unknown value. This helper pulls a single
 * human-readable string out of whichever shape it lands in, falling
 * back to a constant label so callers never have to defend against
 * ``undefined``.
 */

import type { ApiError } from "../api/types";

const FALLBACK_MESSAGE = "操作失败";

export function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (err && typeof err === "object" && "body" in err) {
    const apiErr = err as ApiError;
    const body = apiErr.body;
    if (body && typeof body.message === "string") {
      return body.message;
    }
  }
  return FALLBACK_MESSAGE;
}