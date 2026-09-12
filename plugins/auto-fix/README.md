# auto-fix

指向一个 GitHub issue:一个模型修,一个**不同的**模型审,来回最多 3 轮。如果双方达成一致且项目测试通过,就发起一个**草稿** PR。

它永远不会 merge。人来做 merge。

既能本地跑(斜杠命令),也能在 GitHub Actions 里跑,适用于任何语言的项目 —— 插件不解析你的代码,只运行你声明的命令。

---

## 它到底做什么

```
             /auto-fix:fix-issue <n>            .github/workflows/auto-fix.yml
                        │                                    │
                        └──────────────┬─────────────────────┘
                                       ▼
                          scripts/autofix.py  (Python,仅标准库)
                                       │
       ┌───────────────────────────────┼───────────────────────────────┐
       ▼                               ▼                               ▼
   fixer 模型                     orchestrator                     reviewer 模型
  (独立进程,                 运行测试、暂存 diff、              (独立进程,
   自己的凭据)                 执行策略闸门                      只读、--bare、
                                                                 自己的凭据)
```

每一轮:

1. **fixer** 修改工作区(它不能执行 git 或 `gh`)。
2. **orchestrator** 暂存所有改动,运行项目的测试命令。
3. 结构化**策略闸门**拒绝:改受保护路径、篡改契约、削弱测试、diff 过大。
4. **reviewer** —— 只读、无 shell —— 返回结构化结论,并回显它实际审查的那份产物。
5. 收敛要求**全部**满足:`approve`、无阻塞性问题、测试通过、哈希对齐、且结论未被标记 `requires_human`。

循环上限 3 轮,**写在代码里**。绝不信任模型自己数轮次(或自己宣布成功)。

### 输出语言跟随 issue

issue 是中文,评论和模型回复就是中文;是英文就全是英文。语言在**运行开始时**从 issue 快照里检测(标题+正文,检测前先剥掉代码块,免得一段 Python 把中文 issue 判成英文),之后整轮固定不变。评论模板在 `prompts/comments/*.md`,中文版是可选同名的 `*.zh.md` —— 缺翻译就回落到英文,不会让运行在最后一步失败。给 fixer/reviewer 的提示词里也会带上一条"用 issue 的语言写摘要和问题描述"的指令,所以 `verdict`、issue 列表这些**模型产出**的文字也跟着走。

| 结果 | 会发生什么 |
|---|---|
| 收敛 | 发起草稿 PR,打上待人工处理的标签 |
| 未收敛(3 轮) | 推送分支、评论 issue,**不开 PR** |
| 停滞(同样的问题重复出现) | 提前停止,**不开 PR** |
| 安全闸门触发 | 中止,**不开 PR**,评论 issue |

---

## 安装

```bash
# 从 marketplace 安装
/plugin marketplace add glacierzzz26/claude-plugins
/plugin install auto-fix

# 或者直接用本地 checkout
claude --plugin-dir /path/to/plugins/auto-fix
```

然后在你想修复的仓库里:

```
/auto-fix:fix-init
```

它会识别你的语言和测试命令,写出 `.claude/autofix.toml`,并安装 CI workflow。请检查它识别出的内容 —— 一个错误的测试命令会静默地削弱之后每一次运行。

## 配置两个模型

插件需要**两个不同的模型**。它们在契约里按名字引用,但端点和密钥存放在仓库**之外**,因为提交进仓库的密钥等于已经泄露的密钥。

### 本地

`~/.config/auto-fix/profiles.toml`:

```toml
[profiles.fixer]
model = "claude-sonnet-5"
base_url = "https://api.anthropic.com"
auth_env_var = "AUTOFIX_FIXER_TOKEN"

[profiles.reviewer]
model = "claude-opus-5"
base_url = "https://api.anthropic.com"
auth_env_var = "AUTOFIX_REVIEWER_TOKEN"
```

`~/.config/auto-fix/env`(权限 `600`):

```
AUTOFIX_FIXER_TOKEN=sk-...
AUTOFIX_REVIEWER_TOKEN=sk-...
```

fixer 和 reviewer 必须解析到**不同的模型 id**。如果相同,运行会拒绝启动,除非设 `models.allow_self_review = true` —— 那会强制开草稿 PR 且始终标记待人工处理。

### CI 中

仓库 **variables**(非密钥 —— 端点不敏感,`--dry-run` 会打印它们):

| 变量 | 值 |
|---|---|
| `AUTOFIX_PLUGIN_REF` | 本 marketplace 仓库的一个 tag 或 SHA(需固定) |
| `AUTOFIX_CLAUDE_VERSION` | 例如 `2.1.263` |
| `AUTOFIX_FIXER_MODEL` / `AUTOFIX_REVIEWER_MODEL` | 模型 id |
| `AUTOFIX_FIXER_BASE_URL` / `AUTOFIX_REVIEWER_BASE_URL` | 端点 |

仓库 **secrets**:

| Secret | 用途 |
|---|---|
| `AUTOFIX_FIXER_TOKEN` | fixer 端点的密钥 |
| `AUTOFIX_REVIEWER_TOKEN` | reviewer 端点的密钥 |
| `AUTOFIX_PAT` | 仅对 **metrics** 仓库有 `contents:write` 的 PAT |

两个独立的模型 token 是重点,不是麻烦:隔离模型只给每个阶段的进程它自己的凭据,而如果两个阶段共用一个密钥,这个保证就形同虚设。

> `AUTOFIX_PLUGIN_REF` 必须固定版本。每个目标仓库都会运行这份代码,所以一个会移动的 `main` 等于对所有仓库的远程代码执行。

---

## 使用

```
/auto-fix:fix-issue 123             # 修复 issue 123 并开草稿 PR
/auto-fix:fix-issue 123 --dry-run   # 打印计划,不花任何 token
/auto-fix:fix-issue 123 --no-pr     # 离线运行;推送并打印,不开 PR
/auto-fix:fix-issue 123 --in-place  # 直接在当前 checkout 上改(默认用隔离 worktree)
/auto-fix:fix-issue 123 --max-rounds 2   # 覆盖契约里的轮次上限(仍被钳制到 1..3)
/auto-fix:fix-status                # 什么在跑 / 发生了什么
/auto-fix:fix-summary               # 各阶段耗时、token 和成本
```

本地运行时**默认在临时 git worktree 里工作**,所以你自己 checkout 里的文件不会被碰到;`--in-place` 才直接改当前工作区(工作区有未提交改动时会自动回退到 worktree)。CI 中永远在 checkout 上就地运行。

CI 中:给 issue 打上 **`auto-fix`** 标签。workflow 只在这个标签上触发 —— 这也正是插件自己打的标签不会再次触发它的原因。

---

## 契约 —— `.claude/autofix.toml`

放在**目标**仓库里,不含任何密钥。每个键都是可选的,所以一个空仓库会降级运行而不是直接失败。

```toml
version = 1
language = "python"          # 仅供参考
base_branch = "main"

[commands]
setup = "pip install -e .[dev]"
test  = "pytest -q"          # 需要它才能算作"已验证"
lint  = "ruff check ."
timeout_s = 900
retries = 1

[limits]
max_rounds        = 3        # 在代码里被钳制到 1..3
max_files_changed = 40
max_diff_bytes    = 400000
lock_tests        = true     # 拒绝删改或削弱测试的改动

[paths]
protected  = [".github/workflows/**", "infra/**", "*.lock", "CODEOWNERS"]
test_globs = ["tests/**", "**/test_*.py"]
ignore     = ["docs/**"]

[models]
fixer    = "fixer"           # 只写 profile 名字
reviewer = "reviewer"
allow_self_review = false

[review]
blocking_severity  = "major" # blocker | major | minor | nit
require_tests_pass = true

[pr]
draft         = true         # 草稿无法被误合并
branch_prefix = "autofix/issue-"
```

`.claude/autofix.toml` **始终**受保护、每轮重新哈希,无论你是否列出它 —— 一个能改契约的 fixer 就能删掉测试命令,从而绕开自己的闸门。

**没有测试命令就没有验证。** 运行仍然可用,但会报告为 `review_only` 并标记待人工处理,而不会宣称修复成功。

---

## 指标

每个阶段把墙钟耗时和原始 token 计数写入 `~/.config/auto-fix/metrics/<repo>.jsonl`。CI 中每次运行写自己的文件,并推送到 marketplace 仓库的 `metrics` 分支。

```
/auto-fix:fix-summary
/auto-fix:fix-summary --group-by model      # 对比两个模型
/auto-fix:fix-summary --group-by prompt_ver # 把变化归因到某次提示词改动
```

| 列 | 含义 |
|---|---|
| `wall p50/p90` | 阶段墙钟耗时 |
| `in` / `out` | 输入 / 输出 token |
| `cache_r` | 缓存读取 token。`-p` 阶段是独立进程,所以这个值低是正常的,不是浪费。 |
| `hit%` | 缓存读取占输入的比例 |
| `cost` | 由本插件的 `price_table.toml` 算出。`-` 表示未定价;token 仍然被计数。 |

成本由原始 token 计数算出,**绝不**使用 CLI 的 `total_cost_usd` —— 那个字段报 `costBasis: "unknown"`,并套用一张对第三方网关模型来说错误的价目表。把你的模型费率加进 `price_table.toml` 才能得到真实数字。

因为指标是原始的,价格填错只需重算,不必重跑。

---

## 安全模型

真正要防的不是模型出 bug,而是模型被劫持。issue 正文是攻击者可控的文本,却会到达一个拥有文件写权限的模型。

| 风险 | 缓解 |
|---|---|
| issue 文本被当作指令 | 用一次性 nonce 围栏标记为不可信数据;issue 在运行开始时快照,因此运行中改它无法改变工作目标。提示词走 stdin,绝不走 argv。 |
| 一个阶段读到另一个阶段的密钥 | 每个阶段是独立进程,环境变量按白名单构建。从不 `os.environ.copy()`,所以 `GH_TOKEN`、另一个 profile 的 token、继承的 `ANTHROPIC_*` 是**构造上就不存在**,而不是靠一个会过期的黑名单。 |
| 密钥泄露给模型 | 每次 spawn 前做基于值的断言;推送凭据只在所有模型进程退出后才加入。 |
| fixer 靠删测试来伪造通过 | 测试由 orchestrator 运行,不是 fixer;契约每轮重新哈希;删测试的 hunk 会被拒绝。 |
| reviewer 批准的是别的东西(TOCTOU) | 结论必须回显 base SHA 和 diff hash;被 commit 的正是被审查的那份产物。 |
| 自动合并 | 插件里没有任何 merge 方法。有个测试会 grep 源码,一旦出现就失败。 |
| 陌生人打标签来消耗你的额度 | (CI 中)在花费任何 token 之前检查打标签者的仓库权限。 |

**"必须由人 merge" 靠的是插件没有 merge 能力 —— 但这只是一半保证。** 另一半是插件无法替你做的仓库设置:

> **Settings → Branches → 保护 `main`**:要求 PR、要求 review、禁止直接 push。

每个目标仓库都要设置这一项。

---

## 要求

- Python 3.11+(`tomllib`),仅标准库 —— 任何地方都不需要 `pip install`
- `claude` CLI 在 `PATH` 上
- `gh` CLI(本地运行)或 `GITHUB_TOKEN`(CI)
- `git`

## 测试

```bash
python3 tests/e2e.py
```

无网络、无 token:一个假的 `claude` CLI 回放各种场景,覆盖收敛、3 轮上限、每道安全闸门、环境隔离、"每个失败都有对应评论" 的约定,以及不存在任何 merge 能力。

## 许可证

MIT。
