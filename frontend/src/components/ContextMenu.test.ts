import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import ContextMenu from './ContextMenu.vue'

describe('ContextMenu lifecycle', () => {
  it('removes every global listener on unmount', () => {
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    const wrapper = mount(ContextMenu, {
      props: { visible: true, x: 10, y: 10, items: [] },
    })

    const registered = add.mock.calls
      .filter(([type]) => ['mousedown', 'contextmenu', 'keydown', 'resize', 'scroll'].includes(type))
      .map(([type, handler, options]) => [type, handler, options])

    wrapper.unmount()

    for (const args of registered) {
      const expected = args[2] === undefined ? args.slice(0, 2) : args
      expect(remove).toHaveBeenCalledWith(...expected)
    }
    add.mockRestore()
    remove.mockRestore()
  })
})
