# Dramio

AI 短剧自动化生产平台：选题 → 剧本 → 角色/场景设定 → 分镜 → 配音 → 画面生成 → 口型 → 音乐音效 → 剪辑合成 → 质检审核 → 分发 → 数据回流，全部环节在一个平台内完成。

## 文档

- [架构设计方案](docs/architecture.md)
- [架构治理与落地保障](docs/governance.md)：不变量、ADR 流程、自动检查、分阶段落地、演进触发条件
- [分步实施路线图](docs/roadmap.md)：P0 ~ P3 拆成的小步骤与当前进度
- [架构决策记录（ADR）](docs/adr/README.md)
- [技术债与豁免登记](docs/tech-debt.md)

## 参与开发

- 开工前阅读 [CLAUDE.md](CLAUDE.md)（人和 AI 编码助手共用的项目约束）。
- 提交前运行 `make arch-check`（需要 Python 3.11+）。
- 评审规则见 [REVIEW.md](REVIEW.md)。
