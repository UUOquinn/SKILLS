# SKILLS

运营侧 Agent 能力的 **monorepo**：一个仓库、多个独立 Skill。每个包可单独链到 Cursor；Cookie / Token / 跑批结果不上库。

## Catalog

按工作面分组。点包名进目录；Agent 入口一律是包内 `SKILL.md`。

### 工作台

本机联盟诊断工作台（`:3000`）——只打本地代理，不直连上游域名。

| Skill | 形态 | 覆盖 |
|-------|------|------|
| [alliance-workbench-api](./skills/alliance-workbench-api/) | 契约 | 查询 · 审核 · 延期 · 定投 · 屏蔽 · 数据集 · DataAgent 旁路 · 监控壳 |

### 投放后台

| Skill | 形态 | 覆盖 |
|-------|------|------|
| [ams-default-contact](./skills/ams-default-contact/) | 契约 + 脚本 | 腾讯 DSP AMS：子客批量选中「账户联系人」（API，禁止逐条点页面） |

### 取数与归因

| Skill | 形态 | 覆盖 |
|-------|------|------|
| [query-mapping](./skills/query-mapping/) | 手册 | 时间 / 流量主 / 广告主 / 场景 / 转化目标字段映射；离线 `85587` vs 实时 `129496` |
| [query-cpm](./skills/query-cpm/) | 手册 | 媒体 CPM 下降：预算结构、场景拆解、同场景 CTR / CVR（先走 mapping 再查数） |

索引文件：[`catalog.json`](./catalog.json)（安装器 / CI 可读，与上表同源）。

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
