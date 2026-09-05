# Applications

`web/` 包含 M0 Vue 3 + TypeScript 工作台、确定性 mock adapter 和经过运行时
校验的 BFF HTTP adapter。根目录 Compose 构建 HTTP 模式；独立本地开发默认
使用 mock。应用只能通过版本化 BFF API 访问系统，不能直接连接控制数据库、
结果存储或数据源。详见 [`web/README.md`](web/README.md)。
