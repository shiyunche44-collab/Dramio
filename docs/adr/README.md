# 架构决策记录（ADR）

ADR 记录“为什么这样设计”。流程与规则见 [docs/governance.md §4](../governance.md#4-架构决策记录adr)。

- 新建 ADR：复制 [template.md](template.md)，编号取下一个四位数字，并在下表登记。
- 状态：`Proposed`（待评审）→ `Accepted`（已接受）→ `Deprecated`（已废弃）/ `Superseded`（已被新 ADR 取代）。
- Accepted 后不再修改正文；决策变化时写新的 ADR 并更新旧 ADR 的状态。
- 格式、状态和本索引由 `make arch-check` 自动校验。

| 编号 | 标题 | 状态 |
|---|---|---|
| [ADR-0001](0001-record-architecture-decisions.md) | 用 ADR 记录架构决策 | Proposed |
| [ADR-0002](0002-dramair-single-source-of-truth.md) | DramaIR 作为全链路唯一事实来源 | Proposed |
| [ADR-0003](0003-temporal-for-orchestration.md) | 用 Temporal 编排生产流程 | Proposed |
| [ADR-0004](0004-model-gateway.md) | 所有模型调用经由模型网关 | Proposed |
| [ADR-0005](0005-content-addressed-storage.md) | 媒体产物采用内容寻址存储 | Proposed |
| [ADR-0006](0006-node-key-incremental-recompute.md) | 基于 node_key 的生成缓存与增量重算 | Proposed |
| [ADR-0007](0007-modular-monolith-first.md) | 先模块化单体，按触发条件再拆分服务 | Proposed |
| [ADR-0008](0008-typescript-python-split.md) | TypeScript 与 Python 的语言分工 | Proposed |
| [ADR-0009](0009-opentimelineio.md) | 以 OpenTimelineIO 作为时间线格式 | Proposed |
| [ADR-0010](0010-region-based-llm-routing.md) | LLM 按部署区域路由以满足合规 | Proposed |
