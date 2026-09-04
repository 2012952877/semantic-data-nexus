# Semantic Nexus Web

Vue 3 + TypeScript + Vite 实现的治理分析工作台。默认使用严格 Mock，也可以显式切换到经过运行时校验的控制面 BFF HTTP 客户端。

## 运行

```powershell
cd apps\web
pnpm install
pnpm dev
```

默认配置等价于 `VITE_NEXUS_CLIENT=mock`。连接 BFF 时必须显式设置：

```powershell
$env:VITE_NEXUS_CLIENT = "http"
$env:VITE_NEXUS_BASE_URL = "/"
$env:NEXUS_PROXY_TARGET = "http://127.0.0.1:4310"
pnpm dev
```

| 变量 | 说明 |
| --- | --- |
| `VITE_NEXUS_CLIENT` | 仅接受 `mock` 或 `http`；未设置时为 `mock` |
| `VITE_NEXUS_BASE_URL` | HTTP 模式必填；浏览器中必须与 Web 页面同源，可设置为 `/` |
| `VITE_NEXUS_DEV_SUBJECT` | 可选的本地开发身份，只允许发往 loopback BFF 的 `X-Dev-Subject` |
| `VITE_NEXUS_DEV_ROLES` | 可选的本地开发角色，只允许发往 loopback BFF 的 `X-Dev-Roles` |
| `NEXUS_PROXY_TARGET` | 可选的 Vite 本地开发反向代理目标；只在开发服务器中使用，不进入浏览器产物 |

`VITE_*` 值会进入浏览器产物，绝不能放入密钥或令牌。生产宿主应在应用模块加载前提供 `window.semanticNexusTokenProvider(AbortSignal)`；令牌只注入当前请求，不从 Vite 环境变量读取或持久化。测试和其他嵌入方式也可把同一 provider 直接传给 `createSemanticNexusClient` / `HttpSemanticNexusClient`。客户端拒绝把本地开发身份头发往非 loopback 地址，并拒绝包含换行或疑似密钥内容的开发值。

PR #22 的 BFF 不启用 CORS。生产部署必须在 Web 同源反向代理 `/api/v1` 到 BFF；本地开发可用上面的 `NEXUS_PROXY_TARGET`。客户端会拒绝浏览器中的跨源 BFF URL，避免配置成浏览器无法调用的部署。

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
| `/runs` | Mock 本地历史或 BFF 运行历史 |
| `/runs/:id` | 阶段、节点、结果、提交清单、血缘与诊断 |
| `/ontology` | 区域销售实体、字段、指标、关系与查询策略 |
| `/settings` | 只读提供方与组件健康状态 |

Vue Router 使用浏览器历史模式，部署静态产物时需要将未知路径回退到 `index.html`。

## 架构

- `src/domain/`：前端当前依赖的稳定领域契约。SQG、Run、Stage、Node、Result、Lineage 与 Ontology 类型不依赖 Vue。
- `src/api/semanticNexusClient.ts`：Mock 与 HTTP 适配器共享的唯一客户端接口。
- `src/api/mockSemanticNexusClient.ts`：确定性阶段推进、取消、成功/0 行/失败夹具，以及版本化 localStorage 历史。
- `src/api/httpSemanticNexusClient.ts`：BFF 请求、响应校验、DTO 映射、有界退避轮询、取消和详情读取。
- `src/api/createSemanticNexusClient.ts`：严格解析 `VITE_NEXUS_CLIENT=mock|http`，默认保持 Mock。
- `src/views/`：路由级页面；技术细节通过 `details` 渐进展示。
- `src/components/`：应用壳、阶段账本、结果表与运行详情。

Mock 历史使用带 `version: 1` 的逐运行记录（键前缀 `semantic-nexus:run:v1:`），只保存在当前浏览器。每条记录的 compare-and-write 都在该运行专属的 Web Lock 内完成；浏览器不提供 Web Locks 时写入会失败关闭，而不会降级为非原子更新。逐记录事务和 storage event 同步避免不同标签页覆盖彼此，运行 ID 使用 UUID。读取时会校验每个 Run 及其嵌套 Stage、SQG、Node、Result、Lineage、Diagnostic、Manifest 与执行租约。租约包含 owner、稳定 generation 和可续期 heartbeat；每次异步阶段提交前都会在锁内重新读取记录并核对 active 状态及完整租约，旧 owner 无法覆盖已经终止的运行。观察者会按 heartbeat 安排并重排到期计时器，因此 owner 页面关闭后无需刷新也能将过期运行转为可重试的中断诊断。损坏或不同版本的载荷会移到 `semantic-nexus:runs:quarantine` 并忽略，旧版聚合键 `semantic-nexus:runs` 会在同一锁协议下安全迁移。

## Mock 场景

- **有结果**：完成 `Initialize → Compile → Optimize → Execute → Generate`，返回四个合成销售区域与四节点计划。
- **0 行**：正常提交空结果，并说明数据覆盖范围与可尝试期间。
- **执行失败**：在 Execute 阶段返回可操作诊断，不伪造成功清单。
- **取消**：停止后续阶段，保留问题以便修改或重试。

所有展示数据均为合成区域销售夹具。模型仅提出类型化 SQG，Mock 执行器也不会执行模型生成的 SQL。

## HTTP 行为

HTTP 模式只调用 [`/api/v1/runs` 契约](../../docs/architecture/web-http-client.md)，并与 BFF integration PR #22 的 typed wire contract 对齐。每一个成功响应在映射到界面 `Run` 前都会完整校验；网络错误、非 JSON、结构不符、HTTP 问题响应和轮询超时都会以明确错误关闭，不会伪装成空列表或成功结果。运行创建后使用有上限的指数退避轮询，终态再读取 `/detail`。Vue 只使用文本插值，不渲染 BFF 提供的 HTML。

确定性 HTTP stub 同时供 Vitest 和主要 Playwright 套件使用。主要套件强制 `VITE_NEXUS_CLIENT=http`，覆盖真实 HTTP 创建、轮询、列表、详情、取消、跨标签页历史与不安全 HTML 文本；独立的 Mock Chromium 套件保留原生 Web Locks 租约栅栏与过期行为。

根目录 `.github/workflows/web.yml` 只在 `apps/web/**`、本文件对应契约文档或工作流变化时执行锁定依赖安装、单元测试、类型检查、生产构建与 Chromium E2E。
