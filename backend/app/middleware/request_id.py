"""HTTP 层可观测性中间件（ARCHITECTURE_REVIEW A.8.1 剩余 · 采集端）。

三件事
------

1. **request_id 贯穿**：读入站 ``X-Request-ID``（Nginx 或前端给），校验后放进
   ``log_context``，请求处理期间打的每一条日志都带 ``[req <id>]``；
   再把同一个 ID 写回响应头，前端 ``ApiError`` 能把它交给用户。
2. **缺失/非法则不信任**：没有头、或头里是控制字符 / 超长 / 含空格，
   一律丢弃并本地生成 UUID4——**不洗白原值**。洗白后的值和上游对不上，
   比换一个新的更难查。
3. **access log 只打异常**：Nginx 已经逐条记录全量访问日志。后端再记一遍是
   双份噪音，NAS 上 ``docker logs`` 长得很快。这里只在
   ``status >= 400`` 或耗时超 ``SLOW_MS`` 时打一行，带 id 与耗时。

为什么是纯 ASGI 中间件而不是 ``BaseHTTPMiddleware``
--------------------------------------------------
``BaseHTTPMiddleware`` 会把响应体包成流并在独立 task 里跑下游，
``contextvars`` 的传播行为与「同一个 task 内顺序执行」不同——日志里拿到
空 request_id 是最常见的症状。纯 ASGI 中间件在同一条协程链上 set/reset，
contextvar 一定可见。代价是要手写「把 status 从 ``http.response.start`` 捞出来」。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from app.logging_conf import REQUEST_ID_HEADER, log_context, normalize_request_id

logger = logging.getLogger("hs.api.access")

#: 超过这个耗时就记一行（毫秒）。低于它且状态码正常 → 不出声。
#: 500ms 够宽松：正常读接口都在几十毫秒，超过半秒说明确实值得看一眼。
SLOW_MS = 500

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_HEADER_KEY = REQUEST_ID_HEADER.lower().encode("latin-1")


def _inbound_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or ():
        if name == _HEADER_KEY:
            return normalize_request_id(value.decode("latin-1"))
    return None


def _dedupe_request_id(
    headers: list[tuple[bytes, bytes]], request_id: str
) -> list[tuple[bytes, bytes]]:
    """只保留一个 ``X-Request-ID``，值为本次链路的真实 ID。

    下游若已写同名头（有些框架/中间件会写），位置不变、值以本中间件为准；
    完全没写则追加到末尾。
    """
    encoded = request_id.encode("latin-1")
    out: list[tuple[bytes, bytes]] = []
    placed = False
    for key, value in headers:
        if key.lower() == _HEADER_KEY:
            if not placed:
                out.append((key, encoded))
                placed = True
            continue  # 丢弃重复项
        out.append((key, value))
    if not placed:
        out.append((_HEADER_KEY, encoded))
    return out


class RequestIdMiddleware:
    """注入 request_id 到日志上下文与响应头，并按需打 access log。"""

    def __init__(self, app: ASGIApp, *, slow_ms: int = SLOW_MS) -> None:
        self.app = app
        self.slow_ms = slow_ms

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _inbound_request_id(scope) or uuid.uuid4().hex[:16]
        started = time.perf_counter()
        status_holder: list[int] = []

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder.append(int(message["status"]))
                headers = list(message.get("headers") or [])
                if not any(k.lower() == _HEADER_KEY for k, _v in headers):
                    headers.append((_HEADER_KEY, request_id.encode("latin-1")))
                # 无论下游是否已写，都去重后写回：下游可能写了一个与本次
                # request_id 不一致的值（例如它自己又生成了一次），
                # 保留本中间件的那个才是链路真实 ID。
                message["headers"] = _dedupe_request_id(headers, request_id)
            await send(message)

        scope.setdefault("state", {})["request_id"] = request_id

        # 日志必须留在 with 块**内**：出块后 contextvar 已 reset，
        # 在块外打日志会丢掉 request_id（v3.3.0 记过的同款坑）。
        with log_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                status = status_holder[0] if status_holder else 0
                self._log(scope, status, elapsed_ms)

    def _log(self, scope: Scope, status: int, elapsed_ms: int) -> None:
        failed = status >= 400 or status == 0  # 0 = 下游直接抛了、没发出 response.start
        if not failed and elapsed_ms < self.slow_ms:
            return
        level = logging.ERROR if status >= 500 else logging.WARNING
        logger.log(
            level,
            "%s %s -> %s %dms",
            scope.get("method", "?"),
            scope.get("path", "?"),
            status or "no-response",
            elapsed_ms,
        )
