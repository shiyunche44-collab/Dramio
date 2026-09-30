## 变更说明

<!-- 做了什么、为什么 -->

## 关联

- 需求 / Issue：
- 架构章节：docs/architecture.md §
- ADR：

## 架构自检

- [ ] 本地 `make arch-check` 通过
- [ ] 不涉及架构不变量；或已新增/更新 ADR 并在上方关联
- [ ] 未在模型网关之外调用模型供应商或硬编码模型 ID（INV-01）
- [ ] 未绕过 DramaIR 定义新的剧本/镜头数据结构（INV-02）
- [ ] apps / services / workers 之间只经 API、事件或 Temporal 交互（INV-03）
- [ ] 新增的生成步骤幂等并接入 node_key 缓存（INV-05）
- [ ] 如新增豁免：已填写 owner 和到期日，并登记到 docs/tech-debt.md
- [ ] 已同步更新相关文档

## 测试

<!-- 如何验证 -->
