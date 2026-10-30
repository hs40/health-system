"""运维接口测试：健康检查「已配置」判定口径一致性（TD.4 收尾修复）。

背景：原实现用 `is not None` 判断密钥是否配置，空字符串（如 `LLM_API_KEY=`）
会被判为「已配置」，而 detail 分支用真值判断又输出 `not configured`，
导致健康面板出现 `ok=true / detail=not configured` 的自相矛盾输出。
本测试锁定统一口径：**非 None 且去空白后非空**才算已配置。
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlmodel import Session, SQLModel, create_engine

from app.api.ops import health
from app.config import Settings


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


# ---------------------------- 判定口径 ----------------------------
def test_secret_present_rejects_empty_and_whitespace():
    assert Settings._secret_present(None) is False
    assert Settings._secret_present(SecretStr("")) is False
    assert Settings._secret_present(SecretStr("   ")) is False
    assert Settings._secret_present(SecretStr("sk-abc")) is True


def test_llm_empty_key_is_not_ready():
    assert _settings(llm_api_key=SecretStr("")).llm_ready is False


def test_llm_real_key_is_ready():
    assert _settings(llm_api_key=SecretStr("sk-x")).llm_ready is True


def test_tredict_empty_token_is_not_ready():
    assert _settings(tredict_token=SecretStr("")).tredict_ready is False


# ---------------------------- 健康端点一致性 ----------------------------
def test_health_llm_empty_key_ok_and_detail_agree(session, monkeypatch):
    """空 Key：ok 必须为 False，且 detail 为 not configured（两者一致）。"""
    monkeypatch.setattr(
        "app.api.ops.get_settings", lambda: _settings(llm_api_key=SecretStr(""))
    )
    resp = health(session)
    llm = next(d for d in resp.deps if d.name == "llm")
    assert llm.ok is False
    assert llm.detail == "not configured"


def test_health_llm_configured_reports_provider_and_model(session, monkeypatch):
    monkeypatch.setattr(
        "app.api.ops.get_settings",
        lambda: _settings(
            llm_api_key=SecretStr("sk-x"),
            llm_provider="deepseek",
            llm_model_daily="deepseek-chat",
        ),
    )
    resp = health(session)
    llm = next(d for d in resp.deps if d.name == "llm")
    assert llm.ok is True
    assert llm.detail == "deepseek / deepseek-chat"
