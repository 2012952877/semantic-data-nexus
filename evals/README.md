# Semantic golden evaluation suite

This package is a provider-independent quality gate for semantic query systems.
It checks whether a candidate chose the correct concepts, normalized filters and
time ranges, formed the intended logical plan, respected policy, returned the
right grain and values, and emitted lineage.

Valid SQL or successful execution is not sufficient. A query can run while using
the wrong metric, broadening a time range, joining at the wrong grain, dropping a
policy decision, or returning plausible but incorrect values. The evaluator
scores those properties independently so a single successful execution cannot
hide a semantic regression.

The `v0` JSON shapes are deliberately project-independent evaluation structures.
They are not presented as a final semantic query graph contract. A future
foundation adapter should map its native compiler artifact, execution result,
diagnostics, and lineage into `candidate-v0` while preserving native artifacts
outside this suite.

## Run locally

Python 3.12 is required. From the repository root:

```sh
python -m pip install -e "evals[dev]"
python data/synthetic/generate.py
python evals/tools/build_fixtures.py
pytest evals
semantic-eval evals/fixtures/v0/candidates/passing.json
```

Evaluate a failing reference and write machine-readable reports:

```sh
semantic-eval evals/fixtures/v0/candidates/failing.json \
  --minimum-score 95 \
  --minimum-dimension governance=100 \
  --json-report evaluation.json \
  --junit-report evaluation.xml
```

The command accepts JSON or YAML. It exits successfully when the weighted score
meets `--minimum-score` and every repeatable `--minimum-dimension` gate passes.

## Artifact layout

- `fixtures/v0/ontology.json`: synthetic entities, fields, metrics, relations,
  synonyms, and query policies.
- `fixtures/v0/member_values.json`: allowed synthetic members and aliases.
- `fixtures/v0/golden_cases.json`: Chinese questions and expected semantics,
  generic plans, results, diagnostics, governance, and observability.
- `fixtures/v0/candidates/`: passing and intentionally failing static adapters.
- `../data/synthetic/`: deterministic generator, schema documentation, and CSVs.

Each golden case has a stable ID, Chinese question, optional clarification
context, and an `expected` object. Expected logical operators are generic
(`SCAN`, `JOIN`, `FILTER`, `AGGREGATE`, `PIVOT`, `DERIVE`, `PROJECT`, and so on)
rather than SQL- or vendor-specific nodes.

## Add a case

1. Add only synthetic values to `data/synthetic/generate.py` if new data is
   required, then regenerate the CSVs.
2. Add the case definition and deterministic DuckDB reference query to
   `evals/tools/build_fixtures.py`.
3. Rebuild fixtures and inspect the checked-in JSON diff.
4. Add a focused test when the case introduces a new comparator or behavior.
5. Run `pytest evals` and evaluate both static candidates.

Case result rows are ordered explicitly. Numeric tolerance is absolute and
case-local. Use zero tolerance for identifiers and diagnostic fixtures.

## Scores and quality gates

| Dimension | Default weight | Checks |
| --- | ---: | --- |
| Semantic | 30 | entities, fields, metrics, relations, members, time range |
| Plan | 25 | operator order, graph edges, graph integrity |
| Execution result | 25 | schema, grain, ordering, rows, numeric tolerance |
| Governance | 10 | behavior/diagnostic class and policy decision |
| Observability | 10 | required lineage and source entities |

Console diagnostics identify the dimension and exact property that differs.
JSON preserves expected and actual values; JUnit creates one test case per golden
case. A new adapter should initially report scores without blocking, then gate
each dimension and the total score once its coverage is complete. A mature
adapter should require 100 for this deterministic reference suite; production
corpora may choose explicit thresholds while preventing governance regressions
from being averaged away.

The suite makes no LLM call, uses no live database or cloud resource, and needs no
secret or environment variable.
