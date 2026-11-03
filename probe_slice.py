"""验证按年份/时间切片枚举的可行方案。

上一轮的问题
------------
1. ``?year=2024`` 这类 query 参数**无效**：2024/2010/2000/1990 返回的
   页面长度都是 53790，完全相同，说明过滤没生效
2. ``page=1009`` 又出现了折叠（返回第 1 页数据）

所以「按年份分批」这条路还没验证通。本脚本测三个替代方案：

A. **API 搜索 + air_date 过滤**（最有希望）
   ``POST /v0/search/subjects`` 的 filter 支持 ``air_date`` 范围语法，
   按年切片后每年数据量小，offset 上限 1000 足够。

B. **网页路径形式**
   ``/anime/browser/airtime/2024`` —— 可能是正确的筛选路径。

C. **API 搜索 + 其它维度**
   按 tag、按 rank 分段。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx

UA = (
    "sakura-anime-metadata/0.1 "
    "(https://github.com/guo-chenxin/sakura-anime-metadata)"
)
API = "https://api.bgm.tv"
WEB = "https://bgm.tv"
ITEM_ID = re.compile(r'<li id="item_(\d+)"')
OUT = Path("probe_ids")

out: list[str] = []


def note(m=""):
    out.append(str(m))
    print(m, flush=True)


def main() -> None:
    with httpx.Client(
        timeout=30,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/html",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        follow_redirects=True,
    ) as c:
        # ================= A. API 搜索 + air_date 范围 =================
        note("=" * 74)
        note("方案 A: API 搜索 + air_date 过滤（按年切片）")
        note("=" * 74)

        # A1. 基础：不带 air_date
        cases = [
            ("无过滤", {"type": [2]}),
            ("2024 全年", {"type": [2],
                          "air_date": [">=2024-01-01", "<2025-01-01"]}),
            ("2024 上半年", {"type": [2],
                            "air_date": [">=2024-01-01", "<2024-07-01"]}),
            ("1990 全年", {"type": [2],
                          "air_date": [">=1990-01-01", "<1991-01-01"]}),
        ]
        for label, flt in cases:
            try:
                r = c.post(
                    f"{API}/v0/search/subjects",
                    params={"limit": 50, "offset": 0},
                    json={"keyword": "", "filter": flt},
                )
                if r.status_code == 200:
                    d = r.json()
                    note(
                        f"  {label:16} total={d.get('total'):<8} "
                        f"返回={len(d.get('data') or [])}"
                    )
                else:
                    note(f"  {label:16} HTTP {r.status_code}: "
                         f"{r.text[:80]}")
            except Exception as exc:  # noqa: BLE001
                note(f"  {label:16} {type(exc).__name__}")
            time.sleep(2.2)

        # A2. 确认 air_date 真的生效：比较首条 id 是否不同
        note("  --- 验证过滤是否真生效（比对首条 id）---")
        first_ids: dict[str, int | None] = {}
        for label, flt in cases:
            try:
                r = c.post(
                    f"{API}/v0/search/subjects",
                    params={"limit": 5, "offset": 0},
                    json={"keyword": "", "filter": flt},
                )
                if r.status_code == 200:
                    data = r.json().get("data") or []
                    fid = data[0]["id"] if data else None
                    fd = data[0].get("date") if data else None
                    first_ids[label] = fid
                    note(f"  {label:16} 首条 id={fid} date={fd}")
            except Exception as exc:  # noqa: BLE001
                note(f"  {label:16} {type(exc).__name__}")
            time.sleep(2.2)

        # A3. offset 深度：按年切片后能否翻到底
        note("  --- 2024 年内 offset 深度 ---")
        for off in (0, 100, 300, 600, 900):
            try:
                r = c.post(
                    f"{API}/v0/search/subjects",
                    params={"limit": 50, "offset": off},
                    json={
                        "keyword": "",
                        "filter": {
                            "type": [2],
                            "air_date": [">=2024-01-01", "<2025-01-01"],
                        },
                    },
                )
                if r.status_code == 200:
                    d = r.json()
                    note(
                        f"    offset={off:<5} 返回={len(d.get('data') or []):<3} "
                        f"total={d.get('total')}"
                    )
            except Exception as exc:  # noqa: BLE001
                note(f"    offset={off:<5} {type(exc).__name__}")
            time.sleep(2.2)

        # ================= B. 网页路径形式 =================
        note()
        note("=" * 74)
        note("方案 B: 网页路径形式筛选")
        note("=" * 74)
        for path in (
            "/anime/browser/airtime/2024",
            "/anime/browser/airtime/2024?page=1",
            "/anime/browser?airtime=2024",
            "/anime/browser?year=2024&sort=rank&page=1",
        ):
            try:
                r = c.get(f"{WEB}{path}")
                ids = ITEM_ID.findall(r.text)
                note(f"  {path:44} ids={len(ids):>3} len={len(r.content)}")
            except Exception as exc:  # noqa: BLE001
                note(f"  {path:44} {type(exc).__name__}")
            time.sleep(2.2)

        # ================= C. tag 维度 =================
        note()
        note("=" * 74)
        note("方案 C: 按 tag 维度（用 API 搜索）")
        note("=" * 74)
        for tag in ("TV", "剧场版", "OVA", "日本动画", "中国动画"):
            try:
                r = c.post(
                    f"{API}/v0/search/subjects",
                    params={"limit": 50, "offset": 0},
                    json={
                        "keyword": "",
                        "filter": {"type": [2], "tag": [tag]},
                    },
                )
                if r.status_code == 200:
                    d = r.json()
                    note(
                        f"  tag={tag:10} total={d.get('total'):<8} "
                        f"返回={len(d.get('data') or [])}"
                    )
            except Exception as exc:  # noqa: BLE001
                note(f"  tag={tag:10} {type(exc).__name__}")
            time.sleep(2.2)

    (OUT / "report_slice.txt").write_text(
        "\n".join(out), encoding="utf-8"
    )


if __name__ == "__main__":
    sys.exit(main())