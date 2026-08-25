# SKILLS

**单一仓库 · 多 Skill 项目（monorepo）**

每个子目录是一个独立可安装的 Cursor Agent Skill（契约文档 ± 脚本）。凭证、Cookie、跑批结果只放本机，不上库。

| 包 | 能力 | 入口 |
|----|------|------|
| [alliance-workbench-api](./skills/alliance-workbench-api/) | 联盟诊断工作台本机 HTTP API（3000/api/） | [SKILL.md](./skills/alliance-workbench-api/SKILL.md) |
| [ams-default-contact](./skills/ams-default-contact/) | 腾讯 DSP AMS API 批量绑定「账户联系人」 | [SKILL.md](./skills/ams-default-contact/SKILL.md) |
| [query-mapping](./skills/query-mapping/) | 查询维度映射 · 离线/实时数据集 | [SKILL.md](./skills/query-mapping/SKILL.md) |
| [query-cpm](./skills/query-cpm/) | 媒体 CPM 下降排查与结构拆解 | [SKILL.md](./skills/query-cpm/SKILL.md) |

机器可读索引：[catalog.json](./catalog.json)

---

## 仓库布局

```
SKILLS/                          ← 本仓库（monorepo 根）
├── README.md                    ← 总览 / 目录
├── CONTRIBUTING.md              ← 如何新增 / 发布一个 Skill
├── catalog.json                 ← 包清单（name / path / keywords）
├── .gitignore
└── skills/                      ← 所有 Skill 项目落在这里
    ├── alliance-workbench-api/  ← 工作台 HTTP API 契约
    ├── ams-default-contact/     ← 独立包（含 scripts/）
    ├── query-mapping/
    └── query-cpm/
```

**约定：** 一个 Skill = `skills/<name>/` 下一个完整包；包与包互不耦合，可单独软链到 `~/.cursor/skills/<name>`。

---

## 快速使用

```bash
git clone https://github.com/UUOquinn/SKILLS.git
cd SKILLS

# 安装某一个 Skill 到 Cursor（示例）
ln -s "$(pwd)/skills/alliance-workbench-api" ~/.cursor/skills/alliance-workbench-api
ln -s "$(pwd)/skills/ams-default-contact" ~/.cursor/skills/ams-default-contact
ln -s "$(pwd)/skills/query-mapping" ~/.cursor/skills/query-mapping
ln -s "$(pwd)/skills/query-cpm" ~/.cursor/skills/query-cpm
```

### alliance-workbench-api（契约）

工作台本机 API 全文：[skills/alliance-workbench-api/API.md](./skills/alliance-workbench-api/API.md)  
同源实现：[alliance-advertiser-funnel-web/docs/API.md](https://github.com/UUOquinn/alliance-advertiser-funnel-web/blob/main/docs/API.md)

### ams-default-contact（含脚本）

```bash
cd skills/ams-default-contact/scripts
cp api.example.json api.json   # 本机填 Cookie，勿提交
node bind-contact.js --dry-run
```

契约：[skills/ams-default-contact/API.md](./skills/ams-default-contact/API.md)

---

## 安全

| 规则 | 说明 |
|------|------|
| 不上库 | Cookie、Token、执照号、真实 uid 跑批结果 |
| 占位示例 | `*.example.json` / `uids.example.txt` 仅用假 ID |
| PR / Issue | 勿粘贴真实 Cookie 或内网账号列表 |

新增包请读 [CONTRIBUTING.md](./CONTRIBUTING.md)。
