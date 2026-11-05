"""验证 bangumi-data 的获取方式（跑在 Actions，海外网络）。

为什么必须在 Actions 验证
------------------------
本地试了 7 条路全部失败：
    raw.githubusercontent.com   ConnectError（被墙）
    api.github.com contents     文件是 submodule，返回空
    Git Blobs API              404（submodule 不是 blob）
    npm registry               可达但包里没有季度数据
    jsDelivr / unpkg / npmmirror  各种失败

runner 在海外，应该能直连 raw.githubusercontent.com。
本脚本测多种 URL，确认哪种可用，并输出真实数据结构。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

UA = (
    "sakura-anime-metadata/0.1 "
    "(https://github.com/guo-chenxin/sakura-anime-metadata)"
)
OUT = Path("bgmdata")
OUT.mkdir(exist_ok=True)

log: list[str] = []


def note(m=""):
    log.append(str(m))
    print(m, flush=True)


# 待测 URL：都指向同一份季度数据
CANDIDATES = [
    ("raw.githubusercontent (master)",
     "https://raw.githubusercontent.com/bangumi-data/bangumi-data"
     "/master/data/items/2025.json"),
    ("raw.githubusercontent (gh-pages)",
     "https://raw.githubusercontent.com/bangumi-data/bangumi-data"
     "/gh-pages/data/items/2025.json"),
    ("jsDelivr gh",
     "https://cdn.jsdelivr.net/gh/bangumi-data/bangumi-data@master"
     "/data/items/2025.json"),
    ("jsDelivr npm",
     "https://cdn.jsdelivr.net/npm/bangumi-data@latest"
     "/data/items/2025.json"),
    ("unpkg",
     "https://unpkg.com/bangumi-data@latest/data/items/2025.json"),
    ("github.com 直接下载",
     "https://github.com/bangumi-data/bangumi-data"
     "/raw/master/data/items/2025.json"),
]


def main() -> None:
    with httpx.Client(
        timeout=60,
        headers={"User-Agent": UA},
        follow_redirects=True,
    ) as c:
        got = None
        for name, url in CANDIDATES:
            note("=" * 74)
            note(name)
            note(f"  {url}")
            try:
                r = c.get(url)
                note(f"  HTTP {r.status_code}  {len(r.content)} bytes")
                if r.status_code == 200 and len(r.content) > 5000:
                    try:
                        d = json.loads(r.content)
                        items = d.get("items") or []
                        note(f"  ✅ 解析成功，{len(items)} 部")
                        if items:
                            got = (name, items)
                            break
                    except Exception as exc:  # noqa: BLE001
                        note(f"  JSON 解析失败 {type(exc).__name__}")
                else:
                    note(f"  内容不符合预期: {r.text[:120]}")
            except Exception as exc:  # noqa: BLE001
                note(f"  ERR {type(exc).__name__}")
            note("")

        if not got:
            note("所有方式都失败")
            (OUT / "report.txt").write_text(
                "\n".join(log), encoding="utf-8"
            )
            sys.exit(1)

        name, items = got
        note("=" * 74)
        note(f"采用: {name}")
        note("=" * 74)

        # ---------- 数据结构 ----------
        note(f"共 {len(items)} 部")
        note("")
        note("第 1 条完整结构:")
        note(json.dumps(items[0], ensure_ascii=False, indent=2)[:1500])

        # ---------- 字段覆盖率 ----------
        note("")
        note("字段覆盖率:")
        total = len(items)
        for key in ("id", "type", "title", "titleTranslate", "begin",
                    "broadcast", "comment", "sites", "bangumi"):
            cnt = sum(1 for x in items if x.get(key))
            pct = cnt * 100 // total if total else 0
            note(f"  {key:16} {cnt}/{total} ({pct}%)")

        # ---------- Bangumi id 映射质量（最关键）----------
        note("")
        note("=" * 74)
        note("Bangumi id 映射（决定能否免标题匹配）")
        note("=" * 74)
        with_bgm = [x for x in items if x.get("bangumi")]
        note(f"  有 bangumi id 的: {len(with_bgm)}/{total}")
        note("  样例:")
        for x in with_bgm[:8]:
            note(f"    bgm={x['bangumi']:<8} "
                 f"title={x['title'][:30]}")
        # bangumi 字段可能是字符串或多值
        types = {}
        for x in with_bgm[:50]:
            t = type(x["bangumi"]).__name__
            types[t] = types.get(t, 0) + 1
        note(f"  bangumi 字段类型分布: {types}")

        # ---------- broadcast 格式 ----------
        note("")
        note("=" * 74)
        note("broadcast 字段（ISO 8601 周期）")
        note("=" * 74)
        with_bc = [x for x in items if x.get("broadcast")]
        note(f"  有 broadcast 的: {len(with_bc)}/{total} "
             f"({len(with_bc) * 100 // total if total else 0}%)")
        note("  样例:")
        for x in with_bc[:12]:
            note(f"    bgm={str(x.get('bangumi')):<8} "
                 f"{x['broadcast']}")
            note(f"      title={x['title'][:36]}")

        # ---------- 年份范围 ----------
        note("")
        note("begin（首播日期）范围:")
        begins = sorted(x["begin"] for x in items if x.get("begin"))
        if begins:
            note(f"  最早 {begins[0]}  最晚 {begins[-1]}")

        # ---------- 存样本供后续分析 ----------
        (OUT / "sample_2025.json").write_text(
            json.dumps(items[:200], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        (OUT / "report.txt").write_text("\n".join(log), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        (OUT / "report.txt").write_text("\n".join(log), encoding="utf-8")
        sys.exit(1)