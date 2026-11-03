"""验证 sort=rank 深页返回的是否为真实不同的数据。

可疑点：
- page=600/800/999 返回空（22345 字节）
- page=1200/1500 返回 24 条（53790 字节，与 page=1 完全相同）
- 二分说 1999 都有数据

三种可能：
A. 深页做了「页码折叠」，超过某值就返回第 1 页（那我们永远拿不到全量）
B. 深页有缓存/限速，间歇性失败
C. 分页在某个点之后真的没了

验证方式：比较各页返回的 id 集合是否真的不同。
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
        note("=" * 74)
        note("1. 逐页比对 id 集合（判断是否折叠到同一页）")
        note("=" * 74)

        pages = [1, 2, 400, 500, 550, 580, 600, 650, 700, 800, 999, 1200]
        result: dict[int, list[str]] = {}
        for page in pages:
            try:
                r = c.get(
                    f"{WEB}/anime/browser",
                    params={"page": page, "sort": "rank"},
                )
                ids = ITEM_ID.findall(r.text)
                result[page] = ids
                note(
                    f"  page={page:<5} len={len(r.content):>7} "
                    f"ids={len(ids):>3} first={ids[:2]}"
                )
            except Exception as exc:  # noqa: BLE001
                note(f"  page={page:<5} ERR {type(exc).__name__}")
                result[page] = []
            time.sleep(3.0)

        note()
        note("=" * 74)
        note("2. 集合关系分析")
        note("=" * 74)
        base = set(result.get(1) or [])
        note(f"  page=1 的 id 集合: 大小 {len(base)}")
        for page in pages:
            ids = result.get(page) or []
            s = set(ids)
            if not s:
                note(f"  page={page:<5} 空")
                continue
            same_as_1 = s == base
            overlap = len(s & base)
            note(
                f"  page={page:<5} 与 page1 相同={same_as_1} "
                f"重叠={overlap}/{len(s)}"
            )

        note()
        note("=" * 74)
        note("3. 检查 page=1 与 page=1200 的原始 HTML 是否逐字节相同")
        note("=" * 74)
        for page in (1, 1200):
            try:
                r = c.get(
                    f"{WEB}/anime/browser",
                    params={"page": page, "sort": "rank"},
                )
                (OUT / f"cmp_{page}.html").write_text(
                    r.text, encoding="utf-8"
                )
                note(f"  page={page:<5} 已保存，len={len(r.content)}")
            except Exception as exc:  # noqa: BLE001
                note(f"  page={page:<5} ERR {type(exc).__name__}")
            time.sleep(3.0)

        # 用 css 里的分页当前页判断
        note()
        note("=" * 74)
        note("4. 检查页面里的「当前页码」标记")
        note("=" * 74)
        for page in (1, 400, 600, 1200):
            try:
                r = c.get(
                    f"{WEB}/anime/browser",
                    params={"page": page, "sort": "rank"},
                )
                # 分页控件里 class 含 "p_cur" 或 "focus" 的是当前页
                mm = re.search(
                    r'class="[^"]*(?:p_cur|focus)[^"]*"[^>]*>(\d+)<',
                    r.text,
                )
                cur = mm.group(1) if mm else "?"
                note(f"  请求 page={page:<5} 页面自报当前页 = {cur}")
            except Exception as exc:  # noqa: BLE001
                note(f"  请求 page={page:<5} ERR {type(exc).__name__}")
            time.sleep(3.0)

    (OUT / "report_verify.txt").write_text(
        "\n".join(out), encoding="utf-8"
    )


if __name__ == "__main__":
    sys.exit(main())