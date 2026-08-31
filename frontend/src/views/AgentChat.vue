<script setup lang="ts">
import { apiErrorMessage } from '../lib/apiError'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { agentsApi, buildAgentWsUrl, type AgentConversation } from '../api/agents'
import { getConvCache } from '../stores/agents'
import ChatMessage from '../components/ChatMessage.vue'
import ToolUseBlock from '../components/ToolUseBlock.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'

const route = useRoute()
const router = useRouter()

const cid = computed(() => String(route.params.cid))

interface TextItem {
  kind: 'text'
  id: string
  role: 'user' | 'assistant' | 'system'
  text: string
  ts: string
}
interface ToolItem {
  kind: 'tool'
  id: string  // tool_id
  tool: string
  input: any
  ts: string
  result: { ok: boolean; preview: string } | null
}
type Item = TextItem | ToolItem

const items = ref<Item[]>([])
const status = ref<string>('connecting')
const conv = ref<AgentConversation | null>(null)
const err = ref('')

const input = ref('')
const sending = ref(false)
const interrupting = ref(false)
const composerEl = ref<HTMLTextAreaElement | null>(null)
let ws: WebSocket | null = null
let scrollEl: HTMLDivElement | null = null

// Smart scroll-pin: only auto-scroll when user is near the bottom. When they
// scroll up to read history, incoming messages must not yank them back.
const isAtBottom = ref(true)
const newSinceScroll = ref(0)
const STICKY_THRESHOLD_PX = 80

// Reconnect bookkeeping.
let reconnectAttempt = 0
let reconnectTimer: number | null = null
let manualClose = false

// Track optimistic-echo ids so JSONL replay (which re-emits the same user
// message a few hundred ms later) doesn't double-up.
const pendingUserKeys = ref<Set<string>>(new Set())
function userEchoKey(text: string): string {
  return text.trim().slice(0, 200)
}

let nextId = 1
function newId(): string {
  return `i${nextId++}`
}

function setScrollRef(el: unknown) {
  if (!el) { scrollEl = null; return }
  scrollEl = el as HTMLDivElement
  scrollEl.addEventListener('scroll', onScroll, { passive: true })
  scrollToBottom(true)
}

function onScroll() {
  if (!scrollEl) return
  const dist = scrollEl.scrollHeight - (scrollEl.scrollTop + scrollEl.clientHeight)
  const atBottom = dist <= STICKY_THRESHOLD_PX
  isAtBottom.value = atBottom
  if (atBottom) newSinceScroll.value = 0
}

function scrollToBottom(force = false) {
  nextTick(() => {
    if (!scrollEl) return
    if (force || isAtBottom.value) {
      scrollEl.scrollTop = scrollEl.scrollHeight
      newSinceScroll.value = 0
    }
  })
}

function jumpToBottom() {
  isAtBottom.value = true
  scrollToBottom(true)
}

function noteNewItem() {
  if (isAtBottom.value) {
    scrollToBottom()
  } else {
    newSinceScroll.value++
  }
}

function appendText(role: 'user' | 'assistant' | 'system', text: string, ts: string) {
  // De-dupe optimistic user echo against the JSONL-replayed copy.
  if (role === 'user') {
    const k = userEchoKey(text)
    if (pendingUserKeys.value.has(k)) {
      pendingUserKeys.value.delete(k)
      return
    }
  }
  // Merge consecutive assistant_text chunks into the most recent bubble so
  // streaming-style multi-block messages read as one paragraph. Assistant
  // only: system notes are discrete events, and merging them ran two
  // independent ones ("Agent A finished" / "Agent B finished [failed]") into a
  // single bubble carrying only the first one's timestamp.
  const last = items.value[items.value.length - 1]
  if (last && last.kind === 'text' && last.role === role && role === 'assistant') {
    last.text += (last.text.endsWith('\n') ? '' : '\n') + text
    if (isAtBottom.value) scrollToBottom()
    return
  }
  items.value.push({ kind: 'text', id: newId(), role, text, ts })
  noteNewItem()
}

function appendTool(toolId: string, tool: string, inputArg: any, ts: string) {
  items.value.push({
    kind: 'tool',
    id: toolId || newId(),
    tool,
    input: inputArg,
    ts,
    result: null,
  })
  noteNewItem()
}

function setToolResult(toolId: string, ok: boolean, preview: string) {
  // Find by matching id; if no match (shouldn't happen, but defensively),
  // attach a system note instead so we never silently drop output.
  for (let i = items.value.length - 1; i >= 0; i--) {
    const it = items.value[i]
    if (it.kind === 'tool' && it.id === toolId) {
      it.result = { ok, preview }
      return
    }
  }
  appendText('system', `tool_result for unknown ${toolId}: ${preview.slice(0, 200)}`, '')
}

function clearReconnectTimer() {
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
}

function scheduleReconnect() {
  if (manualClose) return
  clearReconnectTimer()
  const delay = Math.min(15000, 500 * 2 ** Math.min(reconnectAttempt, 5))
  reconnectAttempt++
  status.value = 'reconnecting'
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null
    connectWs()
  }, delay)
}

async function connectWs() {
  if (ws) return
  manualClose = false
  status.value = reconnectAttempt > 0 ? 'reconnecting' : 'connecting'
  const url = buildAgentWsUrl(cid.value)
  const socket = new WebSocket(url)
  ws = socket
  socket.onopen = () => {
    if (ws !== socket) return
    status.value = 'connected'
    reconnectAttempt = 0
  }
  socket.onclose = (ev) => {
    // A close callback from the previous cid may arrive after the next
    // socket is already installed. It must not clear or reconnect over it.
    if (ws !== socket) return
    ws = null
    // Terminal close codes: 4404 = conversation gone, 4401/4403 = unauthorized.
    // Retrying can't recover these — without this gate a deleted conversation
    // reconnects forever every ~15s (and re-reconnects on every visibility
    // change). Stop and surface the ended state. Mirrors useTerminalManager's
    // 4401/4403/4404 handling.
    if (ev.code === 4404 || ev.code === 4401 || ev.code === 4403) {
      manualClose = true // reuse the "do not auto-reconnect" flag (gates scheduleReconnect + onVisibility)
      clearReconnectTimer()
      status.value = 'disconnected'
      if (!err.value) {
        err.value = ev.code === 4404
          ? 'This conversation has ended.'
          : 'Not authorized for this conversation.'
      }
      return
    }
    status.value = 'disconnected'
    scheduleReconnect()
  }
  socket.onerror = () => {
    if (ws === socket) status.value = 'error'
  }
  socket.onmessage = (ev) => {
    if (ws !== socket) return
    let msg: any
    try { msg = JSON.parse(ev.data) } catch { return }
    handleWsMessage(msg)
  }
}

function handleWsMessage(msg: any) {
  switch (msg.type) {
    case 'session_status':
      status.value = msg.status || status.value
      break
    case 'history':
      // A history frame is the WHOLE transcript, sent once per connection
      // (`agents.py` `on_replay`: "ship the whole replay as one frame"), so it
      // REPLACES what is on screen. It used to be applied on top of it, and
      // `items` was only ever emptied by `watch(cid)` — so every reconnect to
      // the SAME conversation appended a second copy of the entire history,
      // and because appendText() merges consecutive assistant chunks, the
      // replayed text was concatenated into the last bubble rather than even
      // being visibly separate.
      //
      // `pendingUserKeys` has to go with it: those keys exist to swallow the
      // JSONL echo of an optimistic bubble, and the bubbles are being cleared
      // here. Leaving them would make the replay's copy of that message get
      // swallowed too, so a message the user had actually sent would vanish.
      // The replay is authoritative — anything it doesn't contain never landed.
      items.value = []
      pendingUserKeys.value.clear()
      newSinceScroll.value = 0
      for (const ev of (msg.events || [])) handleWsMessage(ev)
      scrollToBottom(true)
      break
    case 'user_message':
      // `injected` = role "user" but machine-filed (skill preamble, compaction
      // recap, a headless `claude -p` prompt). Same call as mobile: keep it in
      // the transcript, but don't attribute it to the user.
      appendText(msg.injected ? 'system' : 'user', msg.text || '', msg.ts || '')
      break
    case 'assistant_text':
      appendText('assistant', msg.text || '', msg.ts || '')
      break
    case 'tool_use_start':
      appendTool(msg.tool_id || '', msg.tool || '', msg.input, msg.ts || '')
      break
    case 'tool_use_result':
      setToolResult(msg.tool_id || '', !!msg.ok, msg.preview || '')
      break
    case 'system_note':
      appendText('system', msg.text || '', msg.ts || '')
      break
    case 'error':
      err.value = msg.detail || 'ws error'
      break
  }
}

function onVisibility() {
  if (document.visibilityState !== 'visible') return
  // Page came back to foreground — if disconnected, retry now instead of
  // waiting out the backoff.
  if (!ws && !manualClose) {
    clearReconnectTimer()
    reconnectAttempt = 0
    connectWs()
  }
}

async function loadConversation() {
  try {
    conv.value = await agentsApi.getConversation(cid.value)
  } catch (e) {
    err.value = apiErrorMessage(e)
  }
}

async function send() {
  const text = input.value.trim()
  if (!text) return
  sending.value = true
  err.value = ''
  // Optimistic echo — show the message immediately. JSONL replay (~200ms
  // later) is de-duped via pendingUserKeys; if the de-dupe window misses,
  // the worst case is one extra bubble, not a missing one.
  pendingUserKeys.value.add(userEchoKey(text))
  items.value.push({
    kind: 'text',
    id: newId(),
    role: 'user',
    text,
    ts: new Date().toISOString(),
  })
  noteNewItem()
  const sentText = text
  input.value = ''
  try {
    await agentsApi.sendMessage(cid.value, sentText)
  } catch (e) {
    // Roll back optimistic echo on failure so the user knows it didn't send.
    pendingUserKeys.value.delete(userEchoKey(sentText))
    const lastIdx = items.value.length - 1
    const last = items.value[lastIdx]
    if (last && last.kind === 'text' && last.role === 'user' && last.text === sentText) {
      items.value.splice(lastIdx, 1)
    }
    input.value = sentText  // restore so user can retry
    err.value = apiErrorMessage(e)
  } finally {
    sending.value = false
  }
}

async function interrupt() {
  if (interrupting.value) return
  interrupting.value = true
  err.value = ''
  try {
    await agentsApi.interrupt(cid.value)
  } catch (e) {
    err.value = apiErrorMessage(e)
  } finally {
    interrupting.value = false
  }
}

// "Thinking" indicator: last item is a user message and no assistant content
// has arrived since. Also surfaces during tool loops that haven't emitted text.
const isThinking = computed(() => {
  if (status.value === 'disconnected' || status.value === 'reconnecting') return false
  const last = items.value[items.value.length - 1]
  if (!last) return false
  if (last.kind === 'text' && last.role === 'user') return true
  if (last.kind === 'tool' && last.result === null) return true
  return false
})

function onKeydown(e: KeyboardEvent) {
  // IME composition (Chinese / Japanese / Korean input method): the Enter
  // key during composition is for selecting a candidate, NOT for sending.
  // `isComposing` is the official guard; `e.keyCode === 229` is the legacy
  // signal in older browsers.
  if (e.key !== 'Enter' || e.shiftKey) return
  if ((e as any).isComposing || (e as any).keyCode === 229) return
  e.preventDefault()
  send()
}

const showEndConfirm = ref(false)
function endConversation() {
  showEndConfirm.value = true
}
async function doEndConversation() {
  showEndConfirm.value = false
  try {
    await agentsApi.endConversation(cid.value)
    router.push('/agents')
  } catch (e) {
    err.value = apiErrorMessage(e)
  }
}

function teardownWs() {
  manualClose = true
  clearReconnectTimer()
  if (ws) {
    const socket = ws
    ws = null
    socket.onopen = null
    socket.onmessage = null
    socket.onerror = null
    socket.onclose = null
    try { socket.close() } catch {}
  }
}

onMounted(async () => {
  await loadConversation()
  await connectWs()
  document.addEventListener('visibilitychange', onVisibility)
  nextTick(() => composerEl.value?.focus())
})

onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', onVisibility)
  if (scrollEl) scrollEl.removeEventListener('scroll', onScroll)
  teardownWs()
})

watch(cid, async () => {
  teardownWs()
  items.value = []
  pendingUserKeys.value.clear()
  reconnectAttempt = 0
  await loadConversation()
  await connectWs()
})
</script>

<template>
  <div class="chat-page">
    <div class="toolbar">
      <button class="ghost" @click="router.push('/agents')">← Back to Agents</button>
      <div class="title">
        <span class="serif">{{ conv?.title || 'New conversation' }}</span>
        <small class="mono" v-if="conv">cid {{ conv.id.slice(0,8) }} · session {{ conv.session_id.slice(0,8) }}</small>
      </div>
      <div class="right">
        <span class="status-pill" :class="status">{{ status }}</span>
        <button
          v-if="isThinking"
          class="ghost stop-btn"
          @click="interrupt"
          :disabled="interrupting"
          title="Send Ctrl-C to interrupt the current generation / tool call"
        >■ Stop</button>
        <button class="danger" @click="endConversation">End conversation</button>
      </div>
    </div>

    <div v-if="err" class="err-banner">
      <span style="flex:1">{{ err }}</span>
      <button class="ghost-x" @click="err = ''" aria-label="Close">×</button>
    </div>

    <div class="msgs-wrap">
      <div
        :ref="setScrollRef"
        class="msgs"
        role="log"
        aria-live="polite"
        aria-atomic="false"
        aria-label="Conversation messages"
      >
        <template v-for="it in items" :key="it.id">
          <ChatMessage
            v-if="it.kind === 'text'"
            :role="it.role"
            :text="it.text"
            :ts="it.ts"
          />
          <ToolUseBlock
            v-else
            :tool="it.tool"
            :input="it.input"
            :result="it.result"
            :ts="it.ts"
          />
        </template>
        <div v-if="isThinking" class="thinking" aria-live="polite">
          <span class="dot"></span><span class="dot"></span><span class="dot"></span>
          <span class="thinking-label">Claude is thinking…</span>
        </div>
        <div v-if="!items.length && !isThinking" class="empty">
          <div class="empty-icon">💬</div>
          <div>Type below to start chatting with "{{ conv?.title || 'agent' }}"</div>
          <div class="empty-hint">Enter to send · Shift+Enter for newline · IME candidate selection won't send accidentally</div>
        </div>
      </div>
      <button
        v-if="!isAtBottom"
        class="jump-bottom"
        @click="jumpToBottom"
        :title="newSinceScroll > 0 ? `${newSinceScroll} new messages` : 'Jump to bottom'"
      >
        ↓ <span v-if="newSinceScroll > 0">{{ newSinceScroll }} new</span>
      </button>
    </div>

    <div class="composer">
      <textarea
        ref="composerEl"
        v-model="input"
        @keydown="onKeydown"
        :disabled="sending || status === 'disconnected' || status === 'reconnecting'"
        rows="2"
        :placeholder="status === 'reconnecting' ? 'Reconnecting…' : 'Type a message · Enter to send · Shift+Enter for newline'"
        aria-label="Message input"
      ></textarea>
      <button class="primary" @click="send" :disabled="sending || !input.trim()">
        {{ sending ? '…' : 'Send' }}
      </button>
    </div>

    <ConfirmDialog
      :open="showEndConfirm"
      title="End this conversation?"
      message="The Claude session will be closed. History is kept — you can reopen it from the agent card's History list."
      confirm-text="End"
      :danger="true"
      @confirm="doEndConversation"
      @cancel="showEndConfirm = false"
    />
  </div>
</template>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;          /* allow children flex:1 to actually shrink */
  background: var(--canvas);
}
.toolbar, .err-banner, .composer { flex: 0 0 auto; }
.msgs-wrap { flex: 1 1 auto; min-height: 0; }
.toolbar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 10px 20px;
  background: var(--card);
  border-bottom: 1px solid var(--border);
}
.toolbar .title { flex: 1; display: flex; flex-direction: column; gap: 2px; }
.toolbar .title .serif { font-family: 'Newsreader', serif; font-size: 16px; }
.toolbar .title small { font-size: 10px; color: var(--ink-mute); }
.toolbar .right { display: flex; gap: 8px; align-items: center; }
.toolbar button.ghost {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--ink);
}
.toolbar button.danger {
  background: var(--pastel-red-bg);
  color: var(--pastel-red-fg);
  border: 1px solid var(--pastel-red-fg);
}
.status-pill {
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 10px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.status-pill.connecting,
.status-pill.reconnecting { background: var(--pastel-blue-bg); color: var(--pastel-blue-fg); }
.status-pill.connected,
.status-pill.running,
.status-pill.idle,
.status-pill.waiting_input { background: var(--pastel-green-bg, #e2f0e3); color: var(--pastel-green-fg, #346538); }
.status-pill.disconnected,
.status-pill.exited,
.status-pill.crashed,
.status-pill.error { background: var(--pastel-red-bg); color: var(--pastel-red-fg); }

.stop-btn {
  font-size: 11px;
  padding: 3px 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--canvas);
  color: var(--ink);
  cursor: pointer;
}
.stop-btn:hover:not(:disabled) { background: var(--pastel-red-bg); color: var(--pastel-red-fg); border-color: var(--pastel-red-fg); }
.stop-btn:disabled { opacity: 0.5; cursor: not-allowed; }

.err-banner {
  display: flex; align-items: center; gap: 8px;
  margin: 8px 20px 0;
  padding: 8px 12px;
  background: var(--pastel-red-bg);
  color: var(--pastel-red-fg);
  border-radius: 6px;
  font-size: 12px;
}
.ghost-x {
  background: transparent; border: none; cursor: pointer;
  color: var(--pastel-red-fg); font-size: 16px; padding: 0 4px;
}

.msgs-wrap { position: relative; min-height: 0; overflow: hidden; }
.msgs {
  height: 100%;
  overflow-y: auto;
  padding: 20px max(20px, calc(50vw - 460px));
  scroll-behavior: smooth;
}
.empty {
  text-align: center;
  color: var(--ink-mute);
  font-size: 14px;
  margin-top: 80px;
  display: flex; flex-direction: column; align-items: center; gap: 8px;
}
.empty-icon { font-size: 40px; opacity: 0.4; margin-bottom: 4px; }
.empty-hint {
  font-size: 11px;
  color: var(--ink-mute);
  opacity: 0.7;
  margin-top: 4px;
}

.thinking {
  display: flex; align-items: center; gap: 8px;
  margin: 12px 0;
  padding-left: 4px;
}
.thinking .dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--ink-mute);
  animation: thinking-pulse 1.2s infinite ease-in-out;
}
.thinking .dot:nth-child(2) { animation-delay: 0.15s; }
.thinking .dot:nth-child(3) { animation-delay: 0.3s; }
.thinking-label {
  font-size: 11px; color: var(--ink-mute); margin-left: 6px;
}
@keyframes thinking-pulse {
  0%, 60%, 100% { opacity: 0.3; transform: scale(0.85); }
  30% { opacity: 1; transform: scale(1.1); }
}

.jump-bottom {
  position: absolute;
  bottom: 16px; right: 24px;
  padding: 6px 12px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  font-size: 11px;
  color: var(--ink);
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08);
}
.jump-bottom:hover { background: var(--pastel-blue-bg); }

.composer {
  display: flex;
  gap: 8px;
  padding: 12px 20px;
  background: var(--card);
  border-top: 1px solid var(--border);
}
.composer textarea {
  flex: 1;
  resize: vertical;
  min-height: 44px;
  max-height: 200px;
  padding: 8px 10px;
  font-family: inherit;
  font-size: 13px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--card);
  color: var(--ink);
}
.composer button.primary { align-self: flex-end; min-width: 72px; }
</style>
