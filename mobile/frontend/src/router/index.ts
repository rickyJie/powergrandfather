import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";

// history mode with base '/m/' — matches vite.config.ts base + backend
// mobile_spa_fallback in mobile/backend_patch/mount.py.
//
// SCOPE (2026-08-19 chat-first redesign): the phone is a session companion. The
// home IS the immersive chat of the active session; the session list is a
// left drawer (not a route). Deep links land on /s/:sid. Everything else
// (missions/workflows/token analytics/…) lives on the desktop web UI.
//
// AMENDED 2026-08-30: plan quota is the one exception. "Can I keep going?" is a
// question you ask away from the desk, and it is READ-ONLY and tiny — two
// percentages the backend already caches. The rest of the token surface
// (filters, trends, CSV export, alert-rule authoring) stays on desktop.
//
// AMENDED 2026-08-31: quota is no longer a route either. `/usage` existed to
// hold an agent switcher and a re-probe button — not enough to earn a screen
// you must navigate to and back from one-handed. Both now live on the drawer's
// UsageCard: tap the card to switch agent, tap ↻ to probe. The path redirects
// so old bookmarks and the PWA shortcut don't dead-end.
const routes: RouteRecordRaw[] = [
  {
    path: "/",
    name: "home",
    component: () => import("@/views/ChatView.vue"),
    meta: { title: "Session" },
  },
  {
    path: "/s/:sid",
    name: "chat",
    component: () => import("@/views/ChatView.vue"),
    meta: { title: "Session" },
  },
  {
    path: "/notifications",
    name: "notifications",
    component: () => import("@/views/Notifications.vue"),
    meta: { title: "Notifications" },
  },
  // Retired: quota lives on the drawer's UsageCard now. Kept as an explicit
  // redirect rather than left to the catch-all so the intent is readable.
  { path: "/usage", redirect: "/" },
  // Retired deep links (old /sessions, /missions bookmarks) → chat home.
  { path: "/:pathMatch(.*)*", redirect: "/" },
];

export default createRouter({
  history: createWebHistory("/m/"),
  routes,
});
