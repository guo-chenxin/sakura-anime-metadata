"""诊断 bgm.tv/anime/browser 的翻页行为。

现象：第 11 页被判定为「无条目」，但探测时明明 999 页都存在。

可能原因：
1. 请求太快被临时限速，返回了空列表或错误页
2. ``sort=title`` 这种排序下页码上限不同
3. 某些页需要特定 header
4. 页面结构在特定页变了

本脚本逐页打印状态，定位问题。
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
OUT.mkdir(exist_ok=True)

out: list[str] = []


def note(m=""):
    out.append(str(m))
    print(m, flush=True)


def main() -> None:
    with httpx.Client(
        timeout=30,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        follow_redirects=True,
    ) as c:
        note("=" * 72)
        note("逐页诊断（sort=title，page 1..15）")
        note("=" * 72)

        for page in range(1, 16):
            try:
                r = c.get(
                    f"{WEB}/anime/browser",
                    params={"page": page, "sort": "title"},
                )
                ids = ITEM_ID.findall(r.text)
                note(
                    f"  page={page:<3} HTTP {r.status_code} "
                    f"len={len(r.content):>7} ids={len(ids):>3} "
                    f"final_url={str(r.url)[:60]}"
                )

                if page == 11 or len(ids) == 0:
                    (OUT / f"pg{page}.html").write_text(
                        r.text, encoding="utf-8"
                    )
                    note(f"      已保存 pg{page}.html 供检查")
                    # 打印关键片段
                    low = r.text.lower()
                    for kw in (
                        "limit", "受限", "too many", "denied",
                        "captcha", "找不到", "not found",
                    ):
                        if kw in low:
                            note(f"      页面含关键词 {kw!r}")
                    note(f"      前 200 字: {r.text[:200]!r}")
            except Exception as exc:  # noqa: BLE001
                note(f"  page={page:<3} ERR {type(exc).__name__}")
            time.sleep(2.5)

        note()
        note("=" * 72)
        note("对照：sort=rank / 无 sort 参数")
        note("=" * 72)
        for sort in ("rank", "", "date"):
            params = {"page": 11}
            if sort:
                params["sort"] = sort
            try:
                r = c.get(f"{WEB}/anime/browser", params=params)
                ids = ITEM_ID.findall(r.text)
                label = sort or "(默认)"
                note(
                    f"  sort={label:<8} page=11 HTTP {r.status_code} "
                    f"len={len(r.content):>7} ids={len(ids)}"
                )
            except Exception as exc:  # noqa: BLE001
                note(f"  sort={sort:<8} ERR {type(exc).__name__}")
            time.sleep(2.5)

        note()
        note("=" * 72)
        note("深页测试（确认上限是否真是 999）")
        note("=" * 72)
        for page in (50, 200, 500, 990, 995, 999, 1000):
            try:
                r = c.get(
                    f"{WEB}/anime/browser",
                    params={"page": page, "sort": "title"},
                )
                ids = ITEM_ID.findall(r.text)
                note(
                    f"  page={page:<5} HTTP {r.status_code} "
                    f"len={len(r.content):>7} ids={len(ids)}"
                )
            except Exception as exc:  # noqa: BLE001
                note(f"  page={page:<5} ERR {type(exc).__name__}")
            time.sleep(2.5)

    (OUT / "report.txt").write_text("\n".join(out), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())