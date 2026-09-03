# Versioned contracts

`contracts/` 是模块间唯一的语言无关契约来源。当前 `v0` 使用 JSON Schema Draft 2020-12，包含 Ontology、SQG、Run Event/State、Result Manifest、Answer 和 Lineage。

- `v0` 仍可演进，但破坏性语义变更必须显式记录；
- 未知属性通过 `additionalProperties: false` 拒绝；
- Python 模型必须与 Schema 同时更新；
- Schema 结构有效性与示例实例由 `tests/test_schemas.py` 验证；
- JSON Schema 负责结构，跨节点/跨概念不变量由确定性语义校验器负责。

`sqg/v0.root` 是图的**唯一交付节点**。每个 `nodes[]` 节点都必须位于 `root` 的祖先闭包中；孤立节点以及位于 `root` 下游的节点均无效。该规则防止被声明但未纳入最终结果的工作悄悄进入契约。

高精度十进制常量使用 `{"kind": "decimal", "value": "<canonical decimal>"}`，不通过 JSON binary float 传递。`run-event/v0` 的 Stage 事件必须携带 `stage_id`，Node 事件必须同时携带 `stage_id` 和 `node_id`。
