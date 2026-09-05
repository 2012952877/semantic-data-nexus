// Local-only HTTPS test proxy; the release-gate stack uses nginx instead.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'
import { createServer } from 'vite'

const fixtures = process.env.NEXUS_IDENTITY_FIXTURE_DIR
if (!fixtures) throw new Error('An explicit disposable NEXUS_IDENTITY_FIXTURE_DIR is required')
const root = fileURLToPath(new URL('..', import.meta.url))
process.env.VITE_NEXUS_AUTH_MODE = 'oidc'
process.env.VITE_NEXUS_CLIENT = 'http'
process.env.VITE_NEXUS_BASE_URL = '/api/v1'
delete process.env.VITE_NEXUS_DEV_SUBJECT
delete process.env.VITE_NEXUS_DEV_ROLES
delete process.env.NEXUS_PROXY_TARGET

const proxy = {
  target: 'http://127.0.0.1:5088',
  changeOrigin: false,
  xfwd: true,
  configure(instance) {
    instance.on('proxyReq', (upstream, request, response) => {
      request.on('aborted', () => upstream.destroy())
      response.on('close', () => {
        if (!response.writableEnded) upstream.destroy()
      })
    })
  },
}

const server = await createServer({
  root,
  configFile: join(root, 'vite.config.ts'),
  server: {
    host: '127.0.0.1',
    port: 8444,
    strictPort: true,
    https: {
      key: readFileSync(join(fixtures, 'tls.key')),
      cert: readFileSync(join(fixtures, 'tls.crt')),
    },
    proxy: { '/api': proxy, '/auth': proxy, '/health': proxy },
  },
})
await server.listen()
console.log(`Local identity test proxy listening on https://localhost:8444 (PID ${process.pid})`)
