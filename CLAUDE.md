# CLAUDE.md

Dramio 是 AI 短剧自动化生产平台，长期演进。本文件是 AI 编码助手（以及新成员）必须遵守的项目约束。

## 工作流命令

本项目用 Claude Code 原生机制组织开发（只对本仓库生效）。进度的唯一状态来源是 `docs/progress.md`，每次会话开始时由 SessionStart hook 自动注入摘要。

| 命令 | 作用 |
|---|---|
| `/continue` | 按当前进度自主推进，完成当前里程碑后汇报。**用户说“继续当前进度”“继续”“接着做”时执行它**，不要重新询问背景 |
| `/step [编号]` | 完成一个步骤：探索计划 → 实现 → 验证 → 评审 → PR 合并 |
| `/progress` | 只读查看进度、待决事项、下一步 |
| `/decide 编号 决定` | 用户记录决定（agent 不能调用） |

- 定义位置：`.claude/skills/`（命令）、`.claude/agents/`（`step-planner`、`verifier`、`arch-reviewer` 子代理）、`.claude/settings.json`（hooks 与权限）、`tools/workflow/`（hook 脚本）。
- 自主程度以 `docs/progress.md` 的“自主推进设置”为准，那是用户的授权范围。
- 步骤进行中停止会话前，必须先存档：勾选子任务、追加交接日志、提交并推送（Stop hook 会检查）。

## 开工前

1. 阅读 `docs/architecture.md` 中与任务相关的章节，以及 `docs/adr/` 中相关的 ADR。
2. 如果改动会触及下方任一不变量，或引入新的基础设施、存储、语言、外部供应商类别，或改变目录与模块边界、DramaIR 主版本：**先写或更新 ADR**（`docs/adr/template.md`），再写代码。完整的判断标准见 `docs/governance.md` §4.1。
3. 较大的功能先输出实现计划，写明涉及的架构章节和 ADR 编号，确认后再编码。
4. 按 `docs/roadmap.md` 推进，流程见上方“工作流命令”：一次只做一个步骤，先写步骤卡（目标 / 做 / 不做 / 验收），不要顺手做下一步或范围外的事。
5. ADR 状态：Accepted 是必须遵守的决策；Proposed 表示方向已定、待正式确认，不要写与之矛盾的实现，有疑问先提出来。

## 架构不变量（详见 docs/governance.md §3）

- **INV-01** 模型调用只经模型网关（`services/model-gateway`）。网关之外不 import 供应商 SDK，不硬编码模型 ID。
- **INV-02** DramaIR（`packages/drama-ir`）是剧本数据的唯一事实来源。不要另建同义结构；类型只能生成，不能手写。
- **INV-03** apps / services / workers 之间不直接 import，只经 API、事件或 Temporal 交互；共享代码放 `packages/`。
- **INV-04** 长流程只走 Temporal。API 请求中不做同步生成；Workflow 代码必须确定性，修改已上线的 Workflow 要做版本化。
- **INV-05** 生成步骤必须幂等：用 `node_key` 作为幂等键并接入缓存。
- **INV-06** 媒体产物只写入内容寻址存储（CAS），业务数据只引用 `sha256`。
- **INV-07** 每次模型调用都计量入账（由网关负责，不要绕过网关）。
- **INV-08** AIGC 标识、内容审核、发布前质检在生产环境不可关闭。
- **INV-09** 业务表都有 `tenant_id` 并启用行级安全。
- **INV-10** Prompt 模板放在 `packages/prompts` 并版本化；新模型或新 Prompt 需通过 `evals/` 回归。
- **INV-11** 顶层目录与架构文档一致；架构相关改动在同一个 PR 中更新文档和 ADR。

## 代码放在哪里

| 路径 | 内容 |
|---|---|
| `apps/web` | Next.js 创作工作台 |
| `apps/api` | NestJS 业务 API（模块化单体） |
| `services/orchestrator` | Temporal Workflow 定义（TypeScript） |
| `services/model-gateway` | 模型网关与供应商适配器（唯一允许调用供应商的地方） |
| `services/publisher`、`services/analytics` | 发布分发、数据采集与分析 |
| `workers/` | Python Activity Worker（llm / image / video / audio / lipsync / render / qc） |
| `packages/` | 共享包：`drama-ir`、`prompts`、`node-sdk`、`ui`。TS 包名 `@dramio/<name>`，Python 包名 `dramio_<name>` |
| `evals/` | 评测集与回归脚本 |
| `spikes/` | P0 一次性验证代码，任何产品代码都不能依赖它 |
| `infra/` | Helm、Terraform、Docker |
| `tools/archcheck/` | 架构自动检查 |
| `tools/workflow/` | 进度解析与 Claude Code hook 脚本 |
| `.claude/` | 工作流命令、子代理、hooks 与权限配置 |
| `docs/` | 架构文档、治理文档、ADR、技术债登记 |

## 提交前必须运行

```bash
make arch-check
```

报错格式为 `[INV-xx 规则] 文件:行: 说明`，按编号查 `docs/governance.md` §3 修正代码。

## 不要做的事

- 不要为了让检查通过而修改 `tools/archcheck/rules.toml` 或在 `exceptions.toml` 中添加豁免。确实需要例外时，说明原因，由架构负责人决定。
- 不要手写或手改 DramaIR 的生成类型。
- 不要在网关之外调用模型供应商，也不要硬编码模型 ID。
- 不要 import `spikes/` 中的代码。
- 不要新增顶层目录（需要时先写 ADR 并更新 `docs/architecture.md` §15）。

## 文档同步

架构相关改动在同一个 PR 内同步更新 `docs/architecture.md`、相关 ADR 和 `docs/tech-debt.md`。
