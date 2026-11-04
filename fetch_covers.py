"""批量下载 Bangumi 封面图。

为什么要单独跑在 Actions
-----------------------
``lain.bgm.tv`` 在本地网络完全不可达（ConnectTimeout，实测原图、
r/200、r/400、r/800 全失败，连 wsrv.nl / ghfast 这类图片代理也连不上）。
GitHub Actions 的 runner 在海外，可直连。

产出
----
``covers/{bgm_id}.jpg``  —— 按 Bangumi id 命名，便于本地写入 MinIO 时
                            直接映射成 ``covers/bgm-{id}.jpg``
``covers/_manifest.json``  —— 成功/失败清单，便于重试

尺寸选择
--------
默认拉 ``common``（400px，约 40 KB/张），21421 张约 860 MB。
卡片展示够用；详情页需要大图时再单独拉 ``large``。

用法
----
    python fetch_covers.py                # 全量
    python fetch_covers.py --limit 100    # 调试
    python fetch_covers.py --size large   # 换尺寸
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path

import httpx

UA = (
    "sakura-anime-metadata/0.1 "
    "(https://github.com/guo-chenxin/sakura-anime-metadata)"
)
DATA = Path("data/merged")
OUT = Path("covers")

# 每张图之间的间隔（秒）。图片 CDN 比 API 宽松，0.15s ≈ 6.7 张/秒
DELAY = 0.15

log: list[str] = []


def note(m=""):
    log.append(str(m))
    print(m, flush=True)


def load_subjects() -> list[dict]:
    path = DATA / "subjects.jsonl.gz"
    if not path.exists():
        return []
    rows: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="最多下载多少张")
    ap.add_argument(
        "--size", default="common",
        choices=["large", "common", "medium", "small", "grid"],
        help="图片尺寸",
    )
    ap.add_argument("--retry-failed", action="store_true",
                    help="只重试上次失败的")
    ap.add_argument("--shard", type=int, default=0,
                    help="当前分片号（0 起）")
    ap.add_argument("--shards", type=int, default=1,
                    help="总分片数")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "_manifest.json"

    # 已有的跳过（断点续传）
    done: set[int] = set()
    if manifest_path.exists() and args.retry_failed:
        prev = json.loads(manifest_path.read_text(encoding="utf-8"))
        failed_ids = [x["id"] for x in prev.get("failed", [])]
        targets_override = set(failed_ids)
    else:
        targets_override = None
        for p in OUT.glob("*.jpg"):
            try:
                done.add(int(p.stem))
            except ValueError:
                continue

    subjects = load_subjects()
    note(f"subjects: {len(subjects)} 部，已下载 {len(done)} 张")

    # 收集待下载
    todo: list[tuple[int, str]] = []
    no_image = 0
    for s in subjects:
        sid = s.get("id")
        if not sid:
            continue
        images = s.get("images") or {}
        url = images.get(args.size)
        if not url:
            # 回退到其它尺寸
            for alt in ("large", "common", "medium", "small", "grid"):
                if images.get(alt):
                    url = images[alt]
                    break
        if not url:
            no_image += 1
            continue
        if targets_override is not None:
            if sid not in targets_override:
                continue
        elif sid in done:
            continue
        todo.append((sid, url))

    if args.limit:
        todo = todo[: args.limit]

    # 分片：按索引轮转，保证各片互不重叠
    if args.shards > 1:
        todo = [
            x for i, x in enumerate(todo)
            if i % args.shards == args.shard
        ]
        note(f"分片 {args.shard}/{args.shards}")

    note(f"待下载 {len(todo)} 张，无图 {no_image} 部")
    if not todo:
        note("没有需要下载的")
        return

    eta = len(todo) * DELAY / 60
    note(f"预计 {eta:.0f} 分钟（{DELAY}s/张）")
    note("")

    ok = 0
    failed: list[dict] = []
    t0 = time.time()

    with httpx.Client(
        timeout=30,
        headers={"User-Agent": UA, "Referer": "https://bgm.tv/"},
        follow_redirects=True,
    ) as c:
        for i, (sid, url) in enumerate(todo, 1):
            dest = OUT / f"{sid}.jpg"
            if dest.exists() and dest.stat().st_size > 500:
                ok += 1
                continue
            # 三段重试
            for attempt in range(3):
                try:
                    r = c.get(url)
                    if r.status_code == 200 and len(r.content) > 500:
                        dest.write_bytes(r.content)
                        ok += 1
                        break
                    if r.status_code == 404:
                        failed.append(
                            {"id": sid, "url": url, "reason": "404"}
                        )
                        break
                    if attempt == 2:
                        failed.append(
                            {"id": sid, "url": url,
                             "reason": f"HTTP {r.status_code}"}
                        )
                except Exception as exc:  # noqa: BLE001
                    if attempt == 2:
                        failed.append(
                            {"id": sid, "url": url,
                             "reason": type(exc).__name__}
                        )
                    else:
                        time.sleep(1 + attempt)
            time.sleep(DELAY)

            if i % 200 == 0:
                el = time.time() - t0
                rate = i / el if el else 0
                remain = (len(todo) - i) / rate / 60 if rate else 0
                size_mb = sum(
                    p.stat().st_size for p in OUT.glob("*.jpg")
                ) / 1024 / 1024
                note(
                    f"  [{i}/{len(todo)}] ok={ok} fail={len(failed)} "
                    f"| {rate:.1f} 张/秒 | 剩余 {remain:.0f} 分钟 "
                    f"| 已存 {size_mb:.0f} MB"
                )

    # 清掉过小的坏文件
    bad = 0
    for p in OUT.glob("*.jpg"):
        if p.stat().st_size < 500:
            p.unlink()
            bad += 1

    total_size = sum(p.stat().st_size for p in OUT.glob("*.jpg"))
    manifest = {
        "size_variant": args.size,
        "downloaded": ok,
        "failed_count": len(failed),
        "failed": failed[:500],
        "total_files": len(list(OUT.glob("*.jpg"))),
        "total_mb": round(total_size / 1024 / 1024, 1),
        "bad_removed": bad,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    note("")
    note("=" * 60)
    note(f"完成: 成功 {ok}，失败 {len(failed)}，"
         f"共 {manifest['total_files']} 文件 "
         f"{manifest['total_mb']} MB")
    note(f"耗时 {(time.time() - t0) / 60:.1f} 分钟")
    note("=" * 60)

    (OUT / "_log.txt").write_text("\n".join(log), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())