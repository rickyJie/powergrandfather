import { createApp } from "vue";
import { createPinia } from "pinia";
import Vant, { showNotify, showConfirmDialog } from "vant";
import "vant/lib/index.css";
import { registerSW } from "virtual:pwa-register";

import App from "./App.vue";
import router from "./router";
import { initPerfConsole } from "./lib/perfLog";
import "./styles/global.css";

// window.__csmPerf for latency inspection (surface='mobile'). Axios
// interceptors are installed in api/client.ts on import.
initPerfConsole("mobile");

const app = createApp(App);
app.use(createPinia());
app.use(router);
app.use(Vant);
app.mount("#app");

// PWA: when a new service worker is waiting, offer an actionable reload so
// the user actually gets fresh code (the old toast had no way to update).
if ("serviceWorker" in navigator) {
  const updateSW = registerSW({
    onNeedRefresh() {
      showConfirmDialog({
        title: "Update available",
        message: "A new version is ready. Reload now?",
        confirmButtonText: "Reload",
        cancelButtonText: "Later",
      })
        .then(() => updateSW(true)) // activates the new SW and reloads
        .catch(() => {
          /* user chose Later */
        });
    },
    onOfflineReady() {
      showNotify({
        type: "success",
        message: "Ready for offline use.",
        duration: 2000,
      });
    },
  });
}
