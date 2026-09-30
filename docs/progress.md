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
- **当前步骤**：P0-05
- **步骤状态**：进行中
- **工作分支**：claude/sweet-lamport-cwwsvb
- **PR**：#7（已合并，P0-05 保持进行中，等待 D-004）

## 步骤卡

- **步骤**：P0-05 配音（估时 1 天）
- **目标**：用固定输入 ep01（D-002）的 15 句台词，在豆包语音内部对比 3 种配音方案：逐句音频、实测时长统计、ASR 回检字错率（CER），形成对比表，推荐默认方案（用户试听确认）。
- **做**：
  - 注册供应商 `volc_speech`（`VOLC_SPEECH_API_KEY`，`openspeech.bytedance.com`，能力 `tts`、`asr`；无免费探测，`probe=None`）；同步 `.env.example`、README、“已知的前置条件”。
  - `poc/speech.py`（标准库）：TTS 2.0 `POST /api/v3/tts/unidirectional`（逐行 JSON、base64 拼接、结束块 `20000000` 才算成功）；ASR 2.0 标准版 `POST /api/v3/auc/bigmodel/submit` + `query`（resource `volc.seedasr.auc`，内联 base64，已实测可用）。失败分类 network / http / api_error / truncated / empty / bad_response；瞬时故障重试，每次请求都经 `Run.call()` 入账。
  - CER：字级编辑距离，原始与归一化两种口径。时长：mp3 帧头解析（24 kHz 为 MPEG-2 L3，每帧 576 样本），每句、每镜头容纳（对照 `hint_s`）、全集、多轮波动；实测字/秒对比 4.5 字/秒估算。
  - `pricing` 按字符 / 按时长计价；CLI `python3 -m poc tts`（生成模式 + 离线 `--metrics`，互斥；`--max-cost-cny`）。
  - 对比（均 `seed-tts-2.0`；TTS 1.0 与 ASR 极速版未授权）分两阶段：① 音色初选：每个角色 3 个 2.0 音色（苏晚：知性灿灿、林潇、清新女声；陆沉：高冷沉稳、傲娇霸总、儒雅逸辰），只合成本角色台词、1 轮、无指令；② 全集对比，每个候选 15 句 × 3 轮：A `plain` 首选音色、无指令；B `instruct` 同音色 + 按 `delivery` 生成语音指令（`additions.context_texts`，模板 `tts-instruct.v1`）；C `instruct-speed` 再把 `delivery.speed` 映射到 `speech_rate`；D `alt-instruct-speed` 次选音色 + 指令 + 语速。
  - 证据入库 `docs/reports/p0/P0-05/<候选>/`（第 1 轮逐句 mp3、整集 `ep01.mp3`、`asr/*.json`、`run-summary.json`、`run-calls.jsonl`，≤5 MB）；报告 `docs/reports/p0/P0-05.md`；登记 D-004 由用户试听选默认方案。
- **不做**：其它 TTS 供应商（无密钥）；Seed Audio（MediaKit 被拦截）；声音复刻 / 音色设计 / 按描述自动选声；字幕时间戳、口型、BGM；音频质检；CER 超阈值自动重合成；修改 ep01、Schema 或 CODEOWNERS 路径；模型网关、`packages/prompts`、`evals/`。
- **验收**（全部可离线复核）：
  1. `cd spikes/poc && python3 -m unittest discover -s tests -t .` 全部通过且多于 110 个，覆盖流式拼接、断流重试记账、`code≠0` 不重试、无结束块、MPEG-1/2 帧时长、CER、`--metrics` 互斥。
  2. `make arch-check`、`make drama-ir-check` 通过。
  3. ep01.json sha256 仍为 `cd02daef022c6e78227ef3bf366adb2b5142a979a803bcf4373cdb8765a6fb59`。
  4. `python3 -m poc doctor --offline` 有 `volc_speech` 行（能力 `tts,asr`）；`.env.example`、README、前置条件写明变量名与域名。
  5. 候选 A–D 目录各恰好 15 个 `l_XXXX.mp3`（音色初选目录为该角色的台词），与 ep01 `line_id` 集合一致，为 MPEG layer III。
  6. `python3 -m poc tts --metrics <候选目录>` 退出码 0，时长与报告一致（±0.05 秒）。
  7. 每个样本有 `asr/l_XXXX.json`；`--metrics` 重算的原始 / 归一化 CER 与报告一致；报告写明 ASR 接口与 resource id。
  8. 报告有候选对比表：成功率（首次 / 最终）、重试次数、耗时、每集费用、全集时长、字/秒比值、念不完的镜头数、CER；数字可在 `run-summary.json` 找到出处。
  9. `run-calls.jsonl` 每行 `provider=volc_speech`、`capability∈{tts,asr}`、`cost_basis∈{estimate,free}`（ASR 查询为 free），失败请求也在；费用合计与报告和“本步费用”一致，≤¥100。
  10. 设置了密钥时 `grep -rF "$VOLC_SPEECH_API_KEY" docs spikes/poc --exclude=.env` 无匹配。
  11. 改动只涉及 `spikes/poc/`、`docs/reports/p0/`、`docs/progress.md`、`docs/roadmap.md`；`du -sb docs/reports/p0/P0-05` ≤ 6 MB（原定 5 MB 按 3 个候选估算；实际为 4 个候选 + 6 个初选音色，逐句 mp3 是 `--metrics` 复核的输入，`ep01.mp3` 用于试听，均不能省）。
  12. 默认方案（需用户确认）：报告给出推荐与理由；D-004 登记候选、推荐、试听路径与回复示例。
- **涉及**：architecture.md §2.1 ⑤、§5.5.1、§7.1、§7.4、§19.1；ADR-0004（P0 允许 spikes 直连供应商）；INV-01、INV-03、INV-07、INV-10、INV-11；D-002。

## 子任务

- [x] 1. 注册 `volc_speech` 供应商（providers、`CAPABILITIES` 加 `asr`、`.env.example`、README、前置条件），单测通过
- [x] 2. `poc/speech.py` TTS 客户端 + `pricing` 按字符 / 时长计价，离线单测
- [x] 3. ASR 客户端（submit / query 轮询）+ CER 模块，离线单测
- [x] 4. mp3 帧头时长解析 + 每句 / 镜头容纳 / 全集时长统计，离线单测
- [x] 5. 定候选与音色（音色列表与 TTS 接口文档已读，`context_texts` / `speech_rate` 已确认；冒烟 `20260930-155850-tts-73b3`：陆沉 5 句全部成功）
- [x] 6. CLI `python3 -m poc tts`（生成 + `--metrics` 互斥、`--max-cost-cny`），README 一节
- [x] 7. 正式评测：音色初选 6 个音色（`20260930-160232` ~ `160636`）；候选 A–D 各 3 轮 × 15 句（`20260930-160729-tts-6688`、`161030-tts-8fed`、`161329-tts-ed5c`、`161629-tts-b795`），180/180 成功
- [x] 8. 证据入库、报告 `docs/reports/p0/P0-05.md`、登记 D-004
- [x] 9. 验证（verifier）、评审（arch-reviewer）、交付 PR（P0-05 保持 🔄，等 D-004）

## 本步费用

- **已花费**：¥1.15（估算：探测约 ¥0.005，冒烟 ¥0.02，音色初选 ¥0.23，候选 A–D ¥0.88）
- **上限**：¥100

## 待决事项

需要用户拍板的事项。用户用 `/decide 编号 决定` 或直接回复即可；状态为“待决”的事项会在每次会话开始时提醒。

| 编号 | 步骤 | 问题 | 候选 | 推荐与理由 | 状态 | 决定 |
|---|---|---|---|---|---|---|
| D-001 | P0-01 | archcheck 查不出产品代码经 `PYTHONPATH=spikes/poc` 用 `import poc` 依赖 spikes（INV-03 守卫缺口，评审时在临时副本中复现）。是否在 `tools/archcheck/rules.toml` 的 `[aliases]` 中加 `"poc" = "spikes"`？ | A：加这一行别名（受保护路径，单独 PR，由你审阅合并）；B：暂不处理，P1-01 前一并处理 | A：改动一行，立即补上缺口；目前还没有产品代码，不会误报 | 已决 | A：在 `rules.toml` 的 `[aliases]` 中加 `"poc" = "spikes"`，单独 PR 由用户审阅合并（2026-09-30，用户：“补上漏洞”） |
| D-002 | P0-02 | 是否确认标准样例《雨夜反击》第 1 集作为 P0-03 ~ P0-13 所有评测的**固定输入**（确认后冻结，不再原地修改）？ | A：确认 `packages/drama-ir/examples/v0/ep01.json`（sha256 `cd02daef022c…`），可读版本 `packages/drama-ir/examples/v0/ep01.md`；B：按你的意见修改后再确认（说明要改什么） | A：2 角色、3 场、13 镜头、15 句台词、60 秒；覆盖空镜、道具插入、画外音、音效、3 种场景情绪，结尾有悬念；已按验收审阅修正剧情自洽问题 | 已决 | A：确认 `packages/drama-ir/examples/v0/ep01.json`（sha256 `cd02daef022c6e78227ef3bf366adb2b5142a979a803bcf4373cdb8765a6fb59`）为 P0-03 ~ P0-13 评测的固定输入，冻结（2026-09-30，用户） |
| D-003 | P0-03 | 剧本生成的人工评分与默认方案：15 份样本都已通过严格校验，“有人工评分记录”需要你打分；同时确认默认 LLM | A：15 份全部人工评分；B：人工抽评 3 份——flash s04（初评最高）、flash s03（flash 最低）、v4-pro s01（初评判为不可用），其余沿用 agent 初评；C：只确认 agent 初评，不打分。默认方案候选：`deepseek-flash`（思维链开）/ `deepseek-v4-pro` / flash 关思维链。样本与评分表：`docs/reports/p0/P0-03.md`、`docs/reports/p0/P0-03/<候选>/sNN.md` | B + `deepseek-flash`：抽评工作量小，而且是真正的人工评分（C 不满足验收本意）；flash 初评均分 3.89 最高、5/5 通过、每集约 ¥0.10、53 秒。回复示例：`/decide D-003 B flash-s04:4,5,4,4,4,5,4 flash-s03:… v4pro-s01:… 默认 flash`（7 个数依次为 D1 ~ D7） | 已决 | 人工评分：flash s04“不错”（认可）；其余 14 份沿用 agent 初评，报告中注明；默认 LLM 定为 `deepseek-flash`（思维链默认开）（2026-09-30，用户） |
| D-004 | P0-05 | 配音默认方案与角色音色（主观，需试听）：4 个候选 180/180 成功、同音字折叠后 CER 全为 0、费用与耗时几乎相同，差别只在听感 | A `plain`（首选音色，无指令）；B `instruct`（加语音指令）；C `instruct-speed`（指令 + 语速映射）；D `alt-instruct-speed`（次选音色 苏晚 清新女声 / 陆沉 儒雅逸辰 + 指令 + 语速）。整集试听 `docs/reports/p0/P0-05/<候选>/ep01.mp3`；音色初选 `docs/reports/p0/P0-05/voices/`；报告 `docs/reports/p0/P0-05.md` | C + 音色 苏晚 知性灿灿 2.0（`zh_female_cancan_uranus_bigtts`）/ 陆沉 高冷沉稳 2.0（`zh_male_gaolengchenwen_uranus_bigtts`）：语速映射实测生效（speed 0.9 的句子长 7%–18%），指令不计费、不影响 CER，音色贴合角色描述、初选无重试；若觉得指令腔调不自然则选 A。回复示例：`/decide D-004 C` 或 `/decide D-004 A 苏晚改清新女声` | 已决 | C `instruct-speed`：苏晚 知性灿灿 2.0（`zh_female_cancan_uranus_bigtts`）、陆沉 高冷沉稳 2.0（`zh_male_gaolengchenwen_uranus_bigtts`），加语音指令（`tts-instruct.v1`），并把 `delivery.speed` 映射到 `speech_rate`，作为默认配音方案（2026-09-30，用户） |

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
