import { beforeEach, describe, expect, it, vi } from 'vitest'
import { BrowserSession } from '@/api/browserSession'

const workspace = { tenantId: 'tenant-a', workspaceId: 'workspace-a', name: 'Workspace A', permissions: ['run.reader'] }
const signedIn = { mode: 'oidc', authenticated: true, name: 'Synthetic Alice', csrfToken: 'synthetic-csrf', workspaces: [workspace] }

describe('server-resolved browser session', () => {
  beforeEach(() => sessionStorage.clear())

  it('sends only server session CSRF and a validated workspace selector', async () => {
    sessionStorage.setItem('nexus-workspace-selector', 'workspace-a')
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(signedIn))
    const session = new BrowserSession(fetcher)
    await session.refresh()
    expect(session.headers()).toEqual({ 'X-Workspace-Id': 'workspace-a', 'X-Nexus-CSRF': 'synthetic-csrf' })
    expect(fetcher.mock.calls[0]?.[1]?.credentials).toBe('same-origin')
    expect(localStorage.getItem('access_token')).toBeNull()
  })

  it('rejects an old selector after membership revocation', async () => {
    sessionStorage.setItem('nexus-workspace-selector', 'workspace-a')
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(signedIn))
      .mockResolvedValueOnce(Response.json({ ...signedIn, workspaces: [] }))
    const session = new BrowserSession(fetcher)
    await session.refresh()
    await session.refresh()
    expect(session.state.workspaceId).toBe('')
    expect(() => session.headers()).toThrow('请先登录')
  })

  it('clears identity and blocks data on network failure, without mock fallback', async () => {
    const session = new BrowserSession(vi.fn<typeof fetch>().mockRejectedValue(new Error('offline')))
    await session.refresh()
    expect(session.state.authenticated).toBe(false)
    expect(session.state.error).toContain('重试')
    expect(() => session.headers()).toThrow()
  })

  it('does not pretend a failed logout succeeded', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(signedIn))
      .mockResolvedValueOnce(new Response(null, { status: 503 }))
    const session = new BrowserSession(fetcher)
    await session.refresh()
    await session.logout()
    expect(session.state.authenticated).toBe(true)
    expect(session.state.error).toContain('尚未确认注销')
  })
})
