"""TA.11 / V2 / V3 关键验证：Garmin 计划上传 / 排期 / 回读 / 更新 / 清理。

**完全可逆**：脚本结束时删除自己创建的课程与排期，不留残留。
若中途失败，会尽力清理已创建资源。

用法：
  python scripts/verify_garmin_workout.py            # 使用「明天」作为排期日
  python scripts/verify_garmin_workout.py --dry-run  # 只打印 payload，不调用 Garmin
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from app.adapters.garmin import GarminAdapter
from app.config import get_settings
from app.domain.prescription import to_garmin_workout
from app.schemas.domain import TrainingSession
from app.schemas.enums import Sport

TEST_NAME = "[HS-TEST] 冒烟课程（可自动删除）"


def build_session(day: dt.date) -> TrainingSession:
    return TrainingSession(
        id="verify-garmin-workout",
        date=day,
        sport=Sport.RUNNING,
        name=TEST_NAME,
        warmup=[{"name": "热身", "duration_sec": 300}],
        main=[{"name": "主项", "duration_sec": 600, "hr_zone": 2}],
        cooldown=[{"name": "放松", "duration_sec": 300}],
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", default=None, help="排期日期 YYYY-MM-DD，默认明天")
    args = ap.parse_args()

    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today() + dt.timedelta(days=1)
    session = build_session(day)
    payload = to_garmin_workout(session)

    print(f"目标排期日: {day} (weekday={day.weekday()})")
    print("课程 payload:")
    import json

    print(json.dumps(payload, ensure_ascii=False, indent=2)[:900])

    if args.dry_run:
        print("\n[dry-run] 不调用 Garmin，退出")
        return 0

    settings = get_settings()
    adapter = GarminAdapter(settings)

    created_workout_id: int | None = None
    created_scheduled_id: int | None = None
    rc = 1
    try:
        # 1) 上传
        created_workout_id = adapter.upload_workout(payload)
        print(f"\n[1/5] 上传 OK → workoutId={created_workout_id}")

        # 2) 排期
        created_scheduled_id = adapter.schedule_workout(created_workout_id, day)
        print(f"[2/5] 排期 OK → scheduledId={created_scheduled_id}")

        # 3) 回读
        items = adapter.get_scheduled_workouts(day.year, day.month)
        target = day.isoformat()
        hit = [
            it
            for it in items
            if it.get("itemType") == "workout"
            and str(it.get("date") or it.get("calendarDate") or "").startswith(target)
        ]
        if not hit:
            print(f"[3/5] ❌ 回读未找到 {target} 的排期（V3 存疑）")
            raise SystemExit(1)
        print(f"[3/5] 回读 OK → 该日排期 {len(hit)} 条，首条={hit[0].get('workoutId')}")

        # 4) 更新（PUT 保留 id）
        updated = adapter.update_workout(
            created_workout_id, {**payload, "workoutName": TEST_NAME + " · 已更新"}
        )
        if updated != created_workout_id:
            print(f"[4/5] ❌ 更新后 workoutId 变化：{created_workout_id} → {updated}")
            raise SystemExit(1)
        print(f"[4/5] 更新 OK → workoutId 保持不变（{updated}），已排期不失效")

        print("\n=== TA.11 验证通过：上传 / 排期 / 回读 / 更新 全部可用 ===")
        rc = 0
    finally:
        # 5) 清理（尽力而为，可逆）
        # 安全约束：**取消排期失败时绝不删除课程** —— Garmin 不级联，先删课程会留下
        # 「孤儿排期」（App 仍显示「已安排训练」，但任何端点都删不掉）。
        unschedule_failed = False
        try:
            if created_scheduled_id:
                adapter.unschedule_workout(created_scheduled_id)
                print(f"[5/5] 已取消排期 {created_scheduled_id}")
        except Exception as e:  # noqa: BLE001
            unschedule_failed = True
            print(f"[5/5] 取消排期失败：{e}")
        if unschedule_failed:
            print(
                f"[5/5] ⚠️ 排期未取消 → **跳过删除课程 {created_workout_id}**，"
                "避免留下不可删除的孤儿排期（请先手动取消排期再重跑）"
            )
        else:
            try:
                if created_workout_id:
                    adapter.delete_workout(created_workout_id)
                    print(f"[5/5] 已删除课程 {created_workout_id}")
            except Exception as e:  # noqa: BLE001
                print(f"[5/5] 删除课程失败：{e}")

    return rc


if __name__ == "__main__":
    sys.exit(main())
