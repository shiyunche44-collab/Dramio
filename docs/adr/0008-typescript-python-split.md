# ADR-0008：TypeScript 与 Python 的语言分工

- **状态**：Proposed
- **日期**：2026-09-30
- **决策人**：@shiyunche44-collab
- **关联**：架构文档 §14、§15

## 背景

前端和业务 API 需要强类型和良好的 Web 生态；模型推理、音视频处理的生态几乎都在 Python。语言过多会增加招聘、维护和共享类型的成本。

## 决策

| 部分 | 语言 |
|---|---|
| 前端（apps/web）、业务 API（apps/api）、Workflow 定义（services/orchestrator） | TypeScript |
| AI 与媒体 Worker（workers/）、评测（evals/） | Python |
| 模型网关（services/model-gateway） | TypeScript 或 Go，在 P1 启动时通过新 ADR 确定 |
| 共享契约 | JSON Schema（生成 TS 与 Python 类型） |

- 引入第三种语言必须写 ADR。
- TS 共享包命名为 `@dramio/<name>`，Python 共享包命名为 `dramio_<name>`（与 `tools/archcheck/rules.toml` 中的别名规则一致）。

## 备选方案

| 方案 | 不选的原因 |
|---|---|
| 全部 Python | 前端仍需 TS，契约要维护两份；Web 业务开发体验较弱 |
| 全部 TypeScript | 模型推理和媒体处理生态不足 |

## 后果

- 正面：各取所长；契约由 Schema 统一。
- 负面：两套工具链（lint、测试、依赖管理）。
- 约束：跨语言交互只通过 Schema 定义的数据、HTTP/gRPC 接口或 Temporal，不做语言间直接调用。

## 复审条件

出现某个关键组件在两种语言中都无法满足性能要求。
