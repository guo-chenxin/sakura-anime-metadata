"""Bangumi API 探测脚本。

目的
----
在写正式采集代码之前，先拿到 bangumi 的**真实返回结构**，
避免基于猜测设计数据库映射。

探测内容
--------
1. 搜索接口：`POST /v0/search/subjects`（按标题找 id）
2. 详情接口：`GET /v0/subjects/{id}`（名称/简介/标签/infobox）
3. 剧集接口：`GET /v0/episodes?subject_id=`（分集信息）
4. 关联接口：`GET /v0/subjects/{id}/subjects`（季/剧场版关系）
5. 日历接口：`GET /calendar`（每日放送）
6. 分页上限与限流表现

输出
----
所有原始响应写入 ``probe/`` 目录，一个接口一个文件，
保持**原样不做任何加工**，便于逐字段核对。

用法
----
    python probe.py

在 GitHub Actions 里跑（本地无法访问 api.bgm.tv）。
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

import httpx

# Bangumi 要求带 UA，格式「应用名/版本 (联系方式)」
UA = (
    "sakura-anime-metadata/0.1 "
    "(https://github.com/guo-chenxin/sakura-anime-metadata)"
)
BASE = "https://api.bgm.tv"
OUT_DIR = Path("probe")

# 探测用关键词：覆盖连载中 / 已完结 / 多季 / 剧场版 四种形态
KEYWORDS = [
    "刀剑神域",      # 已完结，多季 + 剧场版
    "海贼王",        # 连载中，超长
    "无职转生",      # 多季，连载中
    "孤独摇滚",      # 单季完结，近期
    "进击的巨人",    # 完结，多剧场版
]

# 请求间隔（秒）。Bangumi 未认证限流约 30 次/分钟，这里保守用 2 秒
DELAY = 2.0


def save(name: str, data: object, *, raw: str | None = None) -> None:
    """把响应写入 probe/ 目录。"""
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"{name}.json"
    if raw is not None:
        path = OUT_DIR / f"{name}.raw.txt"
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    size = path.stat().st_size
    print(f"    -> {path}  ({size} bytes)")


def main() -> None:
    report: list[str] = []
    found_ids: dict[str, int] = {}

    with httpx.Client(
        timeout=30,
        headers={"User-Agent": UA, "Accept": "application/json"},
        follow_redirects=True,
    ) as c:

        # ---------- 1. 搜索接口 ----------
        print("\n[1] POST /v0/search/subjects")
        for kw in KEYWORDS:
            try:
                r = c.post(
                    f"{BASE}/v0/search/subjects",
                    params={"limit": 10},
                    json={
                        "keyword": kw,
                        "filter": {"type": [2]},  # 2 = 动画
                    },
                )
                print(f"  {kw}: HTTP {r.status_code}")
                if r.status_code == 200:
                    d = r.json()
                    items = d.get("data") or []
                    print(f"    命中 {d.get('total')} 条，返回 {len(items)}")
                    for it in items[:3]:
                        print(
                            f"      id={it.get('id')} "
                            f"name_cn={it.get('name_cn')!r} "
                            f"date={it.get('date')}"
                        )
                    if items:
                        found_ids[kw] = items[0]["id"]
                    save(f"search_{kw}", d)
                elif r.status_code == 429:
                    print(f"    限流！Retry-After={r.headers.get('retry-after')}")
                    time.sleep(30)
            except Exception:
                print(f"  {kw}: 失败")
                traceback.print_exc()
            time.sleep(DELAY)

        # ---------- 2. 详情接口 ----------
        print("\n[2] GET /v0/subjects/{id}")
        for kw, sid in found_ids.items():
            try:
                r = c.get(f"{BASE}/v0/subjects/{sid}")
                print(f"  {kw} (id={sid}): HTTP {r.status_code}")
                if r.status_code == 200:
                    d = r.json()
                    print(f"    name      = {d.get('name')!r}")
                    print(f"    name_cn   = {d.get('name_cn')!r}")
                    print(f"    platform  = {d.get('platform')!r}")
                    print(f"    eps       = {d.get('eps')}")
                    print(f"    total_eps = {d.get('total_episodes')}")
                    print(f"    rating    = {d.get('rating', {}).get('score')}")
                    print(f"    summary   = {(d.get('summary') or '')[:40]!r}")
                    ib = d.get("infobox") or []
                    print(f"    infobox   = {len(ib)} 项")
                    for x in ib[:5]:
                        print(f"      {x.get('key')}: "
                              f"{str(x.get('value'))[:40]}")
                    tags = d.get("tags") or []
                    print(f"    tags      = {len(tags)} 个: "
                          f"{[t.get('name') for t in tags[:6]]}")
                    save(f"subject_{sid}", d)
                time.sleep(DELAY)
            except Exception:
                print(f"  {kw}: 失败")
                traceback.print_exc()

        # ---------- 3. 剧集接口 ----------
        print("\n[3] GET /v0/episodes")
        for kw, sid in list(found_ids.items())[:3]:
            try:
                r = c.get(
                    f"{BASE}/v0/episodes",
                    params={"subject_id": sid, "type": 0, "limit": 10},
                )
                print(f"  {kw} (id={sid}): HTTP {r.status_code}")
                if r.status_code == 200:
                    d = r.json()
                    eps = d.get("data") or []
                    print(f"    total={d.get('total')} 返回 {len(eps)}")
                    for e in eps[:3]:
                        print(
                            f"      ep={e.get('ep')} "
                            f"sort={e.get('sort')} "
                            f"name_cn={e.get('name_cn')!r} "
                            f"airdate={e.get('airdate')}"
                        )
                    save(f"episodes_{sid}", d)
                time.sleep(DELAY)
            except Exception:
                print(f"  {kw}: 失败")
                traceback.print_exc()

        # ---------- 4. 关联接口 ----------
        print("\n[4] GET /v0/subjects/{id}/subjects")
        for kw, sid in list(found_ids.items())[:3]:
            try:
                r = c.get(f"{BASE}/v0/subjects/{sid}/subjects")
                print(f"  {kw} (id={sid}): HTTP {r.status_code}")
                if r.status_code == 200:
                    d = r.json()
                    print(f"    关联 {len(d)} 条")
                    for x in d[:5]:
                        print(
                            f"      relation={x.get('relation')!r} "
                            f"id={x.get('id')} "
                            f"name_cn={x.get('name_cn')!r}"
                        )
                    save(f"relations_{sid}", d)
                time.sleep(DELAY)
            except Exception:
                print(f"  {kw}: 失败")
                traceback.print_exc()

        # ---------- 5. 日历接口 ----------
        print("\n[5] GET /calendar")
        try:
            r = c.get(f"{BASE}/calendar")
            print(f"  HTTP {r.status_code}")
            if r.status_code == 200:
                d = r.json()
                total = sum(len(day.get("items") or []) for day in d)
                print(f"  {len(d)} 天，共 {total} 部")
                for day in d[:2]:
                    wd = day.get("weekday") or {}
                    print(f"    {wd.get('cn')}: "
                          f"{len(day.get('items') or [])} 部")
                if d and d[0].get("items"):
                    sample = d[0]["items"][0]
                    print(f"    样例字段: {sorted(sample.keys())}")
                save("calendar", d)
            time.sleep(DELAY)
        except Exception:
            print("  失败")
            traceback.print_exc()

        # ---------- 6. 分页上限 ----------
        print("\n[6] 搜索分页上限（limit=50 是否被接受）")
        try:
            r = c.post(
                f"{BASE}/v0/search/subjects",
                params={"limit": 50, "offset": 0},
                json={"keyword": "动画", "filter": {"type": [2]}},
            )
            print(f"  limit=50: HTTP {r.status_code}")
            if r.status_code == 200:
                d = r.json()
                print(f"    实际返回 {len(d.get('data') or [])} 条，"
                      f"total={d.get('total')}")
            time.sleep(DELAY)
        except Exception:
            print("  失败")

    # ---------- 汇总 ----------
    print("\n" + "=" * 60)
    print("探测完成，文件列表:")
    for p in sorted(OUT_DIR.glob("*")):
        print(f"  {p.name:40} {p.stat().st_size:>8} bytes")
    print("=" * 60)
    print(f"\n找到的 subject id: {found_ids}")

    (OUT_DIR / "_summary.txt").write_text(
        "\n".join(
            [
                f"keywords={KEYWORDS}",
                f"found={found_ids}",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())