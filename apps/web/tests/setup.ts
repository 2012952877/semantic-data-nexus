import { afterEach, vi } from 'vitest'

window.scrollTo = vi.fn()

afterEach(() => {
  window.localStorage.clear()
  document.body.innerHTML = ''
})
