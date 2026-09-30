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
- **当前步骤**：P0-01
- **步骤状态**：进行中
- **工作分支**：claude/ecstatic-euler-z68coi
- **PR**：—

## 步骤卡

（开工时由 `/step` 填写：目标 / 做 / 不做 / 验收 / 涉及 / 估时）

- **步骤**：P0-01 验证脚手架
- **目标**：可运行的 `spikes/poc` Python 项目；`python -m poc doctor` 逐个报告供应商密钥状态（未配置 / 已配置 / 可用 / 无效 / 不可达 / 错误）；每次运行输出到 `runs/<run_id>/`；统一的 `run.call()` 把每次模型调用的耗时和费用写入 `calls.jsonl`，供 P0-03 ~ P0-11 复用。
- **做**：`spikes/poc/`（pyproject、`dependencies = []`、Python ≥ 3.11）；标准库 `.env` 解析（`spikes/poc/.env`，进程环境优先）；供应商注册表（能力类别、变量名、免费探测方式、需放行域名）；`runs/<run_id>/` 固定在 `spikes/poc/runs/`（可用 `POC_RUNS_DIR` 覆盖）、`meta.json`、`calls.jsonl`；`doctor`（`--offline`、`--require`、并发探测、只用免费 GET 接口，无法确认免费接口的供应商只检查是否配置）；unittest；README；在 progress.md 前置条件中写入变量名和域名。
- **不做**：任何生成或付费调用；选定供应商；引入 SDK 或第三方依赖；价格表与费用汇总；node_key 缓存与 `run` 子命令（P0-12）；ffmpeg 等本地工具检查；把 spikes 测试接入 CI（涉及 `.github/`，留给 P1-01）；修改 archcheck 规则、`.claude/`、ADR。
- **验收**：
  1. 无密钥环境中 `cd spikes/poc && python3 -m poc doctor` 返回 0，每家供应商一行“未配置”并列出缺少的变量名，末尾有按能力类别的覆盖统计；
  2. 状态判定有 mock `urlopen` 的单元测试：MISSING / OK / INVALID(401/403) / UNREACHABLE(URLError、超时) / ERROR(5xx) / `--offline` 下 CONFIGURED 且不访问网络；
  3. `--require` 退出码有单元测试；
  4. 密钥哨兵值不出现在 stdout、`doctor.json`、`calls.jsonl`、`meta.json`；`.env` 与 `runs/` 被忽略，`.env.example` 被跟踪；
  5. `.env` 加载有单元测试，且进程环境优先；
  6. 每次运行生成 `spikes/poc/runs/<run_id>/`（`meta.json`、`doctor.json`、`calls.jsonl`）；从仓库根目录运行也不产生顶层 `runs/`，之后 `make arch-check` 通过；
  7. `run.call()` 成功、失败各一次得到 2 行合法 JSON，字段齐全，失败行含 `error` 且异常原样抛出；
  8. 在线探测全部是免费 GET 接口，本步费用 ¥0；
  9. `python3 -m unittest discover -s spikes/poc/tests -t spikes/poc` 通过；`make arch-check` 通过；
  10. 无第三方依赖；无新顶层目录；未改 `rules.toml`、`exceptions.toml`、`.claude/`、`.github/`；
  11. README 与 progress.md 前置条件列出全部变量名和需放行的域名；
  12. 真实密钥在线得到 OK：需用户配置密钥、放行域名后确认，放到 P0-03 的阻塞检查中完成（不阻塞本步）。
- **涉及**：roadmap §3；governance.md §3（spikes 说明）、§5.1；architecture.md §7.1、§7.4、§15；ADR-0004、0008、0010（仅参考）；INV-03、INV-11。
- **估时**：0.5 天

## 子任务

（开工时由 `/step` 填写，格式为 `- [ ] 子任务`，完成后改为 `- [x]`）

- [x] 项目骨架：pyproject、`poc/__main__.py`（argparse）、`.gitignore`（`runs/`、`!.env.example`）、`tests/`
- [x] 配置加载与供应商注册表：`poc/config.py`、`poc/providers.py`、`.env.example`，附单元测试
- [x] 运行目录与调用记录：`poc/runlog.py`（run_id、meta.json、`run.call()` → calls.jsonl），附单元测试
- [x] doctor 命令：状态判定、并发探测、`--offline`、`--require`、doctor.json，附 mock 单元测试与密钥不泄露测试
- [ ] 文档：`spikes/poc/README.md`；progress.md 前置条件写入变量名与域名
- [ ] 验证：从 `spikes/poc` 与仓库根目录运行 doctor，输出写入交接日志；unittest 与 `make arch-check`

## 本步费用

- **已花费**：¥0
- **上限**：¥100

## 待决事项

需要用户拍板的事项。用户用 `/decide 编号 决定` 或直接回复即可；状态为“待决”的事项会在每次会话开始时提醒。

| 编号 | 步骤 | 问题 | 候选 | 推荐与理由 | 状态 | 决定 |
|---|---|---|---|---|---|---|

## 已知的前置条件

- P0-03 起需要调用模型供应商 API：需要在云环境设置中添加对应的密钥环境变量（变量名在 P0-01 中确定），并在网络设置中放行供应商域名。新会话才会生效。
- ADR-0001 ~ 0010 目前为 Proposed，在 P0-14 统一评审。
- GitHub 上需要用户手动完成：把默认分支改为 `main`；为 `main` 开启分支保护（要求 `arch-check` 通过，不要求 Code Owner 评审）。

## 交接日志

只追加，不修改历史记录。

| 日期 | 步骤 | 做了什么 | 验证 | 下一步 |
|---|---|---|---|---|
| 2026-09-30 | — | 搭建自主推进工作流：`/continue`、`/step`、`/progress`、`/decide`，3 个子代理，SessionStart 与 Stop hooks，进度一致性检查 | `make arch-check` 通过 | 用户说“继续当前进度”后开始 M0.1（P0-01、P0-02） |
