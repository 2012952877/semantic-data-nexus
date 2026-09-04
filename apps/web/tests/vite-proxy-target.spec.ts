// @vitest-environment node

import { request as httpRequest } from 'node:http'

import { createServer, type Plugin } from 'vite'

import {
  developmentIdentityProxyGuard,
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
    const headers = {
      'x-dev-subject': 'unit-user',
      'x-dev-roles': 'Reader',
      authorization: 'Bearer redacted',
    }

    stripRemoteDevelopmentIdentityHeaders('https://bff.example.test', headers)

    expect(headers).toEqual({ authorization: 'Bearer redacted' })
  })

  it('retains local development identity headers for loopback proxy requests', () => {
    const headers = {
      'x-dev-subject': 'unit-user',
      'x-dev-roles': 'Reader',
    }

    stripRemoteDevelopmentIdentityHeaders('http://127.0.0.1:4310', headers)

    expect(headers).toEqual({
      'x-dev-subject': 'unit-user',
      'x-dev-roles': 'Reader',
    })
  })

  it('strips remote identity before proxy construction on Expect: 100-continue', async () => {
    let capturedHeaders: Record<string, string | string[] | undefined> = {}
    const capturePlugin: Plugin = {
      name: 'capture-sanitized-api-request',
      configureServer(server) {
        server.middlewares.use('/api', (request, response) => {
          capturedHeaders = { ...request.headers }
          response.statusCode = 204
          response.end()
        })
      },
    }
    const server = await createServer({
      configFile: false,
      appType: 'custom',
      server: { host: '127.0.0.1', port: 0 },
      plugins: [
        developmentIdentityProxyGuard('https://bff.example.test'),
        capturePlugin,
      ],
    })
    try {
      await server.listen()
      const address = server.httpServer?.address()
      if (!address || typeof address === 'string') throw new Error('Vite address unavailable')
      await new Promise<void>((resolve, reject) => {
        const request = httpRequest({
          host: '127.0.0.1',
          port: address.port,
          path: '/api/v1/runs',
          method: 'POST',
          headers: {
            Expect: '100-continue',
            'Content-Length': '1',
            'X-Dev-Subject': 'unit-user',
            'X-Dev-Roles': 'Reader',
          },
        }, (response) => {
          response.resume()
          response.once('end', resolve)
        })
        request.once('continue', () => request.end('x'))
        request.once('error', reject)
        request.flushHeaders()
      })
    } finally {
      await server.close()
    }

    expect(capturedHeaders['x-dev-subject']).toBeUndefined()
    expect(capturedHeaders['x-dev-roles']).toBeUndefined()
  })
})
