"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import activities, data, environment, messages, ops, plans, reports, strategy
from app.config import get_settings
from app.db.session import init_db
from app.errors import AppError
from app.logging_conf import setup_logging
from app.middleware.request_id import RequestIdMiddleware

logger = logging.getLogger("hs.api")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging()
    settings.ensure_dirs()
    init_db()
    logger.info(
        "启动完成 env=%s risk_rule_version=%s db=%s",
        settings.app_env,
        settings.risk_rule_version,
        settings.db_path,
    )
    yield
    logger.info("关闭")


app = FastAPI(
    title="健康运动管理系统 API",
    version=__version__,
    description="本地优先 · AI 教练 · 风险拦截（Plan v3）",
    lifespan=lifespan,
)

# 中间件是「后加的先包在外层」。RequestId 必须最外层：这样 CORS 预检、404、
# 以及其它中间件抛出的异常，其日志与响应头都带得上 request_id。
# CORS 外露 X-Request-ID，跨源时前端也能在 ApiError 里读到它。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本地单用户；生产可由 Nginx 同源代理
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)
app.add_middleware(RequestIdMiddleware)


@app.exception_handler(AppError)
async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": {"code": str(exc.code), "message": exc.message, "detail": exc.detail}},
    )


app.include_router(reports.router, prefix="/api")
app.include_router(plans.router, prefix="/api")
app.include_router(data.router, prefix="/api")
app.include_router(ops.router, prefix="/api")
app.include_router(environment.router, prefix="/api")
app.include_router(strategy.router, prefix="/api")
app.include_router(messages.router, prefix="/api")
app.include_router(activities.router, prefix="/api")
