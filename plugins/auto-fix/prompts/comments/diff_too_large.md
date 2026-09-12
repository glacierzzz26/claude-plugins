**auto-fix aborted: the change was too large to review safely.**

The diff exceeded the configured size limit, so it was not sent for review and
no PR was opened. A change this size cannot be meaningfully reviewed in one
pass, and does not match how issues are normally fixed.

Split the issue into smaller ones, or raise `limits.max_diff_bytes` in
`.claude/autofix.toml` if the size is genuinely expected.
