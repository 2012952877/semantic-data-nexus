import type { InjectionKey } from 'vue'

import type { SemanticNexusClient } from './semanticNexusClient'

export const nexusClientKey: InjectionKey<SemanticNexusClient> = Symbol('nexus-client')
