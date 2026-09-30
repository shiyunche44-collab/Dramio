# ADR-0003：用 Temporal 编排生产流程

- **状态**：Proposed
- **日期**：2026-09-30
- **决策人**：@shiyunche44-collab
- **关联**：架构文档 §6、§8；不变量 INV-04、INV-05

## 背景

一部剧的生产流程持续数天，包含上千个镜头的并行生成、多次人工审核等待、第三方接口的超时和限流。流程状态必须持久化，失败后能从断点继续。

## 决策

- 所有生产流程（剧本、配音、镜头生成、合成、质检、发布）都用 Temporal Workflow 编排。
- Workflow 用 TypeScript 编写；模型推理和媒体处理作为 Python Activity，运行在按能力划分的 Task Queue 上。
- 人工审核用 Signal 实现；GPU 任务排队直接使用 Temporal Task Queue，不另建队列。
- Workflow 代码必须是确定性的（不直接做 IO、不用随机数和系统时间）；修改已上线的 Workflow 必须使用 Temporal 的版本化机制。
- 所有 Activity 必须幂等（见 ADR-0006）。

## 备选方案

| 方案 | 不选的原因 |
|---|---|
| Argo Workflows | 擅长 K8s 批处理 DAG，但人工卡点和长时间等待支持弱 |
| Airflow / Dagster | 面向定时数据管道，不适合事件驱动、交互式的生产流程 |
| Celery / 自建队列 + 状态表 | 需要自己实现持久化、重试、超时、版本化，长期维护成本高 |

## 后果

- 正面：流程可靠、可观测、可恢复；人工卡点是一等公民。
- 负面：团队需要学习 Temporal 的确定性约束；需要运维 Temporal 集群（或使用托管服务）。
- 约束：API 请求中不做同步生成；不允许用 cron 或自建队列编排生产流程；P2 起 CI 增加 Workflow 回放（replay）测试。

## 复审条件

Temporal 的运维成本或吞吐成为瓶颈，且有明确的替代方案能满足人工卡点和长流程要求。
