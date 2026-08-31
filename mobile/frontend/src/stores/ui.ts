import { defineStore } from "pinia";
import { ref } from "vue";

// UI shell state for the chat-first model: which session the immersive chat is
// showing, and whether the session drawer is open. The active session id is
// persisted so reopening the app lands you back in the last conversation
// (ChatGPT-style continuity) instead of an empty home.

const LAST_SID_KEY = "csm_last_sid";

function loadLastSid(): string | null {
  try {
    return localStorage.getItem(LAST_SID_KEY);
  } catch {
    return null;
  }
}

export const useUiStore = defineStore("ui", () => {
  const activeSid = ref<string | null>(loadLastSid());
  const drawerOpen = ref(false);

  function setActive(sid: string | null) {
    activeSid.value = sid;
    try {
      if (sid) localStorage.setItem(LAST_SID_KEY, sid);
      else localStorage.removeItem(LAST_SID_KEY);
    } catch {
      /* private mode — non-fatal, just lose continuity */
    }
  }

  function openDrawer() {
    drawerOpen.value = true;
  }
  function closeDrawer() {
    drawerOpen.value = false;
  }
  function toggleDrawer() {
    drawerOpen.value = !drawerOpen.value;
  }

  return {
    activeSid,
    drawerOpen,
    setActive,
    openDrawer,
    closeDrawer,
    toggleDrawer,
  };
});

/** Best-effort haptic tick for key actions (send / interrupt / swipe-commit).
 *  navigator.vibrate is a no-op on iOS Safari / unsupported WebViews. */
export function haptic(ms = 12) {
  try {
    navigator.vibrate?.(ms);
  } catch {
    /* unsupported */
  }
}
