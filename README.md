# sakura-anime-metadata

Bangumi 元数据采集与同步。

## 为什么单独建仓库

主项目的服务器无法访问 pi.bgm.tv（IP 层封锁，DoH 拿到真实 IP 也会被 Reset）。
GitHub Actions 的 runner 在海外，可直连 Bangumi。

所以采集放这里，数据通过 pi.github.com 回传给主项目
（github.com / pi.github.com 国内可达，aw.githubusercontent.com 不可达）。

## 数据流

`
GitHub Actions (海外)
  → 调 api.bgm.tv 采集
  → 写 data/bangumi.json.gz
  → commit

主项目服务器 (国内)
  → 从 api.github.com 拉 data/bangumi.json.gz
  → 解压 → upsert 到 PostgreSQL
`

## 目录

- probe.py — 探测 Bangumi 真实返回结构（开发期用）
- .github/workflows/probe.yml — 手动触发的探测 workflow

## 手动触发

Actions 页面 → probe → Run workflow。
