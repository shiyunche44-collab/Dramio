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
- **当前步骤**：P0-07
- **步骤状态**：进行中
- **工作分支**：claude/sweet-lamport-cwwsvb
- **PR**：—

## 步骤卡

## P0-07 关键帧与一致性度量

- **目标**：`python3 -m poc keyframe` 对 ep01 每个镜头加 P0-06 定妆参考生成 9:16 首帧，比较 3 种一致性方案；`python3 -m poc face` 对任意一批图给出人脸相似度。产出 ep01 带角色镜头的相似度分布和一个可用方案，供 P0-08 直接取首帧。
- **做**：
  - 固定输入 `packages/drama-ir/examples/v0/ep01.json`（13 镜头，D-002 已冻结）。12 个镜头带角色，预计约 10–11 个脸部可度量（sc02_sh02 手部特写、sc02_sh01 远景可能测不到），分布按“镜头 × 角色”人脸实例统计并写明 n；空镜仍生成首帧，不计相似度。
  - `poc/keyframe.py` + Prompt `keyframe.v1`：字段运行时取自 ep01；1152×2048（9:16，pro ¥0.30 / 张）；只用 Agent Plan 的 Seedream 5.0 pro；`watermark=false`；无字幕文字，底部 1/4 留白；只描述镜头起始姿态。
  - 3 种方案（身份参考按 D-006：pro `main-02` + `ref` 派生）：A `text` 纯文字；B `ref1` 每角色 1 张 `main-02`；C `ref2` 每角色 2 张（`main-02` + neutral 特写）。各 12 镜头 + 1 张共用空镜，每方案 2 轮。
  - `poc/face.py`：检测 + 特征 + 余弦相似度，后端可插拔（arcface / dlib / manual），双人镜头 2×2 指派，分位数与直方图，`--json` / `--csv`。后端按子任务 1 的可达性实测选定。
  - 度量校准（¥0）：用 P0-06 的派生图验证度量排序与 P0-06 初评一致、异性角色互比明显更低；据此定阈值 τ，并写明只有 2 个角色、无同性别“不同人”负样本的局限。
  - 预设“可用”判据：可度量实例 ≥80% 相似度 ≥ τ，中位数 ≥ τ，无“错人”区间实例，agent 目视抽查 ≥12 张无换脸或服装严重漂移。
  - agent 目视抽查 ≥12 张（1–5 分，主观，需用户确认）；报告与证据；README。
- **不做**：图生视频（P0-08，含 MiniMax 海螺；`api.minimaxi.com` 当前被网络策略拦截）；Seedance 可用性核实（P0-08）；口型、剪辑；LoRA、IP-Adapter / InstantID / PuLID、首尾帧；服装、发型、场景一致性度量；不接入模型网关，不写 `apps/`、`services/`、`packages/`；不改 `rules.toml`、`exceptions.toml`、`ep01.json`；不新增顶层目录；不用 flash / v4。
- **验收**：
  1. `python3 -m poc keyframe` 对 13 个镜头各生成 1 张 1152×2048 首帧，A/B/C 各 2 轮；`keyframe --verify docs/reports/p0/P0-07` 返回 0（sha256、尺寸、manifest 与 calls 对应、费用）。
  2. `python3 -m poc face` 在同一批图上两次结果一致，支持 `--json` / `--csv`。
  3. 报告含 12 个带角色镜头的逐镜头表（可度量 / 不可度量及原因、检测脸数、像素大小、相似度）与每方案分布（n、分位数、最小最大、直方图）。
  4. 按预设判据给出至少一种可用方案，或写明“未找到”及原因。
  5. 度量有效性：在 P0-06 的 ref / text / sheet 派生图上排序与 P0-06 初评一致，异性角色互比明显低于同角色。
  6. A/B/C 对比表：成功率、失败率、耗时、单价、相似度中位数与 P10、可度量率。
  7. agent 目视抽查 ≥12 张评分表（主观，需用户确认，登记 D-010）。
  8. manifest 中每镜头有 `selected` 首帧，9:16、无字幕文字、底部留白；写明 P0-08 如何读取。
  9. 费用以 `run-calls.jsonl` 的 `cost_cny` 合计，按 P0-06 口径说明；总额 ≤ ¥100。
  10. `du -sb docs/reports/p0/P0-07` ≤ 10 MB（需放宽先登记待决事项）。
  11. calls 与 summary 无密钥、无 base64 图像数据。
  12. `cd spikes/poc && python3 -m unittest discover` 离线通过；`make arch-check`、`make drama-ir-check` 通过；`costume --verify docs/reports/p0/P0-06` 仍返回 0。
  13. 改动仅限 `spikes/poc/`、`docs/reports/p0/P0-07*`、`docs/progress.md`、`docs/roadmap.md` 状态。
- **涉及**：architecture.md §5.2.2、§5.4、§5.8.1；INV-01 / 06 / 10 在 spikes 内不强制，报告写明产品化落点（P1-11、P1-12）；无需 ADR。
- **估时**：2 天；预计付费约 ¥30–35（约 74 张 pro 首帧，Agent Plan 等价费用，不实付）。
- **风险**：人脸权重可能不可达（dlib 路线降级，再降级为 agent 目视评分，需用户决定放行域名）。

## 子任务

- [x] 1. 可达性与方案选定：实测 pip、GitHub release、HuggingFace / ModelScope；试装 insightface 或 dlib-bin + face_recognition_models，各跑通一次“检测 + 特征”；结论写入交接日志
- [x] 2. `poc/face.py` 骨架：Embedder 协议与选定后端、余弦相似度、最小脸阈值、伪造后端单测
- [x] 3. 基准库、双人指派、统计（分位数、直方图）、`face` CLI（`--json` / `--csv`）与离线单测
- [x] 4. 度量校准与有效性验证（¥0）：P0-06 派生图与其它主图、异性角色互比，定 τ，记局限
- [x] 5. `keyframe.v1` 与 `poc/keyframe.py` 规划层（离线）：三方案提示词、参考图映射、可度量性预标注、计价、node_key；`costume --verify docs/reports/p0/P0-06` 仍返回 0；单测
- [x] 6. 冒烟（约 ¥1）：单人镜头 B 方案、双人镜头 C 方案（4 张参考图），确认 pro 接受多参考图与 1152×2048，检查是否照抄参考图姿态或背景
- [x] 7. 首帧生成第 1 轮（约 ¥11，A/B/C 各 12 镜头 + 空镜，37 张；`--max-cost-cny` ≤ 20）
- [x] 8. 首帧生成第 2 轮（约 ¥11，只留统计）
- [x] 9. 全量度量：74 张首帧逐镜头表与每方案分布；按预设判据评“可用”，必要时对最差镜头改进补跑（≤ ¥8）
- [x] 10. agent 目视抽查 ≥12 张（1–5 分）并与度量交叉对照
- [x] 11. `--export` 第 1 轮 37 张 + manifest（含 `selected`）、run-summary、run-calls、faces 证据；`--verify` 通过；≤ 10 MB
- [x] 12. 报告 `docs/reports/p0/P0-07.md`；登记 D-010（证据体积）、D-011（默认一致性方案与目视评分）
- [x] 13. README（`face`、`keyframe`、依赖安装）与交接日志；`make arch-check`、`make drama-ir-check`、单测
- [ ] 14. verifier、arch-reviewer，按 ship.md 交付 PR

## 本步费用

- **已花费**：¥24.52（估算：冒烟 ¥0.66 + r1 ¥12.08 + r2 ¥11.78；Agent Plan 等价费用，不实付）
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
| D-006 | P0-06 | 定妆选定（主观）：每个角色选定一张主图和一种派生方式，作为 P0-07 / P0-08 的身份参考 | 主图：pro `main-01` / `main-02` / `main-03`（flash、v4 主图留作对照）；派生：A `ref`（主图作参考图的图生图）/ B `text`（独立文生图）/ C `sheet`（设定板，costume.v2）。并排见定妆卡 `docs/reports/p0/P0-06/char_suwan.md`、`char_luchen.md`；报告 `docs/reports/p0/P0-06.md` | 苏晚 pro `main-02`（`4e6cf20a`）+ A，陆沉 pro `main-02`（`33b545ed`）+ A：ref 76 张里脸型、发型、服装与主图最一致；text 表情脸型与年龄漂移、背面带出雨；sheet 板内一致但不是主图那张脸（陆沉发色变黑）。主图是文生图原始产物，可作 Seedance 参考到 2026-10-30（Agent Plan 生成是否计入待 P0-08 确认）。回复示例：`/decide D-006 A` 或 `/decide D-006 苏晚 main-01 + A` | 已决 | A：苏晚 pro `main-02`（`4e6cf20a`）+ `ref`，陆沉 pro `main-02`（`33b545ed`）+ `ref`（2026-09-30，用户：“选择A”） |
| D-009 | P0-06 | 证据体积再次超上限：pro 派生第 1 轮入库后 `docs/reports/p0/P0-06` 为 16.94 MB（D-008 上限 16 MB）；派生图已是接口最小尺寸，主图原图不能降级 | A：上限放宽到 18 MB，全部保留；B：删除已入库的 flash `derive-text-flash`、`derive-ref-flash` 图像（因欠费不完整，约 4.1 MB），只留 run-summary / calls；C：pro 派生每角色只入库 neutral 加 2 个表情（重新导出，约减 1.5 MB） | A：只超 0.94 MB；flash 派生是 pro 的对照组，报告的对比结论依赖这些图；P0 证据不再继续增长（P0-07 起证据放各自目录） | 已决 | A：上限放宽到 18 MB，全部保留（2026-09-30，用户：“选择a”） |
| D-010 | P0-07 | 证据体积超上限：37 张 pro 首帧（平均 276 KB，步骤卡估算时按 160 KB）入库后 `docs/reports/p0/P0-07` 为 10.83 MB（步骤卡上限 10 MB；含补充的 faces、pick-best、校准 JSON） | A：上限放宽到 11 MB，全部保留；B：只入库选定的 13 张 + 各方案各镜头的 r1 缩略说明（减到约 4 MB，但逐方案对比图没了，`faces-r1.json` 里的图像路径失效）；C：入库 `ref2` 与选定图（约 7 MB），`text` / `ref1` 只留统计 | A：只超 0.83 MB；三个方案的原图是报告对比结论与目视评分的依据，P0-08 也会按 manifest 引用 | 待决 | |
| D-011 | P0-07 | 默认一致性方案与目视评分（主观）：推荐 `ref2`，但按预设判据“无错人实例（< 0.30）”严格算三个方案都不达标（`ref2` 有 1 个，r1 `sc01_sh05` 陆沉 0.296，目视为侧脸误判）；agent 目视抽查（缩略图，非盲评）三个方案基本无法区分，都没有发现换脸，只有 `sc01_sh05` 侧脸双人镜头 `text` / `ref1` 略差（3 分），评分表见报告 §6；**“底部 1/4 留白”没有做到**：约 7 / 13 个镜头主体延伸到画面底部，是否可作字幕安全区也请一并确认 | A：采纳 `ref2` 为默认方案，`sc01_sh05` 按“度量局限”处理；B：单人镜头 `ref1`、双人及多人镜头 `ref2`；C：不采纳，要求补测（例如对 `sc01_sh05` 这类侧脸双人镜头加候选数，或改用更严判据）。各方案首帧在 `docs/reports/p0/P0-07/keyframe-r1/`，逐镜头表见 [P0-07.md](reports/p0/P0-07.md) §3 | A：`ref2` 下尾最好（P10 0.48、92% ≥ τ），双人镜头最稳，P0-08 按 manifest 里选定的首帧取；B 与 A 差别主要在 `ref1` 单人镜头中位数更高（0.67 vs 0.57），但 `ref1` 下尾更差，建议 P1-12 用“多候选 + 打分选优”解决而不是在 P0 分方案 | 待决 | |

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
| 2026-09-30 | D-009 | 用户选 A：P0-06 证据上限放宽到 18 MB，全部保留；步骤卡验收第 11 条同步。另：会话 `01DXLc6Z` 与 `01TKEJcW` 在同一分支重复跑了 pro 派生，前者未推送的重复证据已丢弃，以远端为准 | `du -sb docs/reports/p0/P0-06` = 16936183（≤ 18 MB） | 等 D-006（定妆选定）；之后 verifier、arch-reviewer、交付 PR |
| 2026-09-30 | D-006 | 用户选 A：两个角色都用 pro `main-02` + `ref` 派生；决定写入报告“推荐与决定”，待补测补充 P0-08 需确认 Agent Plan 能否调 Seedance | — | 子任务 13：verifier、arch-reviewer、交付 PR |
| 2026-09-30 | P0-06 | verifier：12 条验收全部通过（单测 205 个在无网络命名空间通过；`--verify` 9 个目录 86 张、¥31.48；体积 16.94 MB ≤ 18 MB）；按其建议修正报告耗时口径措辞。arch-reviewer 评审进行中 | verifier 报告；`make arch-check` 通过 | 处理 arch-reviewer 阻断项，然后按 ship.md 交付 PR |
| 2026-09-30 | P0-06 | 补记（会话 `01DXLc6Z`）：D-007 改走 Agent Plan（提交 1e87c7f 起：`ARK_BILLING`、`/api/plan/v3`、`costume.v2`、`--prompt-version`，pro 主图 run `20260930-201207-costume-3165`），D-008 上限 16 MB（e89bb79），pro 派生由会话 `01TKEJcW` 入库（624f910，runs `20260930-203811-costume-88ee` / `-8c43` / `-b7c2`）。arch-reviewer：无阻断项；已处理建议 1–4、6、7（`UnsupportedModel` 写入 README / 报告 / docstring，报告 Seedance 结论注明待 P0-08 确认，`.env.example` 注释说明 `ARK_BILLING`，旧证据缺 `billing` 即 payg，vendor 文档补 Agent Plan，`--model` help 与互斥测试）；建议 8（套餐额度与按量计费分开记账）留给 P1 模型网关 | 单测、`make arch-check` 通过 | 按 ship.md 交付 PR |
| 2026-09-30 | P0-06 | 完成：Seedream 定妆（flash / v4 / pro 主图各 6 张；pro 派生 text / ref / sheet 76/76；flash 派生因欠费不完整，留作对照）；D-006 选定两个角色 pro `main-02` + `ref`；报告 `docs/reports/p0/P0-06.md`、定妆卡、证据 9 个目录。P0-06 标为 ✅，进度指针移到 P0-07 | verifier 12/12 通过（单测 205 个无网络通过；`--verify` 86 张、¥31.48；16.94 MB ≤ 18 MB；密钥无泄露；改动范围合规）；arch-reviewer 无阻断项，建议已处理。费用 ¥31.48（按量实付 ¥6.88，其余为 Agent Plan 等价费用，不实付；另有重复运行的 pro 派生等价 ¥23.40 未入库） | P0-07 关键帧与一致性度量（以 pro `main-02` 为身份参考）；P0-08 前确认 Agent Plan 能否调 Seedance |
| 2026-09-30 | P0-07 | 开工：同步主线；step-planner 出计划，写入步骤卡与 14 个子任务。用户要求 P0-08 用 MiniMax 海螺视频：`MINIMAX_API_KEY` 已配置，但 `api.minimaxi.com`、`api.minimax.io` 在当前容器被网络策略拦截（CONNECT 403），需放行域名（新会话生效），模型 ID 待域名放行后核实；不影响 P0-07 | `make arch-check` 通过。费用 ¥0 | 子任务 1：人脸方案可达性实测 |
| 2026-09-30 | P0-07 | 子任务 1 完成：PyPI、`github.com` release 及重定向域名 `release-assets.githubusercontent.com` 可达（HuggingFace、ModelScope 仍被拦）；venv 装 numpy / onnxruntime / opencv-python-headless / insightface 2.0，下载 buffalo_l（SCRFD det_10g + ArcFace w600k_r50，289 MB，不入库）。P0-06 主图检测成功率 6/6（置信 0.87–0.90，脸框约 140×200 px）；同角色 main 图互比 0.59–0.76，异性互比 -0.04–0.08，区分度足够；特写 / 三视图正脸可检出，背面无脸。选定后端 arcface | 实测命令输出；`make arch-check` 通过。费用 ¥0 | 子任务 2：`poc/face.py` |
| 2026-09-30 | P0-07 | 子任务 2、3、5 完成：`poc/face.py`（arcface 后端懒加载、anchor / bank 两种口径、多人指派与置信差、分位数与直方图、`--json` / `--csv`、`--manifest` 逐镜头表）、`poc/keyframe.py` + `keyframe.v1`（text / ref1 / ref2 三方案，空镜共用，`--dry-run` / `--export` / `--verify`，可度量性预标注：ep01 为 1 个 no、2 个 maybe、10 个 yes）、costume 最小重构（Runner 钩子，`costume --verify` 结果不变）、pyproject `face` extras、README。真实后端在 P0-06 图上试跑通过；`--scheme all` 每轮 37 张预计 ¥11.46 | 279 个单测通过（新增 74 个，不联网、不需要 numpy）；`costume --verify docs/reports/p0/P0-06` 返回 0（86 张、¥31.48）；`make arch-check`、`make drama-ir-check` 通过。费用 ¥0 | 子任务 4：度量校准；子任务 6：冒烟 |
| 2026-09-30 | P0-07 | 子任务 4 完成（度量校准，¥0）：以两个角色的 pro `main-02` 为锚点，对 P0-06 全部 11 个图像目录评分（汇总 `docs/reports/p0/P0-07/calibration.json`）。有效性：`derive-ref-pro` 中位 0.62（陆沉）/ 0.64（苏晚）> `derive-text-pro` 0.51 / 0.52 > `derive-sheet-pro` 0.52 / 0.33，与 P0-06 初评“ref 最像、text 有漂移、sheet 不是同一张脸”一致；异性角色互比中位 0.06–0.08、最大 0.21；“同描述但不同脸”的参照（他家模型 flash / v4 主图 + flash 文生图，23 个实例）中位 0.34、P90 0.43、P95 0.46、最大 0.52。**预设判据（先定规则再看首帧结果）**：τ = 0.45（≈ 不同人参照的 P95）；“错人区间” < 0.30（低于参照的大部分，且高于异性最大 0.21）；0.30–0.45 记“存疑”；“可用”= 可度量实例 ≥ 80% 相似度 ≥ τ、中位数 ≥ τ、无错人实例、目视抽查 ≥12 张无换脸或服装严重漂移。局限：只有 2 个角色、无同性别真正“不同人”的 pro 样本；pro 同描述独立采样本身偏向同一张脸（main-pro 互比 0.64–0.76），所以 text 方案的基线可能不低；首帧脸更小时相似度会下降，报告按 `face_px` 分层说明 | 试跑输出；`make arch-check` 通过。费用 ¥0 | 子任务 6：冒烟（约 ¥1）|
| 2026-09-30 | P0-07 | 子任务 6 完成（冒烟）：单人镜头 sc01_sh02 ref1（86 秒）、双人镜头 sc01_sh05 ref2（4 张参考图，67 秒）均成功，1152×2048，Agent Plan pro 接受多参考图；目视：无字幕文字、底部留白、服装与设定一致、未照抄参考图背景和姿态；相似度 suwan 0.774（正脸）、双人镜头 0.413 / 0.380（侧脸对视，face_px 123–137），说明侧脸角度会拉低 arcface 相似度，报告需按朝向说明。子任务 7、8 已启动：两个后台进程各跑 `--scheme all` 37 张（r1 入库、r2 只留统计），串行约 45 分钟 | 冒烟 run `20260930-213820-keyframe-e5e2`、`20260930-213947-keyframe-a4ce`。费用 ¥0.66 | 等两个后台运行结束，做子任务 9 全量度量 |
| 2026-09-30 | P0-07 | 子任务 7–13 完成，等待 D-010、D-011：首帧 r1 / r2 各 37 张全部成功（首次失败 3 次，`IncompleteRead`，重试成功）；`face` 新增 `--pick largest|best`（脸数多于角色数时默认取最大的脸，避免背景路人“恰好更像”造成高估；先用取最高口径跑过，发现该偏差后修正并补单测）；报告 `docs/reports/p0/P0-07.md`：`ref2` 推荐（26 个可度量实例中位 0.56、P10 0.48、92% ≥ τ=0.45），`ref1` 中位最高但下尾更差（最差 0.20），`text` 54%；按预设判据严格算无方案完全达标（`ref2` 1 个实例 < 0.30，目视为 `sc01_sh05` 侧脸误判），已登记 D-011；13 个镜头已选定首帧（manifest `selected`）；证据 10.76 MB 超上限，登记 D-010。MiniMax 海螺：域名仍被拦截，P0-08 前需放行 | 280 个单测通过；`keyframe --verify docs/reports/p0/P0-07 --require-selected` 通过（37 张、¥12.08、选定 13/13）；`costume --verify docs/reports/p0/P0-06` 返回 0；`make arch-check`、`make drama-ir-check` 通过。本步费用 ¥24.52（估算，等价费用） | 子任务 14：verifier、arch-reviewer，然后按 ship.md 交付 PR；D-010 / D-011 待用户 |
| 2026-09-30 | P0-07 | arch-reviewer：无阻断项；已处理建议 1、3、4（子任务 12 改为 D-010、D-011；`faces-r2.json` 里的临时路径改为 `keyframe-r2（图像未入库）/…`；报告表里 `ref2` 陆沉 `sc01_sh05` 写作 0.296）。verifier 仍在后台核验 | `make arch-check` 通过 | 等 verifier 报告，处理后按 ship.md 交付 PR；D-010 / D-011 待用户 |
| 2026-09-30 | P0-07 | verifier 报告与处理：第 1、2、5、6、9、11–13 条通过；第 3 条缺脸像素与分布表、第 7 条缺逐张评分表、第 8 条“底部留白”不属实，均已修订（§3 加脸像素，§4 加 P25 / P75 / P90 / max 与直方图，§6 改为逐张粗分表并撤回按方案平均分，“底部留白”改为“无字幕文字，约 7 / 13 镜头主体延伸到底部”并并入 D-011）；补存 `faces-r2-pick-best.json`（r1 上 best 与 largest 逐实例相同），校准基线 23 个数值写入 `calibration.json`，报告统一耗时口径（r1+r2，2 进程并发），补充 `--pick largest` 的偏差说明；更正上一条日志：3 次首次失败实为 1 次 IncompleteRead + 2 次 RemoteDisconnected；sheet 排序按角色分开写（陆沉 sheet ≈ text）。证据 10.83 MB（D-010 已更新） | `make arch-check`、单测通过 | 提交推送后按 ship.md 交付 PR；D-010 / D-011 待用户 |
