const isLoopback = (url: URL) =>
  url.hostname === 'localhost'
  || url.hostname === '127.0.0.1'
  || url.hostname === '[::1]'

export const validateProxyTarget = (value: string) => {
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
  return value
}

export const stripRemoteDevelopmentIdentityHeaders = (
  target: string,
  request: { removeHeader: (name: string) => void },
) => {
  if (isLoopback(new URL(target))) return
  request.removeHeader('X-Dev-Subject')
  request.removeHeader('X-Dev-Roles')
}
