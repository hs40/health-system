"""日志接线契约：任务日志必须能追溯到「哪次任务、哪一天」。

A.8.1 的痛点是「12 个模块的日志 0 条带 task_id」，出故障只能靠中文句子猜。
这里从**任务入口**这一公共缝验证：入口返回后，运行期间产生的日志必须带任务标识。

测法：接管根 logger 的全部输出（handler 用生产同款 filter + 格式），
捕获真正被渲染出来的文本——不是断言 LogRecord 上的属性，否则格式化模板
漏写 ``%(task_id)s`` 这类 bug 会逃逸。

数据库：``session_scope`` 走全局 engine，用一个内存库顶替（``init_db``
会在该内存库上建表）。不顶替的话任务会连到真实的 ``data/db/health.db``。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

from app.logging_conf import LOG_FORMAT, LOGGER_NAMESPACES, ContextFilter


@pytest.fixture()
def task_log() -> Iterator[list[str]]:
    """捕获根 logger 全量输出文本，测试结束后还原。

    前置断言而非静默兜底：若前序测试把全局日志状态改坏（典型是
    ``logging.config.fileConfig`` 的 ``disable_existing_loggers`` 把
    ``hs.*`` 置 ``disabled``），这里直接报出明确的根因，而不是等
    断言阶段看到「捕获为空」再回去猜。
    """
    for name in ("hs", *LOGGER_NAMESPACES):
        logger = logging.getLogger(name)
        assert not logger.disabled, f"前序测试把 {name} 置为 disabled（见 fileConfig 契约）"
    assert logging.root.manager.disable == logging.NOTSET, "前序测试调用了 logging.disable()"

    lines: list[str] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = ListHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield lines
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


@pytest.fixture()
def memory_db(monkeypatch) -> None:
    """把 ``session_scope`` 用的全局 engine 换成内存库。"""
    import app.db.session as db_session

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "init_db", lambda: None)


def _fake_sync(*_a, **_k):
    return _FakeSync()


def _fake_standard(*_a, **_k):
    return None


def _fake_report(*_a, **_k):
    return _FakeReport()


def test_daily_job_logs_carry_task_id_and_date(task_log, memory_db, monkeypatch) -> None:
    import app.orchestrator.scheduler as sched

    monkeypatch.setattr(sched, "sync_daily", _fake_sync)
    monkeypatch.setattr(sched, "get_standard", _fake_standard)
    monkeypatch.setattr(sched, "generate_report", _fake_report)
    monkeypatch.setattr(sched, "analyze_daily", lambda *a, **k: {"summary": "ok"})
    monkeypatch.setattr(sched, "build_today_plan", lambda *a, **k: None)
    monkeypatch.setattr(sched, "push_daily_report", lambda *a, **k: "skipped")

    sched.run_daily_job(dt.date(2026, 11, 7), now=dt.datetime(2026, 11, 7, 12, 30))

    joined = "\n".join(task_log)
    # 断言「任务名@日期」整体出现：格式串里二者相邻渲染，
    # 分开断言会让「只有 message 正文带日期」的情况假绿。
    assert "daily@2026-11-07" in joined


def test_tredict_sync_job_logs_carry_task_id(task_log, memory_db, monkeypatch) -> None:
    import app.orchestrator.scheduler as sched

    def _boom(*_a, **_k):
        raise RuntimeError("同步炸了")

    monkeypatch.setattr(sched, "run_tredict_sync", _boom)

    sched.run_tredict_sync_job()

    joined = "\n".join(task_log)
    assert f"tredict_sync@{dt.date.today().isoformat()}" in joined
    assert "同步炸了" in joined


class _FakeSync:
    status = "ok"
    errors: list[str] = []
    written = 0
    skipped = 0
    missing = 0


class _FakeReport:
    risk = None
