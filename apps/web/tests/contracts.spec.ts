import { createSqg, nodes, ontology } from '@/api/mockFixtures'

describe('semantic fixture integrity', () => {
  it('resolves every SQG, policy, relation, and root plan identifier', () => {
    const fieldPaths = new Set(
      ontology.entities.flatMap((entity) =>
        entity.fields.map((field) => `${entity.name}.${field.name}`)),
    )
    const metricNames = new Set(ontology.metrics.map((metric) => metric.name))
    const entityNames = new Set(ontology.entities.map((entity) => entity.name))
    const nodeIds = new Set(nodes.map((node) => node.id))
    const sqg = createSqg({
      question: '比较各区域第三季度销售额',
      scenario: 'failure',
      model: 'Nexus Planner Small',
      executionMode: '受控执行',
      outputMode: '表格',
    })

    expect(sqg.resolvedMembers.every((member) => fieldPaths.has(member))).toBe(true)
    expect(sqg.dimensions.every((dimension) => fieldPaths.has(dimension))).toBe(true)
    expect(sqg.filters.every((filter) => fieldPaths.has(filter.field))).toBe(true)
    expect(sqg.metrics.every((metric) => metricNames.has(metric))).toBe(true)
    expect(ontology.queryPolicy.allowedDimensions.every((field) => fieldPaths.has(field))).toBe(true)
    expect(ontology.relations.every((relation) =>
      fieldPaths.has(relation.from) && fieldPaths.has(relation.to))).toBe(true)
    expect(nodes.every((node) =>
      node.inputs.every((input) => entityNames.has(input) || nodeIds.has(input)))).toBe(true)
    expect(sqg.filters[0]?.value).toBe('2025-Q3')
  })
})
