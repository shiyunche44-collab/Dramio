# 交付：从提交到合并入 main

由 `/step` 在第 6 步和第 0 步引用。

## 1. 收尾提交：标记完成并把指针移到下一步

在**同一次提交**中完成以下改动（`make arch-check` 的 progress 规则要求“当前步骤”不能指向已完成的步骤，所以两件事必须一起改）：

1. roadmap 中该步骤标为 ✅。
2. progress.md 的交接日志末尾追加一行：日期、步骤、做了什么、验证结果（逐条的证据摘要）、费用、下一步。
3. progress.md 的“当前状态”指向下一个步骤：
   - 运行 `python3 tools/workflow/hooks.py summary`，取“下一个可开始的步骤”。如果另一个步骤仍是 🔄（例如在等待用户决定），优先指向它；
   - 下一步属于新的里程碑时，“当前里程碑”一起更新；
   - “步骤状态”改为“未开始”（指向 🔄 步骤时写“进行中”），“PR”改为“—”，“本步费用”的已花费清零；
   - “步骤卡”“子任务”两节恢复为占位说明（本步的步骤卡保留在 PR 描述和 git 历史中）。
4. 运行 `make arch-check`，提交（`P0-01：<步骤名>`）并推送到会话分支。

## 2. 创建 PR

用 GitHub MCP 创建 PR，从会话分支合入 `main`：

- 标题：`P0-01：<步骤名>`
- 正文按 `.github/pull_request_template.md` 组织：步骤卡、验收结果（逐条列出证据）、费用、遗留事项。末尾附上会话要求的署名行。

远端还没有 `main` 时，不创建 PR，停下并提醒用户先创建 `main`。

## 3. 等待 CI

- **云端会话**：订阅该 PR 的事件（`subscribe_pr_activity`），并用 `send_later` 安排约 30 分钟后的兜底检查，然后结束本轮。收到 CI 结果事件或兜底检查唤醒后，继续下面的步骤。
- **本地命令行**：有 `gh` 时运行 `gh pr checks <编号> --watch`；没有时停下，下次 `/continue` 在第 0 步重新检查。

CI 失败时，这是你自己的 PR，要修到通过：定位原因 → 修复 → 本地 `make arch-check` → 推送。修复 3 轮仍失败时停下汇报。

兜底检查时如果发现 CI 从未运行（例如 Actions 未启用），停下告诉用户。

会话中断时，下一次会话的 `/step` 第 0 步会通过“列出打开的 PR”找回这个 PR，从本节继续。

## 4. 合并

CI 通过后，检查 PR 的改动文件：

```bash
git diff --name-only origin/main...HEAD
```

- 有任何文件命中 `.github/CODEOWNERS` 中的路径：**不要自动合并**。告诉用户 PR 链接和命中的路径，请用户审阅合并，然后停下。下一次 `/continue` 会在第 0 步看到它：已合并就继续，未合并就再次提醒。
- 没有命中：用 GitHub MCP 以 **merge commit** 方式合并（不要用 squash，否则会话分支会与 main 分叉）。

合并后：

```bash
git fetch origin main
git merge --ff-only origin/main
git push -u origin <会话分支>
```

最后取消该 PR 的事件订阅，回到 `/continue` 的循环。
