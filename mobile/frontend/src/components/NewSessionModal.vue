<script setup lang="ts">
import { ref, computed } from "vue";
import { showToast } from "vant";
import { sessionsApi } from "@/api/sessions";
import { useSessionsStore } from "@/stores/sessions";

const sessionsStore = useSessionsStore();
// Recent working dirs, most-frequent first — one tap to reuse.
const recentCwds = computed(() => {
  const counts = new Map<string, number>();
  for (const s of sessionsStore.items) {
    if (s.cwd) counts.set(s.cwd, (counts.get(s.cwd) ?? 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map((e) => e[0]);
});

interface Props {
  show: boolean;
}
interface Emits {
  (e: "update:show", val: boolean): void;
  (e: "created", sid: string): void;
}

const props = defineProps<Props>();
const emit = defineEmits<Emits>();

// Only agents registered in the backend registry are accepted — `bash` is
// not one of them (it 400s). Codex is gated by CSM_ENABLE_CODEX server-side;
// if disabled, create returns a clear detail message we surface below.
const adapter = ref<"claude" | "codex">("claude");
const cwd = ref("");
const initialPrompt = ref("");
const submitting = ref(false);

const adapterOptions = [
  { text: "Claude", value: "claude" },
  { text: "Codex", value: "codex" },
];

const canSubmit = computed(() => cwd.value.trim().length > 0 && !submitting.value);

async function submit() {
  if (!canSubmit.value) return;
  submitting.value = true;
  try {
    const body: Parameters<typeof sessionsApi.create>[0] = {
      cwd: cwd.value.trim(),
      agent: adapter.value,
    };
    if (initialPrompt.value.trim()) {
      body.initial_prompt = initialPrompt.value.trim();
    }
    const row = await sessionsApi.create(body);
    showToast({ message: "Session created", type: "success" });
    emit("created", row.id);
    emit("update:show", false);
    // Reset for next open
    cwd.value = "";
    initialPrompt.value = "";
  } catch (e: unknown) {
    const err = e as { response?: { status?: number; data?: { detail?: string } } };
    const msg =
      err?.response?.data?.detail ?? `Create failed (${err?.response?.status ?? "?"})`;
    showToast({ message: msg, type: "fail" });
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <van-popup
    :show="props.show"
    round
    position="bottom"
    :style="{ height: '70%' }"
    @update:show="(v: boolean) => emit('update:show', v)"
  >
    <div class="modal">
      <div class="modal-header">
        <h3>New Session</h3>
        <van-icon name="cross" size="20" @click="emit('update:show', false)" />
      </div>
      <van-cell-group inset>
        <van-field
          v-model="adapter"
          label="Adapter"
          readonly
          is-link
          @click="
            () => {
              /* picker inline below */
            }
          "
        >
          <template #input>
            <van-radio-group v-model="adapter" direction="horizontal">
              <van-radio
                v-for="o in adapterOptions"
                :key="o.value"
                :name="o.value"
              >
                {{ o.text }}
              </van-radio>
            </van-radio-group>
          </template>
        </van-field>
        <van-field
          v-model="cwd"
          label="Working dir"
          placeholder="/absolute/path/to/project"
          clearable
        />
        <div v-if="recentCwds.length" class="recent-cwds">
          <span
            v-for="c in recentCwds"
            :key="c"
            class="cwd-chip"
            @click="cwd = c"
          >
            {{ c.split("/").slice(-2).join("/") }}
          </span>
        </div>
        <van-field
          v-model="initialPrompt"
          label="Initial prompt"
          type="textarea"
          rows="2"
          :autosize="{ maxHeight: 160 }"
          placeholder="(optional) sent as first user message"
        />
      </van-cell-group>
      <div class="modal-footer">
        <van-button
          type="primary"
          block
          :loading="submitting"
          :disabled="!canSubmit"
          @click="submit"
        >
          Create
        </van-button>
      </div>
    </div>
  </van-popup>
</template>

<style scoped>
.modal {
  padding: 16px 0;
  display: flex;
  flex-direction: column;
  height: 100%;
}
.modal-header {
  padding: 0 16px 12px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--border);
}
.modal-header h3 {
  margin: 0;
}
.modal-footer {
  margin-top: auto;
  padding: 16px;
}
.recent-cwds {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 16px;
}
.cwd-chip {
  font-size: 11px;
  font-family: var(--font-mono);
  padding: 3px 10px;
  border-radius: 12px;
  background: var(--pgf-accent-soft, rgba(79, 70, 229, 0.1));
  color: var(--van-primary-color, var(--accent));
}
</style>
