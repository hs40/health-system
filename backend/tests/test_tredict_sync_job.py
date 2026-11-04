"""Tredict 同步 + 云端校验调度任务测试（V42）。

背景：
    NAS 容器 ``build_scheduler()`` 只有 daily(07:30)/sleep_retry/weekly/monthly，
    **缺失活动上传到 Tredict 的调度任务**——活动上传只存在于独立脚本
    ``daily_sync.py --profile activity``，此前从本地开发机手动运行，10-26 后停止。
    同时 ``verify_cloud.py`` 也未进容器调度器，Garmin↔Tredict 差异无人检测。

本测试覆盖：
- config 新增 ``tredict_sync_cron``（默认 ``0 23 * * *``）；
- ``build_scheduler()`` 包含 ``id='tredict_sync'`` 的 job；
- ``run_tredict_sync_job()`` 调用 ``daily_sync.py --profile all``（活动+健康+校验）；
- 子进程失败 / 异常时不崩溃调度器。
"""

from __future__ import annotations

import subprocess


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
    from app.orchestrator.scheduler import build_scheduler

    sched = build_scheduler()
    job = sched.get_job("tredict_sync")
    assert job is not None
    trigger = job.trigger
    # CronTrigger.fields 是一个列表，按 crontab 字段顺序：分 时 日 月 周
    from apscheduler.triggers.cron import CronTrigger

    assert isinstance(trigger, CronTrigger)
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["hour"] == "23"
    assert fields["minute"] == "0"


# ============================================================
# run_tredict_sync_job
# ============================================================
class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_tredict_sync_job_calls_daily_sync_all(monkeypatch):
    """``run_tredict_sync_job`` 调用 ``daily_sync.py --profile all``。"""
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc(0, "[daily] 全部完成", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    from app.orchestrator.scheduler import run_tredict_sync_job

    result = run_tredict_sync_job()

    cmd = captured["cmd"]
    assert any("daily_sync.py" in c for c in cmd), f"cmd 缺少 daily_sync.py: {cmd}"
    assert "--profile" in cmd
    idx = cmd.index("--profile")
    assert cmd[idx + 1] == "all", f"--profile 值不是 all: {cmd}"
    assert result["steps"]["tredict_sync"] == "ok"


def test_tredict_sync_job_nonzero_exit(monkeypatch):
    """子进程 exit!=0 时返回 exit:N，不抛异常。"""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeProc(1, "fail", "boom"))

    from app.orchestrator.scheduler import run_tredict_sync_job

    result = run_tredict_sync_job()
    assert "exit:1" in result["steps"]["tredict_sync"]


def test_tredict_sync_job_handles_exception(monkeypatch):
    """子进程启动异常时不抛出，返回 failed 状态。"""

    def _raise(*a, **kw):
        raise OSError("spawn failed")

    monkeypatch.setattr(subprocess, "run", _raise)

    from app.orchestrator.scheduler import run_tredict_sync_job

    result = run_tredict_sync_job()
    assert "failed" in result["steps"]["tredict_sync"]
