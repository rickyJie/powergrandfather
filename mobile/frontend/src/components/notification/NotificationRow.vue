<script setup lang="ts">
import { computed, ref } from "vue";
import type { NotificationItem } from "@/api/notifications";

interface Props {
  item: NotificationItem;
}
const props = defineProps<Props>();

const expanded = ref(false);

const TYPE_LABELS: Record<string, string> = {
  new_message: "New message",
  auto_needs_review: "Needs review",
  session_crashed: "Session crashed",
  auto_run_failed: "Run failed",
  token_warning: "Token warning",
  port_conflict: "Port conflict",
  mission_done: "Mission done",
};
const typeLabel = computed(
  () => TYPE_LABELS[props.item.type] || props.item.type
);
// Severity colour for the type chip.
const typeColor = computed(() => {
  switch (props.item.type) {
    case "session_crashed":
    case "auto_run_failed":
      return "danger";
    case "token_warning":
    case "port_conflict":
    case "auto_needs_review":
      return "warning";
    case "mission_done":
      return "success";
    default:
      return "primary";
  }
});

const iconForType = computed(() => {
  const t = props.item.type;
  if (t.includes("mission") || t.includes("workflow")) return "records";
  if (t.includes("token") || t.includes("budget")) return "gold-coin-o";
  if (t.includes("port")) return "eye-o";
  if (t.includes("supervisor")) return "user-o";
  if (t.includes("session")) return "chat-o";
  return "info-o";
});

const relTime = computed(() => {
  const then = new Date(props.item.ts).getTime();
  const diff = Date.now() - then;
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
});
</script>

<template>
  <div :class="['nrow', { unread: !item.read }]">
    <van-icon :name="iconForType" size="18" class="nicon" />
    <div class="ncontent">
      <div class="ntitle">
        {{ item.title }}
        <span v-if="!item.read" class="dot" />
      </div>
      <div
        v-if="item.body"
        :class="['nbody', { expanded }]"
        @click.stop="expanded = !expanded"
      >
        {{ item.body }}
      </div>
      <div class="nmeta">
        <van-tag :type="typeColor as any" size="medium">{{ typeLabel }}</van-tag>
        <span class="nts">{{ relTime }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.nrow {
  display: flex;
  gap: 10px;
  padding: 10px 12px;
  align-items: flex-start;
  background: var(--van-background);
  border-bottom: 1px solid var(--border);
}
.nrow.unread {
  background: var(--van-background-2, var(--canvas));
}
.nicon {
  flex-shrink: 0;
  margin-top: 2px;
  color: var(--van-primary-color, var(--accent));
}
.ncontent {
  flex: 1;
  min-width: 0;
}
.ntitle {
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 6px;
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--van-danger-color, var(--van-danger-color));
  display: inline-block;
  flex-shrink: 0;
}
.nbody {
  margin-top: 2px;
  font-size: 13px;
  color: var(--van-text-color-2, var(--ink-2));
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  line-height: 1.4;
  white-space: pre-wrap;
}
.nbody.expanded {
  -webkit-line-clamp: unset;
  line-clamp: unset;
  display: block;
}
.nmeta {
  margin-top: 4px;
  display: flex;
  gap: 8px;
  font-size: 11px;
  color: var(--van-text-color-3, var(--ink-mute));
}
.ntype {
  padding: 0 4px;
  background: var(--border);
  border-radius: 3px;
}
</style>
