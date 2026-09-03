import { afterEach, vi } from 'vitest'

window.scrollTo = vi.fn()

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
  document.body.innerHTML = ''
})
