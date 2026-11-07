"""日志基础设施（ARCHITECTURE_REVIEW A.8.1）。

三件事，只做这三件：

1. **单点配置**：全仓只有这一处 ``basicConfig``。此前 ``main.py`` 与
   ``scheduler.py`` 各写一份，格式字符串重复，且谁都读不到
   ``Settings.log_level``。
2. **上下文注入**：``task_id`` / ``date`` 通过 ``contextvars`` 传递，由
   ``ContextFilter`` 自动附加到每一条日志。调用方在任务入口包一层
   ``log_context`` 即可，不必层层传参。
3. **logger 命名规范**：``hs.<子系统>``（见 ``LOGGER_NAMESPACES``）。
   子树 logger（``hs.daily.garmin`` 之类）自动继承父级配置。

不加 JSON formatter：当前 NAS 单容器只能 ``docker logs`` 看，JSON 反而
更难肉眼读。对外可观测性靠审计表与 Fault 表，日志只服务排障。
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import sys
from collections.abc import Iterator

from app.config import get_settings

#: logger 命名规范。新增子系统时在这里登记，避免 ``main.py`` / ``hs.scheduler`` /
#: 裸 ``__name__`` 三种风格并存（A.8.1 现状盘点）。
LOGGER_NAMESPACES: tuple[str, ...] = (
    "hs.api",  # HTTP 层
    "hs.daily",  # 日流水线
    "hs.scheduler",  # 调度器
    "hs.adapters",  # 外部适配器（Garmin / Tredict / LLM / 天气）
    "hs.services",  # 业务服务
    "hs.storage",  # 持久化
)

#: 格式化模板：`[task_id@date]` 紧邻渲染。
#: 不写成 ``%(asctime)s ... [%(task_id)s] ... %(date)s``——那会让「date 只来自
#: message 正文」时测试假绿；绑定在一起才是一个可断言的整体。
LOG_FORMAT = "%(asctime)s %(levelname)s [%(task_id)s@%(date)s] %(name)s %(message)s"

#: 上下文缺失时的占位（过滤器保证这两个 key 一定存在，模板才不会 KeyError）。
_NO_TASK = "-"

_task_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("task_id", default=_NO_TASK)
_date_var: contextvars.ContextVar[str] = contextvars.ContextVar("date", default=_NO_TASK)


@contextlib.contextmanager
def log_context(*, task_id: str | None = None, date: object | None = None) -> Iterator[None]:
    """在 ``with`` 块内为所有日志附加 ``task_id`` / ``date``。

    ``date`` 接受 ``datetime.date`` 或任意可 ``str()`` 的值，统一成 ISO 字符串。
    退出时恢复上一层上下文（可嵌套）。
    """
    tokens = []
    if task_id is not None:
        tokens.append((_task_id_var, _task_id_var.set(task_id)))
    if date is not None:
        tokens.append((_date_var, _date_var.set(str(date))))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


class ContextFilter(logging.Filter):
    """把 contextvar 里的 ``task_id`` / ``date`` 搬到 LogRecord 上。

    过滤器挂在 handler 上而不是 logger 上：挂 logger 时子 logger 的 record
    不会经过父 logger 的 filter（只经过父 handler），会漏。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.task_id = _task_id_var.get()
        record.date = _date_var.get()
        return True


def setup_logging(level: str | None = None, *, force: bool = False) -> None:
    """初始化根 logger。幂等——重复调用不会叠加 handler。

    :param level: 日志级别；缺省取 ``Settings.log_level``。
    :param force: 为 True 时重建 handler（测试用）。
    """
    root = logging.getLogger()
    if root.handlers and not force:
        return

    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)

    resolved = (level or get_settings().log_level or "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    handler.addFilter(ContextFilter())

    root.addHandler(handler)
    root.setLevel(resolved)

    # 第三方库降噪：uvicorn access 日志与 apscheduler 的 debug 噪音不进入排障视角。
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
