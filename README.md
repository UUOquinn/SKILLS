# SKILLS

可复用的 **Cursor Agent Skills** 合集：契约文档 + 可执行脚本。凭证、Cookie、跑批结果只放本机，不上库。

| Skill | 能力 | 入口 |
|-------|------|------|
| [ams-default-contact](./ams-default-contact/) | 腾讯广告 AMS 批量绑定「账户联系人」 | [SKILL.md](./ams-default-contact/SKILL.md) |

---

## ams-default-contact

服务商后台（`e.qq.com`）把已有联系人选中到子客账户——对应开户页「账户联系人 / 默认联系人」。

| 原则 | 说明 |
|------|------|
| API 优先 | `scripts/bind-contact.js`，禁止浏览器逐条点 |
| 凭证本机 | `api.json` 已 gitignore |
| 默认路径 | `POST /agp/advertiser/update`（`--bind-only` 才走 `bind_link_person`） |

```bash
cd ams-default-contact/scripts
cp api.example.json api.json   # 填 Cookie / agencyUid / linkPersonId
node bind-contact.js --dry-run
node bind-contact.js --file uids.txt
```

契约：[ams-default-contact/API.md](./ams-default-contact/API.md)

---

## 仓库约定

```
SKILLS/
├── README.md
└── <skill-name>/
    ├── SKILL.md          # Agent 入口（YAML frontmatter）
    ├── API.md            # 可选：上游契约
    ├── manifest.json     # 可选：元数据
    └── scripts/          # 可选：可执行脚本；敏感文件 gitignore
```

| 规则 | 说明 |
|------|------|
| 不上库 | Cookie、Token、执照号、真实 uid 跑批结果 |
| 占位示例 | `api.example.json` / `uids.example.txt` 仅用假 ID |
| 安装 | 克隆本仓，或把单个 skill 目录链到 `~/.cursor/skills/` |

```bash
git clone https://github.com/UUOquinn/SKILLS.git
# 可选：链到 Cursor
ln -s "$(pwd)/ams-default-contact" ~/.cursor/skills/ams-default-contact
```

> **安全提示**：Issue / PR 中勿粘贴真实 Cookie 或内网账号列表。
