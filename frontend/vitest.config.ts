import { defineConfig } from "vitest/config";
import path from "node:path";

// Unit tests for pure frontend composition logic (brief §7.9). Node environment —
// no DOM needed; the composition functions are React-free by design.
export default defineConfig({
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  test: { include: ["src/**/*.test.ts"], environment: "node" },
});
