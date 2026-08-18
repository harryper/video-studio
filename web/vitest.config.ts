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
  },
});