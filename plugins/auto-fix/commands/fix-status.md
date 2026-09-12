---
description: 显示本仓库中 auto-fix 运行的当前状态
argument-hint: "[--issue N]"
allowed-tools: Bash
---

# auto-fix: 运行状态

显示 auto-fix 当前在做什么,或者最近做了什么。状态在每个阶段转换后按 issue 写入,所以它也能反映崩溃或进行中的运行。

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/autofix.py" status $ARGUMENTS
```

每行报告:issue 编号、当前轮次、当前阶段,以及若已开 PR 就给出 PR URL。

阶段的含义:

| 阶段 | 含义 |
|---|---|
| `init` | 预检;还没跑任何东西 |
| `fixer` | 某个模型正在改工作区 |
| `tests` | 项目的测试命令正在运行 |
| `reviewer` | 第二个模型正在审查 diff |
| `done` | 已结束;看 PR URL 或 issue 上的评论 |

一个 `fixer`/`reviewer` 阶段的陈旧状态(没有进程在跑)意味着上次运行崩溃了。用 `--resume` 重跑会从该阶段继续,而不是从头开始。

如果某个运行卡在持锁状态而没有进程存活,锁会在下次运行时被自动接管(超过 90 分钟的陈旧锁,或持有进程已消失的锁,会被忽略)。
