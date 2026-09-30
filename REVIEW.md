# REVIEW.md

代码评审规则（人工评审与自动评审共用）。架构不变量的完整说明见 `docs/governance.md` §3。

## 阻断项（必须修复才能合入）

评论时注明对应的不变量编号。

- 违反任一架构不变量（INV-01 ~ INV-11）。常见情形：
  - 在 `services/model-gateway` 之外调用模型供应商或硬编码模型 ID（INV-01）；
  - 绕过 DramaIR 定义新的剧本、分镜、镜头数据结构，或手改生成的类型（INV-02）；
  - apps / services / workers 之间直接 import（INV-03）；
  - 在 API 请求中同步执行生成，或用 cron、自建队列编排生产流程（INV-04）；
  - Temporal Workflow 代码中直接做 IO、使用随机数或系统时间，或修改已上线的 Workflow 却没有版本化（INV-04）；
  - 生成类 Activity 没有幂等键或未接入 node_key 缓存（INV-05）；
  - 绕过资产 SDK 直接写对象存储，或在业务数据中保存可变文件路径（INV-06）；
  - AIGC 标识、内容审核或发布前质检可以在生产环境被关闭或跳过（INV-08）；
  - 新业务表缺少 `tenant_id` 或行级安全（INV-09）。
- 改动触及不变量、模块边界或引入新的基础设施，但没有对应的 ADR（`docs/governance.md` §4.1）。
- 修改 `tools/archcheck/rules.toml` 却没有 ADR。
- 新增或续期的豁免缺少 owner、到期日、原因，或没有在 `docs/tech-debt.md` 登记。
- DramaIR Schema 的破坏性变更没有升级主版本、没有迁移说明。
- 在产品代码中引用 `spikes/`。

## 建议项（不阻断）

- 架构相关改动没有同步更新 `docs/architecture.md`。
- 新的生成节点类型没有说明参与 `node_key` 计算的输入。
- 新增的模型能力没有补充评测用例。
- 命名与仓库约定不一致（TS 包 `@dramio/<name>`，Python 包 `dramio_<name>`）。
