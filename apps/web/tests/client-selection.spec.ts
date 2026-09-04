import { createSemanticNexusClient } from '@/api/createSemanticNexusClient'

describe('Semantic Nexus client selection', () => {
  it('defaults to the Mock client', () => {
    expect(createSemanticNexusClient({}).mode).toBe('mock')
  })

  it('requires an explicit valid HTTP configuration', () => {
    expect(() => createSemanticNexusClient({
      VITE_NEXUS_CLIENT: 'auto',
    })).toThrow(/mock.*http/)
    expect(() => createSemanticNexusClient({
      VITE_NEXUS_CLIENT: 'http',
    })).toThrow(/VITE_NEXUS_BASE_URL/)
    expect(createSemanticNexusClient({
      VITE_NEXUS_CLIENT: 'http',
      VITE_NEXUS_BASE_URL: window.location.origin,
    }).mode).toBe('http')
  })
})
