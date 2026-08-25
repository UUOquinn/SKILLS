# 贡献指南 · 在 monorepo 里加一个 Skill

本仓是 **单一仓库多项目**：每个 Skill 是 `skills/<name>/` 下的独立包。

## 1. 新建包

```bash
mkdir -p skills/my-skill
```

最小文件：

```
skills/my-skill/
├── SKILL.md          # 必填
└── manifest.json     # 推荐
```

可选：`API.md`、`scripts/`、`reference.md` 等。

## 2. SKILL.md

YAML frontmatter 至少包含：

```markdown
---
name: my-skill
description: >-
  一句话说明做什么、何时触发（中英文关键词均可）。
---

# 标题
…
```

`name` 必须与目录名一致（小写、连字符）。

## 3. manifest.json

```json
{
  "name": "my-skill",
  "version": "1.0.0",
  "displayName": "展示名",
  "description": "一句话",
  "entry": "SKILL.md",
  "keywords": ["关键词"]
}
```

## 4. 登记到 catalog.json

在根目录 `catalog.json` 的 `packages` 数组追加一条：

```json
{
  "name": "my-skill",
  "path": "skills/my-skill",
  "entry": "SKILL.md",
  "description": "一句话",
  "keywords": ["关键词"]
}
```

并在根 `README.md` 的包表加一行。

## 5. 敏感文件

若包内有脚本配置：

- 提交 `api.example.json` / `uids.example.txt`（占位 ID）
- **不要**提交 `api.json`、Cookie、真实跑批结果
- 在包内 `scripts/.gitignore` 排除敏感文件

## 6. 本地联调

```bash
ln -sf "$(pwd)/skills/my-skill" ~/.cursor/skills/my-skill
```

在 Cursor 里用触发词验证 Agent 是否加载该 Skill。
