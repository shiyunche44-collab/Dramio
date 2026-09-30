# Dramio

AI 短剧自动化生产平台：选题 → 剧本 → 角色/场景设定 → 分镜 → 配音 → 画面生成 → 口型 → 音乐音效 → 剪辑合成 → 质检审核 → 分发 → 数据回流，全部环节在一个平台内完成。

## 文档

- [架构设计方案](docs/architecture.md)
- [架构治理与落地保障](docs/governance.md)：不变量、ADR 流程、自动检查、分阶段落地、演进触发条件
- [分步实施路线图](docs/roadmap.md)：P0 ~ P3 拆成的小步骤与当前进度
- [架构决策记录（ADR）](docs/adr/README.md)
- [技术债与豁免登记](docs/tech-debt.md)

## 推进开发

在本仓库中打开 Claude Code，然后：

- 说“继续当前进度”或输入 `/continue`：agent 按 [docs/progress.md](docs/progress.md) 推进完当前里程碑后汇报；
- `/progress`：查看做到哪、有什么等你决定；
- `/decide D-001 B`：回复待决事项（`/decide` 不带参数会列出全部）。

这些命令和 hooks 定义在 `.claude/` 中，只对本仓库生效。

## 参与开发

- 开工前阅读 [CLAUDE.md](CLAUDE.md)（人和 AI 编码助手共用的项目约束）。
- 提交前运行 `make arch-check`（需要 Python 3.11+）。
- 评审规则见 [REVIEW.md](REVIEW.md)。
