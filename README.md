# my-plugins

My personal Claude Code plugin marketplace.

## Layout

```
.claude-plugin/marketplace.json   # marketplace catalog — which plugins this repo offers
plugins/claude-hud/               # vendored plugin (local copy, editable)
  .claude-plugin/plugin.json      # plugin manifest
  commands/*.md                   # slash commands (setup, configure)
  src/                            # TypeScript source — edit here
  dist/                           # compiled output served by statusLine (force-added to git)
  package.json                    # npm run build = tsc
plugins/auto-fix/                 # auto-fix plugin (see its own README.md / CLAUDE.md)
  .claude-plugin/plugin.json      # plugin manifest
  commands/*.md                   # fix-issue, fix-init, fix-status, fix-summary
  scripts/autofix/                # Python orchestrator (stdlib only, no deps)
  prompts/                        # fixer/reviewer prompts + one comment per failure mode
  templates/                      # autofix.toml + github-workflow.yml
  price_table.toml                # plugin-owned cost table
  tests/e2e.py                    # full suite, no network or tokens needed
```

## Plugins

### claude-hud

Real-time statusline HUD (context usage, active tools, running agents, todo progress).
Vendored from [`jarrodwatts/claude-hud`](https://github.com/jarrodwatts/claude-hud) (MIT),
version 0.8.0. This is a **local copy** — edit it freely; it no longer tracks upstream.

Rebuild after editing `src/`:

```bash
cd plugins/claude-hud && npm install && npm run build
```

### auto-fix

两个模型处理一个 GitHub issue:一个修,一个**不同的**模型审,最多 3 轮。如果双方达成一致且项目测试通过,就发起一个**草稿** PR。它永不 merge —— 由人来 merge。可本地运行(`/auto-fix:fix-issue <n>`),也可在 GitHub Actions 中运行(给 issue 打上 `auto-fix` 标签),适用于任何语言的仓库。

完整说明、配置和安全模型见 [`plugins/auto-fix/README.md`](plugins/auto-fix/README.md)。

## Use this marketplace

```
/plugin marketplace add ~/plugin                   # local clone
/plugin marketplace add <github-user>/<repo>       # once pushed to GitHub
/plugin install claude-hud@my-plugins
/plugin install auto-fix@my-plugins
/reload-plugins
/claude-hud:setup                                  # writes statusLine into ~/.claude/settings.json
```

Refresh the catalog after changing `marketplace.json`:

```bash
claude plugin marketplace update my-plugins
```

## Note on `dist/`

Upstream `.gitignore` excludes `dist/`, but installs need it. It is force-added
(`git add -f plugins/claude-hud/dist`) so the repo is self-contained. Re-run that
if you rebuild and want the new output committed.
