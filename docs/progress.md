# 开发进度

> 跨会话推进的**唯一状态来源**。由 `/continue`、`/step`、`/decide` 维护，每次会话开始时由 SessionStart hook 自动读取；人也可以直接编辑。
> 计划层（每个步骤的 ⬜ / 🔄 / ✅）在 [roadmap.md](roadmap.md)；工作流说明见 [CLAUDE.md](../CLAUDE.md#工作流命令)。
> 字段格式（`- **字段**：值`、表头、`## 标题`）会被 `tools/workflow/progress_state.py` 解析，修改时请保持格式。

## 自主推进设置

以下是用户对所有会话的持续授权，修改本段即可调整 agent 的自主程度。

| 项 | 设置 |
|---|---|
| 每次“继续”的推进范围 | 推进完**当前里程碑**，在里程碑演示点汇报并停下 |
| 代码汇入方式 | 每步一个 PR；CI（`arch-check`）通过后 agent 用 merge commit 自动合并到 `main`；改动命中 `.github/CODEOWNERS` 路径的 PR 不自动合并，等用户处理 |
| 付费 API 费用上限 | 每步 ¥100；预计会超出时登记待决事项并停下 |
| 主观判断类验收 | 给出候选和推荐，登记待决事项，等用户确认；等待期间先做同一里程碑内不受影响的步骤 |

**无需询问即可执行**：在会话分支上提交与推送；为当前步骤创建 PR；合并满足上表条件的 PR；在费用上限内调用付费模型 API；在 `spikes/`、`packages/` 等路线图规定的位置新建和修改文件。

**必须先问用户**：修改 `tools/archcheck/rules.toml` 或 `exceptions.toml`；变更 ADR 状态；删除分支、数据或已发布内容；超出费用上限；越过当前里程碑；本段未列出的对外操作（例如在外部平台发布内容）。

## 当前状态

- **当前里程碑**：M0.2 单项能力摸底
- **当前步骤**：P0-03
- **步骤状态**：进行中
- **工作分支**：claude/lucid-hamilton-r7bd4w
- **PR**：—

## 步骤卡

- **步骤**：P0-03 剧本生成
- **目标**：`python3 -m poc script` 把一句话梗概经 DeepSeek 生成为 1 集 DramaIR v0 剧本，自动严格校验、有限次修复、记录费用；产出 5 份通过严格校验的样本、候选对比和评分表
- **做**：`poc/llm.py`（OpenAI 兼容 Chat 客户端，urllib，JSON 模式，失败分类）；`poc/pricing.py`（单价，注明来源与日期，费用为估算，用余额差复核）；`poc/prompts/script.v1.md`（Schema + 从 `checks` 常量渲染的语义规则，spikes 内版本化）；`poc/script.py`（生成 → 解析 → `validate` strict → 最多 2 轮修复 → `runs/<run_id>/samples/`、`summary.json`、费用保险）；CLI `python3 -m poc script`（默认梗概取 ep01 的 `series.logline`）；离线单元测试；真实评测 `deepseek-flash` 门禁批次 n=5 与 `deepseek-v4-pro` 对比批次 n=5；证据入库 `docs/reports/p0/P0-03/`；结论报告 `docs/reports/p0/P0-03.md`（含 agent 初评）；登记 D-003 人工评分
- **不做**：其他供应商 LLM 评测（无密钥或被拦截，报告列为待补测）；分镜评测（P0-04）及下游环节；node_key 缓存与串联（P0-12）；多集、审稿 Agent；Prompt 迁入 `packages/prompts`；修改 DramaIR Schema、阈值、ep01 及任何 CODEOWNERS 路径；新增 pip 依赖
- **验收**：1）`cd spikes/poc && python3 -m poc script --n 5` 退出码 0，产物齐全；2）同一次运行顺序生成 5 份、不挑选，每份最终产物 `validate --strict` 0 错误 0 警告、自动修复 ≤ 2 轮，任一失败整批重跑且失败批次写入报告；3）`docs/reports/p0/P0-03/s*.json` 独立 `validate --strict` 通过，每份 1 集、logline 与输入一致；4）summary 与报告含首次通过率与每份修复轮数；5）calls.jsonl 每次尝试 1 行（provider、model、usage、cost_cny、cost_basis、extra.attempt），本步费用 ≤ ¥100 且与“已花费”一致；6）runs 与报告中不含密钥；7）离线单元测试全部通过（不设密钥也通过）；8）报告含样例、单价、耗时、失败率、推荐方案、评分表和标注“agent 初评（非人工）”的初评；9）D-003 人工评分由用户给出后写入报告（此前保持 🔄）；10）`make arch-check`、`make drama-ir-check` 通过，ep01 sha256 不变；11）改动只在 `spikes/poc/`、`docs/reports/p0/`、`docs/progress.md`、`docs/roadmap.md`
- **涉及**：architecture §4、§5.1.2、§5.1.4；ADR-0002、ADR-0004（P0 豁免）、ADR-0010；governance §3 注（spikes 不受 INV-01 ~ INV-10 约束）；INV-03；TD-001
- **估时**：1.5 天

## 子任务

- [ ] LLM 客户端与计价：`poc/llm.py`、`poc/pricing.py`，离线测试 `tests/test_llm.py`
- [ ] Prompt：`poc/prompts/script.v1.md` 与 Prompt 构建（Schema + checks 常量规则），`poc` 能找到 `dramio_drama_ir`，加测试
- [ ] 生成流程 `poc/script.py`：生成、解析、严格校验、修复、产物、summary、费用保险，离线测试 `tests/test_script.py`
- [ ] CLI `python3 -m poc script` 与 README 用法，加 CLI 测试
- [ ] 真实冒烟：`deepseek-flash`、`deepseek-v4-pro` 各 1 次，确认 JSON 模式与输出长度，实测单价并更新“已花费”
- [ ] 门禁批次：`deepseek-flash` `--n 5`（失败则改 Prompt 后整批重跑，全部记录）
- [ ] 对比批次：`deepseek-v4-pro` `--n 5`
- [ ] 证据入库 `docs/reports/p0/P0-03/` 并独立复验
- [ ] 评分表与 agent 初评（含 ep01 锚点）
- [ ] 结论报告 `docs/reports/p0/P0-03.md`
- [ ] 登记 D-003（人工评分），更新本步费用
- [ ] 验证（verifier）、评审（arch-reviewer）、交付

## 本步费用

- **已花费**：¥0
- **上限**：¥100

## 待决事项

需要用户拍板的事项。用户用 `/decide 编号 决定` 或直接回复即可；状态为“待决”的事项会在每次会话开始时提醒。

| 编号 | 步骤 | 问题 | 候选 | 推荐与理由 | 状态 | 决定 |
|---|---|---|---|---|---|---|
| D-001 | P0-01 | archcheck 查不出产品代码经 `PYTHONPATH=spikes/poc` 用 `import poc` 依赖 spikes（INV-03 守卫缺口，评审时在临时副本中复现）。是否在 `tools/archcheck/rules.toml` 的 `[aliases]` 中加 `"poc" = "spikes"`？ | A：加这一行别名（受保护路径，单独 PR，由你审阅合并）；B：暂不处理，P1-01 前一并处理 | A：改动一行，立即补上缺口；目前还没有产品代码，不会误报 | 已决 | A：在 `rules.toml` 的 `[aliases]` 中加 `"poc" = "spikes"`，单独 PR 由用户审阅合并（2026-09-30，用户：“补上漏洞”） |
| D-002 | P0-02 | 是否确认标准样例《雨夜反击》第 1 集作为 P0-03 ~ P0-13 所有评测的**固定输入**（确认后冻结，不再原地修改）？ | A：确认 `packages/drama-ir/examples/v0/ep01.json`（sha256 `cd02daef022c…`），可读版本 `packages/drama-ir/examples/v0/ep01.md`；B：按你的意见修改后再确认（说明要改什么） | A：2 角色、3 场、13 镜头、15 句台词、60 秒；覆盖空镜、道具插入、画外音、音效、3 种场景情绪，结尾有悬念；已按验收审阅修正剧情自洽问题 | 已决 | A：确认 `packages/drama-ir/examples/v0/ep01.json`（sha256 `cd02daef022c6e78227ef3bf366adb2b5142a979a803bcf4373cdb8765a6fb59`）为 P0-03 ~ P0-13 评测的固定输入，冻结（2026-09-30，用户） |

## 已知的前置条件

- P0-03 起需要调用模型供应商 API：在云环境设置中添加要评测的供应商的密钥环境变量，并在网络设置中放行其域名，新会话才会生效。用 `cd spikes/poc && python3 -m poc doctor --require <供应商>` 确认可用。变量名与域名（P0-01 确定，详见 `spikes/poc/README.md`）：
  - Anthropic `ANTHROPIC_API_KEY` · `api.anthropic.com`
  - 阿里云百炼 `DASHSCOPE_API_KEY` · `dashscope.aliyuncs.com`
  - DeepSeek `DEEPSEEK_API_KEY` · `api.deepseek.com`
  - 火山方舟 `ARK_API_KEY` · `ark.cn-beijing.volces.com`
  - MiniMax `MINIMAX_API_KEY`（可选 `MINIMAX_GROUP_ID`）· `api.minimaxi.com`
  - ElevenLabs `ELEVENLABS_API_KEY` · `api.elevenlabs.io`
  - 可灵 `KLING_ACCESS_KEY`、`KLING_SECRET_KEY` · `api-beijing.klingai.com`
  - fal `FAL_KEY` · `fal.run`、`queue.fal.run`
  - Replicate `REPLICATE_API_TOKEN` · `api.replicate.com`、`replicate.delivery`
  - Google Gemini / Veo `GEMINI_API_KEY` · `generativelanguage.googleapis.com`
  - Runway `RUNWAYML_API_SECRET` · `api.dev.runwayml.com`
- 当前云容器（2026-09-30）：没有任何供应商密钥；`api.anthropic.com`、`generativelanguage.googleapis.com` 可以访问，其余供应商域名被网络策略拦截（doctor 报 `UNREACHABLE`）。
- DeepSeek（2026-09-30 更新）：云环境已配置 `DEEPSEEK_API_KEY` 并放行 `api.deepseek.com`，doctor 报 OK；可用模型 `deepseek-flash`（V4.1-Flash）、`deepseek-v4-pro`。
- ADR-0001 ~ 0010 目前为 Proposed，在 P0-14 统一评审。
- GitHub 上需要用户手动完成：把默认分支改为 `main`；为 `main` 开启分支保护（要求 `arch-check` 通过，不要求 Code Owner 评审）。

## 交接日志

只追加，不修改历史记录。

| 日期 | 步骤 | 做了什么 | 验证 | 下一步 |
|---|---|---|---|---|
| 2026-09-30 | — | 搭建自主推进工作流：`/continue`、`/step`、`/progress`、`/decide`，3 个子代理，SessionStart 与 Stop hooks，进度一致性检查 | `make arch-check` 通过 | 用户说“继续当前进度”后开始 M0.1（P0-01、P0-02） |
| 2026-09-30 | P0-01 | `spikes/poc` 验证脚手架：标准库 `.env` 加载（进程环境优先）；11 家候选供应商注册表（变量名、能力、免费探测、域名）；`runs/<run_id>/`（meta.json、calls.jsonl、doctor.json）与 `Run.call()` 调用记录；`python -m poc doctor`（`--offline`、`--require`，只用免费只读 GET，不跟随重定向）；README；前置条件写入变量名与域名。评审阻断项（含换行的密钥可能泄露并导致崩溃）已修复 | verifier 第 1–11 条通过、第 12 条（真实密钥得到 OK）按约定移到 P0-03 的阻塞检查；38 个单元测试通过；`make arch-check` 通过。容器实测：无密钥时 11 家 `MISSING`、退出码 0；伪造密钥时 anthropic `INVALID(401)`、gemini `INVALID(400 API_KEY_INVALID)`、deepseek 等 `UNREACHABLE(Tunnel 403)`。费用 ¥0 | P0-02 DramaIR v0 与标准样例；D-001 待用户决定 |
| 2026-09-30 | D-001 | 按用户决定补上 INV-03 守卫缺口：`rules.toml` `[aliases]` 加 `"poc" = "spikes"`，archcheck 新增 2 个测试（产品代码 `import poc` / `from poc.runlog import …` 被拦截；spikes 内部与 `pocket` 等相似名不受影响） | `make arch-check` 通过 | 受保护路径，单独 PR 等用户合并；P0-02 继续 |
| 2026-09-30 | P0-02 | DramaIR v0：Schema（只用结构关键字、全部必填）、标准样例 ep01（2 角色 3 场 13 镜头 15 句台词 60 秒）与 ep01.md、`dramio_drama_ir` 校验（子集 Schema 校验器 fail closed + 语义校验）与渲染、`make drama-ir-check`、README、architecture §4.2/§15 说明、TD-001。按验收与评审修复：NaN/Infinity、Schema 形状检查、CLI 退出码、render 中文标签、样例剧情自洽 | verifier 第 1–9 条通过（16 个反例全部返回 1 并带路径，与 jsonschema 结论一致）；52 个单元测试通过，jsonschema 差分测试通过；`make arch-check` 通过；评审无阻断项。费用 ¥0 | 等用户审阅合并 PR #2 并决定 D-002；D-002 确认后把 P0-02 标为 ✅，M0.1 完成 |
| 2026-09-30 | D-002 | 用户确认标准样例 `ep01.json`（sha256 `cd02daef022c…`）为 P0 评测的固定输入，即日冻结，修订须新增文件 | — | P0-02 可标为 ✅，M0.1 完成；下一步 M0.2 P0-03 |
| 2026-09-30 | P0-02 | 收尾：合入 D-002 决定记录；P0-02 标为 ✅，M0.1 完成；进度指针移到 M0.2 P0-03 | 样例 sha256 复核与 D-002 一致（`cd02daef022c…`）；`make drama-ir-check`、`make arch-check` 通过。费用 ¥0 | M0.2：P0-03 剧本生成（需先配置供应商密钥与放行域名，见“已知的前置条件”） |
| 2026-09-30 | P0-03 | 未开工：用户提供 DeepSeek 密钥，写入 `spikes/poc/.env`（git 忽略，不入库）；`python3 -m poc doctor --require deepseek` 报 `UNREACHABLE(Tunnel 403)`，`api.deepseek.com` 被网络策略拦截。M0.2 其余步骤（P0-04 ~ P0-11）同样依赖尚未配置或被拦截的供应商，全部阻塞 | doctor 退出码 1；`make arch-check` 通过。费用 ¥0 | 用户放行 `api.deepseek.com` 并在云环境设置中添加 `DEEPSEEK_API_KEY` 后，新会话开始 P0-03 |
