<script setup lang="ts">
import { computed } from "vue";
import { showToast } from "vant";

interface Props {
  sessionId: string;
  adapter: string;
  cwd: string;
}

const props = defineProps<Props>();

const desktopUrl = computed(
  () => `${window.location.origin}/sessions/${props.sessionId}`
);

async function copyDesktopLink() {
  try {
    await navigator.clipboard.writeText(desktopUrl.value);
    showToast({ message: "Desktop link copied", type: "success", duration: 1200 });
  } catch {
    showToast({ message: "Copy failed", type: "fail" });
  }
}
</script>

<template>
  <div class="tui-card">
    <van-empty description="">
      <template #image>
        <div class="tui-icon">📟</div>
      </template>
      <div class="tui-body">
        <h3 class="tui-title">TUI Session</h3>
        <p class="tui-desc">
          This <b>{{ adapter }}</b> session runs a full-screen terminal UI
          that cannot be rendered on mobile. Please use the desktop client
          for interaction.
        </p>
        <p class="tui-cwd">{{ cwd }}</p>
        <van-button
          type="primary"
          size="small"
          block
          @click="copyDesktopLink"
        >
          Copy desktop link
        </van-button>
      </div>
    </van-empty>
  </div>
</template>

<style scoped>
.tui-card {
  padding: 24px 16px;
}
.tui-icon {
  font-size: 60px;
  padding: 20px;
}
.tui-body {
  text-align: center;
  max-width: 320px;
}
.tui-title {
  margin: 8px 0 4px 0;
}
.tui-desc {
  font-size: 13px;
  color: var(--van-text-color-3, var(--ink-mute));
  margin: 8px 0;
  line-height: 1.5;
}
.tui-cwd {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--van-text-color-3, var(--ink-mute));
  margin: 8px 0;
  word-break: break-all;
}
</style>
