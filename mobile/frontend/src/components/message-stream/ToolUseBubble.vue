<script setup lang="ts">
import { ref, computed } from "vue";

interface Props {
  tool: string;
  toolId: string;
  input: unknown;
  ts?: string;
  /** Attached tool_use_result, if the paired result event has arrived. */
  result?: { ok: boolean; preview: string } | null;
}

const props = defineProps<Props>();
const expanded = ref(false);

const inputStr = computed(() => {
  try {
    return JSON.stringify(props.input, null, 2);
  } catch {
    return String(props.input);
  }
});

// A concise one-line argument summary for the collapsed row. Prefer the field
// that best identifies the call (path / command / pattern / url…), else the
// first primitive value, else a compact JSON blob. Kept short; the row uses
// rtl truncation so the tail (usually a filename) stays visible.
const argPreview = computed(() => {
  const inp = props.input;
  if (inp == null) return "";
  if (typeof inp !== "object") return String(inp);
  const o = inp as Record<string, unknown>;
  const keys = [
    "file_path",
    "path",
    "command",
    "pattern",
    "query",
    "url",
    "prompt",
    "description",
    "name",
  ];
  for (const k of keys) {
    const v = o[k];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  for (const v of Object.values(o)) {
    if (typeof v === "string" && v.trim()) return v.trim();
    if (typeof v === "number" || typeof v === "boolean") return String(v);
  }
  try {
    return JSON.stringify(inp);
  } catch {
    return "";
  }
});

const statusClass = computed(() =>
  props.result ? (props.result.ok ? "ok" : "err") : "pending"
);
</script>

<template>
  <div class="tool-card" :class="{ open: expanded }">
    <!-- Collapsed: single dense line. Never a multi-line pre by default. -->
    <button type="button" class="tool-row" @click="expanded = !expanded">
      <span class="zap">⚡</span>
      <span class="t-name">{{ tool }}</span>
      <span class="t-arg" dir="rtl" :title="argPreview">{{ argPreview }}</span>
      <span :class="['t-status', statusClass]">
        <template v-if="!result">·</template>
        <template v-else-if="result.ok">✓</template>
        <template v-else>✕</template>
      </span>
    </button>
    <div v-if="expanded" class="tool-detail">
      <div class="section-label">input</div>
      <pre class="section-code">{{ inputStr }}</pre>
      <template v-if="result">
        <div class="section-label">result ({{ result.ok ? "ok" : "error" }})</div>
        <pre class="section-code">{{ result.preview }}</pre>
      </template>
      <div v-else class="section-label pending">waiting for result…</div>
    </div>
  </div>
</template>

<style scoped>
.tool-card {
  margin: 6px 14px;
  background: var(--surface-1);
  border: 1px solid var(--outline-soft);
  border-radius: 10px;
  overflow: hidden;
}
.tool-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 10px;
  background: transparent;
  border: none;
  cursor: pointer;
  text-align: left;
  font-size: 12.5px;
  color: var(--text);
}
.zap {
  font-size: 13px;
  line-height: 1;
  filter: saturate(0.9);
}
.t-name {
  font-family: var(--font-mono);
  font-weight: 600;
  flex-shrink: 0;
  color: var(--text);
}
.t-arg {
  font-family: var(--font-mono);
  color: var(--text-soft);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  /* rtl keeps the tail (filename) visible when truncated */
  text-align: left;
}
.t-status {
  flex-shrink: 0;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.t-status.ok {
  color: var(--success);
}
.t-status.err {
  color: var(--danger);
}
.t-status.pending {
  color: var(--text-faint);
}

.tool-detail {
  padding: 0 10px 10px;
  border-top: 1px solid var(--outline-soft);
}
.section-label {
  margin-top: 8px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.6px;
  text-transform: uppercase;
  color: var(--text-faint);
}
.section-label.pending {
  font-weight: 400;
  font-style: italic;
  text-transform: none;
}
.section-code {
  margin: 4px 0 0;
  padding: 8px 10px;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.45;
  background: var(--surface-2);
  border-radius: 8px;
  max-height: 240px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text);
}
</style>
