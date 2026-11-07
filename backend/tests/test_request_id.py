"""request_id 贯穿链路的契约（ARCHITECTURE_REVIEW A.8.1 剩余 · 采集端）。

测试缝（seam）全部是**外部可观察的事实**，不碰内部实现细节：

1. 响应头里必须有 ``X-Request-ID``；
2. 入站头合法 → 原样沿用；非法/缺失 → 丢弃并重新生成（不等于原值、且格式合法）；
3. 请求处理期间打的日志**文本里**必须出现该 ID（不是只挂在 LogRecord 上）；
4. 正常且快的请求**不出声**（Nginx 已记全量，后端不重复记）；
5. ``normalize_request_id`` 的字符集与长度边界。

纯 ASGI 中间件可以用裸 scope 直接驱动，不必起 uvicorn；需要「真实路由 + 真实日志
输出」时才用 ``TestClient``。
"""

from __future__ import annotations

import asyncio
import io
import logging

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.logging_conf import LOG_FORMAT, REQUEST_ID_HEADER, ContextFilter, normalize_request_id
from app.middleware.request_id import SLOW_MS, RequestIdMiddleware


def _http_scope(
    *, path: str = "/api/probe", headers: list[tuple[bytes, bytes]] | None = None
) -> dict:
    """最小合法 ASGI HTTP scope。

    ``query_string`` / ``server`` / ``client`` 不能省：Starlette 的 ``Request``
    会直接索引它们（``KeyError: 'query_string'``），缺字段的「假 scope」
    测的不是生产路径。
    """
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
        "state": {},
    }


def _capture_logger(name: str) -> tuple[io.StringIO, logging.Logger]:
    """独立的 logger + 内存 handler，格式与生产一致（含 ContextFilter）。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(ContextFilter())
    logger = logging.getLogger(name)
    logger.propagate = False
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    return stream, logger


class TestNormalizeRequestId:
    """外部头是不可信输入：会进日志与响应头。"""

    @pytest.mark.parametrize(
        "raw",
        ["abc123", "a1b2c3d4e5f6", "req-2026.11.07", "A_b:C-d", "a" * 128],
    )
    def test_accepts_safe_values(self, raw: str) -> None:
        assert normalize_request_id(raw) == raw

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "a" * 129,  # 超长
            "has space",
            "semi;colon",
            "quote'd",
            "中文ID",  # 非 ASCII
            "line\nbreak",  # CRLF 注入 → 日志伪造
            "tab\there",
        ],
    )
    def test_rejects_unsafe_values(self, raw: str | None) -> None:
        assert normalize_request_id(raw) is None


class TestMiddlewareBehaviour:
    """用裸 ASGI scope 驱动，断言响应头与日志文本。"""

    def _attach_capture(self) -> io.StringIO:
        """把内存 handler 挂到**生产** logger 上，保证测的是真实输出路径。"""
        from app.middleware import request_id as mod

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(ContextFilter())
        mod.logger.handlers = [handler]
        mod.logger.propagate = False
        mod.logger.setLevel(logging.DEBUG)
        return stream

    def _drive(self, app: FastAPI, *, inbound: str | None = None) -> list[dict]:
        headers = []
        if inbound is not None:
            headers.append((REQUEST_ID_HEADER.lower().encode(), inbound.encode()))

        messages: list[dict] = []

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            messages.append(message)

        async def _go() -> None:
            await RequestIdMiddleware(app)(_http_scope(headers=headers), receive, send)

        asyncio.run(_go())
        return messages

    def test_inbound_id_is_echoed_and_used(self) -> None:
        """入站 ID 既要在响应头回写，也要出现在后端日志文本里。"""
        stream = self._attach_capture()

        app = FastAPI()

        @app.get("/api/probe")
        async def probe() -> dict:
            raise HTTPException(status_code=400)  # 强制落一行 access log

        messages = self._drive(app, inbound="trace-abc-123")

        start = messages[0]
        assert start["type"] == "http.response.start"
        header_map = {k.lower(): v for k, v in start["headers"]}
        assert header_map[REQUEST_ID_HEADER.lower().encode()] == b"trace-abc-123"
        assert "trace-abc-123" in stream.getvalue(), "日志里没有带上 request_id"

    def test_missing_header_generates_one(self) -> None:
        messages = self._drive(FastAPI())
        header_map = {k.lower(): v for k, v in messages[0]["headers"]}
        generated = header_map[REQUEST_ID_HEADER.lower().encode()].decode()
        assert generated and normalize_request_id(generated) == generated

    def test_illegal_header_is_replaced_not_sanitized(self) -> None:
        """非法值**丢弃**而非清洗：清洗后可能与上游 ID 撞车，更难查。"""
        messages = self._drive(FastAPI(), inbound="bad value;with junk")
        header_map = {k.lower(): v for k, v in messages[0]["headers"]}
        echoed = header_map[REQUEST_ID_HEADER.lower().encode()].decode()
        assert echoed != "bad value;with junk"
        assert normalize_request_id(echoed) == echoed

    def test_downstream_header_is_deduped_down_to_one(self) -> None:
        """下游自己也写了 X-Request-ID 时，最终只剩一个，且以中间件为准。

        真实场景：某些框架/中间件会自己生成一个 ID 写进响应头。若两者共存，
        浏览器读到的可能与后端日志里的不是同一个值，排障就对不上。
        """
        sent: list[list[tuple[bytes, bytes]]] = []

        async def downstream(scope: dict, _receive: object, send: object) -> None:
            await send(  # type: ignore[operator]
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (REQUEST_ID_HEADER.lower().encode(), b"downstream"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"{}"})  # type: ignore[operator]

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.start":
                sent.append(message["headers"])

        asyncio.run(RequestIdMiddleware(downstream)(_http_scope(), receive, send))

        ids = [v for k, v in sent[0] if k == REQUEST_ID_HEADER.lower().encode()]
        assert len(ids) == 1, f"出现了重复的 request_id 头：{ids}"
        assert ids[0] != b"downstream", "应以中间件注入的链路 ID 为准"
        # 其它头原样保留
        assert (b"content-type", b"application/json") in sent[0]

    def test_non_http_scope_passes_through(self) -> None:
        """lifespan / websocket 不走 request_id 逻辑。"""
        seen: list[str] = []

        async def app(scope: dict, _receive: object, _send: object) -> None:
            seen.append(scope["type"])

        async def receive() -> dict:
            return {}

        async def send(_m: dict) -> None:
            return None

        asyncio.run(RequestIdMiddleware(app)({"type": "lifespan"}, receive, send))
        assert seen == ["lifespan"]


class TestAccessLogNoise:
    """只打异常或慢请求——Nginx 已经把全量访问日志记走了。"""

    def _drive(self, app: FastAPI, logger: logging.Logger) -> str:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(ContextFilter())
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.DEBUG)

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_m: dict) -> None:
            return None

        asyncio.run(RequestIdMiddleware(app)(_http_scope(), receive, send))
        return stream.getvalue()

    def test_success_fast_request_is_silent(self) -> None:
        app = FastAPI()

        @app.get("/api/probe")
        async def probe() -> dict:
            return {"ok": True}

        from app.middleware import request_id as mod

        out = self._drive(app, mod.logger)
        assert out == "", f"正常请求不该产生 access log：{out!r}"

    def test_error_status_is_logged(self) -> None:
        app = FastAPI()

        @app.get("/api/probe")
        async def probe() -> dict:
            raise HTTPException(status_code=418, detail="teapot")

        from app.middleware import request_id as mod

        out = self._drive(app, mod.logger)
        assert "418" in out
        assert "/api/probe" in out

    def test_slow_request_is_logged(self) -> None:
        app = FastAPI()

        @app.get("/api/probe")
        async def probe() -> dict:
            await asyncio.sleep(0.05)
            return {"ok": True}

        from app.middleware import request_id as mod

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.addFilter(ContextFilter())
        mod.logger.handlers = [handler]
        mod.logger.propagate = False
        mod.logger.setLevel(logging.DEBUG)

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_m: dict) -> None:
            return None

        asyncio.run(RequestIdMiddleware(app, slow_ms=1)(_http_scope(), receive, send))
        out = stream.getvalue()
        assert "ms" in out and "/api/probe" in out


class TestRealAppIntegration:
    """真实应用（含全部中间件与路由）上的端到端事实。"""

    def test_response_carries_request_id_via_testclient(self) -> None:
        from app.main import app

        with TestClient(app) as client:
            res = client.get("/api/health")
        assert res.headers.get(REQUEST_ID_HEADER)

    def test_client_supplied_id_is_echoed(self) -> None:
        from app.main import app

        with TestClient(app) as client:
            res = client.get("/api/health", headers={REQUEST_ID_HEADER: "from-frontend-1"})
        assert res.headers.get(REQUEST_ID_HEADER) == "from-frontend-1"

    def test_404_also_gets_id(self) -> None:
        """404 是最常见的「用户说点不动」场景，必须也有 ID。"""
        from app.main import app

        with TestClient(app) as client:
            res = client.get("/api/definitely-not-a-route")
        assert res.status_code == 404
        assert res.headers.get(REQUEST_ID_HEADER)


def test_slow_threshold_is_sane() -> None:
    assert 100 <= SLOW_MS <= 5000, "阈值过大等于不打慢日志，过小等于每条都打"
