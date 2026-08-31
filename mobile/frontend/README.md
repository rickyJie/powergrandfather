# mobile/frontend/

Vue 3 + Vite + Vant 4 mobile SPA. **Populated in Phase 1** (脚手架) and filled through Phase 2-6.

## Structure (planned)

```
frontend/
├── package.json                    Vue 3 + Vite 5 + Vant 4 + Pinia + Vue Router + axios + TS
├── vite.config.ts                  base: '/m/', alias @shared-api → ../../frontend/src/api
├── tsconfig.json
├── index.html
├── public/
│   ├── manifest.webmanifest        (Phase 6)
│   └── icons/                      (Phase 6)
├── src/
│   ├── main.ts
│   ├── App.vue
│   ├── router/
│   ├── stores/                     Pinia (chat, sessions, missions, notifications, tokens, ...)
│   ├── layouts/                    MobileLayout with BottomTabBar
│   ├── components/                 (共享: MessageStream / MessageBubble / ToolUseBubble / MessageInput / ...)
│   ├── views/                      (Missions / Chat / Sessions / Notifications / More + subviews)
│   ├── api/                        client.ts (X-CSM-Client injection) + ws.ts
│   ├── composables/                useNetworkStatus / useInstallPrompt / ...
│   └── styles/
└── tests/                          → symlink or path back to mobile/tests/frontend/
```

## Build

```bash
cd mobile/frontend
npm install
npm run build         # → mobile/frontend/dist/
```

`start_with_mobile.sh` will auto-build if `dist/index.html` is missing (unless `CSM_SKIP_FRONTEND_BUILD=1`).

## Dev server

```bash
cd mobile/frontend
npm run dev           # vite on :5174 (or similar), proxy /api to :8000
```

Mobile dev flow is independent of desktop `frontend/`.
