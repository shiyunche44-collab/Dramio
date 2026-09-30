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
- **当前步骤**：P0-04
- **步骤状态**：未开始
- **工作分支**：claude/lucid-hamilton-r7bd4w
- **PR**：—

## 步骤卡

（开工时由 `/step` 填写：目标 / 做 / 不做 / 验收 / 涉及 / 估时）

## 子任务

（开工时由 `/step` 填写，格式为 `- [ ] 子任务`，完成后改为 `- [x]`）

## 本步费用

- **已花费**：¥0
- **上限**：¥100

## 待决事项

需要用户拍板的事项。用户用 `/decide 编号 决定` 或直接回复即可；状态为“待决”的事项会在每次会话开始时提醒。

| 编号 | 步骤 | 问题 | 候选 | 推荐与理由 | 状态 | 决定 |
|---|---|---|---|---|---|---|
| D-001 | P0-01 | archcheck 查不出产品代码经 `PYTHONPATH=spikes/poc` 用 `import poc` 依赖 spikes（INV-03 守卫缺口，评审时在临时副本中复现）。是否在 `tools/archcheck/rules.toml` 的 `[aliases]` 中加 `"poc" = "spikes"`？ | A：加这一行别名（受保护路径，单独 PR，由你审阅合并）；B：暂不处理，P1-01 前一并处理 | A：改动一行，立即补上缺口；目前还没有产品代码，不会误报 | 已决 | A：在 `rules.toml` 的 `[aliases]` 中加 `"poc" = "spikes"`，单独 PR 由用户审阅合并（2026-09-30，用户：“补上漏洞”） |
| D-002 | P0-02 | 是否确认标准样例《雨夜反击》第 1 集作为 P0-03 ~ P0-13 所有评测的**固定输入**（确认后冻结，不再原地修改）？ | A：确认 `packages/drama-ir/examples/v0/ep01.json`（sha256 `cd02daef022c…`），可读版本 `packages/drama-ir/examples/v0/ep01.md`；B：按你的意见修改后再确认（说明要改什么） | A：2 角色、3 场、13 镜头、15 句台词、60 秒；覆盖空镜、道具插入、画外音、音效、3 种场景情绪，结尾有悬念；已按验收审阅修正剧情自洽问题 | 已决 | A：确认 `packages/drama-ir/examples/v0/ep01.json`（sha256 `cd02daef022c6e78227ef3bf366adb2b5142a979a803bcf4373cdb8765a6fb59`）为 P0-03 ~ P0-13 评测的固定输入，冻结（2026-09-30，用户） |
| D-003 | P0-03 | 剧本生成的人工评分与默认方案：15 份样本都已通过严格校验，“有人工评分记录”需要你打分；同时确认默认 LLM | A：15 份全部人工评分；B：人工抽评 3 份——flash s04（初评最高）、flash s03（flash 最低）、v4-pro s01（初评判为不可用），其余沿用 agent 初评；C：只确认 agent 初评，不打分。默认方案候选：`deepseek-flash`（思维链开）/ `deepseek-v4-pro` / flash 关思维链。样本与评分表：`docs/reports/p0/P0-03.md`、`docs/reports/p0/P0-03/<候选>/sNN.md` | B + `deepseek-flash`：抽评工作量小，而且是真正的人工评分（C 不满足验收本意）；flash 初评均分 3.89 最高、5/5 通过、每集约 ¥0.10、53 秒。回复示例：`/decide D-003 B flash-s04:4,5,4,4,4,5,4 flash-s03:… v4pro-s01:… 默认 flash`（7 个数依次为 D1 ~ D7） | 已决 | 人工评分：flash s04“不错”（认可）；其余 14 份沿用 agent 初评，报告中注明；默认 LLM 定为 `deepseek-flash`（思维链默认开）（2026-09-30，用户） |

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
| 2026-09-30 | P0-03 | 实现与评测完成，等待 D-003：`poc/llm.py`（OpenAI 兼容 Chat、JSON 模式、失败分类、余额查询）、`poc/pricing.py`、Prompt `script.v1`、`poc/script.py`（严格校验 + 最多 2 轮修复、瞬时故障重试、费用保险）、CLI `python3 -m poc script`；三个候选（deepseek-flash、flash 关思维链、deepseek-v4-pro）各 5 份全部通过严格校验；证据与结论报告 `docs/reports/p0/P0-03.md`（含 agent 初评）。按评审修复：费用中止时 summary 计入未完成样本的费用、`write_json` 脱敏兜底、入库证据去掉余额绝对值、补交冒烟证据 | verifier 第 1–8、10、11 条通过，第 9 条（人工评分）待用户；75 个离线单元测试通过；15 份报告样本独立 `validate --strict` 通过；`make arch-check`、`make drama-ir-check` 通过；评审无阻断项。费用估算 ¥2.44（余额实际减少 ¥1.46） | 用户决定 D-003 后把人工评分写入报告，P0-03 标为 ✅；M0.2 其余步骤：P0-04 依赖 P0-03，P0-05/06/10 缺供应商密钥或域名被拦截 |
| 2026-09-30 | P0-03 | 收尾：D-003 已决（人工认可 flash s04“不错”，其余沿用 agent 初评；默认 LLM `deepseek-flash`），人工评分写入报告；P0-03 标为 ✅，进度指针移到 P0-04 | verifier 第 1–11 条全部满足（第 9 条由 D-003 满足）；`make arch-check` 通过。本步费用 ¥2.44（估算，余额实际减少 ¥1.46） | P0-04 分镜拆解（DeepSeek 可用，不受阻塞）；P0-05/06/10 仍缺供应商密钥或域名被拦截 |
