<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRegisterSW } from 'virtual:pwa-register/vue'
import { isStandaloneDisplay } from '../lib/desktopNotify'

interface BeforeInstallPromptEvent extends Event {
  prompt(): Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed'; platform: string }>
}

const installEvent = ref<BeforeInstallPromptEvent | null>(null)
const installDismissed = ref(false)
const iosHintDismissed = ref(localStorage.getItem('csm.pwa.ios-hint-dismissed') === '1')

// Shared with the notification-click SW handshake so both agree on what
// "running as the installed app" means.
const standalone = computed(() => isStandaloneDisplay())
const isIos = /iphone|ipad|ipod/i.test(navigator.userAgent)

const { needRefresh, offlineReady, updateServiceWorker } = useRegisterSW({
  onRegisterError(error) {
    console.warn('[pwa] service worker registration failed', error)
  },
})

const showInstall = computed(() =>
  Boolean(installEvent.value) && !installDismissed.value && !standalone.value,
)
const showIosHint = computed(() =>
  isIos && !standalone.value && !iosHintDismissed.value && !needRefresh.value,
)
const visible = computed(() =>
  needRefresh.value || offlineReady.value || showInstall.value || showIosHint.value,
)

function onBeforeInstallPrompt(event: Event) {
  event.preventDefault()
  installEvent.value = event as BeforeInstallPromptEvent
}

async function install() {
  const event = installEvent.value
  if (!event) return
  await event.prompt()
  const choice = await event.userChoice
  if (choice.outcome !== 'accepted') installDismissed.value = true
  installEvent.value = null
}

function dismiss() {
  if (needRefresh.value) needRefresh.value = false
  if (offlineReady.value) offlineReady.value = false
  installDismissed.value = true
  if (showIosHint.value) {
    iosHintDismissed.value = true
    localStorage.setItem('csm.pwa.ios-hint-dismissed', '1')
  }
}

onMounted(() => window.addEventListener('beforeinstallprompt', onBeforeInstallPrompt))
onBeforeUnmount(() => window.removeEventListener('beforeinstallprompt', onBeforeInstallPrompt))
</script>

<template>
  <aside v-if="visible" class="pwa-status" role="status" aria-live="polite">
    <div class="pwa-status-copy">
      <strong v-if="needRefresh">A new version of PowerGrandFather is available</strong>
      <strong v-else-if="showInstall">Install to your home screen</strong>
      <strong v-else-if="showIosHint">Add to your iPhone / iPad home screen</strong>
      <strong v-else>The app is ready</strong>

      <span v-if="needRefresh">Refresh when you're not mid-terminal — your current session won't be interrupted.</span>
      <span v-else-if="showInstall">Once installed it launches in its own window.</span>
      <span v-else-if="showIosHint">Tap Safari's share button, then "Add to Home Screen".</span>
      <span v-else>The static UI is cached; live features still need a connection to the host.</span>
    </div>
    <div class="pwa-status-actions">
      <button v-if="needRefresh" class="primary" @click="updateServiceWorker(true)">Refresh</button>
      <button v-else-if="showInstall" class="primary" @click="install">Install</button>
      <button class="quiet" @click="dismiss">Later</button>
    </div>
  </aside>
</template>

<style scoped>
.pwa-status {
  position: fixed;
  z-index: 120;
  right: 18px;
  bottom: 18px;
  width: min(420px, calc(100vw - 36px));
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 13px 14px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--card);
  color: var(--ink);
  box-shadow: var(--shadow-md);
}
.pwa-status-copy { flex: 1; min-width: 0; display: grid; gap: 2px; }
.pwa-status-copy strong { font-family: 'Newsreader', serif; font-size: 15px; }
.pwa-status-copy span { color: var(--ink-mute); font-size: 11px; line-height: 1.4; }
.pwa-status-actions { display: flex; gap: 6px; flex-shrink: 0; }
.pwa-status-actions button { padding: 6px 9px; }
.pwa-status-actions .quiet { background: transparent; color: var(--ink-mute); }

@media (max-width: 640px) {
  .pwa-status {
    right: 10px;
    bottom: calc(64px + env(safe-area-inset-bottom));
    width: calc(100vw - 20px);
    align-items: flex-start;
  }
  .pwa-status-actions { flex-direction: column; }
  .pwa-status-actions button { min-height: 38px; }
}
</style>
