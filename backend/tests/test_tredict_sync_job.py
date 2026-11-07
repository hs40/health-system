"""Tredict 同步调度任务测试（V43）。

Q4 确认的 seam 之一：``build_scheduler()`` 必须注册 23:00 的 tredict_sync job，
且 ``run_tredict_sync_job`` 直调 app 内服务（不再是 V42 的 subprocess 调脚本）。

服务行为（真上传 / 路径 / audit / 告警）见 ``test_tredict_sync_service.py``；
本文件只管「调度注册」与「调度器兜底不崩」。
"""

from __future__ import annotations

from contextlib import contextmanager


# ============================================================
# config
# ============================================================
def test_tredict_sync_cron_default():
    """默认 cron = '0 23 * * *'（每日 23:00）。"""
    from app.config import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.tredict_sync_cron == "0 23 * * *"


# ============================================================
# build_scheduler
# ============================================================
def test_scheduler_has_tredict_sync_job():
    """``build_scheduler()`` 必须包含 id='tredict_sync' 的 job。"""
    from app.orchestrator.scheduler import build_scheduler

    sched = build_scheduler()
    job = sched.get_job("tredict_sync")
    assert job is not None, "build_scheduler() 缺少 tredict_sync job"


def test_tredict_sync_job_cron_is_23():
    """tredict_sync job 的触发器在 23:00 触发。"""
    from apscheduler.triggers.cron import CronTrigger

    from app.orchestrator.scheduler import build_scheduler

    sched = build_scheduler()
    job = sched.get_job("tredict_sync")
    assert job is not None
    trigger = job.trigger
    assert isinstance(trigger, CronTrigger)
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["hour"] == "23"
    assert fields["minute"] == "0"


# ============================================================
# run_tredict_sync_job：直调服务 + 兜底不崩
# ============================================================
def test_job_result_is_service_result(monkeypatch, tmp_path):
    """调度器把服务结果原样返回（不再包一层 steps/exit 码）。"""
    from app.orchestrator import scheduler as sched

    sentinel = {"ok": True, "activities": {"uploaded": ["42"]}}

    @contextmanager
    def _scope():
        yield object()  # 服务被 mock，session 不会被真正使用

    # scheduler 在模块顶层导入服务（ARCHITECTURE_REVIEW A.2.2），
    # 替换点必须是 scheduler 命名空间。
    monkeypatch.setattr(sched, "session_scope", _scope)
    monkeypatch.setattr(sched, "get_settings", lambda: object())
    monkeypatch.setattr(sched, "run_tredict_sync", lambda sess, st, **kw: sentinel)

    assert sched.run_tredict_sync_job() == sentinel


def test_job_exception_never_crashes_scheduler(monkeypatch):
    """服务/DB 抛异常时调度器返回 ok:False，绝不把异常抛穿进程。"""
    from app.orchestrator import scheduler as sched

    @contextmanager
    def _boom():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(sched, "session_scope", _boom)
    monkeypatch.setattr(sched, "get_settings", lambda: object())

    out = sched.run_tredict_sync_job()
    assert out["ok"] is False
    assert out["errors"]
