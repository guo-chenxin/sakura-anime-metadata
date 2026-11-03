"""确认 sort=rank 的翻页上限与总量。

已知：
- sort=title 只能到第 10 页（第 11 页起返回 10820 字节的「找不到该页」）
- sort=rank  第 11 页仍有 24 条

需要确认 sort=rank 能翻多深、总量多少。
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


def total_count(html: str) -> str:
    for pat in (
        r"共\s*<[^>]*>([\d,]+)",
        r"([\d,]{4,})\s*(?:部|个|条)",
        r"total[^0-9]{0,10}([\d,]+)",
    ):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return "?"


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
        note("=" * 72)
        note("1. sort=rank 探测深页")
        note("=" * 72)

        probes = [1, 11, 50, 100, 200, 400, 600, 800, 999, 1200, 1500]
        last_ok = 0
        for page in probes:
            try:
                r = c.get(
                    f"{WEB}/anime/browser",
                    params={"page": page, "sort": "rank"},
                )
                ids = ITEM_ID.findall(r.text)
                note(
                    f"  page={page:<5} HTTP {r.status_code} "
                    f"len={len(r.content):>7} ids={len(ids):>3}"
                )
                if ids:
                    last_ok = page
            except Exception as exc:  # noqa: BLE001
                note(f"  page={page:<5} ERR {type(exc).__name__}")
            time.sleep(2.5)

        note(f"\n  sort=rank 可用到第 {last_ok} 页")

        # 第 1 页里有没有总数文案
        note()
        note("=" * 72)
        note("2. 总量线索")
        note("=" * 72)
        r = c.get(f"{WEB}/anime/browser", params={"sort": "rank"})
        html = r.text
        (OUT / "rank_p1.html").write_text(html, encoding="utf-8")
        note(f"  页面里提取到的数字线索: {total_count(html)}")

        # 找分页控件里的最大页码
        pages = [int(x) for x in re.findall(r"[?&]page=(\d+)", html)]
        if pages:
            note(f"  分页控件最大页码: {max(pages)}")

        # 找「共 X 部」之类
        for pat in (
            r"共\s*([\d,]+)\s*(?:部|个|条|项)",
            r"([\d,]{3,})\s*部(?:作品|动画)",
            r"全部\s*([\d,]+)",
        ):
            m = re.search(pat, html)
            if m:
                note(f"  匹配到: {m.group(0)!r}")

        # 二分找上限
        note()
        note("=" * 72)
        note("3. 二分找 sort=rank 的真实上限")
        note("=" * 72)
        lo, hi = 1, 2000
        while lo < hi:
            mid = (lo + hi + 1) // 2
            try:
                rr = c.get(
                    f"{WEB}/anime/browser",
                    params={"page": mid, "sort": "rank"},
                )
                ok = len(ITEM_ID.findall(rr.text)) > 0
            except Exception:  # noqa: BLE001
                ok = False
            note(f"  试 page={mid:<5} {'有数据' if ok else '空'}")
            if ok:
                lo = mid
            else:
                hi = mid - 1
            time.sleep(2.5)
            if hi - lo <= 1:
                break

        note(f"\n  sort=rank 最大可用页码 ≈ {lo}")
        note(f"  按每页 24 条估算：约 {lo * 24} 部")

    (OUT / "report_rank.txt").write_text(
        "\n".join(out), encoding="utf-8"
    )


if __name__ == "__main__":
    sys.exit(main())