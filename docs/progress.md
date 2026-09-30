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
- **当前步骤**：P0-06
- **步骤状态**：进行中
- **工作分支**：claude/sweet-lamport-cwwsvb
- **PR**：—

## 步骤卡

- **步骤**：P0-06 角色定妆（估时 1 天，可能 1–1.5 天）
- **目标**：用方舟 Seedream（阶段 1 用按量付费的 5.0 flash 对比 4.0；账号改为 Agent Plan 后只能用 5.0 pro，按 D-007 主图与阶段 2 用 pro 重跑，flash 结果留作对照）为 ep01（D-002）的苏晚、陆沉各生成一套定妆：文生图主图 + 三视图（正 / 侧 / 背）+ 表情集；主图原样保存并记录 sha256，供 P0-07 / P0-08 作身份参考；对比出默认定妆路线，由用户（D-006）为每个角色选定一套。
- **做**：
  - 冒烟核实方舟图像接口 `POST /api/v3/images/generations`：`model`、`prompt`、`size`（9:16、≤1.5K 档）、`response_format`（优先 `b64_json`）、`watermark`、`seed`、参考图 `image` 传法（data URI）、响应与错误字段、审核拒绝错误码；结果写入报告“接口”一节。
  - `poc/seedream.py`（标准库，可注入 transport，复用 `doctor.urlopen`，`config.redact` 脱敏）：失败分类 network / http / api_error / moderation / bad_response / empty，区分瞬时；每个请求经 `Run.call()` 记账，填写 `node_key`（只记录、不缓存）；calls.jsonl 与 summary 不写 base64。
  - `pricing` 图像单价（vendor-volcengine.md §3.2：pro ≤1.5K ¥0.30、>1.5K ¥0.60，flash ¥0.12，第 2 张参考图起 ¥0.02）；审核拒绝与不可重试 4xx 不计费，网络中断 / 5xx / 429 保守按全额。
  - 图像文件工具：sha256、字节数、JPEG SOF / PNG IHDR 宽高。
  - Prompt 模板 `costume.v1`（主图、三视图、表情、ref 版、设定板），字段运行时取自 ep01 `characters[]` 与 `series.visual_style`（INV-02）；表情集 = neutral + 该角色在 ep01 `delivery.emotion` 中出现过的值（≤6）；允许修订 1 次为 v2 并在报告说明。
  - 所有请求 `watermark=false`（D-005）。
  - CLI `python3 -m poc costume`：`--stage main --model pro|flash --n 3`；`--stage derive --mode text|ref|group --base <主图>`；`--max-cost-cny`；离线 `--export`、`--verify`，与生成参数互斥（退出码 2）。
  - 评测：阶段 1 主图 2 角色 × {flash, v4（`doubao-seedream-4-0-20260415`）} × 3 张；阶段 2 以 agent 初评推荐主图为基础、固定阶段 1 推荐模型，比较 text / ref / sheet 三种派生方式（5.0 pro / flash 不支持组图，sheet 为单张文生图设定板：三视图横排一张、表情网格一张），每角色 3 视图 + 表情集，2 轮（第 2 轮只留统计）。
  - manifest 标注 `t2i_original` / `seedance_eligible`：只标在无参考图的文生图原始产物上；ref 派生标为不可用。
  - 证据入库 `docs/reports/p0/P0-06/`：主图原图字节不改动；`manifest.json`；每角色定妆卡 `<角色>.md`（markdown 并排，不拼图）；`run-summary.json` / `run-calls.jsonl`；冒烟证据 `smoke/`。报告 `docs/reports/p0/P0-06.md`（结论、接口、方法、候选对比、agent 初评、Seedance 可用性与 30 天到期日、费用、待补测、给 P0-07 / P0-08 的建议）；README 增加 costume 一节；登记 D-006。
- **不做**：人脸相似度与一致性量化（P0-07）；镜头关键帧（P0-07）；验证 Seedance 是否接受定妆图（P0-08）；其它图像供应商、即梦 / DreamO（无 AK/SK）、MediaKit（被拦截）；场景道具定妆；LoRA / 身份保持插件；修改 DramaIR Schema 或 ep01；模型网关、CAS、缓存命中；doctor 对 ark 的探测；提示词写法系统对比；拼图、缩略图、新增 Python 依赖。
- **验收**（全部可离线复核）：
  1. `cd spikes/poc && python3 -m unittest discover -s tests -t .` 全部通过且不访问网络；新增测试覆盖请求体（文生图 / 单参考图 / 多参考图）、响应解析（b64 / url / 无图）、错误分类与瞬时判定、计价（档位、参考图加价、拒绝不计费、瞬时失败全额）、模板渲染与表情集推导、JPEG / PNG 尺寸解析、`--verify` 正反例、离线与生成参数互斥。
  2. 冒烟核实的接口事实写入报告“接口”一节（端点、参数、响应字段、尺寸限制、参考图传法、计费依据、`watermark` 取值、审核拒绝错误码或“未遇到”）；冒烟 `run-calls.jsonl` 入库 `P0-06/smoke/`。
  3. 主图：2 角色 × {flash, v4} × 3 = 12 张文生图原始产物入库，sha256 与 manifest 一致；失败张数与原因记在 summary。
  4. 派生：每个角色在 text / ref / sheet 下各有三视图 + 表情集的第 1 轮产物或失败记录；第 2 轮统计写入 run-summary。
  5. 对比表数字（成功 / 首次成功、失败类型、每张与每角色一套的耗时和费用、输出尺寸）可从入库 `run-summary.json` / `run-calls.jsonl` 复算。
  6. 入库 `run-calls.jsonl` 的 `cost_cny` 合计与报告和“本步费用”一致（±¥0.01），冒烟在账内；本步总费用 ≤ ¥100。
  7. `python3 -m poc costume --verify ../../docs/reports/p0/P0-06` 返回 0：sha256、字节数、宽高与 manifest 一致；manifest 与成功请求一一对应；`seedance_eligible=true` 只在无参考图的文生图产物上。
  8. `make arch-check`、`make drama-ir-check` 通过；ep01.json sha256 仍为 `cd02daef022c6e78227ef3bf366adb2b5142a979a803bcf4373cdb8765a6fb59`。
  9. 设置了密钥时 `grep -rF "$ARK_API_KEY" docs spikes/poc --exclude=.env` 无匹配；calls.jsonl 与 summary 不含 base64 图像（每行 < 16 KB）。
  10. 改动只涉及 `spikes/poc/`、`docs/reports/p0/`、`docs/progress.md`、`docs/roadmap.md`；`pyproject.toml` 的 `dependencies` 仍为 `[]`。
  11. `du -sb docs/reports/p0/P0-06` ≤ 16 MB（D-008，原 10 MB；主图原图必须入库，runs/ 不入库）；超额时先降派生图尺寸或表情数，主图原图不降级。
  12. 定妆选定（需用户确认）：D-006 登记每个角色的候选主图与派生方式、推荐与理由、定妆卡路径；用户选定后写入报告。
- **涉及**：architecture.md §5.2.1–5.2.3、§9.3、§9.4；ADR-0004、ADR-0005、ADR-0006；INV-01、INV-02、INV-05、INV-06、INV-07、INV-08（D-005）、INV-10；vendor-volcengine.md §1 第 3 条、§3.2；D-002。

## 子任务

- [x] 1. 冒烟核实方舟图像接口（flash，≤5 次，约 ¥1）：b64 / url、size 限制、watermark / seed、data URI 参考图、usage 与错误体、单张字节数（run `20260930-194423-costume-smoke-b617`，¥0.56；url 下载域名被拦截，只能用 b64_json）
- [x] 2. `pricing` 图像单价与计价 + 单测
- [x] 3. `poc/seedream.py` 客户端（请求构造、解析、错误分类、脱敏）+ 单测
- [x] 4. 图像文件工具（sha256、JPEG / PNG 宽高）+ 单测
- [x] 5. Prompt 模板 `costume.v1` 与渲染（ep01 字段、表情集）+ 单测
- [x] 6. `poc/costume.py` 主图阶段 + 注册 CLI + 单测
- [x] 7. 派生阶段 text / ref / sheet（5.0 pro / flash 不支持组图，group 改为 sheet 设定板）（manifest `t2i_original` / `seedance_eligible`）+ 单测
- [x] 8. 离线 `--export` / `--verify` + 单测；README costume 一节
- [x] 9. 评测阶段 1：主图 12 张，agent 初评选推荐主图与模型（flash 6/6、v4 6/6；推荐 flash，苏晚 main-02、陆沉 main-02）
- [x] 10. 评测阶段 2：text / ref / sheet × 2 角色 × 2 轮（D-007：pro 主图 run `20260930-201207-costume-3165` 6/6，基础图苏晚、陆沉 main-02；派生 pro + costume.v2 + `--small`：text run `203811-costume-88ee` 34/34、ref `203811-costume-8c43` 34/34、sheet `203811-costume-b7c2` 8/8，全部首次成功）
- [x] 11. 证据导出、定妆卡、体积检查、`--verify` 返回 0（`--verify` 9 个目录 86 张通过；体积 16.94 MB 超 16 MB，登记 D-009）
- [x] 12. 报告 `docs/reports/p0/P0-06.md`、登记 D-006、更新本步费用
- [ ] 13. 验证（verifier）、评审（arch-reviewer）、交付 PR

## 本步费用

- **已花费**：¥31.48（估算：冒烟 ¥0.56，主图 flash ¥0.72 / v4 ¥1.40 / pro ¥1.80，派生 flash text ¥1.56 / ref ¥1.68 / sheet ¥0.96，派生 pro text ¥10.20 / ref ¥10.20 / sheet ¥2.40；pro 共 ¥24.60 为 Agent Plan 套餐内调用的刊例价等价费用，不实付）
- **上限**：¥100

## 待决事项

需要用户拍板的事项。用户用 `/decide 编号 决定` 或直接回复即可；状态为“待决”的事项会在每次会话开始时提醒。

| 编号 | 步骤 | 问题 | 候选 | 推荐与理由 | 状态 | 决定 |
|---|---|---|---|---|---|---|
| D-001 | P0-01 | archcheck 查不出产品代码经 `PYTHONPATH=spikes/poc` 用 `import poc` 依赖 spikes（INV-03 守卫缺口，评审时在临时副本中复现）。是否在 `tools/archcheck/rules.toml` 的 `[aliases]` 中加 `"poc" = "spikes"`？ | A：加这一行别名（受保护路径，单独 PR，由你审阅合并）；B：暂不处理，P1-01 前一并处理 | A：改动一行，立即补上缺口；目前还没有产品代码，不会误报 | 已决 | A：在 `rules.toml` 的 `[aliases]` 中加 `"poc" = "spikes"`，单独 PR 由用户审阅合并（2026-09-30，用户：“补上漏洞”） |
| D-002 | P0-02 | 是否确认标准样例《雨夜反击》第 1 集作为 P0-03 ~ P0-13 所有评测的**固定输入**（确认后冻结，不再原地修改）？ | A：确认 `packages/drama-ir/examples/v0/ep01.json`（sha256 `cd02daef022c…`），可读版本 `packages/drama-ir/examples/v0/ep01.md`；B：按你的意见修改后再确认（说明要改什么） | A：2 角色、3 场、13 镜头、15 句台词、60 秒；覆盖空镜、道具插入、画外音、音效、3 种场景情绪，结尾有悬念；已按验收审阅修正剧情自洽问题 | 已决 | A：确认 `packages/drama-ir/examples/v0/ep01.json`（sha256 `cd02daef022c6e78227ef3bf366adb2b5142a979a803bcf4373cdb8765a6fb59`）为 P0-03 ~ P0-13 评测的固定输入，冻结（2026-09-30，用户） |
| D-003 | P0-03 | 剧本生成的人工评分与默认方案：15 份样本都已通过严格校验，“有人工评分记录”需要你打分；同时确认默认 LLM | A：15 份全部人工评分；B：人工抽评 3 份——flash s04（初评最高）、flash s03（flash 最低）、v4-pro s01（初评判为不可用），其余沿用 agent 初评；C：只确认 agent 初评，不打分。默认方案候选：`deepseek-flash`（思维链开）/ `deepseek-v4-pro` / flash 关思维链。样本与评分表：`docs/reports/p0/P0-03.md`、`docs/reports/p0/P0-03/<候选>/sNN.md` | B + `deepseek-flash`：抽评工作量小，而且是真正的人工评分（C 不满足验收本意）；flash 初评均分 3.89 最高、5/5 通过、每集约 ¥0.10、53 秒。回复示例：`/decide D-003 B flash-s04:4,5,4,4,4,5,4 flash-s03:… v4pro-s01:… 默认 flash`（7 个数依次为 D1 ~ D7） | 已决 | 人工评分：flash s04“不错”（认可）；其余 14 份沿用 agent 初评，报告中注明；默认 LLM 定为 `deepseek-flash`（思维链默认开）（2026-09-30，用户） |
| D-004 | P0-05 | 配音默认方案与角色音色（主观，需试听）：4 个候选 180/180 成功、同音字折叠后 CER 全为 0、费用与耗时几乎相同，差别只在听感 | A `plain`（首选音色，无指令）；B `instruct`（加语音指令）；C `instruct-speed`（指令 + 语速映射）；D `alt-instruct-speed`（次选音色 苏晚 清新女声 / 陆沉 儒雅逸辰 + 指令 + 语速）。整集试听 `docs/reports/p0/P0-05/<候选>/ep01.mp3`；音色初选 `docs/reports/p0/P0-05/voices/`；报告 `docs/reports/p0/P0-05.md` | C + 音色 苏晚 知性灿灿 2.0（`zh_female_cancan_uranus_bigtts`）/ 陆沉 高冷沉稳 2.0（`zh_male_gaolengchenwen_uranus_bigtts`）：语速映射实测生效（speed 0.9 的句子长 7%–18%），指令不计费、不影响 CER，音色贴合角色描述、初选无重试；若觉得指令腔调不自然则选 A。回复示例：`/decide D-004 C` 或 `/decide D-004 A 苏晚改清新女声` | 已决 | C `instruct-speed`：苏晚 知性灿灿 2.0（`zh_female_cancan_uranus_bigtts`）、陆沉 高冷沉稳 2.0（`zh_male_gaolengchenwen_uranus_bigtts`），加语音指令（`tts-instruct.v1`），并把 `delivery.speed` 映射到 `speech_rate`，作为默认配音方案（2026-09-30，用户） |
| D-005 | P0-06 | 定妆图是否关闭 Seedream 的可见“AI 生成”水印（与 INV-08 相关：定妆图是内部中间资产、不发布，成片 AIGC 标识由 P0-11 负责） | A：关闭（`watermark=false`）；B：保留 | A：水印会污染作为参考图的定妆图；INV-08 约束生产环境，P0 不属于生产 | 已决 | A：定妆图关闭可见水印（`watermark=false`）（2026-09-30，用户：“关掉水印”） |
| D-007 | P0-06 | 方舟改为 Agent Plan 包月套餐（新 Key 只能调 `/api/plan/v3`，图像只支持 Seedream 5.0 pro；flash / 4.0 返回 `UnsupportedModel`，新 Key 在按量付费的 `/api/v3` 上鉴权失败）后，P0-06 剩余评测怎么跑 | A：改用套餐内的 5.0 pro，主图 6 张 + 阶段 2（text / ref / sheet × 2 角色 × 2 轮）全部用 pro 重跑，flash 结果留作对照；B：给按量付费账户充值、换回旧 Key，按原计划用 flash 补跑（约 ¥4.8） | A：同模型内可比，费用走套餐额度；代价是推荐模型变为 pro（按量刊例价 ¥0.30 / 张，flash ¥0.12），结论中注明 | 已决 | A：改用 Agent Plan 的 5.0 pro 重跑（2026-09-30，用户：“选a”）。实现：`ARK_BILLING=plan`（默认）/ `payg`；plan 调用的 `cost_cny` 记按量刊例价的等价费用、不实付 |
| D-008 | P0-06 | 证据体积超上限：加入 pro 主图后 `docs/reports/p0/P0-06` 为 10.95 MB（步骤卡上限 10 MB），pro 派生第 1 轮入库后预计 13–15 MB | A：上限放宽到 16 MB，flash 对照证据完整保留；B：保持 10 MB，删除已入库的 flash 派生图，只留 run-summary / calls | A：flash 是 pro 的对照组，删图后无法复核对比结论 | 已决 | A：上限放宽到 16 MB（2026-09-30，用户：“a”） |
| D-006 | P0-06 | 定妆选定（主观）：每个角色选定一张主图和一种派生方式，作为 P0-07 / P0-08 的身份参考 | 主图：pro `main-01` / `main-02` / `main-03`（flash、v4 主图留作对照）；派生：A `ref`（主图作参考图的图生图）/ B `text`（独立文生图）/ C `sheet`（设定板，costume.v2）。并排见定妆卡 `docs/reports/p0/P0-06/char_suwan.md`、`char_luchen.md`；报告 `docs/reports/p0/P0-06.md` | 苏晚 pro `main-02`（`4e6cf20a`）+ A，陆沉 pro `main-02`（`33b545ed`）+ A：ref 76 张里脸型、发型、服装与主图最一致；text 表情脸型与年龄漂移、背面带出雨；sheet 板内一致但不是主图那张脸（陆沉发色变黑）。主图是文生图原始产物，可作 Seedance 参考到 2026-10-30。回复示例：`/decide D-006 A` 或 `/decide D-006 苏晚 main-01 + A` | 待决 | — |
| D-009 | P0-06 | 证据体积再次超上限：pro 派生第 1 轮入库后 `docs/reports/p0/P0-06` 为 16.94 MB（D-008 上限 16 MB）；派生图已是接口最小尺寸，主图原图不能降级 | A：上限放宽到 18 MB，全部保留；B：删除已入库的 flash `derive-text-flash`、`derive-ref-flash` 图像（因欠费不完整，约 4.1 MB），只留 run-summary / calls；C：pro 派生每角色只入库 neutral 加 2 个表情（重新导出，约减 1.5 MB） | A：只超 0.94 MB；flash 派生是 pro 的对照组，报告的对比结论依赖这些图；P0 证据不再继续增长（P0-07 起证据放各自目录） | 待决 | — |

## 已知的前置条件

- P0-03 起需要调用模型供应商 API：在云环境设置中添加要评测的供应商的密钥环境变量，并在网络设置中放行其域名，新会话才会生效。用 `cd spikes/poc && python3 -m poc doctor --require <供应商>` 确认可用。变量名与域名（P0-01 确定，详见 `spikes/poc/README.md`）：
  - Anthropic `ANTHROPIC_API_KEY` · `api.anthropic.com`
  - 阿里云百炼 `DASHSCOPE_API_KEY` · `dashscope.aliyuncs.com`
  - DeepSeek `DEEPSEEK_API_KEY` · `api.deepseek.com`
  - 火山方舟 `ARK_API_KEY` · `ark.cn-beijing.volces.com`
  - 豆包语音（火山引擎）`VOLC_SPEECH_API_KEY`（新版控制台 API Key，`x-api-key` 头）· `openspeech.bytedance.com`
  - MiniMax `MINIMAX_API_KEY`（可选 `MINIMAX_GROUP_ID`）· `api.minimaxi.com`
  - ElevenLabs `ELEVENLABS_API_KEY` · `api.elevenlabs.io`
  - 可灵 `KLING_ACCESS_KEY`、`KLING_SECRET_KEY` · `api-beijing.klingai.com`
  - fal `FAL_KEY` · `fal.run`、`queue.fal.run`
  - Replicate `REPLICATE_API_TOKEN` · `api.replicate.com`、`replicate.delivery`
  - Google Gemini / Veo `GEMINI_API_KEY` · `generativelanguage.googleapis.com`
  - Runway `RUNWAYML_API_SECRET` · `api.dev.runwayml.com`
- 当前云容器（2026-09-30）：没有任何供应商密钥；`api.anthropic.com`、`generativelanguage.googleapis.com` 可以访问，其余供应商域名被网络策略拦截（doctor 报 `UNREACHABLE`）。
- DeepSeek（2026-09-30 更新）：云环境已配置 `DEEPSEEK_API_KEY` 并放行 `api.deepseek.com`，doctor 报 OK；可用模型 `deepseek-flash`（V4.1-Flash）、`deepseek-v4-pro`。
- 火山引擎 AI MediaKit 视频口型对齐（2026-09-30 用户提供，P0-09 口型候选，尚未加入 `poc` 供应商注册表）：文档 <https://docs.volcengine.com/docs/6448/2658349>。`POST https://mediakit.cn-beijing.volces.com/api/v1/tools/lip-sync`（`video_url`、`audio_url`、可选 `enable_video_loop`）→ `task_id`；`GET /api/v1/tasks/{task_id}` 轮询到 `status=completed`，取 `result.video_url`（24 小时有效）和 `result.duration`。鉴权：`Authorization: Bearer <MediaKit API Key>`（在 AI MediaKit 控制台创建，不是方舟 `ARK_API_KEY`），变量名拟用 `MEDIAKIT_API_KEY`。输入：mp4 视频（单人真人、正脸、偏转 ≤45°、俯仰 ≤15°，≤30 分钟，不支持 HDR）；音频 mp3/aac/wav/m4a/flac；支持公网 URL、`mediakit://` 本地上传（`tools-sync/request-media-upload-url` 取预签名 PUT 地址，文件保留 30 天）、`vod://`、`tos://`。计费：按输出时长 ¥1/分钟；耗时 RTF 约 6–8（1 分钟音频约 6–8 分钟）。需放行的域名：`mediakit.cn-beijing.volces.com`（API），以及上传地址与产物下载地址的域名（文档示例为 `*.volcvod.com`、`*.volcvideo.com`，以实际返回为准）。当前容器实测：`docs.volcengine.com` 可以访问；`ark.cn-beijing.volces.com` 可以访问（无密钥返回 401）；`mediakit.cn-beijing.volces.com` 仍被拦截（`Tunnel 403`）；未配置 MediaKit 密钥。
- 火山引擎能力盘点（2026-09-30）：见 [reports/p0/vendor-volcengine.md](reports/p0/vendor-volcengine.md)。火山可覆盖 P0-03 ~ P0-11 全部环节；只需方舟 `ARK_API_KEY` + MediaKit API Key 两个 Bearer Key（MediaKit 可代理方舟图像 / 视频和 Seed Audio）。当前可达：`ark.cn-beijing.volces.com`、`visual.volcengineapi.com`、`open.volcengineapi.com`；被拦截：`mediakit.cn-beijing.volces.com`（`openspeech.bytedance.com` 已于同日放行，见下一条）。注意 Seedance 不接受直接上传的真人人脸参考，只信任同账号 30 天内 Seedance 2.x / Seedream 5.0 文生图的原始产物或平台授权素材。
- 火山引擎密钥（2026-09-30 更新）：云环境已配置 `ARK_API_KEY`（`GET /api/v3/models` 返回 200，含 Seedream 5.0、Seedance 2.0）与 `VOLC_SPEECH_API_KEY`；`openspeech.bytedance.com` 已放行（代理隧道偶发中途断开，客户端需重试）。豆包语音该 Key 已授权：TTS 2.0（`seed-tts-2.0`）、录音文件识别 2.0 标准版（`volc.seedasr.auc`，submit / query，可内联 base64）；未授权（403 `45000030`）：TTS 1.0（`seed-tts-1.0`）、ASR 1.0（`volc.bigasr.auc`）、极速版（`*.auc_turbo`）。`mediakit.cn-beijing.volces.com` 仍被拦截。
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
| 2026-09-30 | P0-04 | 分镜拆解：`poc/shots.py` + Prompt `shots.v1` + CLI `python3 -m poc shots`。v0 没有未分镜形态，做法是重新分镜：剥掉镜头结构，渲染分场剧本视图 → LLM 只出镜头 → 合并回输入、重排 shot_id → strict 校验 + 台词守恒 + 合理性硬门槛 H1–H4（12–24 镜、单镜 1.5–8 秒、总时长 ±10%、台词念得完）→ 最多修复 2 轮；复用 `script.Generator`（新增 `build_messages`、`add_llm_options`）。离线 `--metrics` 给出 ep01 与 P0-03 基线。评测：flash、flash 关思维链、v4-pro 各在 ep01 上 5 份；flash 对 P0-03 flash s01–s05 各 1 份。报告 `docs/reports/p0/P0-04.md`，证据 `docs/reports/p0/P0-04/`。按评审修复：无台词文档的指标行崩溃、非对象 JSON 崩溃、CLI 模式互斥（防误发付费生成）、summary 加 `n_expected`、报告 token 数与余额口径 | verifier 第 1–9 条通过（第 6 条补单价后复验通过）：批次 A flash 5/5（首次通过 3）、批次 B 5/5（首次通过 5），flash 关思维链 3/5（时长算术失败）、v4-pro 5/5；18 份入库样本独立 `validate --strict` 通过，10 份门禁样本 `--metrics` H1–H4 全过、守恒独立复核通过；110 个单元测试通过；`make arch-check`、`make drama-ir-check` 通过；评审无阻断项。费用估算 ¥3.34（余额实际减少 ¥1.84） | 推荐默认 `deepseek-flash`（平均单镜 2.9 秒，>5 秒镜头 0.5%）；可选：用户抽看 `docs/reports/p0/P0-04/deepseek-flash/s01.md`。未采纳的评审建议：输出 Schema 外层（scenes/scene_id）从 v0 `$defs/scene` 组装、删除 `ShotSettings.target_s` 残留（改了会使入库 Prompt 与代码不一致，留给 P0-12 / P1）。下一步 P0-05 配音：缺 TTS 供应商密钥（DashScope / MiniMax / ElevenLabs 均未配置或被拦截），M0.2 其余步骤同样阻塞 |
| 2026-09-30 | P0-05 | 未开工：PR #6 已合并（P0-04 ✅）。M0.2 其余步骤全部阻塞：P0-05 配音缺 TTS 密钥（DashScope / MiniMax / ElevenLabs），P0-06 定妆缺图像密钥（DashScope / Ark / 可灵 / fal / Replicate），P0-10 缺音乐音效密钥（MiniMax / ElevenLabs / Replicate），P0-07 ~ P0-09、P0-11 依赖它们；只有 DeepSeek 可用 | doctor：除 deepseek 外 10 家均 `MISSING`；`make arch-check` 通过。费用 ¥0 | 用户在云环境设置中添加上述任一组密钥并放行对应域名后，新会话从 P0-05 开始 |
| 2026-09-30 | P0-05 | 未开工：用户放行火山引擎文档域名并提供 AI MediaKit 视频口型对齐文档；整理接口、鉴权、输入限制、单价（¥1/分钟）写入“已知的前置条件”，作为 P0-09 口型候选 | `docs.volcengine.com`、`ark.cn-beijing.volces.com` 可访问；`mediakit.cn-beijing.volces.com` 仍 `Tunnel 403`；doctor 除 deepseek 外均 `MISSING`。费用 ¥0 | P0-05 仍缺 TTS 密钥；要用 MediaKit，需在云环境添加 `MEDIAKIT_API_KEY` 并放行 `mediakit.cn-beijing.volces.com` 及上传、下载域名（P0-09 前） |
| 2026-09-30 | P0-05 | 未开工：按用户要求通读火山引擎文档中心，盘点与 Dramio 相关的能力、单价、鉴权与域名，写成 `docs/reports/p0/vendor-volcengine.md`（覆盖方舟、豆包语音、AI MediaKit、视觉智能 / 即梦、音乐生成、审核） | 域名可达性实测见报告 §8；`make arch-check` 通过。费用 ¥0 | P0-05 仍缺 TTS 凭证：火山路线需豆包语音 API Key 并放行其接口域名（待从控制台核实），或经 MediaKit 用 Seed Audio |
| 2026-09-30 | P0-05 | 实现与评测完成，等待 D-004：用户配置 `ARK_API_KEY` 与 `VOLC_SPEECH_API_KEY` 后开工。注册 `volc_speech`（tts、asr）；`poc/speech.py`（豆包 TTS 2.0 逐行 JSON 流、录音文件识别 2.0 submit / query、失败分类与瞬时重试）、`poc/audio.py`（MP3 帧头时长、CER 与同音字等价折叠）、`poc/tts.py`（`python3 -m poc tts`，语音指令 `tts-instruct.v1`、语速映射，离线 `--metrics` / `--export`）、`pricing` 按字符 / 时长计价。评测：6 个音色初选；候选 A–D 各 3 轮 × 15 句 180/180 成功，`cer_equiv` 全 0，每集约 ¥0.074。报告 `docs/reports/p0/P0-05.md`。按评审修复：ASR 查询失败只重试查询、费用中止计入当句、计费口径统一、离线模式判空、业务错误脱敏、Xing 头 CRC；证据上限由 5 MB 调为 6 MB（4 个候选 + 初选，原因见步骤卡） | verifier 第 1–11 条通过、第 12 条登记完整待用户试听；158 个单元测试通过；`make arch-check`、`make drama-ir-check` 通过；评审无阻断项。费用估算 ¥1.15 | 用户试听后决定 D-004，把决定写入报告，P0-05 标为 ✅；未采纳的评审建议：代理 Tunnel 403 / DNS 等持续性网络错误快速失败（现由重试上限与 `--max-cost-cny` 兜底）。M0.2 其余可做：P0-06 定妆（Ark Seedream 可用）、P0-10 音乐音效 |
| 2026-09-30 | D-004 | 用户选定 C `instruct-speed` 为默认配音方案（苏晚 知性灿灿 2.0 / 陆沉 高冷沉稳 2.0 + 语音指令 `tts-instruct.v1` + 语速映射） | — | P0-05 可把决定写入报告并标为 ✅；P0-09 口型、P0-11 剪辑合成、P0-12 串联使用该方案的配音 |
| 2026-09-30 | P0-05 | 收尾：D-004 已决（用户 `/decide D-004 C`），决定写入报告 `docs/reports/p0/P0-05.md`（结论与“推荐与决定”一节）；P0-05 标为 ✅，进度指针移到 P0-06 | 验收第 1–11 条已由 verifier 通过（PR #7），第 12 条由 D-004 满足；`make arch-check` 通过。本步费用 ¥1.15（估算） | P0-06 角色定妆（Ark Seedream 可用） |
| 2026-09-30 | P0-06 | 开工：PR #8 合并后按用户允许同步会话分支；step-planner 出计划，写入步骤卡与 13 个子任务。D-005 已决（定妆图关闭可见水印）；定妆选定登记为 D-006（待评测后） | `make arch-check` 通过。费用 ¥0 | 子任务 1：冒烟核实方舟图像接口 |
| 2026-09-30 | P0-06 | 阻塞：读取方舟图片生成 API 文档（82379/1541523）与模型价格（82379/1544106）：5.0 pro / flash 不支持组图与 seed，`watermark` 默认 true，9:16 在 1.5K 档为 1152×2048（≤261 万像素，pro ¥0.30 / 张，flash ¥0.12）；派生方式 group 改为 sheet（单张文生图设定板）。冒烟：Seedream 5.0 pro / flash 均返回 404 `ModelNotOpen`（账号未开通），Seedance 2.5 / 2.0 / fast / mini 同样未开通；4 次请求均未计费。先做不需要调用 API 的子任务 2–8 | `poc/seedream.py` 初稿；冒烟 run `20260930-193316-costume-smoke-9f87`、`20260930-193327-costume-smoke-808c`。费用 ¥0 | 用户在方舟控制台“开通管理”开通 Seedream 5.0 pro / flash（P0-08 还需 Seedance 2.5 / 2.0 系列）后重跑冒烟 |
| 2026-09-30 | P0-06 | 不依赖 API 的部分完成（子任务 2–8）：`poc/images.py`（JPEG SOF / PNG IHDR 宽高、sha256）、`pricing` 图像按张计价（档位 261 万像素、pro 第 2 张参考图起加价）、`poc/seedream.py`（错误分类、计费口径 `billable`、密钥与账号 ID 脱敏）、模板 `costume.v1`、`poc/costume.py`（main / derive text·ref·sheet、重试、费用保险、node_key、`--export` / `--verify` / `--cards`）、README 一节 | 197 个单元测试通过（新增 39 个，全部离线）；`make arch-check` 通过。费用 ¥0 | 仍阻塞：等用户在方舟控制台开通 Seedream 5.0 pro / flash，之后做子任务 1（冒烟）、9–13 |
| 2026-09-30 | P0-06 | 用户开通低阶模型，要求“先跑通整个流程”。实测已开通：Seedream 5.0 flash、Seedream 4.0（`doubao-seedream-4-0-20260415`，¥0.20 / 张，不支持 `output_format`）、Seedance 1.0 pro fast；未开通：Seedream 5.0 pro、Seedance 2.x 全系列。主图候选改为 flash 对比 v4。冒烟：flash 文生图 / 单参考图生图成功（约 10 秒 / 张，1152×2048，约 170 KB），v4 文生图成功（约 500 KB）；url 返回的下载域名 `ark-acg-cn-beijing.tos-cn-beijing.volces.com` 被网络策略拦截，只用 b64_json；尺寸小于 921600 像素返回 400 InvalidParameter（不计费） | 冒烟 calls 入库 `docs/reports/p0/P0-06/smoke/`（账号 ID 已脱敏）。费用 ¥0.56 | 子任务 9：主图 flash / v4 各 3 张 × 2 角色 |
| 2026-09-30 | P0-06 | 评测阶段 1 完成、阶段 2 部分完成后**方舟账号欠费**。主图（run `194549-costume-9c0b` flash、`194549-costume-ae73` v4）：各 6/6；v4 有 1 次 IncompleteRead 重试（按全额计）。agent 初评：flash 贴合描述、同角色 3 张脸稳定、无多余配饰；v4 苏晚 3/3 加了描述外的手表、main-02 未正对镜头，陆沉 3/3 裁到大腿（不是全身）、背景偏蓝；推荐 flash，苏晚 main-02（`e76b9c27`）、陆沉 main-02（`e5962809`）。派生（flash、`--small` 720×1280 / 1280×720 / 960×960，控制体积；2 轮）：sheet 8/8；text 13/34、ref 14/34，其余为 403 `AccountOverdueError`（不计费），第 1 轮缺陆沉表情 text 5 张、ref 3 张。初评：ref 身份最接近主图；text 脸明显漂移（苏晚 angry 像另一个人，陆沉侧面更年轻、衬衫变深色）；sheet 板内一致但与主图不是同一张脸，且 `$style`（冷蓝色雨夜）泄漏成背景（苏晚三视图板、陆沉表情板），陆沉表情板手表跑到翻领上——模板需修订为 costume.v2（设定板去掉剧集风格）。新增：账号级错误（欠费、未开通、鉴权）立即中止整次运行；`--small`；证据导出、定妆卡、smoke 目录整理 | `--verify` 5 个目录 42 张通过，费用合计 ¥6.88；证据 9.96 MB（上限 10 MB，补跑会超出，需调整上限或降级）；单测全部通过。费用 ¥6.88 | 用户为方舟账号充值后：补跑陆沉 text / ref（`--char char_luchen`，约 ¥3.8）、costume.v2 设定板（约 ¥1）；然后写报告、登记 D-006 |
