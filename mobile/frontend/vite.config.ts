import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { VitePWA } from "vite-plugin-pwa";

// mobile SPA is served under /m/ by mobile/backend_patch/mount.py
// (attached by mobile/scripts/start_with_mobile.sh). Don't change base
// without also updating mount.py and router base.
export default defineConfig({
  base: "/m/",
  plugins: [
    vue(),
    VitePWA({
      registerType: "autoUpdate",
      strategies: "generateSW",
      // SELF-DESTROYING: ship a service worker that unregisters itself and
      // deletes all Workbox caches. Rationale: this app is a LIVE session
      // companion over an SSH tunnel — it is useless offline, so precaching the
      // shell buys nothing, while `navigateFallback` to a CACHED index.html made
      // the WebView boot a STALE bundle after every rebuild (blank/stuck screen
      // even though the server had fresh dist). Killing the SW removes that whole
      // class of stale-cache bug; existing installs self-clean on next load.
      selfDestroying: true,
      includeAssets: ["icons/*.png", "icons/icon.svg"],
      manifest: {
        name: "CSM Mobile",
        short_name: "CSM",
        description: "Claude Session Manager mobile companion",
        start_url: "/m/",
        scope: "/m/",
        display: "standalone",
        orientation: "portrait",
        theme_color: "#4f46e5",
        background_color: "#f7f6f3",
        icons: [
          {
            src: "icons/icon-192.png",
            sizes: "192x192",
            type: "image/png",
            purpose: "any",
          },
          {
            src: "icons/icon-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "any",
          },
          {
            src: "icons/icon-512-maskable.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
      workbox: {
        // Only the STATIC app-shell is precached (for an offline splash). This
        // console runs live over an SSH tunnel — NEVER cache /api responses:
        //   - caching /api/health made the reachability probe fall back to
        //     stale cache on any tunnel latency spike → false "backend
        //     unreachable" (and stale data on every other endpoint).
        // So all /api + /ws are NetworkOnly and excluded from navigate fallback.
        globPatterns: ["**/*.{js,css,html,png,svg,woff2,webmanifest}"],
        navigateFallback: "/m/index.html",
        navigateFallbackDenylist: [/^\/api\//, /^\/ws\//],
        runtimeCaching: [
          {
            urlPattern: /\/(api|ws)\//,
            handler: "NetworkOnly",
          },
        ],
      },
      devOptions: { enabled: false },
    }),
  ],
  resolve: {
    alias: {
      // Mobile is a fully self-contained UI — no aliases into ../../frontend.
      // The two UIs share the backend API, not TypeScript source.
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5174,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      output: {
        manualChunks: {
          "vendor-vue": ["vue", "vue-router", "pinia"],
          "vendor-vant": ["vant"],
        },
      },
    },
  },
});
