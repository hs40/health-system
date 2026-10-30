"""P2：FIT / 健康快照 / fit_json 资产迁移（文件级，零解析）。

背景
----
既有 Node 管道（``Garmin_Tredict/_tools/garmin-sync.js``）把 Garmin 活动 FIT 与
健康快照 ZIP 落在 ``F:/WorkBuddy AI/Garmin_Tredict/garmin_data/`` 下，
本项目此前**从未持有这些二进制原件**（只有 API JSON）。

两种数据的关系（E29 + Q32=B 并存）
---------------------------------
- ``.fit`` 是**唯一真源**：解析层（P4）以它为准，用官方 ``garmin-fit-sdk`` 主解析
  + ``fitdecode`` 交叉校验重建 schema。
- ``fit_json`` 是 ``fit-file-parser`` 的**二手产物**，实测丢失大量字段
  （``activity_metrics`` 59→12、``user_metrics`` 38→11、10 类私有消息全丢），
  **不参与解析**；但按 **E29** 决策**完整归档**，作为独立于解析器版本的旁证。

迁移清单（1,088 文件 / ≈227 MB）
--------------------------------
======================================  ====  ==================
源路径                                  数量  说明
======================================  ====  ==================
``raw/fit/*.fit``                        298  活动 FIT 原件（唯一真源）
``raw/health_snapshot_zip/*.zip``        468  健康快照（内含 10,629 个 FIT）
``activity/fit_json/*.json``             298  FIT 解析旁证（E29：全量归档）
``activity/*.json``                        4  活动清单与类型表
``_meta/**``                              20  既有管道元数据
======================================  ====  ==================

安全性
------
严格 append-only（REQ-DATA-001）：

- 目标不存在 → 复制并写入 manifest
- 目标存在且 sha256 相同 → **跳过**（幂等，可反复执行）
- 目标存在但 sha256 不同 → **报错退出**，绝不覆盖

源目录全程只读，不修改、不移动、不删除任何文件。

产出
----
``data/raw/garmin/asset_manifest.json``：790 条，含
``path / sha256 / bytes / mtime / source``，供 P4 解析层与 P7 校验层使用。

用法
----
    python scripts/migrate_fit_assets.py --dry-run   # 只报告，不写盘
    python scripts/migrate_fit_assets.py             # 执行迁移
    python scripts/migrate_fit_assets.py --verify    # 只校验现有资产与 manifest
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = Path(r"F:/WorkBuddy AI/Garmin_Tredict/garmin_data")
TARGET_ROOT = PROJECT_ROOT / "data" / "raw" / "garmin"
MANIFEST_PATH = TARGET_ROOT / "asset_manifest.json"

# (源相对路径, 目标子目录, 匹配模式, 是否递归)
SOURCES: tuple[tuple[str, str, str, bool], ...] = (
    ("raw/fit", "fit", "*.fit", False),
    ("raw/health_snapshot_zip", "health_snapshot", "*.zip", False),
    # E29：fit_json 全量归档（不参与解析，仅作旁证）
    ("activity/fit_json", "fit_json", "*.json", False),
    ("activity", "activity_meta", "*.json", False),
    ("_meta", "_meta", "*", True),
)

CHUNK = 1 << 20


@dataclass(slots=True)
class Entry:
    """一条资产记录。

    ``origin`` 区分来源：
    - ``local``  —— P2 从既有 Node 管道目录迁移而来，``source`` 是相对源路径
    - ``cloud``  —— P3 从 Garmin 云端下载而来，``source`` 是云端 URI，无本地对照文件
    """

    path: str
    sha256: str
    bytes: int
    mtime: str
    source: str
    origin: str = "local"


@dataclass(slots=True)
class Result:
    copied: list[Entry] = field(default_factory=list)
    skipped: list[Entry] = field(default_factory=list)
    conflicts: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts


def sha256_of(path: Path) -> str:
    """流式计算 sha256（大文件不整块读入内存）。"""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def iter_sources() -> list[tuple[Path, Path]]:
    """枚举 (源文件, 目标相对路径)，顺序稳定。"""
    out: list[tuple[Path, Path]] = []
    for rel_src, rel_dst, pattern, recursive in SOURCES:
        base = SOURCE_ROOT / rel_src
        if not base.is_dir():
            raise SystemExit(f"[ERR] 源目录不存在：{base}")
        files = base.rglob(pattern) if recursive else base.glob(pattern)
        for p in sorted(files):
            if not p.is_file():
                continue
            out.append((p, Path(rel_dst) / p.relative_to(base)))
    return out


def copy_atomic(src: Path, dst: Path) -> None:
    """先写 .tmp 再 rename，避免中断留下半截文件。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def migrate(dry_run: bool) -> Result:
    result = Result()
    pairs = iter_sources()
    print(f"源目录：{SOURCE_ROOT}")
    print(f"目标  ：{TARGET_ROOT}")
    print(f"待处理：{len(pairs)} 个文件")
    print()

    for i, (src, rel_dst) in enumerate(pairs, 1):
        dst = TARGET_ROOT / rel_dst
        src_hash = sha256_of(src)
        size = src.stat().st_size
        entry = Entry(
            path=str(rel_dst).replace("\\", "/"),
            sha256=src_hash,
            bytes=size,
            mtime=dt.datetime.fromtimestamp(src.stat().st_mtime).isoformat(timespec="seconds"),
            source=str(src.relative_to(SOURCE_ROOT)).replace("\\", "/"),
        )

        if dst.exists():
            dst_hash = sha256_of(dst)
            if dst_hash == src_hash:
                result.skipped.append(entry)
            else:
                result.conflicts.append((entry.path, src_hash, dst_hash))
                print(f"  [冲突] {entry.path}")
                print(f"         源 {src_hash[:16]}…  目标 {dst_hash[:16]}…")
            continue

        if not dry_run:
            copy_atomic(src, dst)
        result.copied.append(entry)

        if i % 100 == 0 or i == len(pairs):
            verb = "待复制" if dry_run else "已复制"
            print(f"  进度 {i}/{len(pairs)}  {verb} {len(result.copied)}  "
                  f"跳过 {len(result.skipped)}  冲突 {len(result.conflicts)}")

    return result


def load_manifest_index() -> dict[str, dict]:
    """读现有 manifest → {path: entry}；文件缺失或损坏时返回空字典。"""
    if not MANIFEST_PATH.exists():
        return {}
    try:
        old = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("[WARN] 既有 manifest 无法解析，将重建")
        return {}
    return {e["path"]: e for e in old.get("entries", []) if "path" in e}


def save_manifest_index(index: dict[str, dict], *, note: str = "") -> None:
    """把 {path: entry} 按 path 排序后原子写入 manifest。"""
    entries = [index[k] for k in sorted(index)]
    counts = Counter(e["path"].split("/")[0] for e in entries)
    total_bytes = sum(e["bytes"] for e in entries)

    payload = {
        "schema": 1,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source_root": str(SOURCE_ROOT),
        "target_root": str(TARGET_ROOT),
        "policy": "append-only (REQ-DATA-001)：已存在且 sha256 相同则跳过，不同则报错不覆盖",
        "note": note
        or (
            "E29：fit_json 全量归档（不参与解析，仅作旁证）；"
            "Q32=B：解析以 .fit 为唯一真源（官方 SDK 主解析 + fitdecode 交叉校验）"
        ),
        "total_files": len(entries),
        "total_bytes": total_bytes,
        "counts": dict(sorted(counts.items())),
        "entries": entries,
    }

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_name(MANIFEST_PATH.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, MANIFEST_PATH)

    print(f"\nmanifest 已写入：{MANIFEST_PATH}")
    print(f"  文件 {len(entries)} 个，{total_bytes / 1048576:.1f} MB")
    for k, v in sorted(counts.items()):
        print(f"    {k:<16} {v}")


def write_manifest(result: Result) -> None:
    """合并已有 manifest 与本次结果，原子写入。"""
    index = load_manifest_index()
    for e in result.copied + result.skipped:
        index[e.path] = {
            "path": e.path,
            "sha256": e.sha256,
            "bytes": e.bytes,
            "mtime": e.mtime,
            "source": e.source,
            "origin": e.origin,
        }
    save_manifest_index(index)


def verify() -> int:
    """校验磁盘资产与 manifest 是否一致（源与目标双向比对）。"""
    if not MANIFEST_PATH.exists():
        print(f"[ERR] manifest 不存在：{MANIFEST_PATH}")
        return 2

    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = payload.get("entries", [])
    print(f"manifest 记录 {len(entries)} 条，校验中…")

    bad: list[str] = []
    missing = 0
    for e in entries:
        dst = TARGET_ROOT / e["path"]
        if not dst.exists():
            bad.append(f"目标缺失：{e['path']}")
            missing += 1
            continue
        if sha256_of(dst) != e["sha256"]:
            bad.append(f"目标哈希不符：{e['path']}")
        # 云端拉取的条目没有本地对照文件，只校验目标侧
        if e.get("origin", "local") == "cloud":
            continue
        src = SOURCE_ROOT / e["source"]
        if not src.exists():
            bad.append(f"源缺失：{e['source']}")
            continue
        if sha256_of(src) != e["sha256"]:
            bad.append(f"源哈希不符（源被改动？）：{e['source']}")

    # 目标目录里多出来的文件（不在 manifest 中）。
    # 只扫本 manifest 管理的子目录 —— data/raw/garmin/ 下还有 API 数据集
    # （activities/ sleep/ hrv/ …），不归本脚本管，不能判为多余。
    known = {e["path"] for e in entries}
    managed = {rel_dst for _, rel_dst, _, _ in SOURCES}
    extra: list[str] = []
    for sub in sorted(managed):
        base = TARGET_ROOT / sub
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or p.name.endswith(".tmp"):
                continue
            rel = str(p.relative_to(TARGET_ROOT)).replace("\\", "/")
            if rel not in known:
                extra.append(rel)

    if bad:
        print(f"\n[FAIL] {len(bad)} 处不一致：")
        for line in bad[:20]:
            print(f"  {line}")
        if len(bad) > 20:
            print(f"  … 另有 {len(bad) - 20} 处")
    if extra:
        print(f"\n[WARN] 目标目录有 {len(extra)} 个文件不在 manifest 中：")
        for line in extra[:10]:
            print(f"  {line}")

    if not bad and not extra:
        total = sum(e["bytes"] for e in entries)
        print(f"\n[OK] {len(entries)} 个文件全部一致，{total / 1048576:.1f} MB")
        return 0
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="P2 资产迁移（FIT / 健康快照）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写盘")
    ap.add_argument("--verify", action="store_true", help="只校验，不迁移")
    args = ap.parse_args(argv)

    if args.verify:
        return verify()

    result = migrate(dry_run=args.dry_run)
    print()
    verb = "待复制" if args.dry_run else "已复制"
    print(f"{verb} {len(result.copied)}  跳过 {len(result.skipped)}  冲突 {len(result.conflicts)}")

    if not result.ok:
        print("\n[ABORT] 存在哈希冲突，未写入 manifest。请人工确认后再执行。")
        for path, s, d in result.conflicts[:10]:
            print(f"  {path}\n    源 {s}\n    目标 {d}")
        return 1

    if args.dry_run:
        print("\n[dry-run] 未写盘、未写 manifest")
        return 0

    write_manifest(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
