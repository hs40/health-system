"""钉钉推送链接前缀（APP_BASE_URL）默认值契约（2026-11-05）。

背景：NAS 换 IP 后旧 http://192.168.2.10:18080 失效，钉钉日报「查看完整报告」
链接打不开。改用公网域名 http://hs.qingtianjian.com:18080。

两条契约：
1. ``Settings`` 默认值 = 新域名（不配 .env 时也对）；
2. ``push_daily_report`` 用该默认值能生成正确的报告页链接。
"""

from __future__ import annotations

from app.config import Settings

NEW_BASE = "http://hs.qingtianjian.com:18080"


def test_default_app_base_url_is_public_domain(monkeypatch):
    """未设置 APP_BASE_URL 时，默认值必须是公网域名（不是空 / 旧内网 IP）。"""
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.app_base_url == NEW_BASE
    assert "192.168.2.10" not in s.app_base_url


def test_env_example_pins_public_domain():
    """``.env.example`` 模板必须带 APP_BASE_URL=新域名（防部署漂移）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    text = (root / ".env.example").read_text(encoding="utf-8")
    assert f"APP_BASE_URL={NEW_BASE}" in text


def test_dingtalk_link_uses_default_base_url(monkeypatch):
    """用默认 Settings 组装的钉钉消息里，链接指向新域名的报告页。"""
    from app.services.dingtalk_service import build_daily_markdown
    from tests.test_dingtalk import _report

    monkeypatch.delenv("APP_BASE_URL", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    _, text = build_daily_markdown(
        _report(), plan_name=None, base_url=s.app_base_url
    )
    assert f"[查看完整报告]({NEW_BASE}/reports/daily?date=2026-10-26)" in text
    assert "192.168.2.10" not in text
