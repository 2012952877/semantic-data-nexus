import { HttpSemanticNexusClient } from './httpSemanticNexusClient'
import type { NexusTokenProvider } from './httpSemanticNexusClient'
import { MockSemanticNexusClient } from './mockSemanticNexusClient'
import type { SemanticNexusClient } from './semanticNexusClient'

export type NexusClientMode = 'mock' | 'http'

interface NexusEnvironment {
  VITE_NEXUS_CLIENT?: string
  VITE_NEXUS_BASE_URL?: string
  VITE_NEXUS_DEV_SUBJECT?: string
  VITE_NEXUS_DEV_ROLES?: string
}

declare global {
  interface Window {
    semanticNexusTokenProvider?: NexusTokenProvider
  }
}

const readEnvironment = (): NexusEnvironment => ({
  VITE_NEXUS_CLIENT: import.meta.env.VITE_NEXUS_CLIENT,
  VITE_NEXUS_BASE_URL: import.meta.env.VITE_NEXUS_BASE_URL,
  VITE_NEXUS_DEV_SUBJECT: import.meta.env.VITE_NEXUS_DEV_SUBJECT,
  VITE_NEXUS_DEV_ROLES: import.meta.env.VITE_NEXUS_DEV_ROLES,
})

const readTokenProvider = () =>
  typeof window === 'undefined' ? undefined : window.semanticNexusTokenProvider

export const createSemanticNexusClient = (
  environment: NexusEnvironment = readEnvironment(),
  tokenProvider: NexusTokenProvider | undefined = readTokenProvider(),
): SemanticNexusClient => {
  const mode = environment.VITE_NEXUS_CLIENT ?? 'mock'
  if (mode === 'mock') return new MockSemanticNexusClient()
  if (mode !== 'http') {
    throw new Error('VITE_NEXUS_CLIENT must be either "mock" or "http".')
  }
  if (!environment.VITE_NEXUS_BASE_URL) {
    throw new Error('VITE_NEXUS_BASE_URL is required when VITE_NEXUS_CLIENT=http.')
  }
  return new HttpSemanticNexusClient({
    baseUrl: environment.VITE_NEXUS_BASE_URL,
    tokenProvider,
    localDevelopment: {
      subject: environment.VITE_NEXUS_DEV_SUBJECT,
      roles: environment.VITE_NEXUS_DEV_ROLES,
    },
  })
}
