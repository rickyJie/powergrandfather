import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { VitePWA } from 'vite-plugin-pwa'
import { NAVIGATION_FALLBACK_DENYLIST } from './pwa-navigation'

export default defineConfig({
  plugins: [
    vue(),
    VitePWA({
      // Never reload a live terminal behind the user's back.  The UI exposes
      // an explicit update action through PwaStatus.vue instead.
      registerType: 'prompt',
      manifest: {
        id: '/',
        name: 'PowerGrandFather',
        short_name: 'PGF',
        description: 'Claude and Codex session control console',
        lang: 'zh-CN',
        start_url: '/sessions',
        scope: '/',
        display: 'standalone',
        orientation: 'any',
        theme_color: '#20211f',
        background_color: '#f5f3ee',
        categories: ['developer', 'productivity', 'utilities'],
        icons: [
          { src: 'pwa-64x64.png', sizes: '64x64', type: 'image/png' },
          { src: 'pwa-192x192.png', sizes: '192x192', type: 'image/png' },
          { src: 'pwa-512x512.png', sizes: '512x512', type: 'image/png' },
          {
            src: 'maskable-icon-512x512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
      workbox: {
        cleanupOutdatedCaches: true,
        // Custom SW logic (OS-notification click handling) lives in a
        // hand-written script under public/ and is appended to the generated
        // SW at runtime. Keeps generateSW mode (and its precache/update flow)
        // intact instead of switching to a full injectManifest custom SW.
        importScripts: ['notif-click-sw.js'],
        navigateFallback: '/index.html',
        // The SW is scoped to `/`, so this fallback intercepts every document
        // load on the origin — including things this app doesn't own. See
        // pwa-navigation.ts for what goes in the list and why omissions fail
        // silently.
        navigateFallbackDenylist: NAVIGATION_FALLBACK_DENYLIST,
        // Manifest icons are injected by the plugin; excluding png here avoids
        // duplicate precache entries for those same files.
        globPatterns: ['**/*.{js,css,html,ico,svg,woff2}'],
      },
    }),
  ],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true, ws: true },
      '/proxy': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
