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

## Use this marketplace

```
/plugin marketplace add ~/plugin                   # local clone
/plugin marketplace add <github-user>/<repo>       # once pushed to GitHub
/plugin install claude-hud@my-plugins
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
