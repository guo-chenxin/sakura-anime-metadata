"""Bangumi 元数据采集。

探测结论（决定了本脚本的结构）
------------------------------
1. ``GET /anime/browser``（网页筛选页）
   - ``sort=title`` 只能翻 10 页
   - ``sort=rank`` 只能翻 435 页（≈1.04 万部），再大**折叠回第 1 页**
   - ``?year=`` 参数无效
   → 网页路线拿不到全量，弃用。

2. ``POST /v0/search/subjects``（API 搜索）
   - ``air_date`` 过滤**真的生效**（2024 首条是 2024-01-13）
   - 每页硬上限 **20 条**（limit 传 50/100 也只给 20）
   - ``total`` 被钳制在 **1000**，超过就不准了
   - ``offset`` 无额外限制，``offset < total`` 就能取到
   → **按季度切片**：每季 250~300 部，远低于 1000，可完整翻完。

3. 搜索结果的字段与详情接口几乎一致
   （id / name / name_cn / summary / images / rating / tags / infobox /
     eps / date / platform / collection / total_episodes / volumes ...）
   → **搜索结果直接入库**，省掉 ``GET /v0/subjects/{id}`` 的请求。

采集流程
--------
1. **枚举**：1980~2026 逐季度调用搜索接口，翻页拿全部 subject
   （每季度数据量小，不受 total 钳制影响）
2. **剧集**：``GET /v0/episodes?subject_id=&limit=100`` 分页拉完
3. **关联**（可选 ``--with-relations``）：
   ``GET /v0/subjects/{id}/subjects``

产出
----
``data/subjects.jsonl.gz``    每行一个 subject（含搜索结果全部字段）
``data/episodes.jsonl.gz``    每行 ``{subject_id, episodes: [...]}``
``data/manifest.json``        版本、数量、进度

分片
----
``--shard N --shards M``：按 subject 轮转分配，多个 job 并行。

用法
----
    python collect.py --limit 100               # 调试：只采 100 部
    python collect.py --ids-only                # 只枚举，不采详情
    python collect.py --shard 0 --shards 8      # 第 1 片（共 8 片）
"""

from __future__ import annotations

import argparse
import gzip
import json
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
OUT = Path("data")

# 搜索接口每页硬上限 20（传更大也只给 20）
PAGE_SIZE = 20
# 每季度分片：季度比年细，保证 total < 1000
YEAR_FROM = 1980
YEAR_TO = 2026

# 秒。实测约 27 次/分钟不会触发限流
DELAY = 2.2

log: list[str] = []


def note(m=""):
    log.append(str(m))
    print(m, flush=True)


class Collector:
    def __init__(self, client: httpx.Client) -> None:
        self.c = client
        self.calls = 0
        self.rate_limited = 0

    def _pace(self) -> None:
        time.sleep(DELAY)

    def search_page(
        self, flt: dict, offset: int
    ) -> tuple[int, list[dict]]:
        """调搜索接口。返回 (total, data)。"""
        for attempt in range(3):
            try:
                r = self.c.post(
                    f"{API}/v0/search/subjects",
                    params={"limit": PAGE_SIZE, "offset": offset},
                    json={"keyword": "", "filter": flt},
                )
                self.calls += 1
                if r.status_code == 200:
                    d = r.json()
                    total = d.get("total") or 0
                    data = d.get("data") or []
                    self._pace()
                    return total, data
                if r.status_code == 429:
                    self.rate_limited += 1
                    wait = int(r.headers.get("Retry-After", 60))
                    note(f"    限流，等 {wait}s")
                    time.sleep(wait)
                    continue
                note(f"    HTTP {r.status_code} offset={offset}")
                self._pace()
                return 0, []
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    note(f"    失败 {type(exc).__name__} offset={offset}")
                    self._pace()
                    return 0, []
                time.sleep(5 * (attempt + 1))
        return 0, []

    def fetch_episodes(self, sid: int) -> list[dict]:
        """拉全部剧集（limit 100 分页）。"""
        eps: list[dict] = []
        offset = 0
        while True:
            for attempt in range(3):
                try:
                    r = self.c.get(
                        f"{API}/v0/episodes",
                        params={
                            "subject_id": sid,
                            "type": 0,
                            "limit": 100,
                            "offset": offset,
                        },
                    )
                    self.calls += 1
                    if r.status_code == 200:
                        d = r.json()
                        data = d.get("data") or []
                        eps.extend(data)
                        total = d.get("total") or 0
                        offset += len(data)
                        self._pace()
                        if not data or offset >= total:
                            return eps
                        break
                    if r.status_code == 429:
                        self.rate_limited += 1
                        wait = int(r.headers.get("Retry-After", 60))
                        note(f"    剧集限流，等 {wait}s")
                        time.sleep(wait)
                        continue
                    self._pace()
                    return eps
                except Exception:  # noqa: BLE001
                    if attempt == 2:
                        self._pace()
                        return eps
                    time.sleep(5 * (attempt + 1))
        return eps

    def fetch_relations(self, sid: int) -> list[dict]:
        try:
            r = self.c.get(f"{API}/v0/subjects/{sid}/subjects")
            self.calls += 1
            self._pace()
            if r.status_code == 200:
                d = r.json()
                return d if isinstance(d, list) else []
        except Exception:  # noqa: BLE001
            self._pace()
        return []


def quarters(year_from: int, year_to: int) -> list[tuple[str, dict]]:
    """生成按季度的过滤条件。

    为什么按季度而不是按年：``total`` 被钳制在 1000，2022~2025 各年都超过
    1000 部，按年会漏数据。实测每季 250~300 部，远低于上限。
    """
    out: list[tuple[str, dict]] = []
    for y in range(year_from, year_to + 1):
        for q, (mm_from, mm_to) in enumerate(
            ((1, 4), (4, 7), (7, 10), (10, 13)), start=1
        ):
            start = f">={y}-{mm_from:02d}-01"
            if mm_to == 13:
                end = f"<{y + 1}-01-01"
            else:
                end = f"<{y}-{mm_to:02d}-01"
            out.append(
                (f"{y}Q{q}", {"type": [2], "air_date": [start, end]})
            )
    return out


def append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with gzip.open(path, "at", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_done_ids(path: Path) -> set[int]:
    """读已采 subject id（断点续传）。"""
    done: set[int] = set()
    if not path.exists():
        return done
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["id"])
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        pass
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="最多采多少部（0=不限）")
    ap.add_argument("--ids-only", action="store_true",
                    help="只枚举 id，不采剧集")
    ap.add_argument("--with-relations", action="store_true",
                    help="同时采关联作品（额外 1 请求/部）")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--year-from", type=int, default=YEAR_FROM)
    ap.add_argument("--year-to", type=int, default=YEAR_TO)
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    suffix = f"_s{args.shard}of{args.shards}" if args.shards > 1 else ""

    subj_path = OUT / f"subjects{suffix}.jsonl.gz"
    ep_path = OUT / f"episodes{suffix}.jsonl.gz"
    rel_path = OUT / f"relations{suffix}.jsonl.gz"

    started = datetime.now(timezone.utc)
    ok = 0
    fail = 0
    total_eps = 0
    total_rel = 0

    with httpx.Client(
        timeout=30,
        headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        follow_redirects=True,
    ) as c:
        col = Collector(c)

        done = load_done_ids(subj_path)
        if done:
            note(f"断点续传：已采 {len(done)} 部")

        # ---------- 枚举 + 采集 ----------
        qs = quarters(args.year_from, args.year_to)
        if args.shards > 1:
            qs = [
                x for i, x in enumerate(qs)
                if i % args.shards == args.shard
            ]
            note(f"分片 {args.shard}/{args.shards}：{len(qs)} 个季度")

        note(f"季度切片：{len(qs)} 个")
        note("")

        seen: set[int] = set(done)
        collected = 0

        for qi, (label, flt) in enumerate(qs, 1):
            offset = 0
            quarter_total = 0
            while True:
                total, data = col.search_page(flt, offset)
                if offset == 0:
                    quarter_total = total
                if not data:
                    break
                offset += len(data)

                # 过滤：只留动画、去重、跳过已采
                batch = []
                for s in data:
                    sid = s.get("id")
                    if not sid or sid in seen:
                        continue
                    if s.get("type") != 2:
                        continue
                    seen.add(sid)
                    batch.append(s)

                if batch:
                    append_jsonl(subj_path, batch)
                    ok += len(batch)
                    collected += len(batch)

                    if not args.ids_only:
                        for s in batch:
                            sid = s["id"]
                            try:
                                eps = col.fetch_episodes(sid)
                                if eps:
                                    append_jsonl(
                                        ep_path,
                                        [{"subject_id": sid,
                                          "episodes": eps}],
                                    )
                                    total_eps += len(eps)
                                if args.with_relations:
                                    rels = col.fetch_relations(sid)
                                    if rels:
                                        append_jsonl(
                                            rel_path,
                                            [{"subject_id": sid,
                                              "relations": rels}],
                                        )
                                        total_rel += len(rels)
                            except Exception:  # noqa: BLE001
                                fail += 1
                                traceback.print_exc()

                if offset >= total or offset >= 1000:
                    break

            el = (datetime.now(timezone.utc) - started).total_seconds()
            rate = collected / el * 60 if el > 0 else 0
            note(
                f"  [{qi}/{len(qs)}] {label} total={quarter_total} "
                f"累计 {collected} 部 | {rate:.1f} 部/分 "
                f"| calls={col.calls}"
            )

            if args.limit and collected >= args.limit:
                note(f"  已达 --limit {args.limit}，停止")
                break

    # ---------- manifest ----------
    manifest = {
        "version": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started.isoformat(),
        "shard": args.shard,
        "shards": args.shards,
        "year_range": [args.year_from, args.year_to],
        "subjects": ok,
        "episodes": total_eps,
        "relations": total_rel,
        "fail": fail,
        "api_calls": col.calls,
        "rate_limited": col.rate_limited,
        "files": sorted(p.name for p in OUT.glob("*.jsonl.gz")),
    }
    (OUT / f"manifest{suffix}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    note()
    note("=" * 64)
    note(f"完成：subject {ok} 部，剧集 {total_eps} 条，"
         f"关联 {total_rel} 条，失败 {fail}")
    note(f"API 调用 {col.calls} 次，限流 {col.rate_limited} 次")
    for p in sorted(OUT.glob("*.gz")):
        note(f"  {p.name:32} {p.stat().st_size / 1024:.1f} KB")
    note("=" * 64)

    (OUT / f"log{suffix}.txt").write_text(
        "\n".join(log), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())