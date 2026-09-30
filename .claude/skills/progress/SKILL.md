---
name: progress
description: 查看 Dramio 当前开发进度（只读）：当前里程碑与步骤、子任务断点、待决事项、费用、下一步。当用户问“现在做到哪了”“进度怎么样”时使用。
allowed-tools: Read, Grep, Glob, Bash(python3 tools/workflow/hooks.py summary), Bash(git log *), Bash(git status *)
---

# /progress：查看进度（只读）

1. 运行 `python3 tools/workflow/hooks.py summary` 获取摘要。
2. 读取 `docs/progress.md` 交接日志的最后 3 条。
3. 用 GitHub MCP 查看本仓库打开的 PR（如果可用）。

用简体中文回复，结构如下，保持简短：

- **当前位置**：里程碑、步骤、状态；进行中时给出子任务进度和断点；
- **最近完成**：交接日志中最近几条的一句话摘要；
- **等你处理**：待决事项（编号、问题、推荐，以及回复方式 `/decide 编号 决定`）、等待人工合并的 PR、缺少的密钥或域名；没有就写“无”；
- **下一步**：说“继续当前进度”后 agent 会做什么。

本命令只读：不修改任何文件，不提交，不调用付费 API。
