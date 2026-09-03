# Contributing

感谢参与 Semantic Data Nexus。当前优先级是小而可验证的架构与契约变更，不接受以代码量为目标的“完整平台”PR。

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
python -m semantic_core validate examples\regional-quarter-profit.json --ontology examples\retail-ontology.json
```

## 变更要求

1. 先阅读 [Clean-room checklist](docs/clean-room-checklist.md)；
2. 跨模块行为先修改/新增版本化 JSON Schema，再更新 Python 模型、示例和测试；
3. 未知字段、枚举、操作符和能力必须 fail closed；
4. 行为变更同时提供正例和负例；错误诊断应可操作；
5. 架构决策或依赖边界改变时新增/更新 ADR；
6. 不提交生成物、真实数据、凭据、Prompt、内部 URL 或任意 SQL 执行通道；
7. PR 应单一主题、说明公开来源/许可证，并包含 clean-room attestation。

## 契约兼容

`contracts/v0` 虽处于实验阶段，也不允许无说明地改变已提交语义。破坏性变更应提出新版本或明确迁移方式。Pydantic 模型和 JSON Schema 必须保持行为一致。

## Commit 与 PR

建议使用 Conventional Commits。PR 描述至少包含范围、非目标、验证命令、契约影响和 clean-room 声明。不要把基础设施、UI、连接器和核心契约混入同一基础 PR。

