# mobile/tests/frontend/

Vitest + Vue Test Utils suite for mobile SPA components. **Populated Phase 1+**.

## Run

```bash
cd mobile/frontend
npm run test         # runs mobile/tests/frontend/**/*.spec.ts
```

Mobile frontend `vitest.config.ts` points `test.include` to `../tests/frontend/**/*.spec.ts`.

## Planned cases (per Phase)

- Phase 1: `unit/components/BottomTabBar.spec.ts`, `unit/api/client.spec.ts`
- Phase 2: `MessageStream / MessageInput / ws / SessionDetail`
- Phase 3: `mission/MissionRow`, `Notifications`, `Tokens`, `stores/notifications`
- Phase 4: `AgentDeck`, `Settings`, `Feedback`, `Files`
- Phase 6: `useNetworkStatus`, `OfflineBanner`
