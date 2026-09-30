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
| `MISSING` 未配置 | 缺少必需的环境变量 | 配置密钥（不评测该供应商可忽略） |
| `CONFIGURED` 已配置 | 已配置，未在线验证（`--offline`，或没有免费校验接口） | 在首次真实调用时确认 |
| `OK` 可用 | 探测接口返回 2xx；429 视为有效但被限流 | — |
| `INVALID` 无效 | 返回 401 / 403 | 检查密钥、账户权限或区域（如国际站） |
| `UNREACHABLE` 不可达 | DNS、连接、超时、代理拒绝（`Tunnel connection failed: 403`）、TLS 错误 | 在网络设置中放行上表的域名 |
| `ERROR` 错误 | 5xx 等其他响应 | 稍后重试 |

缺少密钥或网络被拦截都不算 doctor 失败（返回 0）；只有 `--require` 列出的供应商不是 `OK` 时返回 1。
每次在线探测也写入 `calls.jsonl`（`cost_cny=0`、`cost_basis=free`）。任何输出都不包含密钥的值。

## 运行记录

```
runs/<run_id>/            # run_id = YYYYMMDD-HHMMSS-<命令>-<4 位 hex>（UTC）
  meta.json               # 命令、参数、起止时间、状态、Python 版本
  calls.jsonl             # 每次模型调用一行
  doctor.json             # doctor 的结果与能力覆盖
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
