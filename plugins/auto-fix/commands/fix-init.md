---
description: 在本仓库设置 auto-fix —— 识别项目并写出一个初始契约
allowed-tools: Bash, Read, Edit, Write, AskUserQuestion
---

# auto-fix: 设置本仓库

识别这个项目如何构建和测试,然后写出一个初始契约供用户确认。

## 1. 识别并生成

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/autofix.py" init
```

这会写出 `.claude/autofix.toml`(带识别出的命令)和 `.github/workflows/auto-fix.yml`。除非传 `--force`,否则它拒绝覆盖已存在的文件。

## 2. 向用户展示识别结果并请其确认

读取生成的 `.claude/autofix.toml`,把识别到的 `setup`、`test`、`lint` 命令展示给用户。用 AskUserQuestion 确认它们是否正确 —— 一个错误的测试命令会静默地削弱之后每一次运行,所以这一步很重要。

`test` 命令是必须弄对的那一个。如果没识别出来,直接问用户什么命令跑测试套件;没有它,auto-fix 只能审查,无法验证。

## 3. 解释模型 profile

契约里写的是 profile 名字(`models.fixer`、`models.reviewer`),不含端点和密钥。它们在仓库之外:

- 本地:`~/.config/auto-fix/profiles.toml` 和 `~/.config/auto-fix/env`(权限 `600`)
- CI:用仓库 **variables** 存端点和模型,**secrets** 存 token

把这一点告诉用户,并指向插件 README 给出确切格式。绝不把凭据写进仓库。

## 4. 提醒分支保护

插件永不 merge。但"必须由人 merge"只有在仓库也这么要求时才能端到端成立:

- **Settings → Branches → 保护 `main`**:要求 PR、要求 review、禁止直接 push。

这是插件改不了的仓库设置 —— 直说。

## 5. 验证

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/autofix.py" validate
```

报告任何缺失的 profile 或命令,并确认 fixer 和 reviewer 解析到不同的模型。如果相同,运行会被强制进入自审模式(草稿 PR,始终标记)—— 把这点作为"在依赖它之前需要先修"的问题提醒用户。
