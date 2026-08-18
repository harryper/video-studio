/**
 * Single-user password gate for Content Studio.
 *
 * The API issues an HttpOnly session cookie + per-session CSRF token on
 * ``POST /api/session``. ``client.ts.login()`` stores the CSRF token in a
 * module-level variable so subsequent mutations can attach it via
 * ``X-CSRF-Token``. We render this form whenever the SPA is not yet
 * authenticated (no CSRF token in memory) and lift state back to App
 * on success.
 *
 * WHY a separate page (not a modal): the API rejects every request
 * other than ``/api/session`` and ``/api/health`` without a session
 * cookie, so a modal that lets the user click around an empty page
 * just produces a wall of 401s. A full-page gate keeps the rest of
 * the SPA off the wire until the operator is authenticated.
 */

import { useState } from "react";

import { getCsrfToken, login } from "../api/client";

import styles from "./LoginPage.module.css";

interface Props {
  onLoggedIn: () => void;
}

export function LoginPage({ onLoggedIn }: Props): React.ReactElement {
  const [password, setPassword] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState<boolean>(false);

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await login(password);
      onLoggedIn();
    } catch (err) {
      const msg = (err as { body?: { message?: string } }).body?.message;
      setError(msg ?? "登录失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className={styles.shell}>
      <form className={styles.card} onSubmit={handleSubmit}>
        <h1 className={styles.title}>Content Studio</h1>
        <p className={styles.subtitle}>输入访问密码</p>
        <input
          type="password"
          className={styles.input}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoFocus
          autoComplete="current-password"
          disabled={submitting}
        />
        {error ? <p className={styles.error}>{error}</p> : null}
        <button
          type="submit"
          className={styles.submit}
          disabled={submitting || password.length === 0}
        >
          {submitting ? "登录中…" : "登录"}
        </button>
      </form>
    </div>
  );
}

export function isAuthenticated(): boolean {
  return getCsrfToken() !== null;
}
