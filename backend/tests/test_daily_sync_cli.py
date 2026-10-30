"""日任务命令行编排（``scripts/daily_sync.py``）的回归测试。

断言来自真实故障（2026-10-20）：

1. **日志必须实时落盘**——原来的日志句柄不带 flush，被计划任务/管道调用时
   子进程输出全部滞留在缓冲区，进程卡死或被杀后日志只剩 55 字节的启动行，
   导致「超时了但查不出卡在哪」。
2. **步骤必须真实存在**——``recompute_reports.py`` 在负荷重构中已删除
   （需求 10.1：不设独立手动重算入口），但步骤表仍引用它，每天固定 exit 2。
3. **子进程必须流式读取**——``subprocess.run(capture_output=True)`` 在子进程
   卡死时一个字都拿不到，只能干等 1 小时超时。
4. **步骤表可解析**——``-m`` 模块调用与脚本调用都要能拼出正确命令行。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "daily_sync.py"


@pytest.fixture(scope="module")
def ds():
    """按文件路径加载 daily_sync（它不在包内，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("daily_sync_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_all_steps_point_to_existing_targets(ds) -> None:
    """每个步骤的目标都必须存在（脚本文件或可导入模块），否则每天固定失败。"""
    backend = SCRIPT.parents[1]
    missing: list[str] = []

    for step in ds.ALL_STEPS.values():
        if step.args[0] == "-m":
            module = step.args[1]
            if importlib.util.find_spec(module) is None:
                missing.append(f"{step.key} → 模块 {module}")
            continue
        path = backend / "scripts" / step.args[0]
        if not path.exists():
            missing.append(f"{step.key} → {path.name}")

    assert not missing, "步骤表引用了不存在的目标：\n" + "\n".join(missing)


def test_recompute_step_uses_shared_orchestration(ds) -> None:
    """需求 10.1：重算走应用内统一编排，不引入独立脚本。"""
    step = ds.ALL_STEPS["recompute_reports"]
    assert step.args[0] == "-m"
    assert step.args[1] == "app.orchestrator.recompute_cli"


def test_cmd_for_script_and_module(ds) -> None:
    """命令行拼装：脚本走 ``scripts/``，``-m`` 走模块。"""
    script = ds.Step("x", "脚本", ("pull_garmin_health.py",))
    module = ds.Step("y", "模块", ("-m", "app.orchestrator.recompute_cli"))

    script_cmd = ds._cmd_for(script)
    assert script_cmd[0] == sys.executable
    assert script_cmd[1] == "-u"
    assert script_cmd[2].endswith("scripts\\pull_garmin_health.py") or script_cmd[2].endswith(
        "scripts/pull_garmin_health.py"
    )

    module_cmd = ds._cmd_for(module)
    assert module_cmd[:2] == [sys.executable, "-u"]
    assert module_cmd[2:] == ["-m", "app.orchestrator.recompute_cli"]


def test_step_header_is_flushed_before_run(ds, tmp_path, monkeypatch) -> None:
    """回归：步骤「开始」必须立刻进日志。

    原实现只在步骤**结束**后写日志，进程卡住时日志停在上一行，
    看不出到底卡在哪个步骤。
    """
    log_path = tmp_path / "run.log"
    step = ds.Step("ok_step", "正常步骤", ("-c", "print('hello')"))
    # 用 python -c 模拟：``-c`` 不是 -m，会被当成 scripts/-c 路径，故直接构造等价命令
    monkeypatch.setattr(
        ds, "_cmd_for", lambda s: [sys.executable, "-u", "-c", "print('hello')"]
    )
    with log_path.open("w", encoding="utf-8") as log:
        result = ds.run_step(step, log, dry_run=False)
    text = log_path.read_text(encoding="utf-8")

    assert result.ok is True
    assert "=== [ok_step]" in text, "步骤开始行必须写入日志"
    assert "hello" in text, "子进程输出必须写入日志"


def test_stalled_child_is_reported_not_silent(ds, monkeypatch, tmp_path) -> None:
    """回归：子进程长时间无输出时，心跳提示要进日志（不能静默干等）。"""
    monkeypatch.setattr(
        ds, "_cmd_for", lambda s: [sys.executable, "-u", "-c", "import time; time.sleep(3)"]
    )
    monkeypatch.setattr(ds, "STEP_STALL_WARN_SEC", 1)
    step = ds.Step("slow", "卡住步骤", ("noop",))
    log_path = tmp_path / "slow.log"
    with log_path.open("w", encoding="utf-8") as log:
        result = ds.run_step(step, log, dry_run=False)
    text = log_path.read_text(encoding="utf-8")

    assert result.ok is True  # 最终正常退出
    assert "[stall]" in text, "卡住期间必须留下心跳日志"


def test_failed_step_returns_nonzero_and_records_tail(ds, monkeypatch, tmp_path) -> None:
    """失败步骤要带 exit code 与输出尾部，便于事后定位。"""
    monkeypatch.setattr(
        ds,
        "_cmd_for",
        lambda s: [sys.executable, "-u", "-c", "import sys; print('boom'); sys.exit(2)"],
    )
    step = ds.Step("bad", "失败步骤", ("noop",))
    with (tmp_path / "bad.log").open("w", encoding="utf-8") as log:
        result = ds.run_step(step, log, dry_run=False)

    assert result.ok is False
    assert result.exit_code == 2
    assert "boom" in result.tail


def test_profile_tables_are_complete(ds) -> None:
    """每档都要能独立执行；``all`` 覆盖 activity + health + verify。"""
    keys = {s.key for s in ds.PROFILES["all"]}
    assert {s.key for s in ds.STEPS_ACTIVITY} <= keys
    assert {s.key for s in ds.STEPS_HEALTH} <= keys
    assert {s.key for s in ds.STEPS_VERIFY} <= keys
    for profile in ds.PROFILES:
        assert ds.PROFILES[profile], f"{profile} 档为空"
