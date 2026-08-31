import { fileURLToPath, URL } from "node:url";
import path from "node:path";
import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

// vitest picks up specs from mobile/tests/frontend/ (outside this project
// root). Two knobs are load-bearing:
//   - `server.fs.allow: ['..']` — allow vite to serve files from mobile/
//     one level above mobile/frontend/, otherwise vite refuses.
//   - `test.include` path is relative to this config file location.
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
      "@shared-api": path.resolve(__dirname, "../../frontend/src/api"),
      "@shared-types": path.resolve(__dirname, "../../frontend/src/types"),
    },
  },
  server: {
    fs: {
      allow: [".."],
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    include: ["../tests/frontend/**/*.spec.ts"],
  },
});
