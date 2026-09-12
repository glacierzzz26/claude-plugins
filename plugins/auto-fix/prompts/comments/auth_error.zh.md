**auto-fix 无法通过模型端点完成认证。**

请检查 fixer 和 reviewer 的模型凭据(CI 中是 GitHub Secrets,本地是
`~/.config/auto-fix/env`)是否已设置且有效,以及 `~/.config/auto-fix/profiles.toml`
中的端点是否可达。
