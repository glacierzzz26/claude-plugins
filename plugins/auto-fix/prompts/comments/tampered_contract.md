**auto-fix aborted: `.claude/autofix.toml` was modified during the run.**

The fixer changed the project contract — which declares how the project is
tested and what is protected. That would let a run escape its own safety checks,
so the run was stopped and no PR was opened.

This may be an honest mistake (the fixer thought the contract was in scope) or
an attempt to disable verification. Please check the run log and the branch
`{branch}`.
