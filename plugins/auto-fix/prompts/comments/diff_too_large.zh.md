**auto-fix 已中止:改动过大,无法安全审查。**

该 diff 超过了配置的大小上限,因此没有被送去审查,也未开 PR。这个体量的改动无法
在一次审查中有效评估,也不符合通常修复 issue 的方式。

请把 issue 拆成更小的几个,或者如果这个体量确实是预期的,调高
`.claude/autofix.toml` 里的 `limits.max_diff_bytes`。
