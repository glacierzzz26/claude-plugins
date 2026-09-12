**auto-fix 已停止:某个阶段超时。**

未开 PR。可能是项目测试耗时超过了 `commands.timeout_s`,或某个模型响应过慢。
如果测试确实需要更长时间,请在 `.claude/autofix.toml` 里调高该上限。
