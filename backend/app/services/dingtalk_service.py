"""钉钉日报推送服务（2026-10-26）。

- ``build_daily_markdown``：纯函数，把 ``DailyReport`` + 当日计划名组装成钉钉 markdown；
- ``push_daily_report``：取库里的日报与当日训练会话 → 发送 → 落审计。
  **推送失败绝不抛给主流程**（返回 ``ok=False``），保证日报生成/定时任务不被阻断。
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, select

from app.adapters.dingtalk import DingTalkClient
from app.config import Settings
from app.db.models import DailyReportRow, TrainingSessionRow
from app.errors import AppError
from app.schemas.domain import DailyReport
from app.schemas.enums import AuditCategory, ErrorCode
from app.storage.audit_store import record as audit


def _fmt(value: object, unit: str = "") -> str:
    return f"{value}{unit}" if value is not None else "—"


def build_daily_markdown(
    report: DailyReport, *, plan_name: str | None, base_url: str
) -> tuple[str, str]:
    """组装钉钉 markdown，返回 ``(title, text)``。纯函数，便于单测。"""
    risk = report.risk
    priority = risk.priority.value if risk else "—"
    quality = report.quality.grade.value if report.quality else "—"

    acwr = report.acwr
    if acwr is not None and acwr.ratio is not None:
        acwr_txt = f"{acwr.ratio:.2f}"
        if not acwr.sufficient and acwr.note:
            acwr_txt += "（仅供参考）"
    else:
        acwr_txt = "不可计算"

    recovery = report.recovery or {}
    sleep = report.sleep or {}
    sleep_sec = sleep.get("duration_sec")
    sleep_txt = (
        f"{sleep_sec / 3600:.1f}h"
        if isinstance(sleep_sec, (int, float)) and sleep_sec
        else "—"
    )

    lines = [
        f"📊 **每日训练报告 · {report.date.isoformat()}**",
        f"- 优先级：{priority} · 数据质量：{quality}",
        f"- ACWR：{acwr_txt}",
        (
            f"- 恢复：静息 {_fmt(recovery.get('resting_heart_rate'), ' bpm')}"
            f" · HRV {_fmt(recovery.get('hrv_last_night_avg'), ' ms')}"
            f" · 睡眠 {sleep_txt}"
            f" · 晨起电量 {_fmt(recovery.get('body_battery_morning'))}"
        ),
        f"- 今日安排：{plan_name or '无计划 / 休息'}",
    ]
    if report.limitations:
        lines.append(f"- 提示：{report.limitations[0]}")
    if base_url:
        link = f"{base_url.rstrip('/')}/reports/daily?date={report.date.isoformat()}"
        lines.append(f"[查看完整报告]({link})")

    return f"每日训练报告 · {report.date.isoformat()}", "\n".join(lines)


def push_daily_report(session: Session, day: dt.date, settings: Settings) -> dict:
    """推送指定日期的日报到钉钉；失败不抛（返回 ``ok=False``），并落审计。"""
    client = DingTalkClient(settings)
    if not client.enabled:
        return {"ok": False, "skipped": True, "error": "未配置 DINGTALK_WEBHOOK"}

    row = session.exec(select(DailyReportRow).where(DailyReportRow.date == day)).first()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"日报不存在，无法推送: {day}")
    report = DailyReport(**row.payload)

    ts = session.exec(
        select(TrainingSessionRow).where(
            TrainingSessionRow.id == f"session-{day.isoformat()}"
        )
    ).first()
    plan_name = ts.name if ts is not None else None

    title, text = build_daily_markdown(
        report, plan_name=plan_name, base_url=settings.app_base_url
    )
    error: str | None = None
    response: dict | None = None
    try:
        response = client.send_markdown(title, text)
    except Exception as exc:  # noqa: BLE001 — 推送失败绝不阻断主流程
        error = f"{type(exc).__name__}: {exc}"

    audit(
        session,
        AuditCategory.TASK,
        ref=f"dingtalk:{day.isoformat()}",
        detail={"ok": error is None, "error": error, "plan": plan_name},
        audit_dir=settings.audit_dir,
    )
    return {"ok": error is None, "error": error, "response": response}
