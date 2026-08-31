import { onBeforeUnmount, onMounted, ref } from "vue";

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

/**
 * PWA install prompt state. Chrome / Edge on Android fire
 * `beforeinstallprompt`; iOS Safari does not — the returned `iosHint`
 * flag lets views show a manual "Share → Add to Home Screen" hint
 * instead of a native button.
 */
export function useInstallPrompt() {
  const promptEvent = ref<BeforeInstallPromptEvent | null>(null);
  const iosHint = ref<boolean>(false);

  function isIos() {
    const ua = navigator.userAgent.toLowerCase();
    // iPad, iPhone, iPod
    return /iphone|ipad|ipod/.test(ua) && !/crios|fxios/.test(ua);
  }

  function isStandalone() {
    return (
      window.matchMedia?.("(display-mode: standalone)").matches ||
      (window.navigator as unknown as { standalone?: boolean }).standalone ===
        true
    );
  }

  const capture = (e: Event) => {
    e.preventDefault();
    promptEvent.value = e as BeforeInstallPromptEvent;
  };

  onMounted(() => {
    if (isStandalone()) return; // already installed
    if (isIos()) {
      iosHint.value = true;
      return;
    }
    window.addEventListener("beforeinstallprompt", capture);
  });

  onBeforeUnmount(() => {
    window.removeEventListener("beforeinstallprompt", capture);
  });

  async function promptInstall(): Promise<"accepted" | "dismissed" | "unavailable"> {
    if (!promptEvent.value) return "unavailable";
    await promptEvent.value.prompt();
    const { outcome } = await promptEvent.value.userChoice;
    promptEvent.value = null;
    return outcome;
  }

  return { promptEvent, iosHint, promptInstall };
}
