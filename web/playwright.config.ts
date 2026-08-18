import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the offline Content Studio web e2e.
 *
 * The web server is Vite's preview mode — the build has already been
 * produced by `npm run build` so no JS bundling happens during the test.
 * The e2e spec intercepts the `/api/*` requests at the browser level via
 * Playwright's `page.route()` so the test never touches the real
 * backend. Run with `npm --prefix web run e2e`.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:4173",
    headless: true,
    trace: "off",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: "npm run preview -- --host 127.0.0.1 --port 4173",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
