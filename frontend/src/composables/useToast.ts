/**
 * Tiny global toast queue. Replaces native `alert()` everywhere.
 *
 * Why a composable + module-level state (not pinia/vuex): we have exactly
 * one queue, no SSR, and don't want to wire DI for what's a 30-line module.
 */
import { ref } from 'vue'

export type ToastKind = 'info' | 'success' | 'warn' | 'error'

export interface Toast {
  id: number
  kind: ToastKind
  msg: string
  ttlMs: number
}

const toasts = ref<Toast[]>([])
let _seq = 0

function push(kind: ToastKind, msg: string, ttlMs = 4000): number {
  const id = ++_seq
  toasts.value.push({ id, kind, msg, ttlMs })
  if (ttlMs > 0) {
    window.setTimeout(() => dismiss(id), ttlMs)
  }
  return id
}

function dismiss(id: number) {
  toasts.value = toasts.value.filter(t => t.id !== id)
}

export function useToast() {
  return {
    toasts,
    info:    (msg: string, ttl?: number): number => push('info', msg, ttl),
    success: (msg: string, ttl?: number): number => push('success', msg, ttl),
    warn:    (msg: string, ttl?: number): number => push('warn', msg, ttl ?? 6000),
    error:   (msg: string, ttl?: number): number => push('error', msg, ttl ?? 8000),
    // ttlMs = 0 → sticky, must be dismissed manually. Callers use this
    // for "pending operation" toasts they'll clear once the operation
    // resolves — otherwise the toast can auto-dismiss before the op
    // completes and the user is left with no visible status.
    dismiss,
  }
}
