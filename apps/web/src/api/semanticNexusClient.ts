import type {
  AskRequest,
  ComponentStatus,
  Ontology,
  Run,
} from '@/domain'

export interface SemanticNexusClient {
  listRuns(): Promise<Run[]>
  getRun(id: string): Promise<Run | undefined>
  startRun(request: AskRequest, onProgress?: (run: Run) => void): Promise<Run>
  cancelRun(id: string): Promise<Run>
  getOntology(): Promise<Ontology>
  getComponentStatus(): Promise<ComponentStatus[]>
}
