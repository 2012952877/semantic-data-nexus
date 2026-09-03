# Clean-room 贡献检查清单

每个 Pull Request 的作者和评审者都应逐项确认。无法确认时不要合并。

## 来源

- [ ] 实现仅依据公开资料、通用工程知识、许可兼容的开源项目或独立设计；
- [ ] 未查看、复制、转写或近似改写任何专有源码、Prompt、内部协议或受限文档；
- [ ] PR 描述列出影响设计的公开来源和许可证；
- [ ] 引入的代码/资产保留必要的版权与归属，并与 Apache-2.0 分发兼容；
- [ ] 未通过逆向工程、流量截取或规避访问控制获取信息。

## 内容与数据

- [ ] 不包含凭据、令牌、连接串、内部 URL、客户名、员工信息或其他私有数据；
- [ ] 示例和截图全部为合成内容，不能反推真实组织；
- [ ] 标识符、错误文本和对象模型为本项目独立设计；
- [ ] 日志、测试 fixture 和快照不包含 Prompt 或业务数据正文。

## 行为

- [ ] 只重建一般、公开可观察的能力，不声称内部兼容；
- [ ] 没有添加直接执行 LLM SQL 的路径、调试后门或静默回退；
- [ ] 新跨模块对象具有明确版本、Schema、正例与负例；
- [ ] 不确定的来源或许可证已请求维护者/法律评审。

建议 PR 中加入：

```text
Clean-room attestation: I confirm this contribution was independently
implemented from public, license-compatible sources and contains no
proprietary or private material.
```

