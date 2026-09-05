# Services

M0 服务位于此目录：

- `control-api/`：ASP.NET Core BFF、Development/Entra 身份边界和进程内 run
  repository；
- `semantic-api/`：确定性 initializer、compiler 和 SQG validation；
- `query-runtime/`：能力感知 physical planner、DuckDB 本地算子、取消、提交和
  lineage；
- `semantic-backend/`：compiler-to-runtime adapter、run orchestration 和
  fake/live resolver 选择。

M0 状态与 inline results 均为进程内、非持久化实现。模块边界见
[`docs/architecture/modules.md`](../docs/architecture/modules.md)。
