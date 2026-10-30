"""和风天气适配器测试（Plan §5 T2.1/T2.1b）。

不依赖网络：mock ``urllib.request.urlopen``；验证 JWT 结构、token 缓存、降级语义。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error

import pytest

from app.adapters.weather import WeatherAdapter
from app.config import Settings


def _settings(tmp_path, **kw) -> Settings:
    # 生成一次性 Ed25519 私钥
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "qweather_ed25519_private.pem"
    key_path.write_bytes(pem)
    return Settings(
        _env_file=None,
        qweather_private_key_path=key_path,
        qweather_jwt_kid="KHPN5E55QA",
        qweather_jwt_iss="Q17D5568C6",
        qweather_jwt_sub="4KTHBX5GGE",
        qweather_jwt_ttl_sec=900,
        **kw,
    )


def _decode_segment(seg: str) -> dict:
    pad = "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg + pad))


# ---------------------------- JWT 结构 ----------------------------
def test_build_jwt_header_has_no_typ(tmp_path):
    """关键：header 仅 {alg, kid}，**不得**含 typ（否则和风拒绝，§3.1.2）。"""
    adapter = WeatherAdapter(_settings(tmp_path))
    token = adapter.build_jwt()
    header_b64, payload_b64, sig_b64 = token.split(".")
    header = _decode_segment(header_b64)
    assert header == {"alg": "EdDSA", "kid": "KHPN5E55QA"}
    assert "typ" not in header
    assert "aud" not in header and "nbf" not in header


def test_build_jwt_payload_fields(tmp_path):
    adapter = WeatherAdapter(_settings(tmp_path))
    _, payload_b64, _ = adapter.build_jwt().split(".")
    payload = _decode_segment(payload_b64)
    assert payload["iss"] == "Q17D5568C6"
    assert payload["sub"] == "4KTHBX5GGE"
    now = int(time.time())
    assert payload["iat"] <= now  # iat = now-30
    assert payload["exp"] > now


def test_build_jwt_signature_is_ed25519_verifiable(tmp_path):
    s = _settings(tmp_path)
    adapter = WeatherAdapter(s)
    token = adapter.build_jwt()
    signing_input, sig_b64 = token.rsplit(".", 1)
    sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))

    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    priv = load_pem_private_key(s.qweather_private_key_path.read_bytes(), password=None)
    # 不抛异常即验证通过
    priv.public_key().verify(sig, signing_input.encode())
    assert len(sig) == 64  # Ed25519 签名恒为 64 字节


def test_build_jwt_missing_key_raises(tmp_path):
    s = Settings(_env_file=None, qweather_private_key_path=tmp_path / "nope.pem")
    with pytest.raises(FileNotFoundError):
        WeatherAdapter(s).build_jwt()


# ---------------------------- token 缓存 ----------------------------
def test_auth_token_is_cached(tmp_path, monkeypatch):
    adapter = WeatherAdapter(_settings(tmp_path))
    calls = {"n": 0}
    orig = adapter.build_jwt

    def _counted():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(adapter, "build_jwt", _counted)
    t1 = adapter._auth_token()
    t2 = adapter._auth_token()
    assert t1 == t2
    assert calls["n"] == 1  # 只签一次


# ---------------------------- 降级语义 ----------------------------
def _fake_urlopen(payload: bytes):
    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return payload

    return lambda *a, **kw: _Resp()


def test_lookup_city_success(tmp_path, monkeypatch):
    body = json.dumps(
        {
            "code": "200",
            "location": [
                {"id": "101220601", "name": "宜秀区", "lat": "30.51", "lon": "117.03",
                 "adm1": "安徽省", "adm2": "安庆市"}
            ],
        }
    ).encode()
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(body))
    adapter = WeatherAdapter(_settings(tmp_path))
    locs = adapter.lookup_city("安徽省安庆市宜秀区", adm="安庆")
    assert len(locs) == 1
    assert locs[0].name == "宜秀区"
    assert locs[0].lon == "117.03"


def test_lookup_city_401_returns_empty_not_fabricated(tmp_path, monkeypatch):
    """鉴权失败 → 空列表（不编造地点）。"""

    def _raise_401(*a, **kw):
        raise urllib.error.HTTPError(
            "url", 401, "Unauthorized", {}, None  # type: ignore[arg-type]
        )

    monkeypatch.setattr("urllib.request.urlopen", _raise_401)
    adapter = WeatherAdapter(_settings(tmp_path))
    assert adapter.lookup_city("x") == []


def test_current_unavailable_on_error(tmp_path, monkeypatch):
    def _raise(*a, **kw):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    adapter = WeatherAdapter(_settings(tmp_path))
    res = adapter.current("30.5", "117.0")
    assert res.available is False
    assert res.error


def test_resolve_training_location_prefers_exact_name(tmp_path, monkeypatch):
    body = json.dumps(
        {
            "code": "200",
            "location": [
                {"id": "1", "name": "安庆市", "lat": "30.5", "lon": "117.0"},
                {"id": "2", "name": "宜秀区", "lat": "30.51", "lon": "117.03"},
            ],
        }
    ).encode()
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(body))
    adapter = WeatherAdapter(_settings(tmp_path))
    loc = adapter.resolve_training_location("安徽省安庆市宜秀区")
    assert loc is not None
    assert loc.name == "宜秀区"  # 精确匹配（endswith）


def test_resolve_uses_cached_config_when_set(tmp_path, monkeypatch):
    s = _settings(
        tmp_path,
        qweather_location_id="101220601",
        qweather_lat="30.51",
        qweather_lon="117.03",
    )
    # 不 mock 网络；若走了网络会失败 → 证明用了缓存
    loc = WeatherAdapter(s).resolve_training_location()
    assert loc is not None
    assert loc.location_id == "101220601"


# ---------------------------- v1 新版格式 ----------------------------
_V1_CURRENT = {
    "metadata": {"tag": "abc", "attributions": ["https://developer.qweather.com/attribution.html"]},
    "condition": {"text": "雾", "code": "501"},
    "temperature": {"value": 21.25, "unit": "°C"},
    "humidity": 0.91,
    "precipitation": {"amount": {"value": 0.09, "unit": "mm"}, "type": "rain"},
    "wind": {"scale": 1, "speed": {"value": 1.31, "unit": "m/s"}},
}

_V1_DAILY = {
    "metadata": {"tag": "def"},
    "days": [
        {
            "forecastStartTime": "2026-10-15T16:00Z",
            "temperatureMax": {"value": 24.14, "unit": "°C"},
            "temperatureMin": {"value": 20.45, "unit": "°C"},
            "daytime": {
                "condition": {"text": "阵雨", "code": "300"},
                "precipitation": {"amount": {"value": 5.01, "unit": "mm"}, "type": "rain"},
                "wind": {"scale": 3},
                "humidity": 0.86,
            },
        }
    ],
}


def test_v1_current_parses_success(tmp_path, monkeypatch):
    """v1 无 code 字段：含 metadata/业务字段即视为成功。"""
    body = json.dumps(_V1_CURRENT).encode()
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(body))
    adapter = WeatherAdapter(_settings(tmp_path))
    res = adapter.current("30.51", "117.03")
    assert res.available is True
    assert res.code == "200"
    assert res.data["temperature"]["value"] == 21.25


def test_v1_error_body_marked_unavailable(tmp_path, monkeypatch):
    """v1 错误体 {"error": {...}} → available=False，且带鉴权提示。"""
    body = json.dumps(
        {"error": {"status": 401, "title": "Unauthorized", "detail": "Authentication failed"}}
    ).encode()
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(body))
    adapter = WeatherAdapter(_settings(tmp_path))
    res = adapter.current("30.51", "117.03")
    assert res.available is False
    assert "401" in (res.error or "")
    assert "Authentication failed" in (res.error or "")


def test_v1_daily_available(tmp_path, monkeypatch):
    body = json.dumps(_V1_DAILY).encode()
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(body))
    adapter = WeatherAdapter(_settings(tmp_path))
    res = adapter.daily("30.51", "117.03")
    assert res.available is True
    assert len(res.data["days"]) == 1


def test_http_500_marked_unavailable(tmp_path, monkeypatch):
    def _raise_500(*a, **kw):
        raise urllib.error.HTTPError("url", 500, "Server Error", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", _raise_500)
    res = WeatherAdapter(_settings(tmp_path)).current("30.51", "117.03")
    assert res.available is False
    assert res.http_status == 500


def test_old_format_code_non_200_still_unavailable(tmp_path, monkeypatch):
    """向后兼容：出现旧版 code!=200 也判定失败。"""
    body = json.dumps({"code": "401", "location": []}).encode()
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(body))
    res = WeatherAdapter(_settings(tmp_path)).current("30.51", "117.03")
    assert res.available is False
