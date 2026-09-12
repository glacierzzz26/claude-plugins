---
description: 汇总 auto-fix 运行的指标 —— 各阶段耗时、token 和成本
argument-hint: "[--repo owner/name] [--group-by phase,model] [--json]"
allowed-tools: Bash
---

# auto-fix: 指标

汇总每次运行记录的指标,让用户看清时间和 token 实际花在哪里。

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/autofix.py" summary $ARGUMENTS
```

默认视图按 `phase`(fixer / tests / reviewer)分组。有用的其他分组:

- `--group-by model` —— 直接对比两个模型。
- `--group-by prompt_ver` —— 把数字的变化归因到某次提示词改动。
- `--group-by repo` —— 跨项目对比。
- `--json` —— 机器可读,便于画图。

各列和怎么读:

| 列 | 含义 |
|---|---|
| `wall p50/p90` | 阶段墙钟耗时 |
| `in` / `out` | 输入 / 输出 token 之和 |
| `cache_r` | 缓存读取 token。`-p` 阶段是独立进程,所以这里值低是预期内的,不是浪费。 |
| `hit%` | 缓存读取占所有输入的比例。随时间上升说明提示词和前缀在趋于稳定。 |
| `cost` | 由插件自有的 `price_table.toml` 算出。`-` 表示该模型没有价格条目 —— token 仍然被计数。 |

成本由原始 token 计数算出,**绝不**使用 CLI 的 `total_cost_usd` —— 它用的是内置价目表,对第三方网关模型来说是错的。

指标累积在 `~/.config/auto-fix/metrics/`。要把 CI 中发生的运行并进来,从 workflow run 下载 artifact 然后导入:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/autofix.py" import <downloaded-dir>
```

把表格呈现给用户,并指出任何可行动的点 —— 占据了墙钟耗时大头的阶段、每轮 token 成本高得多的模型,或者暗示提示词改动很大的低缓存命中率。
