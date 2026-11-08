"""逐秒心率补齐调度任务测试（v3.4.5）。

Seam: ``build_scheduler()`` 必须注册 23:30 的 ``fit_backfill`` job
（避开 23:00 ``tredict_sync``）；``run_fit_backfill_job`` 直调 app 内服务、
绝不把异常抛穿调度器进程。风格对齐 ``test_tredict_sync_job.py``（V43）。
"""

from __future__ import annotations


def test_fit_backfill_cron_default() -> None:
    """默认 cron = '30 23 * * *'（每日 23:30，给 23:00 tredict_sync 留余量）。"""
    from app.config import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.fit_backfill_cron == "30 23 * * *"


def test_scheduler_has_fit_backfill_job() -> None:
    """build_scheduler() 必须包含 id='fit_backfill' 的 job。"""
    from app.orchestrator.scheduler import build_scheduler

    sched = build_scheduler()
    assert sched.get_job("fit_backfill") is not None, (
        "build_scheduler() 缺少 fit_backfill job —— 容器不会自动补齐逐秒心率"
    )


def test_fit_backfill_job_runs_at_2330() -> None:
    """触发器在 23:30，不与 23:00 tredict_sync 抢 Garmin 会话。"""
    from apscheduler.triggers.cron import CronTrigger

    from app.orchestrator.scheduler import build_scheduler

    job = build_scheduler().get_job("fit_backfill")
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "23"
    assert fields["minute"] == "30"


def test_job_returns_service_result(monkeypatch) -> None:
    """调度器把服务结果原样返回，不再包一层 steps/exit 码。"""
    from app.orchestrator import scheduler as sched

    sentinel = {"ok": True, "filled": ["655296550"]}
    monkeypatch.setattr(sched, "get_settings", lambda: object())
    monkeypatch.setattr(sched, "run_backfill", lambda *a, **kw: sentinel)

    assert sched.run_fit_backfill_job() == sentinel


def test_job_exception_never_crashes_scheduler(monkeypatch) -> None:
    """服务抛异常时返回 ok:False，绝不把异常抛穿进程（对齐 V43 兜底）。"""
    from app.orchestrator import scheduler as sched

    def _boom(*a: object, **kw: object) -> None:
        raise RuntimeError("garmin down")

    monkeypatch.setattr(sched, "get_settings", lambda: object())
    monkeypatch.setattr(sched, "run_backfill", _boom)

    out = sched.run_fit_backfill_job()
    assert out["ok"] is False
    assert out["errors"]
