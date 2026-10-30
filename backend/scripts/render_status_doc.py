"""生成/更新《数据同步状态》文档（机器写的四章明细）。

为什么单独一份文档（2026-10-19 用户决定）
----------------------------------------
`docs/数据来源与同步链路核定报告.md` 是**人写**的：需求满足度、差距清单、口径说明，
这些机器写不出来。同步明细是**机器写**的：每天变，四章全是数字与清单。
两类内容放一起会互相覆盖，所以拆成两份：

| 文档 | 作者 | 内容 |
|---|---|---|
| `数据来源与同步链路核定报告.md` | 人 | 需求对照、差距、口径 |
| **`数据同步状态.md`** | **本脚本** | 本地 Garmin / 本地 Tredict / 上传 Tredict / 本次同步 |

整份由本脚本重写（不是局部替换），因此**不要手工编辑**。

四章粒度：概要 + 缺口清单 + 近 7 天明细（用户选定）。异常为空时不列。

用法
----
    python scripts/render_status_doc.py --profile health
    python scripts/render_status_doc.py --profile activity
    python scripts/render_status_doc.py --profile weekly --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

RAW = ROOT / "data" / "raw"
GARMIN_RAW = RAW / "garmin"
TREDICT_RAW = RAW / "tredict"
LOG_DIR = ROOT / "data" / "logs"
DOC = ROOT / "数据同步状态.md"   # 根目录：此文件入库（docs/ 为本地文档，不入库）

RECENT_DAYS = 7


# ============================================================
# 数据采集
# ============================================================
def _count_files(directory: Path, pattern: str = "*.json") -> tuple[int, str, str]:
    files = sorted(directory.glob(pattern))
    if not files:
        return 0, "-", "-"
    return len(files), files[0].stem, files[-1].stem


def garmin_section() -> dict[str, Any]:
    """本地 Garmin 数据：各数据集条数 / 日期范围 / 缺口。"""
    from sqlmodel import select

    from app.db.models import StandardDailyRow
    from app.db.session import session_scope

    # 2026-10-21：纳入新增数据集（体重/生活方式/饮水 + 训练准备度等 12 项）
    datasets = (
        "daily_summary", "sleep", "hrv", "weight", "activities",
        "spo2", "respiration", "heart_rate", "body_battery", "stress",
        "lifestyle_logging", "hydration", "training_readiness", "training_status",
        "max_metrics", "fitness_age", "endurance_score", "hill_score", "rhr_daily",
        "steps_data", "floors", "intensity_minutes", "calories_daily", "all_day_events",
    )
    rows = []
    for ds in datasets:
        n, first, last = _count_files(GARMIN_RAW / ds)
        rows.append({"dataset": ds, "files": n, "first": first, "last": last})

    fit_n = len(list((GARMIN_RAW / "fit").glob("*.fit")))
    jsonl_n = len(list((GARMIN_RAW / "fit_jsonl").glob("*.jsonl.gz")))

    with session_scope() as s:
        days = [
            r.date
            for r in s.exec(
                select(StandardDailyRow).order_by(StandardDailyRow.date)
            ).all()
        ]

    # 缺口：以 daily_summary 覆盖的日期为基准，找中间断档
    summary_days = sorted(
        dt.date.fromisoformat(p.stem) for p in (GARMIN_RAW / "daily_summary").glob("*.json")
    )
    gaps: list[str] = []
    for a, b in zip(summary_days, summary_days[1:], strict=False):
        if (b - a).days > 1:
            gaps.append(f"{a} → {b}（缺 {(b - a).days - 1} 天）")

    return {
        "datasets": rows,
        "fit": fit_n,
        "jsonl": jsonl_n,
        "standard_rows": len(days),
        "standard_first": days[0].isoformat() if days else "-",
        "standard_last": days[-1].isoformat() if days else "-",
        "gaps": gaps,
        "recent": summary_days[-RECENT_DAYS:],
    }


def tredict_local_section() -> dict[str, Any]:
    """本地 Tredict 数据：活动表、自生成数据、缺口。"""
    from sqlmodel import select

    from app.db.models import TredictActivityRow
    from app.db.session import session_scope
    from app.domain import tredict_generated as gen

    with session_scope() as s:
        acts = s.exec(select(TredictActivityRow)).all()
        snapshot = gen.snapshot(s)
        act_rows = [
            {
                "id": r.tredict_id,
                "date": r.local_date,
                "sport": r.sport_type,
                "sub": r.sub_sport_type,
                "matched": r.matched_garmin_id,
                "confidence": r.match_confidence,
            }
            for r in acts
        ]

    unmatched = [r for r in act_rows if not r["matched"]]
    by_conf = Counter(r["confidence"] for r in act_rows)
    raw_n = len(list((TREDICT_RAW / "activities").glob("*.json")))

    return {
        "activities": len(act_rows),
        "raw_details": raw_n,
        "confidence": dict(by_conf),
        "unmatched": unmatched,
        "generated": snapshot,
        "recent": sorted(act_rows, key=lambda r: r["date"], reverse=True)[:RECENT_DAYS],
    }


def upload_section() -> dict[str, Any]:
    """上传到 Tredict 的数据：健康三类 + 活动 + 待传缺口。"""
    from app.adapters.garmin import load_raw_dataset, load_raw_weight_list
    from app.adapters.tredict import (
        build_bodyvalue_records,
        build_hrv_records,
        build_sleep_records,
    )
    from app.config import get_settings

    raw_root = Path(get_settings().raw_data_dir) / "garmin"
    hrv, hrv_null, hrv_notime = build_hrv_records(load_raw_dataset(raw_root, "hrv"))
    sleep, slp_skip, slp_future = build_sleep_records(load_raw_dataset(raw_root, "sleep"))
    bv, meta = build_bodyvalue_records(
        load_raw_dataset(raw_root, "daily_summary"), load_raw_weight_list(raw_root)
    )

    return {
        "hrv": {"records": len(hrv), "skipped_null": hrv_null, "skipped_no_time": hrv_notime,
                "first": hrv[0]["timestamp"][:10] if hrv else "-",
                "last": hrv[-1]["timestamp"][:10] if hrv else "-"},
        "sleep": {"records": len(sleep), "skipped": slp_skip, "skipped_future": slp_future,
                  "first": sleep[0]["endTimestamp"][:10] if sleep else "-",
                  "last": sleep[-1]["endTimestamp"][:10] if sleep else "-"},
        "bodyvalues": {"records": len(bv), "with_weight": meta.get("weightCount", 0),
                       "first": meta.get("earliest") or "-", "last": meta.get("latest") or "-"},
    }


def sync_section(profile: str) -> dict[str, Any]:
    """本次同步：读 daily-last.json + 最新云端校验报告 + 最新健康拉取报告。

    **按链分别取最新**：``--scope activity`` 与 ``--scope health`` 是两次可能分开
    的运行，若只取「最近一个文件」，另一条链的结论就会从文档里消失
    （实测踩过：verify 档跑全量后又单独跑了健康，活动表整块不见了）。
    """
    out: dict[str, Any] = {"profile": profile}

    if LOG_DIR.joinpath("daily-last.json").exists():
        out["last_run"] = json.loads(LOG_DIR.joinpath("daily-last.json").read_text("utf-8"))

    audit = ROOT / "data" / "audit"
    reports = sorted(audit.glob("cloud-verify-*.json"), reverse=True)
    blocks: dict[str, dict[str, Any]] = {}
    latest_file = None
    for path in reports:
        try:
            report = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        latest_file = latest_file or path.name
        for key in ("activity", "health"):
            if report.get(key) and key not in blocks:
                blocks[key] = {
                    "file": path.name,
                    "generated_at": report.get("generated_at"),
                    **report[key],
                }
        if len(blocks) == 2:
            break

    if blocks:
        out["cloud"] = {
            "file": latest_file,
            "generated_at": (blocks.get("activity") or blocks.get("health") or {}).get(
                "generated_at"
            ),
            "diff": sum(int(b.get("diff") or 0) for b in blocks.values()),
            **blocks,
        }

    pulls = sorted(audit.glob("health-pull-*.json"))
    if pulls:
        report = json.loads(pulls[-1].read_text("utf-8"))
        out["health_pull"] = {
            "file": pulls[-1].name,
            "generated_at": report.get("generated_at"),
            "written": report.get("written"),
            "superseded": report.get("superseded"),
            "frozen": report.get("frozen"),
            "drift": len(report.get("drift") or []),
            "errors": len(report.get("errors") or []),
        }
    return out


# ============================================================
# 渲染
# ============================================================
def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def render(garmin: dict, tredict: dict, upload: dict, sync: dict) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L: list[str] = []
    L.append("# 数据同步状态")
    L.append("")
    L.append("> 本文件由 `backend/scripts/render_status_doc.py` 自动生成，**请勿手工编辑**。")
    L.append(f"> 最近更新：{now}（profile = `{sync.get('profile')}`）")
    L.append(">")
    L.append(
        "> 需求满足度、差距清单、口径说明见 "
        "[数据来源与同步链路核定报告.md](数据来源与同步链路核定报告.md)。"
    )
    L.append("")
    L.append("---")
    L.append("")

    # ---------------- 1. 本地 Garmin ----------------
    L.append("## 1. 本地 Garmin 数据")
    L.append("")
    L.append(
        f"- 标准层 `standard_daily`：**{garmin['standard_rows']} 天**"
        f"（{garmin['standard_first']} ~ {garmin['standard_last']}）"
    )
    L.append(f"- 活动 FIT：**{garmin['fit']} 个**；已解析 jsonl：**{garmin['jsonl']} 个**")
    L.append("")
    rows = [
        [f"`{d['dataset']}`", d["files"], d["first"], d["last"]] for d in garmin["datasets"]
    ]
    L.append(_table(["数据集", "文件数", "起", "止"], rows))
    L.append("")
    if garmin["gaps"]:
        L.append("**缺口**")
        L.append("")
        for g in garmin["gaps"]:
            L.append(f"- {g}")
    else:
        L.append("**缺口**：无（`daily_summary` 逐日连续）")
    L.append("")
    L.append("近 7 天：")
    L.append("")
    L.append("- " + "、".join(str(d) for d in garmin["recent"]))
    L.append("")

    # ---------------- 2. 本地 Tredict ----------------
    L.append("## 2. 本地 Tredict 数据")
    L.append("")
    conf = tredict["confidence"]
    L.append(
        f"- `tredict_activity`：**{tredict['activities']} 条**；"
        f"本地详情快照 **{tredict['raw_details']} 份**"
    )
    L.append(
        f"- 匹配：high {conf.get('high', 0)} / medium {conf.get('medium', 0)} / "
        f"ambiguous {conf.get('ambiguous', 0)} / 未匹配 {len(tredict['unmatched'])}"
    )
    L.append("")
    gen = tredict["generated"]
    from app.domain.tredict_generated import PULLED_ENTITIES

    L.append("**Tredict 自生成数据**（`tredict_generated`）")
    L.append("")
    g_rows = [
        [f"`{k}`", gen[k]["keys"], gen[k]["versions"], gen[k]["latest_fetched_at"] or "-"]
        for k in PULLED_ENTITIES
        if k in gen
    ]
    L.append(_table(["类别", "键数", "版本数", "最近拉取"], g_rows))
    L.append("")
    L.append(
        "> `plannedTrainingList` / `equipmentList` 按 2026-10-21 决策**不再拉取**"
        "（端点存在、账号实测为空）；`sleep-list`/`hrv-list` 次元素（Tredict 自算"
        "基线）按 10-17 决策不拉。"
    )
    L.append("")
    if tredict["unmatched"]:
        L.append("**缺口**")
        L.append("")
        for r in tredict["unmatched"][:10]:
            L.append(f"- {r['id']}（{r['date']} {r['sport']}/{r['sub']}）未能匹配 Garmin 活动")
        L.append("")
    L.append("近 7 条：")
    L.append("")
    t_rows = [
        [
            r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
            r["sport"] + "/" + (r["sub"] or "-"),
            r["matched"] or "—",
            r["confidence"],
        ]
        for r in tredict["recent"]
    ]
    L.append(_table(["日期", "类型", "Garmin id", "置信度"], t_rows))
    L.append("")

    # ---------------- 3. 上传 Tredict ----------------
    L.append("## 3. 上传到 Tredict 的数据")
    L.append("")
    h, sl, bv = upload["hrv"], upload["sleep"], upload["bodyvalues"]
    L.append("**健康三类**（差集上传，`/hrv` 与 `/bodyvalues` 为纯追加端点）")
    L.append("")
    L.append(
        _table(
            ["类别", "条数", "起", "止", "跳过"],
            [
                ["HRV", h["records"], h["first"], h["last"],
                 f"空值 {h['skipped_null']} / 无时间 {h['skipped_no_time']}"],
                ["睡眠", sl["records"], sl["first"], sl["last"],
                 f"不完整 {sl['skipped']} / 未来 {sl['skipped_future']}"],
                ["身体", bv["records"], bv["first"], bv["last"],
                 f"含体重 {bv['with_weight']}"],
            ],
        )
    )
    L.append("")
    L.append("**活动**：以 FIT 原始文件上传（`POST /activity/upload`），Tredict 自行解析；"
             "标题/力量 notes 走 `POST /activity/update`。")
    L.append("")
    cloud = sync.get("cloud") or {}
    if cloud.get("activity"):
        a = cloud["activity"]
        L.append(
            f"- 云端校验：A 路仅远端 {len(a['a_local_garmin']['only_remote'])} / "
            f"仅本地 {len(a['a_local_garmin']['only_local'])}；"
            f"B 路仅本地 {len(a['b_local_tredict']['only_local'])} / "
            f"仅云端 {len(a['b_local_tredict']['only_remote'])}；"
            f"C 路字段不符 {a['c_garmin_tredict']['mismatch']}"
        )
        L.append("")
    L.append("**待传缺口**：见下方第 4 章「本次同步」的云端校验结果。")
    L.append("")

    # ---------------- 4. 本次同步 ----------------
    L.append("## 4. 本次同步")
    L.append("")
    last = sync.get("last_run") or {}
    if last:
        L.append(
            f"- 运行档位：`{last.get('profile')}`；"
            f"开始 {last.get('started_at')} → 结束 {last.get('finished_at')}；"
            f"结果 **{'成功' if last.get('ok') else '有失败'}**"
        )
        L.append(f"- 日志：`{last.get('log')}`")
        L.append("")
        s_rows = []
        for st in last.get("steps") or []:
            s_rows.append(
                [
                    f"`{st['key']}`",
                    "OK" if st["ok"] else f"失败（exit {st['exit_code']}）",
                    st["seconds"],
                    st.get("attempts", 1),
                    "是" if st.get("repaired") else "",
                ]
            )
        L.append(_table(["步骤", "结果", "耗时(s)", "尝试", "已修复"], s_rows))
        L.append("")
        L.append(
            "> 表格不含 `md_report`——它就是生成本文档的那一步，"
            "运行时本文件还没写完，故不列入。"
        )
        L.append("")
    else:
        L.append("- 尚无运行记录（`data/logs/daily-last.json` 不存在）")
        L.append("")

    hp = sync.get("health_pull") or {}
    if hp:
        L.append(
            f"- 健康拉取：新写 {hp.get('written')} / 覆盖 {hp.get('superseded')} / "
            f"冻结 {hp.get('frozen')}；云端差异 {hp.get('drift')} 组；"
            f"错误 {hp.get('errors')}（`{hp.get('file')}`）"
        )
        L.append("")

    if cloud:
        L.append(f"**云端三路校验**（差异合计 {cloud.get('diff')}）")
        L.append("")
        if cloud.get("activity"):
            L.append(f"- 活动：`{cloud['activity'].get('file')}`")
        if cloud.get("health"):
            L.append(f"- 健康：`{cloud['health'].get('file')}`")
        L.append("")
        c_rows = []
        act = cloud.get("activity") or {}
        hea = cloud.get("health") or {}
        if act:
            c_rows.append(
                ["活动 A（本地↔Garmin）",
                 len(act["a_local_garmin"]["only_remote"]),
                 len(act["a_local_garmin"]["only_local"]),
                 len(act["a_local_garmin"]["field_mismatch"])]
            )
            c_rows.append(
                ["活动 B（本地↔Tredict id）",
                 len(act["b_local_tredict"]["only_local"]),
                 len(act["b_local_tredict"]["only_remote"]),
                 0]
            )
            c_rows.append(
                ["活动 C（Garmin↔Tredict）", 0, 0, act["c_garmin_tredict"]["mismatch"]]
            )
        if hea:
            c_rows.append(
                ["健康 A（本地↔Garmin）", 0, 0, hea["a_local_garmin"]["count"]]
            )
            b = hea["b_local_tredict"]
            c_rows.append(
                ["健康 B（本地↔Tredict）",
                 b.get("lagging", 0), b.get("extra", 0), b.get("mismatch", 0)]
            )
            c_rows.append(
                ["健康 C（Garmin↔Tredict）", 0, 0, hea["c_garmin_tredict"]["count"]]
            )
        L.append(_table(["校验", "未上传", "本地缺", "值不符"], c_rows))
        L.append("")

        # 差异明细（只列前若干条）
        details: list[str] = []
        for item in ((act.get("a_local_garmin") or {}).get("only_remote") or [])[:5]:
            details.append(f"- Garmin 云端有、本地无的活动：`{item}`")
        for item in ((act.get("b_local_tredict") or {}).get("only_local") or [])[:5]:
            details.append(f"- 本地有、Tredict 云端无的活动：`{item}`")
        for item in (act.get("c_garmin_tredict") or {}).get("field_mismatch") or []:
            fields = "、".join(f["field"] for f in item["fields"])
            details.append(
                f"- 活动 `{item['garmin_id']}` ↔ `{item['tredict_id']}` 字段不符：{fields}"
            )
        for issue in (hea.get("a_local_garmin") or {}).get("problems") or []:
            details.append(
                f"- 健康 `{issue.get('date')}`/`{issue.get('dataset')}` "
                f"`{issue.get('field', '')}` 本地 {issue.get('local')} → 云端 {issue.get('remote')}"
            )
        for problem in (hea.get("c_garmin_tredict") or {}).get("problems") or []:
            details.append(
                f"- Garmin↔Tredict `{problem.get('kind')}` `{problem.get('date')}`："
                f"Garmin {problem.get('garmin')} vs Tredict {problem.get('tredict')}"
            )
        if details:
            L.append("**差异明细**（最多 20 条）")
            L.append("")
            L.extend(details[:20])
            L.append("")
        else:
            L.append("**差异明细**：无")
            L.append("")
    else:
        L.append("- 尚无云端校验报告（`data/audit/cloud-verify-*.json` 不存在）")
        L.append("")

    return "\n".join(L).rstrip() + "\n"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="生成《数据同步状态》文档")
    ap.add_argument(
        "--profile",
        choices=("activity", "health", "verify", "weekly", "all"),
        default="all",
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = ap.parse_args(argv)

    garmin = garmin_section()
    tredict = tredict_local_section()
    upload = upload_section()
    sync = sync_section(args.profile)

    text = render(garmin, tredict, upload, sync)
    if args.dry_run:
        print(text)
        return 0

    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text(text, encoding="utf-8")
    print(f"[doc] → {DOC.relative_to(ROOT)}  {len(text)} 字符")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
