<script setup lang="ts">
import { ref } from "vue";
import OfflineBanner from "@/components/OfflineBanner.vue";
import SessionDrawer from "@/components/SessionDrawer.vue";
import { useUiStore } from "@/stores/ui";

// Chat-first shell: the routed view (immersive chat / notifications) fills the
// screen; the session list is a left drawer opened by the ☰ button or an
// edge-swipe from the left. No bottom tab bar — navigation is drawer + back.
const ui = useUiStore();

// Edge-swipe to open the drawer: a rightward drag that STARTS near the left
// edge. Kept deliberately narrow so it doesn't fight the chat's own scroll /
// text selection.
const startX = ref(0);
const startY = ref(0);
const tracking = ref(false);
function onTouchStart(e: TouchEvent) {
  const t = e.touches[0];
  startX.value = t.clientX;
  startY.value = t.clientY;
  tracking.value = t.clientX <= 24 && !ui.drawerOpen;
}
function onTouchMove(e: TouchEvent) {
  if (!tracking.value) return;
  const t = e.touches[0];
  const dx = t.clientX - startX.value;
  const dy = Math.abs(t.clientY - startY.value);
  if (dx > 60 && dy < 40) {
    tracking.value = false;
    ui.openDrawer();
  }
}
function onTouchEnd() {
  tracking.value = false;
}
</script>

<template>
  <div
    class="shell"
    @touchstart.passive="onTouchStart"
    @touchmove.passive="onTouchMove"
    @touchend.passive="onTouchEnd"
  >
    <OfflineBanner />
    <router-view />

    <van-popup
      :show="ui.drawerOpen"
      position="left"
      :style="{ width: '86%', maxWidth: '360px', height: '100%' }"
      @update:show="(v: boolean) => (v ? ui.openDrawer() : ui.closeDrawer())"
    >
      <SessionDrawer />
    </van-popup>
  </div>
</template>

<style scoped>
.shell {
  height: 100vh;
  height: 100dvh;
  background: var(--canvas);
  overflow: hidden;
}
</style>
