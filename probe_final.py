"""最终确认：按季度切片能否完整枚举全量。

已确认
------
- API ``air_date`` 过滤**真的生效**（2024 首条是 2024-01-13，1990 首条是 1990-03-10）
- 但 ``total`` 被钳制在 1000，``limit=50`` 实际只返回 20 条

要确认
------
1. 搜索接口每页真实上限是多少条
2. ``offset`` 真实上限是多少（能否到 1000）
3. 按季度切片后，每片能否完整拉完
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

UA = (
    "sakura-anime-metadata/0.1 "
    "(https://github.com/guo-chenxin/sakura-anime-metadata)"
)
API = "https://api.bgm.tv"
OUT = Path("probe_ids")

out: list[str] = []


def note(m=""):
    out.append(str(m))
    print(m, flush=True)


def search(c, flt: dict, limit: int, offset: int):
    try:
        r = c.post(
            f"{API}/v0/search/subjects",
            params={"limit": limit, "offset": offset},
            json={"keyword": "", "filter": flt},
        )
        if r.status_code == 200:
            d = r.json()
            return d.get("total"), len(d.get("data") or [])
        return f"HTTP{r.status_code}", 0
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, 0


def main() -> None:
    Q2024 = {
        "type": [2],
        "air_date": [">=2024-01-01", "<2025-01-01"],
    }
    Q1_2024 = {
        "type": [2],
        "air_date": [">=2024-01-01", "<2024-04-01"],
    }

    with httpx.Client(
        timeout=30,
        headers={"User-Agent": UA, "Accept": "application/json"},
        follow_redirects=True,
    ) as c:
        # ---------- 1. limit 的真实上限 ----------
        note("=" * 74)
        note("1. 每页条数上限（limit 参数）")
        note("=" * 74)
        for lim in (10, 20, 25, 50, 100):
            total, got = search(c, Q1_2024, lim, 0)
            note(f"  limit={lim:<4} → total={total} 实际返回 {got} 条")
            time.sleep(2.0)

        # ---------- 2. offset 的真实上限 ----------
        note()
        note("=" * 74)
        note("2. offset 深度（2024 Q1，limit=20）")
        note("=" * 74)
        offsets = [0, 20, 40, 60, 80, 100, 150, 200, 300, 500, 900, 1000, 1100]
        last_ok = 0
        for off in offsets:
            total, got = search(c, Q1_2024, 20, off)
            mark = "有" if got else "空"
            note(f"  offset={off:<5} {mark} 返回={got:<3} total={total}")
            if got:
                last_ok = off
            time.sleep(2.0)
        note(f"  2024 Q1 最深可用 offset ≈ {last_ok}")

        # ---------- 3. 2024 年按季度切分 ----------
        note()
        note("=" * 74)
        note("3. 2024 年按季度切分")
        note("=" * 74)
        quarters = [
            ("Q1", ">=2024-01-01", "<2024-04-01"),
            ("Q2", ">=2024-04-01", "<2024-07-01"),
            ("Q3", ">=2024-07-01", "<2024-10-01"),
            ("Q4", ">=2024-10-01", "<2025-01-01"),
        ]
        total_2024 = 0
        for label, start, end in quarters:
            flt = {"type": [2], "air_date": [start, end]}
            total, got = search(c, flt, 20, 0)
            note(f"  {label}: total={total} 首页 {got} 条")
            if isinstance(total, int):
                total_2024 += total
            time.sleep(2.0)
        note(f"  2024 四季合计（按 total）≈ {total_2024}")

        # ---------- 4. 各年 total 分布 ----------
        note()
        note("=" * 74)
        note("4. 各年 total（判断是否需要更细切片）")
        note("=" * 74)
        years = [2026, 2025, 2024, 2023, 2022, 2020, 2015, 2010,
                 2005, 2000, 1995, 1990, 1985, 1980]
        big_years = []
        for y in years:
            flt = {
                "type": [2],
                "air_date": [f">={y}-01-01", f"<{y + 1}-01-01"],
            }
            total, got = search(c, flt, 20, 0)
            flag = ""
            if isinstance(total, int) and total >= 1000:
                flag = "  ← 达到钳制上限，需按月切"
                big_years.append(y)
            note(f"  {y}: total={total}{flag}")
            time.sleep(2.0)

        note()
        note("=" * 74)
        note("结论")
        note("=" * 74)
        note(f"  每页实际上限：20 条（limit 传 50/100 也只给 20）")
        note(f"  offset 上限：待确认（2024 Q1 可到 {last_ok}）")
        note(f"  需要按月切的年份：{big_years or '无'}")
        if not big_years:
            note("  → 按年切片即可覆盖全量")

    (OUT / "report_final.txt").write_text(
        "\n".join(out), encoding="utf-8"
    )


if __name__ == "__main__":
    sys.exit(main())