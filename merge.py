"""合并 8 个分片的采集结果。

问题
----
采集时为了避免 7 个 job 并发 git push 冲突，只有 shard 0 会 push，
其余分片的数据只在 artifact 里。

本脚本把 8 个 artifact 下载、解压、合并、去重、验证，产出单一数据集。

验证项
------
1. **季度覆盖**：188 个切片是否全都采到
2. **无重复**：按 subject id 去重后的数量
3. **无遗漏**：分片并集是否等于全年份范围
4. **字段完整**：关键字段覆盖率
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import httpx

UA = (
    "sakura-anime-metadata/0.1 "
    "(https://github.com/guo-chenxin/sakura-anime-metadata)"
)
# artifact 下载需要认证。优先用环境变量里的 token（Actions 自动注入），
# 本地跑时若没配就只能匿名（会 401）。
TOKEN = None
REPO = "guo-chenxin/sakura-anime-metadata"
ART_DIR = Path("artifacts")
DATA = Path("data")
MERGED = DATA / "merged"

log: list[str] = []


def note(m=""):
    log.append(str(m))
    print(m, flush=True)


def api_headers() -> dict[str, str]:
    """构造 GitHub API 请求头，有 token 就带上。"""
    h = {
        "User-Agent": UA,
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def find_artifacts(c: httpx.Client) -> dict[str, int]:
    """列出所有 bangumi-shard-N artifact，返回 {name: id}。"""
    out: dict[str, int] = {}
    r = c.get(
        f"https://api.github.com/repos/{REPO}/actions/artifacts",
        params={"per_page": 100},
        headers=api_headers(),
    )
    if r.status_code != 200:
        note(f"  列出 artifact 失败 HTTP {r.status_code}: "
             f"{r.text[:120]}")
        return out
    for a in r.json().get("artifacts") or []:
        if a["name"].startswith("bangumi-shard-") and not a["expired"]:
            out[a["name"]] = a["id"]
    return out


def download(c: httpx.Client, name: str, aid: int) -> bool:
    """下载并解压 artifact。"""
    dest = ART_DIR / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        r = c.get(
            f"https://api.github.com/repos/{REPO}/actions/artifacts/"
            f"{aid}/zip",
            headers=api_headers(),
            follow_redirects=True,
        )
        if r.status_code != 200:
            note(f"  {name}: 下载失败 HTTP {r.status_code}: "
                 f"{r.text[:100]}")
            return False
        zpath = ART_DIR / f"{name}.zip"
        zpath.write_bytes(r.content)
        with zipfile.ZipFile(zpath) as zf:
            zf.extractall(dest)
        note(f"  {name}: 已下载 {len(r.content) // 1024} KB")
        return True
    except Exception as exc:  # noqa: BLE001
        note(f"  {name}: 异常 {type(exc).__name__}")
        return False


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        continue
    except Exception as exc:  # noqa: BLE001
        note(f"  读取 {path.name} 失败: {type(exc).__name__}")
    return rows


def main() -> None:
    ART_DIR.mkdir(exist_ok=True)
    MERGED.mkdir(parents=True, exist_ok=True)

    with httpx.Client(
        timeout=120, headers={"User-Agent": UA}, follow_redirects=True
    ) as c:
        note("=" * 70)
        note("1. 列出 artifacts")
        note("=" * 70)
        arts = find_artifacts(c)
        note(f"  找到 {len(arts)} 个: {sorted(arts)}")

        note()
        note("=" * 70)
        note("2. 下载")
        note("=" * 70)
        for name, aid in sorted(arts.items()):
            download(c, name, aid)

    # ---------- 3. 合并 ----------
    note()
    note("=" * 70)
    note("3. 合并去重")
    note("=" * 70)

    subj_by_id: dict[int, dict] = {}
    ep_by_sid: dict[int, list[dict]] = {}
    sources: dict[int, str] = {}  # subject_id → 来自哪个分片
    dup_count = 0

    # 先加载 git 里的 shard0（artifact 里也有，但保险起见）
    local = DATA / "subjects_s0of8.jsonl.gz"
    local_ep = DATA / "episodes_s0of8.jsonl.gz"
    files: list[tuple[str, Path, Path]] = []
    if local.exists():
        files.append(("git-s0", local, local_ep))

    # 再加载 artifact 里的每个分片
    for shard_dir in sorted(ART_DIR.glob("bangumi-shard-*")):
        for sub in shard_dir.rglob("subjects_*.jsonl.gz"):
            ep = sub.parent / sub.name.replace("subjects_", "episodes_")
            files.append((shard_dir.name, sub, ep))

    note(f"待合并文件组: {len(files)}")
    for label, sp, ep in files:
        s_rows = load_jsonl(sp)
        e_rows = load_jsonl(ep)
        note(f"  {label:20} subjects={len(s_rows):<6} "
             f"episode_groups={len(e_rows)}")

        for s in s_rows:
            sid = s.get("id")
            if not sid:
                continue
            if sid in subj_by_id:
                dup_count += 1
                continue
            subj_by_id[sid] = s
            sources[sid] = label

        for e in e_rows:
            sid = e.get("subject_id")
            if sid and sid not in ep_by_sid:
                ep_by_sid[sid] = e.get("episodes") or []

    note()
    note(f"  去重后 subjects: {len(subj_by_id)}")
    note(f"  重复丢弃: {dup_count}")
    note(f"  剧集组: {len(ep_by_sid)}")

    # ---------- 4. 验证覆盖 ----------
    note()
    note("=" * 70)
    note("4. 验证季度覆盖")
    note("=" * 70)

    # 按年-季度统计
    q_counter: Counter[str] = Counter()
    no_date = 0
    for s in subj_by_id.values():
        d = s.get("date") or ""
        if len(d) >= 7:
            y = d[:4]
            m = int(d[5:7])
            q = (m - 1) // 3 + 1
            q_counter[f"{y}Q{q}"] += 1
        else:
            no_date += 1

    # 期望的 188 个季度
    expected = [
        f"{y}Q{q}" for y in range(1980, 2027) for q in (1, 2, 3, 4)
    ]
    got = set(q_counter)
    missing_q = [q for q in expected if q not in got]
    note(f"  期望切片 {len(expected)} 个")
    note(f"  实际覆盖 {len(got & set(expected))} 个")
    note(f"  缺失 {len(missing_q)} 个")
    if missing_q:
        note(f"    缺失列表(前20): {missing_q[:20]}")
    note(f"  无日期的条目: {no_date}")

    # 年份分布
    years = Counter((s.get("date") or "?")[:4] for s in subj_by_id.values())
    note()
    note("  年份分布:")
    for y in sorted(years):
        if y == "?":
            continue
        note(f"    {y}: {years[y]:>5}")

    # ---------- 5. 字段完整性 ----------
    note()
    note("=" * 70)
    note("5. 字段完整性")
    note("=" * 70)
    n = len(subj_by_id)

    def cov(field, pred=None):
        cnt = 0
        for s in subj_by_id.values():
            v = s.get(field)
            if pred:
                if pred(v):
                    cnt += 1
            elif v not in (None, "", [], {}):
                cnt += 1
        return f"{cnt}/{n} ({cnt * 100 // max(n, 1)}%)"

    note(f"  name_cn        {cov('name_cn')}")
    note(f"  summary        {cov('summary')}")
    note(f"  images.large   {cov('images', lambda v: v and v.get('large'))}")
    note(f"  rating.score   {cov('rating', lambda v: v and v.get('score'))}")
    note(f"  tags           {cov('tags')}")
    note(f"  infobox        {cov('infobox')}")
    note(f"  eps            {cov('eps', lambda v: v is not None)}")

    te = 0
    has_title = 0
    for sid in subj_by_id:
        for x in ep_by_sid.get(sid, []):
            te += 1
            if (x.get("name_cn") or x.get("name") or "").strip():
                has_title += 1
    note(f"  总集数         {te}")
    note(f"  有集标题       {has_title}/{te} "
         f"({has_title * 100 // max(te, 1)}%)")

    # ---------- 6. 写出合并结果 ----------
    note()
    note("=" * 70)
    note("6. 写出合并文件")
    note("=" * 70)

    out_subj = MERGED / "subjects.jsonl.gz"
    with gzip.open(out_subj, "wt", encoding="utf-8") as f:
        for sid in sorted(subj_by_id):
            f.write(
                json.dumps(subj_by_id[sid], ensure_ascii=False) + "\n"
            )
    note(f"  {out_subj}  {out_subj.stat().st_size // 1024} KB")

    out_ep = MERGED / "episodes.jsonl.gz"
    with gzip.open(out_ep, "wt", encoding="utf-8") as f:
        for sid in sorted(ep_by_sid):
            f.write(
                json.dumps(
                    {"subject_id": sid, "episodes": ep_by_sid[sid]},
                    ensure_ascii=False,
                ) + "\n"
            )
    note(f"  {out_ep}  {out_ep.stat().st_size // 1024} KB")

    # manifest
    manifest = {
        "subjects": len(subj_by_id),
        "episodes": te,
        "episode_groups": len(ep_by_sid),
        "duplicates_dropped": dup_count,
        "quarters_expected": len(expected),
        "quarters_covered": len(got & set(expected)),
        "quarters_missing": missing_q,
        "no_date_entries": no_date,
        "sources": {
            k: v for k, v in
            Counter(sources.values()).most_common()
        },
    }
    (MERGED / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    note(f"  {MERGED / 'manifest.json'}")

    note()
    note("=" * 70)
    note("完成")
    note("=" * 70)
    (MERGED / "merge_log.txt").write_text(
        "\n".join(log), encoding="utf-8"
    )


if __name__ == "__main__":
    sys.exit(main())