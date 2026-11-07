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


# ---------------------- 无开发机硬编码路径（ARCHITECTURE_REVIEW A.2.1）------------------------
def test_health_source_has_no_hardcoded_dev_paths():
    """健康检查源码不得出现开发机路径（Windows 用户目录 / 特定盘符）。

    原实现把插件 session（``~/AppData/Local/dsh-plugin-...``）与 Tredict 配置
    （``F:/WorkBuddy AI/...``）写死在判断里：在 Linux/NAS 容器中永远不存在，
    判断只能靠「或」分支才碰巧正确，且泄露开发机目录结构。
    契约：外部依赖是否可用，只以 ``Settings`` 为唯一真相源。
    """
    import inspect

    from app.api import ops

    source = inspect.getsource(ops.health)
    for forbidden in ("AppData", "dsh-plugin", "WorkBuddy AI", "F:/", "F:\\\\"):
        assert forbidden not in source, f"health() 仍含开发机路径痕迹: {forbidden}"


def test_health_garmin_ready_only_from_settings(session, monkeypatch, tmp_path):
    """Garmin 判定只看 tokenstore 与凭据，不受宿主环境残留文件影响。"""
    tokenstore = tmp_path / "garmin_tokens"
    tokenstore.mkdir()
    (tokenstore / "tokens.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "app.api.ops.get_settings",
        lambda: _settings(garmin_tokenstore=tokenstore),
    )
    resp = health(session)
    garmin = next(d for d in resp.deps if d.name == "garmin")
    assert garmin.ok is True
    assert garmin.detail == f"tokenstore={tokenstore / 'tokens.json'}"


def test_health_garmin_not_configured_without_tokenstore_or_credentials(
    session, monkeypatch, tmp_path
):
    """既无 tokenstore 也无凭据 → not configured（不再有第三条「插件 session」分支）。"""
    monkeypatch.setattr(
        "app.api.ops.get_settings",
        lambda: _settings(garmin_tokenstore=tmp_path / "absent", garmin_email=None),
    )
    resp = health(session)
    garmin = next(d for d in resp.deps if d.name == "garmin")
    assert garmin.ok is False
    assert garmin.detail == "not configured"


def test_health_tredict_ready_only_from_settings(session, monkeypatch):
    """Tredict 判定只认 ``settings.tredict_ready``，不探测任何外部配置文件。"""
    monkeypatch.setattr("app.api.ops.get_settings", lambda: _settings())
    resp = health(session)
    tredict = next(d for d in resp.deps if d.name == "tredict")
    assert tredict.ok is False
    assert tredict.detail == "not configured"

    monkeypatch.setattr(
        "app.api.ops.get_settings",
        lambda: _settings(tredict_token=SecretStr("tk")),
    )
    resp = health(session)
    tredict = next(d for d in resp.deps if d.name == "tredict")
    assert tredict.ok is True
    assert tredict.detail == "token configured"
