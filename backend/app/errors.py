"""统一业务异常与 HTTP 映射（Plan §4.5）。"""

from __future__ import annotations

from app.schemas.enums import ERROR_HTTP_STATUS, ErrorCode


class AppError(Exception):
    """业务异常：携带统一错误码，由全局处理器转为标准错误响应。"""

    def __init__(self, code: ErrorCode, message: str, detail: dict | None = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.http_status = ERROR_HTTP_STATUS.get(code, 500)
        super().__init__(message)