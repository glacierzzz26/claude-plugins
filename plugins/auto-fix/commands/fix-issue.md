---
description: 用两个模型的 fix/review 循环修复一个 GitHub issue,并开草稿 PR
argument-hint: "[issue-number] [--no-pr] [--dry-run] [--in-place] [--max-rounds N]"
allowed-tools: Bash, Read, AskUserQuestion
---

# auto-fix: 修复一个 issue

对 issue **$1** 运行 auto-fix orchestrator。

所有逻辑都在 Python 里,所以本地命令和 GitHub Actions workflow 走的是完全相同的路径。不要在这个命令里重新实现任何逻辑 —— 只运行脚本并汇报结果。

## 运行

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/autofix.py" run --issue $1 $EXTRA_ARGS
```

把用户给出的任何额外参数(比如 `--no-pr`、`--dry-run`、`--in-place`、`--max-rounds 2`)作为 `$EXTRA_ARGS` 接在 `$1` 之后传过去。

如果 `${CLAUDE_PLUGIN_ROOT}` 未设置,改用本文件的位置来解析插件目录(脚本在 `commands/` 往上两级的 `scripts/autofix.py`)。

## 运行前

确认目标仓库里存在契约:

```bash
test -f .claude/autofix.toml && echo "contract found" || echo "no contract — run /auto-fix:fix-init first"
```

如果没有契约,告诉用户并提出运行 `/auto-fix:fix-init`。没有契约也能跑,但会降级为仅审查模式的验证,每个 PR 都会被标记待人工处理。

如果用户没有给出 issue 编号,用 AskUserQuestion 问,不要猜。

## 汇报结果

脚本会以有意义的退出码退出,并打印一行结果。把它映射成用户能据以行动的东西:

| 结果 | 含义 |
|---|---|
| `converged` | 已开草稿 PR。必须由人 review 并 merge。 |
| `review_only` | 已开草稿 PR,但没有测试验证过。 |
| `max_iterations_exceeded` | 3 轮后停止;已推送分支,**没有 PR**,已评论 issue。 |
| `stalled` | 同样的问题重复出现;提前停止,没有 PR。 |
| `no_changes` / `already_fixed` | fixer 没改任何东西;没有 PR。 |
| `protected_path_violation` / `contract_tampered` / `tests_weakened` | 安全闸门触发;没有 PR。 |
| `auth_error` / `config_error` | 配置问题;什么都没跑。 |
| `unauthorized_labeler` | 打标签的人没有写权限;什么都没跑。 |
| `lock_held` | 另一个运行正持有该 issue 的锁;稍后重试。 |

始终明确告诉用户是否开了 PR,如果开了就给出 URL。如果没开 PR,绝不要说运行"成功了" —— 报告真实结果。

**不要 merge 任何东西。** merge 是人的决定;本插件按设计没有任何 merge 能力。
