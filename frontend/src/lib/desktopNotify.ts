// Desktop (OS-level) notifications via the Service Worker's showNotification.
//
// Driven by the notification WS (stores/notifications.ts): when a high-signal
// notification arrives and the app is NOT in the foreground, pop an OS
// notification whose click focuses the app and routes to the session. The
// in-app panel/toasts cover the focused case, so we only pop when unfocused to
// avoid double-notifying.
//
// Constraints (surfaced in the Settings/panel toggle):
//   - Requires a secure context (HTTPS or localhost) — over a plain-http LAN
//     IP the Notification API is unavailable. Use the SSH tunnel's localhost.
//   - The app must be running (tab / installed PWA window open, even
//     backgrounded). Notifications while fully closed would need Push+VAPID,
//     which the single-process local design deliberately omits.
import type { NotificationRow } from '../api/notifications'

const LS_KEY = 'csm.desktopNotify.enabled'

// Every current NotificationType is high-signal; kept as an explicit allow-set
// so a future low-signal type doesn't start popping OS notifications by
// default. Mirrors backend csm/models/notification.py NotificationType.
const HIGH_SIGNAL = new Set<string>([
  'new_message',
  'auto_needs_review',
  'session_crashed',
  'auto_run_failed',
  'mission_done',
])

export type DesktopNotifState =
  | 'granted' | 'denied' | 'default' | 'unsupported' | 'insecure'

export function desktopNotifSupported(): boolean {
  return typeof window !== 'undefined'
    && 'Notification' in window
    && 'serviceWorker' in navigator
}

/**
 * True when this document runs as an installed app window (PWA) instead of a
 * browser tab. iOS Safari predates the display-mode media query and exposes
 * `navigator.standalone` instead.
 */
export function isStandaloneDisplay(): boolean {
  if (typeof window === 'undefined') return false
  const installed = ['standalone', 'fullscreen', 'minimal-ui', 'window-controls-overlay']
  if (installed.some((mode) => window.matchMedia(`(display-mode: ${mode})`).matches)) {
    return true
  }
  return Boolean((navigator as Navigator & { standalone?: boolean }).standalone)
}

// This document's Client id, learned from the SW during the handshake below.
// Stamped onto every notification so the click handler can focus THIS window
// rather than guessing between the installed app and a stale browser tab.
let selfClientId: string | null = null

/**
 * Tell the Service Worker whether this window is the installed app or a
 * browser tab, and learn our own Client id from its reply.
 *
 * Why: `clients.matchAll()` in the SW returns the PWA window and every stale
 * same-origin browser tab in an order we don't control, and a Client exposes
 * nothing that distinguishes them — focusing the wrong one drops the user in a
 * browser instead of the app. The SW persists what we report here (see
 * public/notif-click-sw.js), because it is routinely killed between
 * showNotification() and the click.
 */
export async function registerDisplayModeWithSW(): Promise<void> {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return
  try {
    const reg = await navigator.serviceWorker.ready
    const sw = reg.active
    if (!sw) return
    const channel = new MessageChannel()
    channel.port1.onmessage = (e: MessageEvent) => {
      const d = e.data
      if (d && typeof d.clientId === 'string') selfClientId = d.clientId
    }
    sw.postMessage(
      { type: 'csm-display-mode', standalone: isStandaloneDisplay() },
      [channel.port2],
    )
  } catch {
    // No SW / not ready — the click handler falls back to display-mode
    // matching, and failing that to openWindow().
  }
}

/** Current permission/availability state for the Settings UI. */
export function desktopNotifState(): DesktopNotifState {
  if (!desktopNotifSupported()) return 'unsupported'
  if (!window.isSecureContext) return 'insecure'
  return Notification.permission as DesktopNotifState
}

export function isDesktopNotifEnabled(): boolean {
  try { return localStorage.getItem(LS_KEY) === '1' } catch { return false }
}

export function setDesktopNotifEnabled(on: boolean): void {
  try { localStorage.setItem(LS_KEY, on ? '1' : '0') } catch { /* ignore */ }
}

/** Request OS permission (must be called from a user gesture). */
export async function requestDesktopNotifPermission(): Promise<DesktopNotifState> {
  if (!desktopNotifSupported()) return 'unsupported'
  if (!window.isSecureContext) return 'insecure'
  try {
    return (await Notification.requestPermission()) as DesktopNotifState
  } catch {
    return Notification.permission as DesktopNotifState
  }
}

function appHasFocus(): boolean {
  return typeof document !== 'undefined'
    && !document.hidden
    && document.hasFocus()
}

function urlForNotification(n: NotificationRow): string {
  // Deep-link to the session it concerns (route is /sessions/:sid); otherwise
  // the sessions list.
  return n.session_id ? `/sessions/${n.session_id}` : '/sessions'
}

/**
 * Fire an OS notification for `n` when appropriate. No-op unless: the feature
 * is enabled, permission is granted, we're in a secure context, the type is
 * high-signal, and the app is NOT focused. Deduped per session via `tag` so a
 * chatty session collapses into one popup instead of a stream.
 */
export async function maybeDesktopNotify(n: NotificationRow): Promise<void> {
  if (!isDesktopNotifEnabled()) return
  if (!desktopNotifSupported() || !window.isSecureContext) return
  if (Notification.permission !== 'granted') return
  if (!HIGH_SIGNAL.has(n.type)) return
  if (appHasFocus()) return
  try {
    const reg = await navigator.serviceWorker.ready
    const title = n.title || 'PowerGrandFather'
    const sessionTitle =
      n.metadata && typeof n.metadata.session_title === 'string'
        ? (n.metadata.session_title as string)
        : undefined
    const body = [sessionTitle, n.body].filter(Boolean).join(' — ') || undefined
    await reg.showNotification(title, {
      body,
      // Collapse repeat pushes from the same session into one popup.
      tag: n.session_id ? `csm-sess-${n.session_id}` : `csm-notif-${n.id}`,
      renotify: true,
      icon: '/pwa-192x192.png',
      badge: '/pwa-64x64.png',
      // `clientId` / `standalone` describe the window that fired this — the SW
      // uses them to focus the installed app instead of a stale browser tab.
      data: {
        url: urlForNotification(n),
        notifId: n.id,
        sessionId: n.session_id,
        clientId: selfClientId,
        standalone: isStandaloneDisplay(),
      },
    } as NotificationOptions)
  } catch {
    // SW not ready / not installed — silently skip (in-app panel still shows it).
  }
}
