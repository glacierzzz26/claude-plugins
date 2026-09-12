**auto-fix 已中止:改动涉及的文件过多。**

该 diff 改动的文件数超过了 `limits.max_files_changed` 的上限,因此未开 PR。
影响面过大的改动通常意味着 fixer 超出了该 issue 的范围。

考虑拆分该 issue,或者如果这个广度是预期的,在 `.claude/autofix.toml` 里调高该上限。
