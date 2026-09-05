# Plugin extension contracts

These contracts are additive and do not change `contracts/v0` or the root product
capability registry. They describe an opt-in implementation, not commercial-release
status or evidence of externally live infrastructure.

`v1/plugin.schema.json` defines resolver/compute/result-store metadata and accepted
runtime versions. `v1/operator.schema.json` describes the tagged `OperatorSpecV1`;
the v1 logical/physical graph schemas require an explicit `query-runtime/v1` tag.
Source fragments retain their v0 restricted operator payloads.

JSON Schemas provide structural validation. The corresponding Pydantic models also
enforce cross-field/version rules and the capability matrix's supported forms;
the runtime checks input schemas, authority and resource limits before/during execution.
A schema-valid payload alone is not authorization or a claim that every form is supported.

Regenerate structural snapshots with:

```powershell
python -m nexus_plugins.export_contracts contracts/plugins/v1
```

`examples.json` contains synthetic, model-validated extension examples.
`operator-capabilities.json` accounts for the 26 families, including explicit
unsupported ASK/ACT/SEARCH. SOURCE/LIMIT are runtime internals, not extra product families.
The source of the executable models is `services/query-runtime/src/query_runtime/domain.py`.
Adapters, usage and evidence boundaries are documented in `connectors/plugins/README.md`.
