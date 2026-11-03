"""Bangumi 元数据采集。

产出
----
``data/subjects.jsonl.gz``   —— 每行一个 subject 的完整元数据
``data/episodes.jsonl.gz``   —— 每行一个 episode
``data/manifest.json``       —— 版本、数量、时间戳

为什么用 JSONL
--------------
1. **可流式追加**：采一批写一批，中断不丢已采数据
2. **可增量**：新数据 append，不用重写整个文件
3. **gz 压缩**：2 万部 subject 约 40 MB 原始 → 压缩后 5 MB 左右

id 枚举
-------
Bangumi API 没有「列出全部动画」的接口（实测确认）。用网页筛选页：

    https://bgm.tv/anime/browser?page=N     # 1..999，每页 24 条

``sort=title`` 时翻页稳定无重叠（实测第 1、2 页重叠 0）。
共 999 页 ≈ 24000 部。

采集粒度
--------
- subject 详情：``GET /v0/subjects/{id}``
- 剧集列表：``GET /v0/episodes?subject_id={id}&limit=100``
- 关联作品：``GET /v0/subjects/{id}/subjects``

限流
----
未认证约 30 次/分钟。本脚本保守用 ``DELAY=2.2`` 秒，
即约 27 次/分钟。2 万部 × 3 个接口 ≈ 7.4 小时，
分多个 workflow 分片跑（见 ``--shard``）。

用法
----
    python collect.py --shard 0 --shards 20    # 第 1 片（共 20 片）
    python collect.py --limit 100              # 只采 100 部（调试）
    python collect.py --ids-only               # 只枚举 id
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import httpx

UA = (
    "sakura-anime-metadata/0.1 "
    "(https://github.com/guo-chenxin/sakura-anime-metadata)"
)
API = "https://api.bgm.tv"
WEB = "https://bgm.tv"
OUT = Path("data")

# 网页列表项：<li id="item_545465" ...>
ITEM_ID = re.compile(r'<li id="item_(\d+)"')

MAX_PAGE = 999
PER_PAGE = 24

# 秒。实测 27 次/分钟不会触发限流
DELAY = 2.2

log: list[str] = []


def note(m=""):
    log.append(str(m))
    print(m, flush=True)


class Collector:
    def __init__(self, client: httpx.Client) -> None:
        self.c = client
        self.ok = 0
        self.fail = 0
        self.rate_limited = 0

    # ---------- 底层请求（带限流与重试） ----------
    def _get(self, url: str, params: dict | None = None) -> dict | list | None:
        for attempt in range(3):
            try:
                r = self.c.get(url, params=params)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 404:
                    return None
                if r.status_code == 429:
                    self.rate_limited += 1
                    wait = int(r.headers.get("Retry-After", 60))
                    note(f"    限流，等待 {wait}s")
                    time.sleep(wait)
                    continue
                note(f"    HTTP {r.status_code} {url}")
                return None
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    note(f"    失败 {type(exc).__name__}: {url}")
                    return None
                time.sleep(5 * (attempt + 1))
        return None

    # ---------- 1. 枚举 id ----------
    def enumerate_ids(self) -> list[int]:
        """爬网页筛选页拿到全部动画 id。

        ``sort=title`` 保证翻页稳定（默认排序可能因新番插入而漂移）。
        """
        ids: list[int] = []
        seen: set[int] = set()

        for page in range(1, MAX_PAGE + 1):
            try:
                r = self.c.get(
                    f"{WEB}/anime/browser",
                    params={"page": page, "sort": "title"},
                )
                if r.status_code != 200:
                    note(f"  第 {page} 页 HTTP {r.status_code}，跳过")
                    time.sleep(DELAY)
                    continue
                page_ids = [int(x) for x in ITEM_ID.findall(r.text)]
                new = [x for x in page_ids if x not in seen]
                if not page_ids:
                    note(f"  第 {page} 页无条目，可能已到末尾")
                    break
                if not new:
                    note(f"  第 {page} 页全是重复 id，停止（共 {len(ids)}）")
                    break
                for x in new:
                    seen.add(x)
                    ids.append(x)
                if page % 50 == 0:
                    note(f"  已扫 {page} 页，累计 {len(ids)} 个 id")
            except Exception as exc:  # noqa: BLE001
                note(f"  第 {page} 页异常 {type(exc).__name__}")
            time.sleep(DELAY)

        return ids

    # ---------- 2. 采单个 subject ----------
    def fetch_subject(self, sid: int) -> dict | None:
        d = self._get(f"{API}/v0/subjects/{sid}")
        time.sleep(DELAY)
        return d if isinstance(d, dict) else None

    def fetch_episodes(self, sid: int) -> list[dict]:
        """分页拉全部剧集。limit 上限 100。"""
        eps: list[dict] = []
        offset = 0
        while True:
            d = self._get(
                f"{API}/v0/episodes",
                {"subject_id": sid, "type": 0, "limit": 100,
                 "offset": offset},
            )
            time.sleep(DELAY)
            if not isinstance(d, dict):
                break
            data = d.get("data") or []
            eps.extend(data)
            total = d.get("total") or 0
            offset += len(data)
            if not data or offset >= total:
                break
        return eps

    def fetch_relations(self, sid: int) -> list[dict]:
        d = self._get(f"{API}/v0/subjects/{sid}/subjects")
        time.sleep(DELAY)
        return d if isinstance(d, list) else []


def append_jsonl(path: Path, rows: list[dict]) -> None:
    """追加写入 gz 格式的 JSONL。"""
    if not rows:
        return
    with gzip.open(path, "at", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="最多采多少部（0=不限）")
    ap.add_argument("--ids-only", action="store_true",
                    help="只枚举 id，不采详情")
    ap.add_argument("--shard", type=int, default=0,
                    help="当前分片号（0 起）")
    ap.add_argument("--shards", type=int, default=1,
                    help="总分片数")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    suffix = f"_s{args.shard}of{args.shards}" if args.shards > 1 else ""

    ids_file = OUT / "ids.json"
    with httpx.Client(
        timeout=30,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        follow_redirects=True,
    ) as c:
        col = Collector(c)

        # ---------- id 枚举（复用已有结果） ----------
        if ids_file.exists():
            ids = json.loads(ids_file.read_text(encoding="utf-8"))
            note(f"复用已有 id 列表：{len(ids)} 个")
        else:
            note("枚举 id（爬 bgm.tv 网页筛选页）...")
            ids = col.enumerate_ids()
            ids_file.write_text(
                json.dumps(ids), encoding="utf-8"
            )
            note(f"枚举完成：{len(ids)} 个 id")

        if args.ids_only:
            note("--ids-only，结束")
            return 0

        # ---------- 分片 ----------
        if args.shards > 1:
            ids = [
                x for i, x in enumerate(ids)
                if i % args.shards == args.shard
            ]
            note(f"分片 {args.shard}/{args.shards}：{len(ids)} 个 id")

        if args.limit:
            ids = ids[: args.limit]
            note(f"--limit 限制为 {len(ids)} 个")

        # ---------- 采集 ----------
        subj_path = OUT / f"subjects{suffix}.jsonl.gz"
        ep_path = OUT / f"episodes{suffix}.jsonl.gz"

        # 已采过的跳过（断点续传）
        done: set[int] = set()
        if subj_path.exists():
            with gzip.open(subj_path, "rt", encoding="utf-8") as f:
                for line in f:
                    try:
                        done.add(json.loads(line)["id"])
                    except Exception:  # noqa: BLE001
                        pass
            note(f"已采 {len(done)} 部，续传剩余")

        todo = [x for x in ids if x not in done]
        note(f"待采 {len(todo)} 部，预计 "
             f"{len(todo) * DELAY * 3 / 60 / 60:.1f} 小时（每部 3 请求）")
        note("")

        t0 = time.time()
        for i, sid in enumerate(todo, 1):
            try:
                subj = col.fetch_subject(sid)
                if not subj:
                    col.fail += 1
                    continue

                # 只保留动画（type=2），网页列表偶尔混入其他类型
                if subj.get("type") != 2:
                    continue

                eps = col.fetch_episodes(sid)
                rels = col.fetch_relations(sid)

                append_jsonl(subj_path, [subj])
                if eps:
                    append_jsonl(
                        ep_path,
                        [{"subject_id": sid, "episodes": eps}],
                    )
                if rels:
                    append_jsonl(
                        OUT / f"relations{suffix}.jsonl.gz",
                        [{"subject_id": sid, "relations": rels}],
                    )

                col.ok += 1
                if i % 10 == 0 or i == 1:
                    elapsed = time.time() - t0
                    rate = i / elapsed * 60 if elapsed else 0
                    eta = (len(todo) - i) / (rate / 60) if rate else 0
                    note(
                        f"  [{i}/{len(todo)}] id={sid} "
                        f"{subj.get('name_cn') or subj.get('name')!r} "
                        f"eps={len(eps)} rel={len(rels)} | "
                        f"{rate:.1f} 部/分 ETA {eta/60:.1f}h"
                    )
            except Exception:  # noqa: BLE001
                col.fail += 1
                traceback.print_exc()
                note(f"    id={sid} 异常，继续")

    # ---------- manifest ----------
    manifest = {
        "version": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "shard": args.shard,
        "shards": args.shards,
        "ids_total": len(ids),
        "collected_ok": col.ok,
        "collected_fail": col.fail,
        "rate_limited": col.rate_limited,
        "files": sorted(p.name for p in OUT.glob("*.jsonl.gz")),
    }
    (OUT / f"manifest{suffix}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    note()
    note("=" * 60)
    note(f"完成：成功 {col.ok}，失败 {col.fail}，"
         f"限流 {col.rate_limited}")
    note(f"产出目录：{OUT}")
    for p in sorted(OUT.glob("*.gz")):
        note(f"  {p.name:36} {p.stat().st_size / 1024:.1f} KB")
    note("=" * 60)

    (OUT / f"log{suffix}.txt").write_text(
        "\n".join(log), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())