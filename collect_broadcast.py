"""采集 bangumi-data 的广播周期（broadcast），供计算每集播出时刻。

数据来源
--------
GitHub: bangumi-data/bangumi-data
路径:   data/items/{year}/{month}.json    ← 注意是按月分文件
结构:   顶层是数组，每条一个作品

关键字段
--------
``sites``
    数组，从中找 ``{"site": "bangumi", "id": "454684"}``
    → ``454684`` 就是 Bangumi subject id，可直接对上 ``anime.bgm_id``
    **不需要标题匹配**。

``broadcast``
    ISO 8601 重复周期，如 ``R/2025-01-02T14:00:00.000Z/P7D``
    = 从 2025-01-02 14:00 UTC 起，每 7 天一次
    这是算出每一集精确播出时刻的钥匙。

``begin``
    首播时刻（UTC）。

产出
----
``data/broadcast.json``
    ``{bgm_id: {"start": iso, "intervalDays": 7, "title": ...}}``
    只保留有 broadcast 且能提取到 bangumi id 的条目。

为什么在 Actions 跑
------------------
本地 raw.githubusercontent.com 不可达；runner 在海外可直连。
另外 GitHub 的 contents API 对 data/items/2025 这种目录只返回空，
必须走 Git Trees + Blobs API，本脚本已按这条路实现。
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

import httpx

UA = (
    "sakura-anime-metadata/0.1 "
    "(https://github.com/guo-chenxin/sakura-anime-metadata)"
)
REPO = "bangumi-data/bangumi-data"
OUT_DIR = Path("data")
OUT_FILE = OUT_DIR / "broadcast.json"

# ISO 8601 周期：R/<起始时刻>/<时长或周期>
# 例：R/2025-01-02T14:00:00.000Z/P7D
# 也支持 P1W（周）、P2W3D 这类组合
BROADCAST_RE = re.compile(
    r"^R/(?P<start>[^/]+)/(?P<dur>P(?:\d+[WD])*(?:T(?:\d+[HM])*)?)$"
)
# P7D → 7；P1W → 7；PT30M → 0（不足一天）
DUR_RE = re.compile(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?")

log: list[str] = []


def note(m=""):
    log.append(str(m))
    print(m, flush=True)


def parse_broadcast(value: str) -> tuple[str, float] | None:
    """解析 broadcast，返回 ``(起始 ISO, 周期天数)``。

    无法解析或周期不足一天时返回 None——周期不足一天无法定位到具体一集。

    支持的形式：P7D、P1W、P14D、P2W1D、PT30M（返回 None）。
    """
    if not value:
        return None
    m = BROADCAST_RE.match(value.strip())
    if not m:
        return None
    start = m.group("start")
    dur = m.group("dur")
    dm = DUR_RE.match(dur)
    if not dm:
        return None
    weeks = int(dm.group(1) or 0)
    days = int(dm.group(2) or 0)
    total = weeks * 7 + days
    if total < 1:
        return None
    return start, float(total)


def extract_bangumi_id(item: dict) -> str | None:
    """从 sites 数组里取 Bangumi subject id。"""
    for s in item.get("sites") or []:
        if not isinstance(s, dict):
            continue
        if s.get("site") == "bangumi" and s.get("id"):
            return str(s["id"])
    return None


def get_tree(c: httpx.Client, sha: str) -> list[dict]:
    r = c.get(
        f"https://api.github.com/repos/{REPO}/git/trees/{sha}"
    )
    if r.status_code != 200:
        return []
    return r.json().get("tree") or []


def get_blob(c: httpx.Client, sha: str) -> str | None:
    r = c.get(
        f"https://api.github.com/repos/{REPO}/git/blobs/{sha}"
    )
    if r.status_code != 200:
        return None
    d = r.json()
    if "content" not in d:
        return None
    return base64.b64decode(d["content"]).decode("utf-8", errors="replace")


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)

    with httpx.Client(
        timeout=120,
        headers={"User-Agent": UA},
        follow_redirects=True,
    ) as c:
        # ---------- 定位 data/items ----------
        note("定位 data/items ...")
        root = get_tree(c, "master")
        data_sha = None
        for x in root:
            if x["path"] == "data":
                data_sha = x["sha"]
                break
        if not data_sha:
            note("找不到 data/ 目录")
            sys.exit(1)

        items_sha = None
        for x in get_tree(c, data_sha):
            if x["path"] == "items":
                items_sha = x["sha"]
                break
        if not items_sha:
            note("找不到 data/items/")
            sys.exit(1)

        years = [
            x for x in get_tree(c, items_sha)
            if x["type"] == "tree" and x["path"].isdigit()
        ]
        years.sort(key=lambda x: x["path"])
        note(f"年份目录 {len(years)} 个: "
             f"{years[0]['path']} ~ {years[-1]['path']}")

        # ---------- 逐月拉取 ----------
        result: dict[str, dict] = {}
        no_bgm = 0
        no_bc = 0
        bad_bc = 0
        total_items = 0

        for yi, y in enumerate(years, 1):
            months = [
                x for x in get_tree(c, y["sha"])
                if x["type"] == "blob" and x["path"].endswith(".json")
            ]
            months.sort(key=lambda x: x["path"])
            ycount = 0

            for m in months:
                txt = get_blob(c, m["sha"])
                if not txt:
                    note(f"  {y['path']}/{m['path']} 读取失败")
                    continue
                try:
                    arr = json.loads(txt)
                except Exception:  # noqa: BLE001
                    note(f"  {y['path']}/{m['path']} JSON 解析失败")
                    continue
                if not isinstance(arr, list):
                    continue

                for item in arr:
                    total_items += 1
                    bgm = extract_bangumi_id(item)
                    if not bgm:
                        no_bgm += 1
                        continue
                    bc = item.get("broadcast")
                    if not bc:
                        no_bc += 1
                        continue
                    parsed = parse_broadcast(bc)
                    if not parsed:
                        bad_bc += 1
                        continue
                    start, interval = parsed
                    # 同一 bgm_id 可能多次出现（不同站点条目），保留首条
                    if bgm not in result:
                        result[bgm] = {
                            "start": start,
                            "intervalDays": interval,
                            "title": item.get("title"),
                            "begin": item.get("begin"),
                        }
                        ycount += 1

            note(f"  [{yi}/{len(years)}] {y['path']} 累计 {len(result)} 条")

        # ---------- 写出 ----------
        OUT_FILE.write_text(
            json.dumps(result, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

        note("")
        note("=" * 66)
        note(f"原始条目 {total_items}")
        note(f"  无 bangumi id:  {no_bgm}")
        note(f"  无 broadcast:   {no_bc}")
        note(f"  broadcast 解析失败: {bad_bc}")
        note(f"可用（有 id + 有周期）: {len(result)}")
        note(f"  写入 {OUT_FILE} "
             f"({OUT_FILE.stat().st_size // 1024} KB)")
        note("=" * 66)

        (OUT_DIR / "broadcast_log.txt").write_text(
            "\n".join(log), encoding="utf-8"
        )


if __name__ == "__main__":
    main()