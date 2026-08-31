<script setup lang="ts">
interface Props {
  message?: string;
}
withDefaults(defineProps<Props>(), {
  message: "Failed to load.",
});
defineEmits<{ (e: "retry"): void }>();
</script>

<template>
  <div class="error-retry">
    <van-icon name="warning-o" size="28" />
    <div class="msg">{{ message }}</div>
    <van-button size="small" plain @click="$emit('retry')">Retry</van-button>
  </div>
</template>

<style scoped>
.error-retry {
  padding: 40px 16px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  color: var(--van-text-color-3, var(--ink-mute));
}
.msg {
  font-size: 13px;
}
</style>
