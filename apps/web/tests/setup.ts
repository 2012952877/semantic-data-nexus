import { afterEach, vi } from 'vitest'

window.scrollTo = vi.fn()

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  window.localStorage.clear()
  document.body.innerHTML = ''
})
