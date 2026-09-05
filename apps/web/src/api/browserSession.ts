import { reactive, type InjectionKey } from 'vue'

export interface WorkspaceMembership {
  tenantId: string
  workspaceId: string
  name: string
  permissions: string[]
}

interface SessionResponse {
  mode: 'oidc'
  authenticated: boolean
  name?: string
  providers?: string[]
  workspaces?: WorkspaceMembership[]
  csrfToken?: string
}

const isSession = (value: unknown): value is SessionResponse => {
  if (typeof value !== 'object' || value === null) return false
  const session = value as Record<string, unknown>
  if (session.mode !== 'oidc' || typeof session.authenticated !== 'boolean') return false
  if (!session.authenticated) {
    return Array.isArray(session.providers)
      && session.providers.every(p => typeof p === 'string' && /^[a-zA-Z0-9]+$/.test(p))
  }
  return typeof session.name === 'string' && typeof session.csrfToken === 'string'
    && Array.isArray(session.workspaces) && session.workspaces.every(workspace =>
      typeof workspace === 'object' && workspace !== null
      && typeof workspace.tenantId === 'string' && typeof workspace.workspaceId === 'string'
      && typeof workspace.name === 'string' && Array.isArray(workspace.permissions)
      && workspace.permissions.every((permission: unknown) => typeof permission === 'string'))
}

export class BrowserSession {
  readonly state = reactive({
    loading: true,
    authenticated: false,
    name: '',
    providers: [] as string[],
    workspaces: [] as WorkspaceMembership[],
    workspaceId: '',
    error: '',
  })
  private csrf = ''

  constructor(private readonly fetcher: typeof fetch = (input, init) => globalThis.fetch(input, init)) {}

  async refresh(): Promise<void> {
    this.state.error = ''
    try {
      const response = await this.fetcher('/auth/session', {
        credentials: 'same-origin',
        cache: 'no-store',
        signal: AbortSignal.timeout(10_000),
      })
      const value: unknown = await response.json()
      if (!response.ok || !isSession(value)) throw new Error('Invalid session response')
      this.state.authenticated = value.authenticated
      this.state.name = value.name ?? ''
      this.state.providers = value.providers ?? []
      this.state.workspaces = value.workspaces ?? []
      const selection = sessionStorage.getItem('nexus-workspace-selector') ?? ''
      this.state.workspaceId = this.state.workspaces.some(w => w.workspaceId === selection) ? selection : ''
      this.csrf = value.csrfToken ?? ''
    } catch {
      this.invalidate()
      this.state.error = '无法确认登录或工作区权限。请检查连接后重试。'
    } finally {
      this.state.loading = false
    }
  }

  headers(): Record<string, string> {
    if (!this.state.authenticated || !this.state.workspaceId || !this.csrf) {
      throw new Error('请先登录并选择已授权的工作区。')
    }
    return { 'X-Workspace-Id': this.state.workspaceId, 'X-Nexus-CSRF': this.csrf }
  }

  select(workspaceId: string): void {
    if (!this.state.workspaces.some(w => w.workspaceId === workspaceId)) {
      throw new Error('工作区未授权。')
    }
    sessionStorage.setItem('nexus-workspace-selector', workspaceId)
    // A full navigation discards every cached run, pending retry and in-flight UI callback.
    window.location.assign('/ask')
  }

  invalidate(): void {
    if (this.state.authenticated) this.state.error = '会话或权限已更改，请重新确认登录。'
    this.state.authenticated = false
    this.state.name = ''
    this.state.workspaceId = ''
    this.state.workspaces = []
    this.csrf = ''
  }

  async logout(): Promise<void> {
    this.state.error = ''
    try {
      const response = await this.fetcher('/auth/logout', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-Nexus-CSRF': this.csrf },
        signal: AbortSignal.timeout(10_000),
      })
      if (!response.ok) throw new Error('Logout failed')
      this.invalidate()
      sessionStorage.removeItem('nexus-workspace-selector')
      window.location.assign('/')
    } catch {
      this.state.error = '退出未完成，请重试。当前会话尚未确认注销。'
    }
  }
}

export const browserSessionKey: InjectionKey<BrowserSession> = Symbol('browserSession')
