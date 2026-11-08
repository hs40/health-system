"""力量 notes 历史回填（v3.4.6 B 层）：离线受控清单 + 白名单回填。

Seam: ``backend/scripts/strength_notes_backfill.py`` 的公开接口。
不联网、不写云端：用构造的 pairs 与 fake tredict 断言「三重判据」与
「显式 tredict_id 白名单」行为，判定逻辑永远不进生产常驻代码。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.parsers.fit import ParsedFit
from app.services.fit_backfill import write_parsed_fit

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _tool():
    path = SCRIPTS / "strength_notes_backfill.py"
    spec = importlib.util.spec_from_file_location("strength_notes_backfill", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_set_jsonl(root: Path, aid: str, reps_per_set: list[int]) -> None:
    """写一份带 ``set`` 逐组消息的 gz，供 ``load_sets`` 读到 FIT 分布。"""
    rows = [
        {
            "set_type": "active",
            "category": ["plank", "plank"],
            "category_subtype": [0, 0],
            "repetitions": r,
            "duration": 0,
            "weight": 0,
            "start_time": None,
        }
        for r in reps_per_set
    ]
    write_parsed_fit(
        root,
        ParsedFit(
            activity_id=aid,
            file_sha256="fake",
            file_bytes=8,
            messages={"set": rows},
        ),
    )


SETS = [
    {
        "category": "PLANK",
        "subCategory": "WIDE_STANCE_PLANK",
        "reps": 60,
        "sets": 3,
        "duration": 0,
        "maxWeight": 0,
    }
]


def _pair(tid: str, gid: str, date: str, have: str) -> dict:
    return {
        "tredict_id": tid,
        "garmin_id": gid,
        "date": date,
        "have": have,
        "summarized_sets": SETS,
    }


class _FakeTredict:
    """记录 update 调用；绝不联网。"""

    def __init__(self, *, fail_ids: set[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fail_ids = fail_ids or set()

    def update_activity(self, tid: str, patch: dict) -> dict:
        self.calls.append((tid, dict(patch)))
        if tid in self.fail_ids:
            return {"ok": False, "status": 500, "text": "boom"}
        return {"ok": True, "status": 200, "text": ""}


def test_plan_candidates_only_picks_system_generated_wrong_notes(tmp_path: Path) -> None:
    """三重判据：时间窗 + 本系统生成 + 与 FIT 重算的 want 有差异；且必须有 FIT。"""
    tool = _tool()
    root = tmp_path / "garmin"
    for aid in ("A1", "A4", "A5", "A7"):
        _write_set_jsonl(root, aid, [20, 20, 20])
    # A6 故意不写 gz：无 FIT 逐组明细 → 无法重算正确 want → 不进清单

    wrong = "力量明细：宽距平板鸟狗式 60次 × 3组"
    want_ok = tool.want_notes(root, "A4", SETS)  # 与当前生成器一致 = 已正确
    pairs = [
        _pair("T1", "A1", "2026-10-27", wrong),          # ✓ 唯一应进清单
        _pair("T2", "A2", "2026-10-20", "3组×60次"),      # 旧 Node，非本系统生成
        _pair("T3", "A3", "2026-10-28", "我手写的训练感受"),  # 用户手写
        _pair("T4", "A4", "2026-10-28", want_ok),          # 已正确（want == have）
        _pair("T5", "A5", "2026-10-28", ""),               # 空 notes → 走只补空路径
        _pair("T6", "A6", "2026-10-28", wrong),            # 无 FIT，无法重算
        _pair("T7", "A7", "2026-10-20", wrong),            # 时间窗外（< 10-26）
    ]

    out = tool.plan_candidates(pairs, root)

    assert [c["tredict_id"] for c in out] == ["T1"]
    assert "60次 × 3组" in out[0]["have"]
    assert "20次 × 3组" in out[0]["want"]
    assert "缺逐组明细" not in out[0]["want"]


def test_apply_backfill_only_updates_whitelisted_ids() -> None:
    """只覆盖显式 ids；清单外/已一致/幂等不动，单条失败隔离。"""
    tool = _tool()
    candidates = [
        {"tredict_id": "T1", "garmin_id": "A1", "date": "2026-10-27",
         "have": "力量明细：甲 60次 × 3组", "want": "力量明细：甲 20次 × 3组"},
        {"tredict_id": "T2", "garmin_id": "A2", "date": "2026-10-28",
         "have": "力量明细：乙 60次 × 6组", "want": "力量明细：乙 10次 × 6组"},
        {"tredict_id": "T3", "garmin_id": "A3", "date": "2026-10-28",
         "have": "力量明细：丙 23次 × 3组", "want": "力量明细：丙 23次 × 3组"},
        {"tredict_id": "T4", "garmin_id": "A4", "date": "2026-10-28",
         "have": "力量明细：丁 70次 × 6组", "want": "力量明细：丁 12次 × 6组"},
    ]
    tredict = _FakeTredict(fail_ids={"T4"})

    out = tool.apply_backfill(
        tredict, candidates, ids=["T1", "T3", "T4", "T-MISSING"]
    )

    # 只有 T1 真写、T4 尝试但失败；T2 不在白名单、T3 已一致、T-MISSING 不在清单
    assert [tid for tid, _ in tredict.calls] == ["T1", "T4"]
    assert tredict.calls[0][1] == {"notes": "力量明细：甲 20次 × 3组"}
    assert out["updated"] == ["T1"]
    assert any(f["tredict_id"] == "T4" for f in out["failed"])
    assert any(s["tredict_id"] == "T3" and "幂等" in s["reason"] for s in out["skipped"])
    assert any(s["tredict_id"] == "T-MISSING" for s in out["skipped"])
