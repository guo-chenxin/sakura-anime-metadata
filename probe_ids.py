"""探测如何枚举 Bangumi 全量动画 id。

问题
----
Bangumi 的 API **没有「列出全部动画」的接口**。要采全量（约 2 万部
``type=2``），必须先拿到 id 列表。

三套候选方案
------------
A. 网页筛选页 ``bgm.tv/anime/browser?page=N``
   —— 服务端渲染，翻页拿 id。若可行，833 页 ≈ 14 分钟。
B. 搜索 API ``POST /v0/search/subjects`` 分页
   —— 用假名/汉字做关键词遍历。覆盖度不确定。
C. 直接扫 id 区间
   —— 太慢（5 万次请求 ≈ 11 天），只作兜底参考。

本脚本只探测，不采集数据。

用法
----
    python probe_ids.py
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
OUT = Path("probe_ids")
OUT.mkdir(exist_ok=True)

# 网页里指向条目的链接：/subject/12345 或 /anime/12345
SUBJECT_LINK = re.compile(r'href="/(?:subject|anime)/(\d+)"')

report: list[str] = []


def note(m=""):
    report.append(str(m))
    print(m)


def probe_web_browser(c: httpx.Client) -> int | None:
    """方案 A：网页筛选页。返回可翻的最大页数（探测到的）。"""
    note("=" * 70)
    note("方案 A: bgm.tv/anime/browser 网页筛选页")
    note("=" * 70)

    url = f"{WEB}/anime/browser"
    try:
        r = c.get(url, params={"page": 1})
        note(f"  HTTP {r.status_code}  {len(r.content)} bytes")
        if r.status_code != 200:
            note("  失败")
            return None

        html = r.text
        (OUT / "browser_p1.html").write_text(html, encoding="utf-8")

        ids = SUBJECT_LINK.findall(html)
        uniq = list(dict.fromkeys(ids))
        note(f"  第 1 页提取到 {len(uniq)} 个 id: {uniq[:10]}")

        # 找分页控件里的最大页码
        pages = re.findall(r"[?&]page=(\d+)", html)
        max_page = max((int(p) for p in pages), default=0)
        note(f"  分页控件里出现的最大页码: {max_page}")

        # 找总数（常见文案：「共 X 条」「X 项」）
        for pat in (
            r"共\s*([\d,]+)\s*(?:条|项|个)",
            r"total[^0-9]{0,8}([\d,]+)",
            r"([\d,]+)\s*(?:部作品|部动画)",
        ):
            m = re.search(pat, html)
            if m:
                note(f"  文案里发现总数: {m.group(1)}  (~{pat})")
                break

        # 抽样第 2 页，确认翻页有效（id 不重复）
        time.sleep(1.5)
        r2 = c.get(url, params={"page": 2})
        if r2.status_code == 200:
            ids2 = list(dict.fromkeys(SUBJECT_LINK.findall(r2.text)))
            overlap = len(set(uniq) & set(ids2))
            note(
                f"  第 2 页 {len(ids2)} 个 id，与第 1 页重叠 {overlap}"
                f"  {'← 翻页有效' if overlap == 0 else '← 可能无效'}"
            )

        # 测 sort 参数是否影响
        time.sleep(1.5)
        r3 = c.get(url, params={"page": 1, "sort": "rank"})
        if r3.status_code == 200:
            ids3 = list(dict.fromkeys(SUBJECT_LINK.findall(r3.text)))
            note(f"  sort=rank 时 {len(ids3)} 个 id，与默认页重叠 "
                 f"{len(set(uniq) & set(ids3))}")

        return max_page or None
    except Exception as exc:  # noqa: BLE001
        note(f"  异常: {type(exc).__name__}: {exc}")
        return None


def probe_search(c: httpx.Client) -> None:
    """方案 B：搜索 API 的分页能力。"""
    note()
    note("=" * 70)
    note("方案 B: POST /v0/search/subjects 分页")
    note("=" * 70)

    # B1. 空关键词能否返回全部？
    for kw in ("", "a", "あ", "的"):
        try:
            r = c.post(
                f"{API}/v0/search/subjects",
                params={"limit": 50, "offset": 0},
                json={"keyword": kw, "filter": {"type": [2]}},
            )
            if r.status_code == 200:
                d = r.json()
                note(
                    f"  关键词 {kw!r:6} total={d.get('total')} "
                    f"返回={len(d.get('data') or [])}"
                )
            else:
                note(f"  关键词 {kw!r:6} HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            note(f"  关键词 {kw!r:6} {type(exc).__name__}")
        time.sleep(1.5)

    # B2. offset 能否翻到深处？
    note("  --- offset 深度测试 ---")
    for off in (0, 50, 500, 1000, 5000):
        try:
            r = c.post(
                f"{API}/v0/search/subjects",
                params={"limit": 50, "offset": off},
                json={"keyword": "的", "filter": {"type": [2]}},
            )
            if r.status_code == 200:
                d = r.json()
                got = len(d.get("data") or [])
                first = (d.get("data") or [{}])[0].get("id")
                note(
                    f"  offset={off:<6} 返回={got:<3} 首个id={first}"
                )
            else:
                note(f"  offset={off:<6} HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            note(f"  offset={off:<6} {type(exc).__name__}")
        time.sleep(1.5)


def probe_sitemap(c: httpx.Client) -> None:
    """方案附加项：sitemap 是否含条目列表。"""
    note()
    note("=" * 70)
    note("附加: sitemap")
    note("=" * 70)
    for p in ("/sitemap.xml", "/sitemap_index.xml", "/robots.txt"):
        try:
            r = c.get(f"{WEB}{p}")
            note(f"  {p:22} HTTP {r.status_code}  {len(r.content)} bytes")
            if r.status_code == 200 and len(r.content) < 4000:
                note(f"      {r.text[:200]}")
        except Exception as exc:  # noqa: BLE001
            note(f"  {p:22} {type(exc).__name__}")
        time.sleep(1)


def main() -> None:
    with httpx.Client(
        timeout=30,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        follow_redirects=True,
    ) as c:
        max_page = probe_web_browser(c)
        probe_search(c)
        probe_sitemap(c)

    note()
    note("=" * 70)
    note("结论")
    note("=" * 70)
    if max_page:
        note(f"  方案 A 可用：网页筛选页支持翻页，最大页码 ≥ {max_page}")
        note(f"  按每页 24 条估算，总量约 {max_page * 24} 部")
    else:
        note("  方案 A 未探测到有效分页，需看方案 B 结果")

    (OUT / "report.txt").write_text("\n".join(report), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())