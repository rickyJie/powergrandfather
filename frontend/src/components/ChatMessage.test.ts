/**
 * A streaming reply merges each block into ONE bubble's `text`, so rendering it
 * as a plain computed re-parsed the markdown and re-ran highlight.js over the
 * whole message-so-far on every block — quadratic in the reply's length, on the
 * main thread, while the user is watching it stream.
 *
 * The two properties that matter are in tension, so both are pinned here:
 * renders must be throttled while the text grows, AND the last render must
 * still show the final text. A naive throttle satisfies the first and drops the
 * tail.
 */
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ChatMessage from './ChatMessage.vue'

const renderMarkdown = vi.hoisted(() => vi.fn((t: string) => `<p>${t}</p>`))
vi.mock('../lib/markdown', () => ({ renderMarkdown }))

describe('ChatMessage streaming render', () => {
  beforeEach(() => {
    renderMarkdown.mockClear()
    vi.useFakeTimers({
      toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date', 'performance'],
    })
  })
  afterEach(() => vi.useRealTimers())

  it('does not re-render markdown once per streamed block', async () => {
    const w = mount(ChatMessage, {
      props: { role: 'assistant', text: 'a', ts: '' },
    })
    expect(renderMarkdown).toHaveBeenCalledTimes(1) // immediate first paint

    // Ten blocks land in quick succession, as a real stream does.
    for (let i = 0; i < 10; i++) {
      await w.setProps({ text: 'a'.repeat(i + 2) })
      await vi.advanceTimersByTimeAsync(5)
    }
    // Without the throttle this is 11. The exact number depends on timing; what
    // must hold is that it is far below one-per-block.
    expect(renderMarkdown.mock.calls.length).toBeLessThan(6)
    w.unmount()
  })

  it('still ends on the final text, not a stale one', async () => {
    const w = mount(ChatMessage, {
      props: { role: 'assistant', text: 'start', ts: '' },
    })
    await w.setProps({ text: 'start middle' })
    await w.setProps({ text: 'start middle end' })
    // Let the trailing render fire.
    await vi.advanceTimersByTimeAsync(300)

    expect(w.html()).toContain('start middle end')
    expect(renderMarkdown).toHaveBeenLastCalledWith('start middle end')
    w.unmount()
  })

  it('cancels a queued render when the bubble unmounts', async () => {
    const w = mount(ChatMessage, {
      props: { role: 'assistant', text: 'x', ts: '' },
    })
    await w.setProps({ text: 'xy' }) // queues a trailing render
    const before = renderMarkdown.mock.calls.length
    w.unmount()
    await vi.advanceTimersByTimeAsync(300)
    expect(renderMarkdown.mock.calls.length).toBe(before)
  })
})
