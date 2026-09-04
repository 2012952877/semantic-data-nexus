// @vitest-environment node

import {
  createServer as createHttpServer,
  request as httpRequest,
  type IncomingHttpHeaders,
  type Server as HttpServer,
} from 'node:http'
import { Agent as HttpsAgent } from 'node:https'
import { connect } from 'node:net'

import { createServer } from 'vite'

import {
  developmentIdentityProxyGuard,
  stripRemoteDevelopmentIdentityHeaders,
  validateProxyTarget,
} from '../viteProxyTarget'

const listen = (server: HttpServer, host: string) =>
  new Promise<number>((resolve, reject) => {
    const handleError = (error: Error) => reject(error)
    server.once('error', handleError)
    server.listen(0, host, () => {
      server.off('error', handleError)
      const address = server.address()
      if (!address || typeof address === 'string') {
        reject(new Error('HTTP server address unavailable'))
        return
      }
      resolve(address.port)
    })
  })

const close = (server: HttpServer) =>
  new Promise<void>((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve())
  })

const proxyExpectRequest = async (remote: boolean) => {
  let capturedHeaders: IncomingHttpHeaders | undefined
  const upstream = createHttpServer((request, response) => {
    capturedHeaders = { ...request.headers }
    request.resume()
    response.writeHead(204, { Connection: 'close' })
    response.end()
  })
  const upstreamPort = await listen(upstream, '127.0.0.1')
  const remoteAgent = remote ? new HttpsAgent() : undefined
  if (remoteAgent) {
    // Exercise an HTTPS proxy target without committing a test private key.
    remoteAgent.createConnection = (_options, callback) => {
      const socket = connect({ host: '127.0.0.1', port: upstreamPort })
      socket.once('connect', () => callback?.(null, socket))
      return socket
    }
  }
  const target = remote
    ? `https://bff.example.test:${upstreamPort}`
    : `http://127.0.0.1:${upstreamPort}`
  const server = await createServer({
    configFile: false,
    appType: 'custom',
    optimizeDeps: { noDiscovery: true },
    server: {
      host: '127.0.0.1',
      port: 0,
      proxy: {
        '/api': {
          target,
          changeOrigin: true,
          agent: remoteAgent,
        },
      },
    },
    plugins: [developmentIdentityProxyGuard(target)],
  })
  let receivedContinue = false
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
          Connection: 'close',
          'X-Dev-Subject': 'unit-user',
          'X-Dev-Roles': 'Reader',
        },
      }, (response) => {
        expect(response.statusCode).toBe(204)
        response.resume()
        response.once('end', resolve)
      })
      request.once('continue', () => {
        receivedContinue = true
        request.end('x')
      })
      request.once('error', reject)
      request.setTimeout(10_000, () => {
        request.destroy(new Error('Timed out waiting for the Vite proxy response'))
      })
      request.flushHeaders()
    })
  } finally {
    await server.close()
    remoteAgent?.destroy()
    await close(upstream)
  }
  return { capturedHeaders, receivedContinue }
}

describe('Vite BFF proxy target', () => {
  it.each([
    'http://localhost:4310',
    'http://127.0.0.1:4310',
    'http://[::1]:4310',
  ])('accepts loopback target %s with development identity configured', (target) => {
    expect(validateProxyTarget(target, true)).toBe(target)
  })

  it('accepts a remote HTTPS target without development identity', () => {
    const target = 'https://bff.example.test'

    expect(validateProxyTarget(target)).toBe(target)
  })

  it('rejects remote plaintext targets before credentials can be forwarded', () => {
    expect(() => validateProxyTarget('http://bff.example.test'))
      .toThrow(/requires HTTPS/)
  })

  it('rejects a remote HTTPS target when development identity is configured', () => {
    expect(() => validateProxyTarget('https://bff.example.test', true))
      .toThrow(/require a loopback/)
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

  it('strips remote identity on the real proxy Expect: 100-continue path', async () => {
    const { capturedHeaders, receivedContinue } = await proxyExpectRequest(true)

    expect(receivedContinue).toBe(true)
    expect(capturedHeaders?.['x-dev-subject']).toBeUndefined()
    expect(capturedHeaders?.['x-dev-roles']).toBeUndefined()
  })

  it('preserves loopback identity on the real proxy Expect: 100-continue path', async () => {
    const { capturedHeaders, receivedContinue } = await proxyExpectRequest(false)

    expect(receivedContinue).toBe(true)
    expect(capturedHeaders?.['x-dev-subject']).toBe('unit-user')
    expect(capturedHeaders?.['x-dev-roles']).toBe('Reader')
  })
})
