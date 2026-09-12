**auto-fix aborted: the change touched too many files.**

The diff changed more files than `limits.max_files_changed` allows, so no PR was
opened. Wide-reaching changes are usually a sign the fixer went beyond the
issue's scope.

Consider splitting the issue, or raising the limit in `.claude/autofix.toml` if
the breadth is expected.
