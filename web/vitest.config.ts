import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// vitest@2 bundles its own vite type tree which collides with the project's
// vite@8 install at the type level; the runtime is fine. Cast to `any` so
// tsconfig stays strict for src/ without dragging vitest's plugin typing in.
const plugins: unknown = [react()];

export default defineConfig({
  plugins: plugins as never,
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    // Playwright e2e specs live under ./e2e and use `@playwright/test`'s
    // own `test()` runner — vitest must ignore the directory or it tries
    // to load those files and fails with "Playwright Test did not expect
    // test() to be called here".
    exclude: ["**/node_modules/**", "**/dist/**", "e2e/**"],
  },
});