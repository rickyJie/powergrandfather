<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, computed } from "vue";
import ChatShell from "@/layouts/ChatShell.vue";
import { useNotificationsStore } from "@/stores/notifications";
import { lightVars, darkVars } from "@/styles/theme";

// Initialize notifications app-wide so the header badge is live everywhere and
// realtime pushes arrive regardless of which tab is open (WS + poll fallback).
const notifications = useNotificationsStore();

// Follow the system colour scheme for ALL Vant components (plain CSS media
// queries don't flip Vant's component tokens — ConfigProvider does).
const theme = ref<"light" | "dark">("light");
const themeVars = computed(() =>
  theme.value === "dark" ? darkVars : lightVars
);
let mql: MediaQueryList | null = null;
function syncTheme() {
  theme.value = mql?.matches ? "dark" : "light";
}

onMounted(() => {
  mql = window.matchMedia("(prefers-color-scheme: dark)");
  syncTheme();
  mql.addEventListener?.("change", syncTheme);

  notifications.refresh().catch(() => {
    /* transient — polling/WS will retry */
  });
  // App-level ownership of the notification stream: the realtime WS + a 60s
  // polling safety net stay up for the whole app lifetime, so visiting and
  // leaving the Notifications view can't tear down the global stream (it used
  // to, degrading push to poll-only after one visit).
  notifications.connectWs();
  notifications.startPolling(60_000);
});

onBeforeUnmount(() => {
  mql?.removeEventListener?.("change", syncTheme);
  notifications.disconnectWs();
  notifications.stopPolling();
});
</script>

<template>
  <van-config-provider
    :theme="theme"
    :theme-vars="themeVars"
    theme-vars-scope="global"
  >
    <ChatShell />
  </van-config-provider>
</template>
