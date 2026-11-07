"""日志基础设施的行为契约（ARCHITECTURE_REVIEW A.8.1）。

测试缝（seam）全部是公共接口：

- ``setup_logging``：根 logger 的级别与 handler 数量必须是确定的；
- ``log_context`` + handler 上的 filter：上下文必须真正出现在**最终输出文本**里，
  而不是只出现在 LogRecord 对象上——后者会让「格式化模板里漏写字段」的 bug 逃逸；
- ``LOGGER_NAMESPACES``：命名规范本身是可断言的常量。
"""

from __future__ import annotations

import io
import logging

from app.logging_conf import (
    LOG_FORMAT,
    LOGGER_NAMESPACES,
    ContextFilter,
    log_context,
    setup_logging,
)


def _capture() -> tuple[io.StringIO, logging.Logger]:
    """构造一个独立的 logger + 内存 handler，挂上与生产一致的 filter 与格式。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(ContextFilter())
    logger = logging.getLogger("hs.test.logging")
    logger.propagate = False
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    return stream, logger


def test_setup_logging_is_idempotent() -> None:
    setup_logging("INFO", force=True)
    first = len(logging.getLogger().handlers)
    setup_logging("INFO", force=True)
    assert len(logging.getLogger().handlers) == first == 1


def test_setup_logging_uses_settings_log_level() -> None:
    setup_logging("warning", force=True)
    assert logging.getLogger().level == logging.WARNING
    setup_logging("INFO", force=True)


def test_output_carries_task_and_date_from_context() -> None:
    stream, logger = _capture()

    import datetime as dt

    with log_context(task_id="job-42", date=dt.date(2026, 11, 7)):
        logger.info("日报生成完成")

    out = stream.getvalue()
    assert "job-42" in out
    assert "2026-11-07" in out
    assert "日报生成完成" in out


def test_context_is_restored_after_block() -> None:
    stream, logger = _capture()
    with log_context(task_id="job-42"):
        pass
    logger.info("块外日志")
    assert "job-42" not in stream.getvalue()


def test_context_nests() -> None:
    stream, logger = _capture()
    with log_context(task_id="outer"):
        with log_context(task_id="inner"):
            logger.info("内层")
        logger.info("外层")
    out = stream.getvalue()
    assert "inner" in out
    assert "outer" in out


def test_missing_context_renders_placeholder_not_error() -> None:
    stream, logger = _capture()
    logger.info("无上下文")
    out = stream.getvalue()
    assert "无上下文" in out
    assert "[" in out  # task_id 占位渲染成 [-]，不是 KeyError


def test_logger_namespaces_follow_hs_prefix() -> None:
    assert LOGGER_NAMESPACES
    assert all(ns == "hs" or ns.startswith("hs.") for ns in LOGGER_NAMESPACES)


def test_logger_namespaces_are_unique() -> None:
    assert len(LOGGER_NAMESPACES) == len(set(LOGGER_NAMESPACES))


def test_file_config_does_not_disable_existing_loggers() -> None:
    """``env.py`` 调用 ``fileConfig`` 时不能关掉进程内已有 logger。

    ``logging.config.fileConfig`` 默认 ``disable_existing_loggers=True``，
    会把调用前已创建的全部 logger 置为 ``disabled``。迁移与应用同进程跑时
    （测试、启动引导、未来的 ``alembic upgrade`` API），此后所有 ``hs.*``
    日志会静默消失——实测导致全量测试里日志接线用例捕获为空。

    ``alembic.context.config`` 只在 alembic 运行时存在，无法直接 import
    ``env`` 模块，故这里锁住 ``env.py`` 实际使用的那次调用的语义。
    """
    import logging.config
    from pathlib import Path

    watcher = logging.getLogger("hs.test.survives_fileconfig")
    watcher.disabled = False

    ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    try:
        logging.config.fileConfig(ini, disable_existing_loggers=False)
        assert watcher.disabled is False, "fileConfig 关掉了既有 logger"
    finally:
        # fileConfig 会重装根 handler，测试结束必须还原，否则污染后续用例。
        watcher.disabled = False
        setup_logging("INFO", force=True)
