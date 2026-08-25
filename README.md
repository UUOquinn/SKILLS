# SKILLS

运营 Agent 能力库。一仓多包，按场景装；凭证不上库。

<small>

| Skill | 定位 | 入口 |
|-------|------|------|
| [alliance-workbench-api](./skills/alliance-workbench-api/) | **联盟工作台控制面** — 本机 `:3000` 全量 HTTP 契约：策略查询 / 审核编排 / 延期托管 / 定投 / 屏蔽 / 结构化查数 | [SKILL.md](./skills/alliance-workbench-api/SKILL.md) |
| [ams-default-contact](./skills/ams-default-contact/) | **AMS 联系人** — 腾讯 DSP 服务商后台，API 批量选中子客「账户联系人」，禁止开户页逐条点选 | [SKILL.md](./skills/ams-default-contact/SKILL.md) |
| [query-mapping](./skills/query-mapping/) | **取数口径引擎** — 时间 / 流量主 / 广告主 / 场景 / 转化目标 → 字段映射；离线 `85587` vs 实时 `129496` | [SKILL.md](./skills/query-mapping/SKILL.md) |
| [query-cpm](./skills/query-cpm/) | **CPM 归因诊断** — 媒体 CPM 下滑拆预算结构、场景占比与同场景 CTR / CVR（先 mapping 再查数） | [SKILL.md](./skills/query-cpm/SKILL.md) |

</small>

机器索引：[catalog.json](./catalog.json)

---

## 安装

```bash
git clone https://github.com/UUOquinn/SKILLS.git
cd SKILLS
ln -s "$(pwd)/skills/<name>" ~/.cursor/skills/<name>
```

含脚本的包（AMS）先拷示例配置，勿提交 Cookie：

```bash
cd skills/ams-default-contact/scripts
cp api.example.json api.json
node bind-contact.js --dry-run
```

工作台契约全文：[alliance-workbench-api/API.md](./skills/alliance-workbench-api/API.md)  
实现源：[funnel-web docs/API.md](https://github.com/UUOquinn/alliance-advertiser-funnel-web/blob/main/docs/API.md)

新增包见 [CONTRIBUTING.md](./CONTRIBUTING.md)。Issue / PR 勿贴真实 Cookie、执照号或 uid 跑批名单。
