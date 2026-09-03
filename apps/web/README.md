# Semantic Nexus Web

Vue 3 + TypeScript + Vite 实现的治理分析工作台。M0 完全运行在严格 Mock 模式，不依赖真实后端、云资源或密钥。

## 运行

```powershell
cd apps\web
pnpm install
pnpm dev
```

验证命令：

```powershell
pnpm test
pnpm run typecheck
pnpm run build
pnpm run test:e2e
```

## 路由

| 路由 | 用途 |
| --- | --- |
| `/ask` | 提问、选择 Mock 场景、查看五阶段执行进度与结果 |
| `/runs` | 本地运行历史 |
| `/runs/:id` | 阶段、节点、结果、提交清单、血缘与诊断 |
| `/ontology` | 区域销售实体、字段、指标、关系与查询策略 |
| `/settings` | 只读提供方与组件健康状态 |

Vue Router 使用浏览器历史模式，部署静态产物时需要将未知路径回退到 `index.html`。

## 架构

- `src/domain/`：前端当前依赖的稳定领域契约。SQG、Run、Stage、Node、Result、Lineage 与 Ontology 类型不依赖 Vue。
- `src/api/semanticNexusClient.ts`：唯一客户端接口。未来 HTTP 适配器实现同一接口即可替换 Mock。
- `src/api/mockSemanticNexusClient.ts`：确定性阶段推进、取消、成功/0 行/失败夹具，以及版本化 localStorage 历史。
- `src/views/`：路由级页面；技术细节通过 `details` 渐进展示。
- `src/components/`：应用壳、阶段账本、结果表与运行详情。

Mock 历史使用带 `version: 1` 的逐运行记录（键前缀 `semantic-nexus:run:v1:`），只保存在当前浏览器。逐记录写入和 storage event 同步避免不同标签页覆盖彼此，运行 ID 使用 UUID。读取时会校验每个 Run 及其嵌套 Stage、SQG、Node、Result、Lineage、Diagnostic、Manifest 与执行租约。损坏或不同版本的载荷会移到 `semantic-nexus:runs:quarantine` 并忽略；只有当前所有者或心跳超过 30 秒的运行才会被终止为可重试的中断诊断。旧版聚合键 `semantic-nexus:runs` 会被安全迁移。

## Mock 场景

- **有结果**：完成 `Initialize → Compile → Optimize → Execute → Generate`，返回四个合成销售区域与四节点计划。
- **0 行**：正常提交空结果，并说明数据覆盖范围与可尝试期间。
- **执行失败**：在 Execute 阶段返回可操作诊断，不伪造成功清单。
- **取消**：停止后续阶段，保留问题以便修改或重试。

所有展示数据均为合成区域销售夹具。模型仅提出类型化 SQG，Mock 执行器也不会执行模型生成的 SQL。

根目录 `.github/workflows/web.yml` 会在 Web 文件变化时执行依赖安装、单元测试、类型检查、生产构建与 Chromium E2E。
