import { afterEach, vi } from 'vitest'

window.scrollTo = vi.fn()

const lockTails = new Map<string, Promise<void>>()
Object.defineProperty(navigator, 'locks', {
  configurable: true,
  value: {
    async request<T>(
      name: string,
      optionsOrCallback: LockOptions | LockGrantedCallback,
      callback?: LockGrantedCallback,
    ): Promise<T> {
      const action = typeof optionsOrCallback === 'function' ? optionsOrCallback : callback
      if (!action) throw new Error('A lock callback is required')
      const previous = lockTails.get(name) ?? Promise.resolve()
      let release = () => {}
      const current = new Promise<void>((resolve) => {
        release = resolve
      })
      const tail = previous.then(() => current)
      lockTails.set(name, tail)
      await previous
      try {
        return await action({ name, mode: 'exclusive' } as Lock) as T
      } finally {
        release()
        if (lockTails.get(name) === tail) lockTails.delete(name)
      }
    },
  } satisfies Pick<LockManager, 'request'>,
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
  window.localStorage.clear()
  document.body.innerHTML = ''
})
