**auto-fix could not authenticate to a model endpoint.**

Check that the fixer and reviewer model credentials (GitHub Secrets in CI,
`~/.config/auto-fix/env` locally) are set and valid, and that the profile
endpoints in `~/.config/auto-fix/profiles.toml` are reachable.
