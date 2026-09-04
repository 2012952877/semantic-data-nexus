import type { Plugin } from 'vite'

const isLoopback = (url: URL) =>
  url.hostname === 'localhost'
  || url.hostname === '127.0.0.1'
  || url.hostname === '[::1]'

export const validateProxyTarget = (
  value: string,
  developmentIdentityConfigured = false,
) => {
  const url = new URL(value)
  if (!['http:', 'https:'].includes(url.protocol)
    || url.username
    || url.password
    || url.search
    || url.hash) {
    throw new Error(
      'NEXUS_PROXY_TARGET must be an HTTP(S) URL without credentials, query, or fragment.',
    )
  }
  if (url.protocol === 'http:' && !isLoopback(url)) {
    throw new Error(
      'NEXUS_PROXY_TARGET requires HTTPS unless the target is localhost, 127.0.0.1, or [::1].',
    )
  }
  if (!isLoopback(url) && developmentIdentityConfigured) {
    throw new Error(
      'VITE_NEXUS_DEV_SUBJECT and VITE_NEXUS_DEV_ROLES require a loopback NEXUS_PROXY_TARGET.',
    )
  }
  return value
}

export const stripRemoteDevelopmentIdentityHeaders = (
  target: string,
  headers: Record<string, string | string[] | undefined>,
) => {
  if (isLoopback(new URL(target))) return
  delete headers['x-dev-subject']
  delete headers['x-dev-roles']
}

export const developmentIdentityProxyGuard = (target: string): Plugin => ({
  name: 'semantic-nexus-development-identity-proxy-guard',
  configureServer(server) {
    if (isLoopback(new URL(target))) return
    server.middlewares.use('/api', (request, _response, next) => {
      stripRemoteDevelopmentIdentityHeaders(target, request.headers)
      next()
    })
  },
})
