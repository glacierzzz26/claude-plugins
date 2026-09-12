**auto-fix stopped: a phase exceeded its time limit.**

No PR was opened. Either the model was slow, or the task is larger than the
configured `commands.timeout_s`. Raise the timeout in `.claude/autofix.toml` if
the work legitimately takes longer, or narrow the issue.
