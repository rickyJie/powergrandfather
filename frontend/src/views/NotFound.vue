<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()
</script>

<template>
  <div class="notfound">
    <div class="panel">
      <h2 class="serif">Page not found</h2>
      <p class="mono path">{{ route.fullPath }}</p>
      <p class="hint">The route you tried doesn't exist.</p>
      <router-link class="btn" to="/sessions">Back to Sessions</router-link>
    </div>
  </div>
</template>

<style scoped>
.notfound {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 60px 20px;
  flex: 1;
}
.panel {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 32px 40px;
  max-width: 480px;
  text-align: center;
}
.panel h2 { font-size: 22px; margin: 0 0 12px; }
.panel .path {
  display: inline-block;
  padding: 2px 8px;
  background: var(--canvas);
  border-radius: 4px;
  font-size: 12px;
  color: var(--ink-mute);
  margin-bottom: 16px;
}
.panel .hint { color: var(--ink-mute); margin-bottom: 20px; }
.panel .btn {
  display: inline-block;
  padding: 8px 16px;
  background: var(--ink);
  color: var(--card);
  border-radius: 6px;
  text-decoration: none;
  font-size: 13px;
  transition: opacity 150ms;
}
.panel .btn:hover { opacity: 0.85; }
</style>
