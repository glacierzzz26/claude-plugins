**auto-fix aborted: the change touched protected paths.**

The fixer modified files that the project's `.claude/autofix.toml` marks as
protected:

{violations}

No PR was opened. If the fix genuinely requires a change there, a human should
make it by hand — or widen the `paths.protected` list if the rule is too strict.
