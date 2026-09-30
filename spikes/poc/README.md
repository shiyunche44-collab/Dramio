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

## 运行记录

```
runs/<run_id>/            # run_id = YYYYMMDD-HHMMSS-<命令>-<4 位 hex>（UTC）
  meta.json               # 命令、参数、起止时间、状态、Python 版本
  calls.jsonl             # 每次模型调用一行
  doctor.json             # doctor 的结果与能力覆盖
  summary.json 等         # script、shots 的输出，见上两节
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
