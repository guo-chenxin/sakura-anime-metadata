"""确定全量枚举策略。

已确认的事实
------------
- ``sort=title`` 只能到第 10 页
- ``sort=rank`` 能到约 400~450 页，之后返回空页；
  再大的页码会**折叠回第 1 页**（page=1200 与 page=1 逐字节相同）

所以单纯翻页最多约 450 × 24 ≈ 1.08 万部，拿不到全量。

本脚本验证「按年份分批」是否可行
--------------------------------
``bgm.tv/anime/browser?year=YYYY&sort=rank``

若每年都能完整翻页，就可以按年份循环，覆盖全部历史。
先测 1990~2026 各年份的总量和翻页深度。
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import httpx

UA = (
    "sakura-anime-metadata/0.1 "
    "(https://github.com/guo-chenxin/sakura-anime-metadata)"
)
WEB = "https://bgm.tv"
ITEM_ID = re.compile(r'<li id="item_(\d+)"')
OUT = Path("probe_ids")

out: list[str] = []


def note(m=""):
    out.append(str(m))
    print(m, flush=True)


def fetch(c, params: dict) -> tuple[int, list[str], str]:
    try:
        r = c.get(f"{WEB}/anime/browser", params=params)
        ids = ITEM_ID.findall(r.text)
        return (len(r.content), ids, r.text)
    except Exception as exc:  # noqa: BLE001
        return (0, [], f"__ERR__{type(exc).__name__}")


def main() -> None:
    with httpx.Client(
        timeout=30,
        headers={
            "User-Agent": UA,
            "Accept": "text/html",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        follow_redirects=True,
    ) as c:
        # ---------- 1. 精确定位 sort=rank 的最后一页 ----------
        note("=" * 74)
        note("1. 精确定位 sort=rank 的最后一页（400~500 二分）")
        note("=" * 74)
        lo, hi = 400, 500
        while lo < hi:
            mid = (lo + hi + 1) // 2
            n, ids, _ = fetch(c, {"page": mid, "sort": "rank"})
            note(f"  page={mid:<4} ids={len(ids)}")
            if ids:
                lo = mid
            else:
                hi = mid - 1
            time.sleep(2.5)
            if hi - lo <= 1:
                break
        note(f"\n  sort=rank 最后一页 = {lo}，约 {lo * 24} 部")
        max_rank_page = lo

        # ---------- 2. 按年份分批 ----------
        note()
        note("=" * 74)
        note("2. 按年份分批（测 2024 / 2010 / 2000 / 1990）")
        note("=" * 74)

        year_stats: list[tuple[int, int, int]] = []  # (year, 第1页ids, 最多页)
        for year in (2024, 2010, 2000, 1990):
            n1, ids1, html1 = fetch(
                c, {"year": year, "sort": "rank", "page": 1}
            )
            note(f"\n  year={year}: page1 ids={len(ids1)} len={n1}")

            if not ids1:
                note(f"    year={year} 第 1 页就空，跳过")
                time.sleep(2.5)
                continue

            # 页面里找最大页码
            pages = [int(x) for x in re.findall(
                r"[?&]page=(\d+)", html1
            )]
            hint = max(pages) if pages else 0
            note(f"    分页控件提示最大页码: {hint}")

            # 试探：翻到 hint 和 hint+10
            for probe_page in (hint, hint + 10, 100, 200):
                if probe_page < 1:
                    continue
                _, ids_probe, _ = fetch(
                    c,
                    {"year": year, "sort": "rank", "page": probe_page},
                )
                note(
                    f"    page={probe_page:<4} ids={len(ids_probe)}"
                )
                time.sleep(2.5)

            year_stats.append((year, len(ids1), hint))
            time.sleep(2.5)

        # ---------- 3. 年度分布抽样 ----------
        note()
        note("=" * 74)
        note("3. 各年份第 1 页是否有数据（1990~2026 抽样）")
        note("=" * 74)
        counts: dict[int, int] = {}
        for year in list(range(2026, 2015, -1)) + [
            2014, 2010, 2005, 2000, 1995, 1990, 1985, 1980
        ]:
            n, ids, _ = fetch(
                c, {"year": year, "sort": "rank", "page": 1}
            )
            counts[year] = len(ids)
            note(f"  year={year}  第1页 ids={len(ids)}")
            time.sleep(2.2)

        note()
        note("=" * 74)
        note("结论")
        note("=" * 74)
        note(f"  sort=rank 单排序可翻到第 {max_rank_page} 页 "
             f"≈ {max_rank_page * 24} 部")
        ok_years = sum(1 for v in counts.values() if v > 0)
        note(f"  按年份过滤：{ok_years}/{len(counts)} 个年份第 1 页有数据")
        if ok_years == len(counts):
            note("  → 按年份分批可行，可覆盖全量")
        else:
            note("  → 部分年份无数据，需进一步确认")

    (OUT / "report_year.txt").write_text(
        "\n".join(out), encoding="utf-8"
    )


if __name__ == "__main__":
    sys.exit(main())