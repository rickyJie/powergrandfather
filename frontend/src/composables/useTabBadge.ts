import { ref, watch, onBeforeUnmount, type Ref } from 'vue'

// Sync unread-notification state into the browser tab surface (title + favicon)
// so a user on another tab still sees when something new arrived. The in-app
// bell only helps when this tab is focused; users asked for the same red-dot
// signal on the tab bar (2026-07-11 feedback).
//
// Two channels, both cheap:
//   - document.title:  "(N) <base>"  — visible in wide tabs, task bars,
//     Cmd-Tab/Alt-Tab titles.
//   - favicon:         canvas-composited red dot on top of the base PNG —
//     the only visible surface for narrow / pinned tabs.
//
// Title mutation is tricky because router.afterEach also wants to write
// document.title on every route change. Sharing a module-level `baseTitle`
// ref makes this composable the SOLE writer to `document.title`: the router
// calls setBaseTitle() with the unbadged label and the watcher re-composes.

const baseTitle = ref<string>(
  typeof document !== 'undefined' ? document.title : ''
)

export function setBaseTitle(t: string) {
  baseTitle.value = t
}

export function useTabBadge(unread: Ref<number>, permPending?: Ref<boolean>) {
  if (typeof document === 'undefined') return

  const link = document.querySelector<HTMLLinkElement>("link[rel~='icon']")
  const originalFaviconHref = link?.href ?? null

  // Load the base favicon into an Image once so we can composite over it on
  // demand. Same-origin PNG → canvas is not tainted, toDataURL is legal.
  let baseImage: HTMLImageElement | null = null
  let baseImageLoaded = false
  if (link?.href) {
    baseImage = new Image()
    baseImage.onload = () => { baseImageLoaded = true; apply() }
    baseImage.src = link.href
  }

  function drawBadged(color: string): string | null {
    if (!baseImage || !baseImageLoaded) return null
    const size = 64
    const canvas = document.createElement('canvas')
    canvas.width = size
    canvas.height = size
    const ctx = canvas.getContext('2d')
    if (!ctx) return null
    ctx.drawImage(baseImage, 0, 0, size, size)
    const r = size * 0.28
    const cx = size - r - 2
    const cy = r + 2
    ctx.beginPath()
    ctx.arc(cx, cy, r, 0, Math.PI * 2)
    ctx.fillStyle = color
    ctx.fill()
    // Thin white halo so the dot stays legible on any favicon background.
    ctx.lineWidth = 3
    ctx.strokeStyle = 'rgba(255,255,255,0.9)'
    ctx.stroke()
    return canvas.toDataURL('image/png')
  }

  function apply() {
    const n = unread.value
    const perm = !!permPending?.value
    const base = baseTitle.value || document.title
    if (n > 0 || perm) {
      // `[!]` prefix when any session is blocked waiting on permission —
      // distinct from the count-only `(N)` prefix so a fullscreen user
      // eyeballing the tab bar can distinguish "new message" from
      // "someone is stuck". `[!]` sits BEFORE the count so it wins the
      // truncation race in narrow tabs.
      const countPart = n > 0 ? (n > 99 ? '(99+)' : `(${n})`) : ''
      const prefix = perm
        ? (countPart ? `[!] ${countPart}` : '[!]')
        : countPart
      document.title = `${prefix} ${base}`.trim()
      // Amber for permission-pending, red for pure unread — same palette
      // as the in-app bell dot variant so both surfaces stay in sync.
      const color = perm ? '#d97706' : '#c94f4f'
      const dataUrl = drawBadged(color)
      if (link && dataUrl) link.href = dataUrl
    } else {
      document.title = base
      if (link && originalFaviconHref) link.href = originalFaviconHref
    }
  }

  const stopUnread = watch(unread, apply, { immediate: true })
  const stopBase = watch(baseTitle, apply)
  const stopPerm = permPending ? watch(permPending, apply) : null

  onBeforeUnmount(() => {
    stopUnread()
    stopBase()
    if (stopPerm) stopPerm()
    document.title = baseTitle.value
    if (link && originalFaviconHref) link.href = originalFaviconHref
  })
}
