# poc：P0 技术验证脚手架

P0 一次性验证代码（见 [roadmap §3](../../docs/roadmap.md)）。可以直连供应商、硬编码模型，但**任何产品代码都不能依赖本目录**（INV-03），P1 结束前删除。

- Python ≥ 3.11，只用标准库（`dependencies = []`）。
- 每次运行输出到 `spikes/poc/runs/<run_id>/`（与当前目录无关；可用 `POC_RUNS_DIR` 覆盖），不入库。
- 每次模型调用经 `Run.call()` 记录耗时和费用，追加到 `runs/<run_id>/calls.jsonl`。

## 用法

```bash
cd spikes/poc
python3 -m poc doctor                       # 检查各供应商密钥（只用免费的只读接口）
python3 -m poc doctor --offline             # 不访问网络，只检查是否配置
python3 -m poc doctor --require anthropic,dashscope   # 列出的供应商不是 OK 时返回 1
python3 -m unittest discover -s tests -t .  # 单元测试
```

在仓库根目录运行：`PYTHONPATH=spikes/poc python3 -m poc doctor`。也可以用 `uv run python -m poc doctor`。

## 密钥

复制 `.env.example` 为 `.env` 后填写（`.env` 被 git 忽略）；也可以直接设置环境变量，**进程环境变量优先于 `.env`**。云端会话在云环境设置中添加环境变量，新会话生效。只需配置打算评测的供应商。

| 供应商 | 名称 | 能力 | 环境变量 | doctor 探测（免费 GET） | 需要放行的域名 |
|---|---|---|---|---|---|
| Anthropic | `anthropic` | LLM | `ANTHROPIC_API_KEY` | `GET /v1/models` | `api.anthropic.com` |
| 阿里云百炼 DashScope | `dashscope` | LLM、TTS、图像、视频 | `DASHSCOPE_API_KEY` | `GET /compatible-mode/v1/models` | `dashscope.aliyuncs.com` |
| DeepSeek | `deepseek` | LLM | `DEEPSEEK_API_KEY` | `GET /models` | `api.deepseek.com` |
| 火山方舟 Ark | `ark` | LLM、图像、视频 | `ARK_API_KEY` | 只检查是否配置 | `ark.cn-beijing.volces.com` |
| 豆包语音（火山引擎） | `volc_speech` | 配音 TTS、语音识别 ASR | `VOLC_SPEECH_API_KEY`（新版控制台 API Key） | 只检查是否配置 | `openspeech.bytedance.com` |
| MiniMax | `minimax` | TTS、视频、音乐音效 | `MINIMAX_API_KEY`（可选 `MINIMAX_GROUP_ID`） | 只检查是否配置 | `api.minimaxi.com` |
| ElevenLabs | `elevenlabs` | TTS、音乐音效 | `ELEVENLABS_API_KEY` | `GET /v1/user` | `api.elevenlabs.io` |
| 可灵 Kling | `kling` | 图像、视频、口型 | `KLING_ACCESS_KEY`、`KLING_SECRET_KEY` | 只检查是否配置 | `api-beijing.klingai.com` |
| fal | `fal` | 图像、视频、口型 | `FAL_KEY` | 只检查是否配置 | `fal.run`、`queue.fal.run` |
| Replicate | `replicate` | 图像、视频、口型、音乐音效 | `REPLICATE_API_TOKEN` | `GET /v1/account` | `api.replicate.com`、`replicate.delivery` |
| Google Gemini / Veo | `gemini` | LLM、视频 | `GEMINI_API_KEY` | `GET /v1beta/models` | `generativelanguage.googleapis.com` |
| Runway | `runway` | 视频 | `RUNWAYML_API_SECRET` | `GET /v1/organization` | `api.dev.runwayml.com` |

这是候选全集，不代表选定；各步骤（P0-03 ~ P0-10）自行决定评测哪几家。注册表在 `poc/providers.py`。
“只检查是否配置”的供应商尚未确认有免费校验接口，doctor 不会为了验证密钥去调用可能计费的接口。

## doctor 状态

| 状态 | 含义 | 怎么办 |
|---|---|---|
| `MISSING` 未配置 | 缺少必需的环境变量（只含空白也算缺少；首尾空白会被去掉） | 配置密钥（不评测该供应商可忽略） |
| `CONFIGURED` 已配置 | 已配置，未在线验证（`--offline`，或没有免费校验接口） | 在首次真实调用时确认 |
| `OK` 可用 | 探测接口返回 2xx；429 视为有效但被限流 | — |
| `INVALID` 无效 | 返回 401 / 403，或 400 且含 `API_KEY_INVALID`（Google）；密钥含换行等控制字符时直接判为无效，不发请求 | 检查密钥、账户权限或区域（如国际站） |
| `UNREACHABLE` 不可达 | DNS、连接、超时、代理拒绝（`Tunnel connection failed: 403`）、TLS 错误 | 在网络设置中放行上表的域名 |
| `ERROR` 错误 | 5xx、3xx（不跟随重定向，以免鉴权头被带到其他主机）等其他响应，或探测时出现意外异常（只记录异常类型） | 稍后重试；持续出现时检查探测接口是否变化 |

缺少密钥或网络被拦截都不算 doctor 失败（返回 0）；只有 `--require` 列出的供应商不是 `OK` 时返回 1。
每次在线探测也写入 `calls.jsonl`（`cost_cny=0`、`cost_basis=free`）。任何输出都不包含密钥的值。

## script：剧本生成（P0-03）

一句话梗概 → 1 集 DramaIR v0 剧本。每份样本：生成 → `json.loads` → `dramio_drama_ir.validate`（strict，警告也算失败）+ 只生成 1 集、logline 原样使用输入 → 不通过时把上一版输出和问题清单（带 JSON 路径）回喂，要求重写完整 JSON，最多 `--max-repairs` 轮（默认 2）。

```bash
python3 -m poc script --n 5                                  # 默认梗概取标准样例 ep01 的 series.logline
python3 -m poc script --logline "……" --n 1 --model deepseek-v4-pro
python3 -m poc script --thinking disabled --max-cost-cny 5   # 关闭思维链；累计估算费用达到上限即中止
```

- 模型经 OpenAI 兼容的 Chat Completions 调用（`poc/llm.py` 的 `ENDPOINTS`，目前只登记 DeepSeek），开启 JSON 模式。
- Prompt 模板在 `poc/prompts/<版本>.md`（`--prompt-version`，默认 `script.v1`）；Schema 和阈值在运行时从 `packages/drama-ir` 读取，不会与校验器漂移。P1 起 Prompt 迁入 `packages/prompts`（INV-10）。
- 单价在 `poc/pricing.py`（估算）；每次运行前后查询账户余额（免费接口），`summary.json` 记录余额差用于复核。
- 网络错误、429、5xx 在同一次尝试内最多重试 2 次，不占用修复轮数；截断、空输出、非 JSON、校验失败都算一次失败的尝试。
- 退出码：N 份全部通过为 0；有失败或因费用上限中止为 1；缺少密钥等用法错误为 2。

输出（在 `runs/<run_id>/` 下）：

```
prompt.json                        system / user 消息、Prompt 版本、参数
samples/sNN/attemptK.raw.txt       第 K 次尝试的原始输出（K=0 为首次生成）
samples/sNN/attemptK.report.json   该次尝试的结论与问题清单
samples/sNN/final.json、final.md   通过校验的剧本及其可读版本
summary.json                       每份的尝试、修复轮数、tokens、费用、耗时；首次通过率、通过率、失败类型、余额差
```

## shots：分镜拆解（P0-04）

1 集 DramaIR v0 剧本 → 重新分镜。v0 的台词只能挂在镜头上，没有“已分场、未分镜”的形态，所以做法是：剥掉输入的镜头结构，渲染成分场剧本视图（场设定、动作描写、原样的台词对象）交给 LLM → LLM 只输出各场的新镜头 → 合并回输入文档，`shot_id` 按 `<scene_id>_shNN` 重排 → 检查 → 不通过带路径回喂，最多 `--max-repairs` 轮。生成循环、重试、记账和费用保险与 `script` 相同。

每份最终产物必须同时满足：

- `validate` strict：0 错误、0 警告；
- 守恒：场的集合与顺序不变；每场台词序列（`line_id`、`speaker`、`kind`、`text`、`delivery` 与顺序）与输入完全相同；
- 合理性硬门槛（T 为目标时长，括号内为 T=60）：H1 镜头数 ⌈T/5⌉–⌊T/2.5⌋（12–24）；H2 单镜 1.5–8 秒；H3 时长合计偏差 ≤ 10%（54–66 秒）；H4 每镜台词估算朗读 ≤ `hint_s`。阈值集中在 `poc/shots.py` 顶部，依据见 `docs/reports/p0/P0-04.md`。

```bash
python3 -m poc shots --n 5                                   # 默认输入标准样例 ep01
python3 -m poc shots --input a.json b.json --n 1             # 多个输入，每个 1 份
python3 -m poc shots --metrics x.json y.json                 # 离线：只算指标与 H1–H4，不调用模型
python3 -m poc shots --metrics new.json --source old.json --json   # 附带与原分镜的相似度，输出完整 JSON
```

- 输入必须通过严格校验且只有 1 集，否则退出码 2。
- 退出码：全部通过为 0；有失败或因费用上限中止为 1；用法错误（缺少密钥、输入不合法、参数组合不对）为 2。`--metrics` 模式下，全部文件通过严格校验且 H1–H4 全部通过为 0，有文件读不了、结构不合法或门槛不通过为 1，`--source` 不合法为 2。
- `--metrics` 是离线模式，不能与 `--input`、`--n` 同用；`--source`、`--json` 只能与 `--metrics` 同用，以免误发起付费生成。
- 输出与 `script` 相同，另外每份通过的样本有 `samples/<名称>/metrics.json`（时长分布、台词占满率与留白、挂载统计、景别与运镜分布、H1–H4、与输入分镜的相似度）。单个输入时样本名为 `sNN`，多个输入时为 `inII_sNN`。

## tts：配音与 ASR 回检（P0-05）

DramaIR 台词 → 逐句配音（豆包语音 TTS 2.0，`seed-tts-2.0`）→ ASR 回转写（录音文件识别 2.0 标准版，`volc.seedasr.auc`，音频内联 base64）→ 时长与字错率。默认输入标准样例 ep01；一次运行只跑一个方案，多轮顺序执行，不挑选、不丢弃。

方案由三部分组成：

- `--voice 角色id=音色id`（每个出场角色都要指定，可重复）：豆包语音 2.0 音色，列表见 doc 6561/1257544；
- `--instruct`：按台词的 `delivery.emotion / intensity` 和 `kind`（旁白）生成一句语音指令，放在 `additions.context_texts`（不计费），模板版本 `tts-instruct.v1`，见 `poc/tts.py` 的 `instruction()`；
- `--speed`：把 `delivery.speed` 线性映射到 `speech_rate`（1.0 → 0，0.9 → −10，1.1 → +10；−50 为 0.5 倍、100 为 2 倍）。

```bash
python3 -m poc tts --name A --voice char_suwan=<音色> --voice char_luchen=<音色>              # 3 轮 15 句
python3 -m poc tts --name B --voice ... --instruct --speed --rounds 1
python3 -m poc tts --name v-x --voice char_luchen=<音色> --only char_luchen --rounds 1       # 音色初选：只合成该角色的台词
python3 -m poc tts --metrics ../../docs/reports/p0/P0-05/A-plain                            # 离线：从 mp3 与 asr/*.json 重算指标
python3 -m poc tts --metrics <目录> --json
python3 -m poc tts --export runs/<run_id> ../../docs/reports/p0/P0-05/<方案名>               # 离线：整理入库证据
```

- 每句指标：文件时长（MP3 帧头计数，24 kHz 为 MPEG-2，每帧 576 个采样）、ASR 返回的时长、有效语音时长（首个 utterance 起点到末个终点）、首尾静音、字/秒、与 4.5 字/秒估算（`dramio_drama_ir.checks.speech_seconds`）之比；CER 三种口径：`cer`（NFKC、小写、去标点和空白后的字级编辑距离）、`cer_raw`（只去空白）、`cer_equiv`（在 `cer` 基础上把 ASR 分不出的同音字“他 / 她 / 它”“的 / 得 / 地”各折叠为一个字）。ASR 关闭了 ITN，数字保留为汉字。
- 每个镜头：台词文件时长之和与 `hint_s` 对照（留白、是否超出）；整集：总时长、平均字/秒、CER 汇总。
- 瞬时故障（网络、流截断、429、5xx、服务端繁忙或并发限流）同一句最多重试 2 次；ASR 查询失败时用原 request_id 重试查询，不重新提交，只有提交失败或轮询超时（60 秒）才重新提交。每次请求都记账（查询记为 `free`）。TTS 按结束块返回的计费字符数（`usage.text_words`）× 单价估算，ASR 按音频时长估算；不可重试的失败（HTTP 4xx〔429 除外〕、业务错误码）说明请求被拒，不计费；其余失败（网络中断、流截断、429、5xx）供应商可能已受理，保守按全额计。单价见 `poc/pricing.py`。
- 退出码：全部句子成功为 0；有失败或因费用上限（`--max-cost-cny`，默认 10 元）中止为 1；用法错误（缺少密钥、缺少音色、参数组合不对）为 2。`--metrics` 模式下，目录中每个 mp3 都能解析且都有 ASR 结果为 0，否则为 1。
- `--metrics`、`--export` 是离线模式，不能与 `--name`、`--voice`、`--instruct`、`--speed`、`--rounds`、`--only` 同用；`--json` 只能与 `--metrics` 同用，以免误发起付费生成。
- 输出：`runs/<run_id>/samples/rN/<line_id>.mp3`、`samples/rN/asr/<line_id>.json`（转写文本、utterances、时长、resource id）、`summary.json`（每句指标、每轮整集统计与镜头容纳、方案汇总、指令原文）。`--export` 把第 1 轮的逐句 mp3 与 asr、按台词顺序拼接的 `ep01.mp3`、`run-summary.json`、`run-calls.jsonl` 复制到目标目录（目标目录必须为空）。

## costume：角色定妆（P0-06）

DramaIR 角色描述 → 定妆主图、三视图（正 / 左侧 / 背）、表情集（火山方舟 Seedream 5.0 pro / flash，文档 82379/1541523）。计费模式由 `ARK_BILLING` 决定（D-007）：默认 `plan` 走方舟 Agent Plan 包月套餐 `POST /api/plan/v3/images/generations`，只支持 5.0 pro，`cost_cny` 记按量刊例价的等价费用、不实付；`payg` 走按量付费 `POST /api/v3/images/generations`，可用 pro / flash / v4。两种模式的 Key 不能混用。`ARK_BILLING` 不是密钥，可写在进程环境或 `.env`，留空即 `plan`。默认输入标准样例 ep01；提示词模板 `poc/prompts/costume.v1.md`（`--prompt-version costume.v2`：设定板去掉剧集风格，其它节不变），字段取自 `series.visual_style` 与 `characters[]`；表情集为 neutral 加该角色台词 `delivery.emotion` 中出现过的值（最多 6 个）。

需要在方舟控制台“开通管理”中开通对应模型，否则返回 404 `ModelNotOpen`（不计费）。

```bash
python3 -m poc costume --stage main --model pro --n 3                                   # 每个角色 3 张主图
python3 -m poc costume --stage main --model flash --char char_suwan --n 3
python3 -m poc costume --stage derive --mode text  --model pro --base char_suwan=<主图> --base char_luchen=<主图> --rounds 2
python3 -m poc costume --stage derive --mode ref   --model pro --base ...                # 以主图为参考图的图生图
python3 -m poc costume --stage derive --mode sheet --model pro --base ...                # 三视图设定板 + 表情设定板
python3 -m poc costume --export runs/<run_id> ../../docs/reports/p0/P0-06/<方案名>        # 离线：整理入库证据
python3 -m poc costume --verify ../../docs/reports/p0/P0-06                             # 离线：复核全部证据
python3 -m poc costume --cards ../../docs/reports/p0/P0-06                              # 离线：生成每个角色的定妆卡
```

- 派生方式：`text` 每张独立文生图；`ref` 以主图为唯一参考图（`image` 传 data URI）；`sheet` 两张文生图设定板（三视图 2048×1152、表情网格 1536×1536）。5.0 pro / flash 不支持组图（`sequential_image_generation`），所以用设定板代替。`--base` 在三种方式下都要给，记录在 summary 中作为派生的基础；只有 `ref` 会把它发给模型。
- 尺寸：竖图 1152×2048（9:16）；`--small` 用接口允许的最小像素数（竖 720×1280、横 1280×720、方 960×960），用于派生图以控制入库体积。均不超过 261 万像素，pro 按 ¥0.30 / 张计（超过为 ¥0.60），flash ¥0.12 / 张；pro 第 2 张参考图起每张 ¥0.02。单价见 `poc/pricing.py`。
- 所有请求 `watermark=false`（D-005：定妆图是内部中间资产，不发布；成片的 AIGC 标识由 P0-11 负责）、`response_format=b64_json`、`output_format=jpeg`。图像字节原样保存，不转码、不裁剪。
- Seedance 可用性：只有**没有参考图的文生图原始产物**标为 `seedance_eligible`（Seedance 只信任同账号 30 天内 Seedream 5.0 文生图的原始文件，见 `docs/reports/p0/vendor-volcengine.md` §1）。`ref` 派生是图生图，标为不可用。
- 瞬时故障（网络、429、5xx、服务端繁忙）同一张最多重试 2 次，每次请求都记账：成功按 `usage.generated_images` 计；网络中断、响应无法解析、429、5xx 可能已生成，保守按全额计；审核拒绝（错误码含 `SensitiveContent`）、其它 4xx、无图不计费。每个请求记录 `node_key`（请求内容的 sha256，含采样序号），只记录、不做缓存。错误信息中的密钥和方舟账号 ID 会被脱敏。
- 账号级错误（`AccountOverdueError` 欠费、`ModelNotOpen` 未开通、鉴权失败）立即中止整次运行，不再继续发请求。
- 退出码：全部成功为 0；有失败、因费用上限（`--max-cost-cny`，默认 15 元，下一张会超出即中止）或账号级错误中止为 1；用法错误为 2。`--verify` 全部一致为 0，否则为 1。
- `--export`、`--verify`、`--cards` 是离线模式，只能选一个，且不能与生成参数同用。
- 输出：`runs/<run_id>/images/rN/<角色>/<kind>-<label>.jpg`、`summary.json`（每张的提示词、参考图 sha256、尺寸、耗时、费用、sha256、Seedance 可用性）。`--export` 把第 1 轮成功的图像复制为 `<角色>/<kind>-<label>-<sha256 前 8 位>.jpg`，并写 `manifest.json`、`run-summary.json`、`run-calls.jsonl`（目标目录必须为空）。

## 运行记录

```
runs/<run_id>/            # run_id = YYYYMMDD-HHMMSS-<命令>-<4 位 hex>（UTC）
  meta.json               # 命令、参数、起止时间、状态、Python 版本
  calls.jsonl             # 每次模型调用一行
  doctor.json             # doctor 的结果与能力覆盖
  summary.json 等         # script、shots、tts 的输出，见上文各节
```

在代码中记录一次模型调用：

```python
from poc.runlog import Run

with Run("script", {"logline": "..."}) as run:
    with run.call(provider="anthropic", capability="llm", model="<模型>", node_key=None) as call:
        resp = ...  # 真正的调用
        call.cost_cny = 0.05          # 费用（元）
        call.cost_basis = "estimate"  # reported（供应商返回）/ estimate（按单价估算）/ free
        call.usage = {"input_tokens": 1200, "output_tokens": 800}
        call.request_id = "..."
```

`calls.jsonl` 字段：`run_id`、`provider`、`capability`、`model`、`node_key`、`started_at`、`elapsed_ms`、`status`（ok / error）、`error`、`cost_cny`、`cost_basis`、`usage`、`request_id`、`extra`。调用抛出异常时同样记录（`status=error`），异常原样抛出。
