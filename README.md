# SKILLS

运营 Agent 能力库。

<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>紧凑表格</title>
<style>
  body {
    font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif;
    background: #f5f5f5;
    padding: 24px;
    color: #333;
  }

  /* 核心：紧凑表格样式 */
  .compact-table {
    font-size: 12px;          /* 小字号 */
    line-height: 1.5;         /* 紧凑行高 */
    font-weight: 400;
    border-collapse: collapse;
    background: #fff;
    border-radius: 6px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }

  .compact-table th,
  .compact-table td {
    padding: 8px 12px;
    text-align: left;
    border-bottom: 1px solid #eee;
    vertical-align: top;
  }

  .compact-table th {
    background: #fafafa;
    font-weight: 600;
    color: #666;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .compact-table tr:last-child td {
    border-bottom: none;
  }

  .compact-table a {
    color: #0066cc;
    text-decoration: none;
  }

  .compact-table a:hover {
    text-decoration: underline;
  }

  .compact-table code {
    font-family: "SF Mono", Consolas, monospace;
    background: #f0f0f0;
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 11px;
    color: #c7254e;
  }

  .compact-table .skill-name {
    font-weight: 500;
    color: #222;
  }

  .compact-table .desc {
    color: #555;
  }

  .compact-table .entry {
    color: #888;
    font-size: 11px;
  }
</style>
</head>
<body>

<table class="compact-table">
  <thead>
    <tr>
      <th>Skill</th>
      <th>定位</th>
      <th>入口</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td class="skill-name"><a href="./skills/alliance-workbench-api/">alliance-workbench-api</a></td>
      <td class="desc"><strong>联盟工作台控制面</strong> — 本机 <code>:3000</code> 全量 HTTP 契约：策略查询 / 审核编排 / 延期托管 / 定投 / 屏蔽 / 结构化查数</td>
      <td class="entry"><a href="./skills/alliance-workbench-api/SKILL.md">SKILL.md</a></td>
    </tr>
    <tr>
      <td class="skill-name"><a href="./skills/ams-default-contact/">ams-default-contact</a></td>
      <td class="desc"><strong>AMS 联系人</strong> — 腾讯 DSP 服务商后台，API 批量选中子客「账户联系人」，禁止开户页逐条点选</td>
      <td class="entry"><a href="./skills/ams-default-contact/SKILL.md">SKILL.md</a></td>
    </tr>
    <tr>
      <td class="skill-name"><a href="./skills/query-mapping/">query-mapping</a></td>
      <td class="desc"><strong>取数口径引擎</strong> — 时间 / 流量主 / 广告主 / 场景 / 转化目标 → 字段映射；离线 <code>85587</code> vs 实时 <code>129496</code></td>
      <td class="entry"><a href="./skills/query-mapping/SKILL.md">SKILL.md</a></td>
    </tr>
    <tr>
      <td class="skill-name"><a href="./skills/query-cpm/">query-cpm</a></td>
      <td class="desc"><strong>CPM 归因诊断</strong> — 媒体 CPM 下滑拆预算结构、场景占比与同场景 CTR / CVR（先 mapping 再查数）</td>
      <td class="entry"><a href="./skills/query-cpm/SKILL.md">SKILL.md</a></td>
    </tr>
  </tbody>
</table>

</body>
</html>


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
