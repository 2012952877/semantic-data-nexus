// @vitest-environment node

import { validateProxyTarget } from '../viteProxyTarget'

describe('Vite BFF proxy target', () => {
  it.each([
    'http://localhost:4310',
    'http://127.0.0.1:4310',
    'http://[::1]:4310',
    'https://bff.example.test',
  ])('accepts secure or explicit loopback target %s', (target) => {
    expect(validateProxyTarget(target)).toBe(target)
  })

  it('rejects remote plaintext targets before credentials can be forwarded', () => {
    expect(() => validateProxyTarget('http://bff.example.test'))
      .toThrow(/requires HTTPS/)
  })
})
