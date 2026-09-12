**auto-fix opened a draft PR, but verification was review-only.**

This repository has no test command configured (or no `.claude/autofix.toml`),
so the change could **not** be verified by running anything. The second model
reviewed the diff and approved it, but an automated review is not a substitute
for a test suite — so this run is flagged for human attention rather than
reported as a successful fix.

The draft PR: {pr_url}

Add a `commands.test` entry to `.claude/autofix.toml` to enable full
verification on future runs.
