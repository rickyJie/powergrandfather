<script setup lang="ts">
import { ref, watch } from "vue";
import { sessionsApi } from "@/api/sessions";

// Self-contained "changed files + diff" bottom sheets for a session. Owns its
// own fetch state — the parent just toggles `show` with the session id.
const props = defineProps<{
  sid: string;
  show: boolean;
}>();
const emit = defineEmits<{ (e: "update:show", v: boolean): void }>();

const changesLoading = ref(false);
const changesFiles = ref<{ path: string; edit_count: number; tools: string[] }[]>([]);
const diffOpen = ref(false);
const diffLoading = ref(false);
const diffText = ref("");
const diffPath = ref("");

async function loadChanges() {
  changesLoading.value = true;
  try {
    changesFiles.value = (await sessionsApi.changes(props.sid)).files;
  } catch {
    changesFiles.value = [];
  } finally {
    changesLoading.value = false;
  }
}

async function openDiff(path: string) {
  diffPath.value = path;
  diffOpen.value = true;
  diffLoading.value = true;
  diffText.value = "";
  try {
    diffText.value = await sessionsApi.changesDiff(props.sid, path);
  } catch {
    diffText.value = "(diff unavailable)";
  } finally {
    diffLoading.value = false;
  }
}

// Fetch when the sheet opens.
watch(
  () => props.show,
  (open) => {
    if (open) loadChanges();
  }
);
</script>

<template>
  <div>
    <van-popup
      :show="show"
      round
      position="bottom"
      :style="{ height: '60%' }"
      @update:show="(v: boolean) => emit('update:show', v)"
    >
      <div class="sheet">
        <div class="sheet-head">
          <h3>Changed files</h3>
          <van-icon name="cross" size="20" @click="emit('update:show', false)" />
        </div>
        <div v-if="changesLoading" class="sheet-empty"><van-loading /></div>
        <van-empty v-else-if="!changesFiles.length" description="No file changes" />
        <van-cell-group v-else inset>
          <van-cell
            v-for="f in changesFiles"
            :key="f.path"
            :title="f.path.split('/').pop()"
            :label="f.path"
            is-link
            @click="openDiff(f.path)"
          >
            <template #value><span class="edits">{{ f.edit_count }}×</span></template>
          </van-cell>
        </van-cell-group>
      </div>
    </van-popup>

    <van-popup v-model:show="diffOpen" position="bottom" :style="{ height: '85%' }">
      <div class="sheet">
        <div class="sheet-head">
          <h3 class="mono">{{ diffPath.split("/").pop() }}</h3>
          <van-icon name="cross" size="20" @click="diffOpen = false" />
        </div>
        <div v-if="diffLoading" class="sheet-empty"><van-loading /></div>
        <pre v-else class="diff-text">{{ diffText }}</pre>
      </div>
    </van-popup>
  </div>
</template>

<style scoped>
.sheet {
  padding: 12px 0;
  height: 100%;
  overflow-y: auto;
}
.sheet-head {
  padding: 0 16px 12px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--outline-soft);
}
.sheet-head h3 {
  margin: 0;
  font-size: 15px;
  word-break: break-all;
}
.sheet-empty {
  display: grid;
  place-items: center;
  padding: 40px;
}
.mono {
  font-family: var(--font-mono);
}
.edits {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-soft);
}
.diff-text {
  padding: 8px 12px;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.4;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text);
}
</style>
