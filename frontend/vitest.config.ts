import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
  },
  // Test runner ONLY — `vite.config.ts` drives dev/build and is untouched, so
  // this does not widen what the dev server will serve.
  // `wsLiveness.identity.test.ts` reads the mobile package's copy of a file
  // this app duplicates, and Vite refuses to read outside its root by default.
  server: { fs: { allow: ['..'] } },
})
