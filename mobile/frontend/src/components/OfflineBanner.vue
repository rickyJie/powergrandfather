<script setup lang="ts">
import { useNetworkStatus } from "@/composables/useNetworkStatus";

// Reachability is judged ONLY by the /api/health probe (5 consecutive misses),
// never by navigator.onLine — that reflects public-internet state, which is
// irrelevant to (and often wrong about) a localhost SSH tunnel.
const { tunnelAlive, probe } = useNetworkStatus();
</script>

<template>
  <transition name="fade">
    <div v-if="!tunnelAlive" class="banner tunnel">
      <van-icon name="warning-o" />
      <span>Backend unreachable — SSH tunnel down?</span>
      <button class="retry" @click="probe">Retry</button>
    </div>
  </transition>
</template>

<style scoped>
.banner {
  position: fixed;
  top: 46px;
  left: 0;
  right: 0;
  z-index: 200;
  padding: 6px 12px;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  color: #fff;
}
.offline {
  background: var(--van-danger-color, var(--van-danger-color));
}
.tunnel {
  background: var(--van-warning-color, var(--van-warning-color));
}
.retry {
  margin-left: auto;
  padding: 2px 8px;
  background: rgba(255, 255, 255, 0.2);
  border: 1px solid rgba(255, 255, 255, 0.4);
  border-radius: 3px;
  color: #fff;
  font-size: 11px;
  cursor: pointer;
}
.fade-enter-active,
.fade-leave-active {
  transition: opacity 150ms;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
