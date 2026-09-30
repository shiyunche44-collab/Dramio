# 技术债与架构豁免登记

规则见 [governance.md §6](governance.md#6-豁免与技术债)。所有架构豁免（`tools/archcheck/exceptions.toml`）和阶段性简化（governance §7.2 中的 🟡 项）都要在这里登记。

- **编号**：`TD-NNN`，豁免的 `link` 字段指向这里的锚点（如 `#td-001`）。
- **类型**：豁免（违反自动检查规则）/ 阶段性简化 / 其他技术债。
- **偿还条件**：写明截止日期、阶段，或 governance §8 中的触发条件。
- **状态**：计划中 / 进行中 / 已偿还（已偿还的条目保留一个阶段后归档）。

| 编号 | 描述 | 类型 | 关联豁免 / ADR | Owner | 偿还条件 | 状态 |
|---|---|---|---|---|---|---|
| <a id="td-001"></a>TD-001 | DramaIR v0 没有生成类型，也没有 Schema 兼容性检查；用 `packages/drama-ir/python/dramio_drama_ir` 中手写的子集校验器（只支持结构关键字）加语义校验代替 | 阶段性简化（governance §7.2 DramaIR 🟡 草案 Schema） | ADR-0002 | @shiyunche44-collab | P1-03：Schema v1、生成 zod / pydantic 类型、CI 检查生成一致性与向后兼容 | 计划中 |
