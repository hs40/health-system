"""钉钉群机器人适配器（日报晨报推送，2026-10-26）。

只负责「把 markdown 发到 webhook」这一件事；消息内容由
``services/dingtalk_service`` 组装。支持「自定义关键词」与「加签」两种安全模式：
配置了 ``DINGTALK_SECRET`` 即加签，否则按关键词模式直发。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse

import httpx

from app.config import Settings


def sign_dingtalk(secret: str, timestamp_ms: int) -> str:
    """钉钉「加签」：``base64(HMAC-SHA256(secret, f"{ts}\\n{secret}"))`` → urlencode。"""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return urllib.parse.quote_plus(base64.b64encode(digest))


def signed_url(webhook: str, secret: str, timestamp_ms: int | None = None) -> str:
    """给 webhook 追加 ``timestamp`` 与 ``sign``（secret 为空则原样返回）。"""
    if not secret:
        return webhook
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={ts}&sign={sign_dingtalk(secret, ts)}"


class DingTalkClient:
    """钉钉群机器人客户端（markdown 消息）。"""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self.webhook = settings.dingtalk_webhook
        self.secret = settings.dingtalk_secret
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self.webhook)

    def send_markdown(self, title: str, text: str) -> dict:
        """发送 markdown；未配置 / HTTP 错误 / ``errcode≠0`` 均抛异常（调用方降级）。"""
        if not self.enabled:
            raise RuntimeError("未配置 DINGTALK_WEBHOOK，无法推送")
        payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
        client = self._client or httpx.Client(timeout=10.0)
        try:
            resp = client.post(signed_url(self.webhook, self.secret), json=payload)
            resp.raise_for_status()
            data = resp.json()
        finally:
            if self._client is None:
                client.close()
        if data.get("errcode") != 0:
            raise RuntimeError(f"钉钉返回错误：{data}")
        return data
