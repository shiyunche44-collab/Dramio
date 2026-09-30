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

- **当前里程碑**：M0.1 准备
- **当前步骤**：P0-02
- **步骤状态**：进行中
- **工作分支**：claude/ecstatic-euler-z68coi
- **PR**：https://github.com/shiyunche44-collab/Dramio/pull/2 已合并（含 D-001）；只剩 D-002

## 步骤卡

（开工时由 `/step` 填写：目标 / 做 / 不做 / 验收 / 涉及 / 估时）

- **步骤**：P0-02 DramaIR v0 与标准样例
- **目标**：最小 DramaIR v0 JSON Schema、一集手写的 1 分钟标准样例、零依赖的校验命令；P0-03 ~ P0-13 以该样例为固定输入，P0-03 用同一命令检查 LLM 输出。
- **做**：
  - `packages/drama-ir/schema/v0/drama.schema.json`：JSON Schema 2020-12，`$defs` 定义剧、角色、集、场、镜、台词；只用结构关键字（`type`、`properties`、`required`、`additionalProperties: false`、`items`、`enum`、`$ref`、`$defs`、`title`、`description`、`$schema`、`$id`），全部字段 required、不用 null；字段是 architecture.md §4.2 的子集，命名与嵌套对齐。
  - 标准样例 `packages/drama-ir/examples/v0/ep01.json`：中文竖屏，约 60 秒，2 个角色，约 3 场、约 12 个镜头、约 15 句台词；含空镜、道具插入、画外音、音效、至少 2 种场景情绪；结尾有钩子；纯虚构。
  - Python 包 `packages/drama-ir/python/dramio_drama_ir/`（仅标准库）：子集 Schema 校验器（遇到不支持的关键字报错）；语义校验（错误 / 警告两级，`--strict`）；`validate [--strict] [--json] FILE...`（退出码 0 / 1 / 2）；`render FILE` 输出 Markdown 剧本摘要；错误带 JSON 路径；不定义镜像 Schema 的类型（INV-02）。
  - `examples/v0/ep01.md` 由 `render` 生成并提交，有一致性测试；单元测试用变异样例生成反例；装了 `jsonschema` 时做可选的差分测试。
  - Makefile `drama-ir-check`；`packages/drama-ir/README.md`；architecture.md §4 加指向 v0 的说明；tech-debt.md 登记 TD-001（v0 无生成类型与兼容性检查，P1-03 偿还）。
  - 登记 D-002：确认 `ep01.json`（附 sha256）为 P0 评测的固定输入。
- **不做**：生成 zod / pydantic 类型、兼容性检查（P1-03）；Take、Bible、Location、Prop、Look、`generation_policy`、`locks`、`resolved_s`、revision；spikes 接入校验、LLM 生成剧本（P0-03）；第三方依赖与打包（P1-01）；把校验接入 CI；ADR 变更。
- **验收**：
  1. `make drama-ir-check` 返回 0，其中 `validate --strict` 对 `ep01.json` 0 错误 0 警告；
  2. 样例规模：角色 2；镜头 10–14；台词 13–17；`hint_s` 合计 50–70 秒；至少 1 个无人物镜头、1 句 `voiceover`、1 个非空 `sfx`；
  3. 反例都返回 1 并给出 JSON 路径，至少覆盖：speaker 不存在、shot_id 重复、缺必需字段、多余字段、非法枚举、`hint_s` 越界、时长偏离；
  4. Schema 只用支持的关键字；注入不支持的关键字时报错；
  5. `ep01.md` 与 `render` 输出逐字一致（有测试）；
  6. 在 `spikes/poc` 下经 `PYTHONPATH=../../packages/drama-ir/python` 能 `import dramio_drama_ir` 并校验样例得到 0 错误；
  7. 包代码只 import 标准库和本包；`make arch-check` 通过；
  8. 没有镜像 Schema 的类或类型定义（INV-02，评审项）；
  9. tech-debt.md 有 TD-001；architecture.md §4 有指向 v0 的说明；
  10. 团队确认样例作为固定输入：需用户确认，登记 D-002。
- **涉及**：architecture.md §4.1、§4.2、§5.3、§15；governance.md §3（INV-02）、§4.1、§7.2；ADR-0002（Proposed，本步与之一致）、ADR-0008；INV-02、INV-03、INV-11。
- **估时**：1 天（最多 1.5 天）

## 子任务

（开工时由 `/step` 填写，格式为 `- [ ] 子任务`，完成后改为 `- [x]`）

- [x] Schema：`schema/v0/drama.schema.json` 与 README 字段说明表
- [x] 子集 Schema 校验器与 `load_schema`，附单元测试
- [x] 语义校验与 `validate` 命令行（`--strict`、`--json`、退出码），附反例测试
- [x] 标准样例 `examples/v0/ep01.json`，`validate --strict` 零警告
- [x] `render` 子命令与 `ep01.md`，一致性测试；可选 `jsonschema` 差分测试
- [x] Makefile `drama-ir-check`；README 用法与冻结规则；architecture.md §4 指向说明；tech-debt.md TD-001
- [ ] 验证与交付（verifier 1–9 通过；PR #2 已合并；只等 D-002）：verifier 验收；登记 D-002（附 sha256）；PR 等用户审阅合并

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
