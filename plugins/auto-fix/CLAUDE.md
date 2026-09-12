# auto-fix —— 给 Claude 的说明

一个 Claude Code 插件,把 GitHub issue 推进到一个已审查的**草稿** PR:一个模型修,一个不同的模型审,最多 3 轮,由人来 merge。

## 不变量 —— 不要去"修"它们

这些是承重的。每一条要么由测试保证,要么由构造保证,而每一条看起来都像是善意的重构会顺手删掉的东西。

1. **不存在 merge 能力。** 没有 `gh pr merge`,没有 merge 端点,GitHub 客户端上没有 merge 方法。`scenario_no_merge_unit` 会 grep 插件源码(先剥掉注释和 docstring),一旦出现就失败。"必须由人 merge" 是一条测试,不是一句提示词里的恳求。

2. **模型作为独立进程运行,不是插件 subagent。** 插件 subagent 无法设置 `hooks`、`mcpServers` 或 `permissionMode` —— harness 会忽略它们 —— 而且它的 `model:` frontmatter 只接受别名或模型 id,无法路由到第二个端点。只有独立的 `claude -p` 进程才能拥有自己的凭据和端点。把一个阶段改成 subagent 会静默地摧毁隔离模型。

3. **每个子进程的环境都从 `{}` 按白名单构建。** 绝不 `os.environ.copy()`。`isolation.py` 里的 `build_phase_env` 是唯一构造子进程环境的地方。不存在的密钥是**构造上就不存在**,而不是靠一个会过期的黑名单。

4. **每次 spawn 前都跑 `assert_no_foreign_secrets`**,把值与所有已知密钥比对。它会失败运行而不是泄露。

5. **reviewer 是只读的**(`--bare`,没有 `Bash`/`Edit`/`Write`),并且如果它没有回显收到的 `base_sha` 和 `diff_hash`,结论会被丢弃。

6. **commit、push、PR 只由 orchestrator 做。** fixer 的 git 写命令被禁用。

7. **`max_rounds` 在代码里被钳制到 1..3**,无论契约怎么写。循环在 Python 里;没有模型自己数轮次。

8. **收敛由 orchestrator 重新计算**,绝不只读 reviewer 的 `verdict`。一个嘴上说 `approve` 却列出 major 问题的 reviewer 是真实存在的失败模式。

9. **diff 前先 `git add -A`。** 普通 `git diff` 会漏掉未跟踪文件,所以一个只**新建**文件的 fixer 会看起来什么都没做。这是这里最容易犯的错误。

10. **契约始终受保护**且每轮重新哈希,无论 `paths.protected` 是否列出它。

11. **构建产物绝不进入被审查的 diff。** orchestrator 自己跑测试会在工作区留下字节码缓存和覆盖率数据;这些会被 unstage(`contract.ignore_globs`),这样第 N+1 轮只审 fixer 的工作。

12. **只有在阶段失败时才去读 CLI 的输出做诊断。** `claude -p --output-format json` 会把被审查的产物(schema、sha、diff hash、引用代码)整段回显在 stdout 里,所以任何"在输出里找 `429`/`401`"的判定都会撞上哈希或行号里的数字,把一个本该收敛的运行误判成限流/鉴权失败而中止。`_classify` 因此先判"是否干净成功",只在失败路径上跑 `_AUTH_RE`/`_RATE_RE`,且用词边界(真实形态是 `HTTP 429`、`status 429`,不是孤立数字)。`scenario_classify_unit` 会断言含 `429` 的 sha 不被误判。

## 目录结构

```
commands/       斜杠命令(很薄:只是调用 scripts/autofix.py)
scripts/autofix/  orchestrator —— Python 3.11+,仅标准库,无依赖
  isolation.py  ★ 安全核心:环境白名单 + 密钥断言
  loop.py       轮次循环、收敛、停滞检测
  changes.py    暂存后 diff + 策略闸门
  github.py     gh/urllib 客户端 —— 故意没有 merge 方法
prompts/        fixer/reviewer 提示词;comments/ 里每种失败模式一个
                (中文版是同名的 *.zh.md,可选;缺了就回落英文)
templates/      autofix.toml + github-workflow.yml,由 `init` 写出
price_table.toml  插件自有;成本在这里算,绝不用 CLI 的
tests/e2e.py    完整测试;一个假的 `claude` CLI 回放各种场景
```

## 输出语言跟随 issue

评论和模型输出的语言由 **issue 本身**决定,而不是插件配置。`prompts.detect_language`
在运行开始时从 issue 快照(标题+正文)判语言,先剥掉代码块/行内代码再数汉字 —— 一个
讲 Python 的中文 issue 满屏英文标识符,不剥代码会把 CJK 信号淹掉。判定结果存进
`RunState.lang`,整轮不再变。

- 评论模板:`prompts.comments` 的英文 `X.md` 是必需的,`X.zh.md` 是可选的。
  `load_localized` 找不到译文就回落英文 —— **缺翻译绝不能让运行在最后一步炸掉**。
- 模型产出:`fixer.md` / `fixer_revise.md` / `reviewer.md` 里有一个 `{reply_language}`
  占位符,渲染时填成目标语言名。reviewer 的指令覆盖 `summary` 和每个 issue 的
  `title`/`detail`/`suggestion`;fixer 覆盖它写的摘要与 issue 正文。
- 新增失败模式或新增语言时:`scenario_comment_unit` 会断言"每个失败原因都有模板""每个
  模板都有中文版""译文占位符与英文一致"。加一个英文模板而不加 `.zh.md`,测试会直接
  失败 —— 这是故意的。

## 跑测试

```bash
python3 tests/e2e.py        # 无网络、无 token、无 GitHub
```

假 CLI 是 `tests/fake_claude.py`;场景是一串 fixer/reviewer 步骤。两个花了真实时间才重新弄明白的细节:

- `--version` 是探测,不是场景步骤 —— 假 CLI 会直接回答它,不消耗脚本步骤。
- 插件的环境隔离会剥离未知变量,所以假 CLI 的 shim **硬编码**它的场景路径,而不是从环境读取。那是隔离在正常工作,不是测试框架的 bug。

要直接驱动循环,构造一个 `RunConfig` 并调用 `loop._run_locked(...)` —— 但注意 `loop.run()` 把它包在 try/except 里,会把意外异常变成 `internal_error`,从而藏住 traceback。调试时,把 `_run_locked` 包一层让它重新抛出。

## 成本与指标

`claude -p --output-format json` 会报 `total_cost_usd`,但带着 `costBasis: "unknown"`,并且套用的内置费率表对**第三方网关模型来说是错的**。插件记录原始 token 计数,并从自己的 `price_table.toml` 算成本;价格填错只需重算,不必重跑。不要为了"简化"去用 CLI 的那个数字。

每个阶段都会记录 `prompt_ver`(`file@model@hash`),这样数字的变化可以归因到某次提示词改动。没有它,指标就无法自解释,也就失去了意义。

## 本地状态

- `~/.config/auto-fix/` —— profiles、密钥(`env`,权限 600)、指标
- `<repo>/.git/auto-fix/` —— 运行状态和每 issue 的锁
- `$RUNNER_TEMP/autofix/` —— CI 中同上

`--resume` 从最后一个完成的阶段继续;状态文件在每次转换后原子写入,所以崩溃的运行可以恢复。
