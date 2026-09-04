// @vitest-environment node

import {
  stripRemoteDevelopmentIdentityHeaders,
  validateProxyTarget,
} from '../viteProxyTarget'

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

  it('strips local development identity headers from remote HTTPS proxy requests', () => {
    const removeHeader = vi.fn()

    stripRemoteDevelopmentIdentityHeaders('https://bff.example.test', { removeHeader })

    expect(removeHeader.mock.calls).toEqual([
      ['X-Dev-Subject'],
      ['X-Dev-Roles'],
    ])
  })

  it('retains local development identity headers for loopback proxy requests', () => {
    const removeHeader = vi.fn()

    stripRemoteDevelopmentIdentityHeaders('http://127.0.0.1:4310', { removeHeader })

    expect(removeHeader).not.toHaveBeenCalled()
  })
})
