"""和风天气适配器（Plan §3.1.1 / §5 T2.1；决策 E3/E20/E21/E22）。

**认证：JWT（EdDSA / Ed25519）** —— 依据和风官方《身份认证》：
- header：``{"alg": "EdDSA", "kid": <凭据ID>}``（**不加 typ**）；
- payload：``{"iss": <开发者ID>, "sub": <项目ID>, "iat": now-30, "exp": iat+ttl}``；
- 签名：``Ed25519(私钥, base64url(header) + "." + base64url(payload))``；
- 请求头：``Authorization: Bearer <token>``。

**关键实现约束**：
- **必须用 `cryptography` 直接签名，不用 PyJWT** —— 实测 PyJWT 会强制注入 ``typ: JWT``，
  与和风"移除保留字段"要求冲突（§3.1.2）。
- Token **按期缓存**（默认 900s，留 60s 提前量重签），避免每次请求重签。
- **私钥不入日志/镜像**：仅以 ``Path`` 引用，读取异常不回显内容。

**降级（§3.1.1 / §13.8）**：失败 → **不编造**、不放宽安全阈值；
返回结构显式携带 ``available=False`` 与原因，调用方据此标注缺失。

**纯 IO 边界**：本模块是唯一与和风网络交互处；上层（environment service）负责落库与业务。

**响应格式（v1 新版）**：和风开放平台已切换新版 API 规范，**不再返回顶层 ``code`` 字段**：
- 成功：响应体只含业务数据 + ``metadata``（``tag`` / ``attributions``）；
- 失败：HTTP 非 2xx，或响应体含 ``error``（``{status, title, detail}``）。
故 ``_parse`` **以是否含 ``metadata``/业务字段判成功**，不再依赖 ``code=="200"``。
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings

logger = logging.getLogger("hs.adapters.weather")

# Token 提前重签量（秒）：避免边界过期导致请求失败
_TOKEN_REFRESH_MARGIN_SEC = 60


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass(slots=True)
class WeatherResult:
    """天气查询结果（统一可用性语义）。"""

    available: bool
    data: dict = field(default_factory=dict)
    error: str | None = None   # 失败原因（人类可读；**不含密钥/私钥内容**）
    http_status: int | None = None

    @property
    def code(self) -> str | None:
        """兼容旧读取方：返回成功/错误码语义字符串（v1 无 code 字段，用可用性映射）。"""
        if self.available:
            return "200"
        err = self.data.get("error") if isinstance(self.data, dict) else None
        if isinstance(err, dict) and err.get("status") is not None:
            return str(err.get("status"))
        return None


@dataclass(slots=True)
class LocationInfo:
    """地点解析结果（E21）。"""

    location_id: str
    name: str
    lat: str
    lon: str
    adm1: str = ""
    adm2: str = ""


class WeatherAdapter:
    """和风天气适配器：JWT 认证 + 地点解析 + 实时/预报。"""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._token: str | None = None
        self._token_exp: float = 0.0

    # ------------------------------------------------------------
    # JWT 签名（EdDSA）
    # ------------------------------------------------------------
    def _private_key_path(self) -> Path:
        return self._settings.qweather_private_key_path

    def build_jwt(self) -> str:
        """按和风规范生成 JWT（EdDSA；header 仅 alg/kid，无 typ）。"""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key_path = self._private_key_path()
        if not key_path.exists():
            raise FileNotFoundError(f"和风天气私钥不存在：{key_path}")

        try:
            private_key = load_pem_private_key(key_path.read_bytes(), password=None)
        except Exception as exc:  # noqa: BLE001 — 不回显私钥内容
            raise ValueError(f"和风天气私钥加载失败（{type(exc).__name__}）") from exc

        now = int(time.time())
        ttl = self._settings.qweather_jwt_ttl_sec
        header = {"alg": "EdDSA", "kid": self._settings.qweather_jwt_kid}
        payload = {
            "iss": self._settings.qweather_jwt_iss,
            "sub": self._settings.qweather_jwt_sub,
            "iat": now - 30,
            "exp": now + ttl,
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(payload, separators=(",", ":")).encode())
        )
        signature = _b64url(private_key.sign(signing_input.encode()))
        return f"{signing_input}.{signature}"

    def _auth_token(self) -> str:
        """取缓存 token；临近过期（或未生成）时重签。"""
        now = time.time()
        if self._token is None or now >= self._token_exp - _TOKEN_REFRESH_MARGIN_SEC:
            self._token = self.build_jwt()
            self._token_exp = now + self._settings.qweather_jwt_ttl_sec
            logger.info("和风天气 JWT 已刷新（ttl=%ss）", self._settings.qweather_jwt_ttl_sec)
        return self._token

    # ------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------
    def _request(self, path: str, params: dict[str, str] | None = None) -> WeatherResult:
        """发起带 JWT 的 GET；失败返回 ``available=False`` + 原因（**不抛**，便于降级）。"""
        host = self._settings.qweather_api_host
        url = f"https://{host}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        try:
            token = self._auth_token()
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("和风天气认证准备失败：%s", exc)
            return WeatherResult(available=False, error=str(exc))

        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return self._parse(resp.read(), getattr(resp, "status", 200))
        except urllib.error.HTTPError as e:
            body = self._read_body(e)
            # 401 等鉴权失败：明确标记，不编造数据
            return WeatherResult(
                available=False,
                error=f"HTTP {e.code}（{self._auth_hint(e.code)}）",
                http_status=e.code,
                data=body,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return WeatherResult(available=False, error=f"网络失败（{type(exc).__name__}）")

    @staticmethod
    def _read_body(err: urllib.error.HTTPError) -> dict:
        try:
            raw = err.read()
            with contextlib.suppress(OSError):
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _auth_hint(status: int) -> str:
        if status == 401:
            return (
                "鉴权失败：检查公钥是否已上传控制台、"
                "凭据认证方式是否为 JWT、kid/iss/sub 是否一致"
            )
        if status == 403:
            return "无权限：检查凭据是否启用、套餐是否包含该 API"
        return "请求失败"

    def _parse(self, raw: bytes, status: int) -> WeatherResult:
        with contextlib.suppress(OSError):
            raw = gzip.decompress(raw)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return WeatherResult(available=False, error="响应非 JSON", http_status=status)

        if not isinstance(data, dict):
            return WeatherResult(available=False, error="响应结构异常", http_status=status)

        # v1 新版：失败体形如 {"error": {"status": 401, "title": ..., "detail": ...}}
        err = data.get("error")
        if isinstance(err, dict):
            raw_code = err.get("status")
            try:
                code_int = int(raw_code) if raw_code is not None else None
            except (TypeError, ValueError):
                code_int = None
            detail = err.get("detail") or err.get("title") or ""
            hint = self._auth_hint(code_int) if code_int in (401, 403) else ""
            msg = f"和风返回错误 status={raw_code}"
            if detail:
                msg += f"（{detail}）"
            if hint:
                msg += f"；{hint}"
            return WeatherResult(
                available=False,
                error=msg,
                http_status=status,
                data=data,
            )

        # 兼容旧版：若出现 code 字段且非 200，则视为失败
        if "code" in data and str(data.get("code")) != "200":
            return WeatherResult(
                available=False,
                error=f"和风返回 code={data.get('code')}",
                http_status=status,
                data=data,
            )

        # v1 成功：含 metadata 或任意业务字段
        if status >= 400:
            return WeatherResult(
                available=False,
                error=f"HTTP {status}",
                http_status=status,
                data=data,
            )
        return WeatherResult(available=True, data=data, http_status=status)

    # ------------------------------------------------------------
    # 业务端点（§3.1.1）
    # ------------------------------------------------------------
    def lookup_city(self, location: str, *, adm: str = "", number: int = 3) -> list[LocationInfo]:
        """城市/地点解析（``/geo/v2/city/lookup``）。失败 → 空列表（不编造）。"""
        params = {"location": location, "range": "cn", "number": str(number)}
        if adm:
            params["adm"] = adm
        res = self._request("/geo/v2/city/lookup", params)
        if not res.available:
            logger.warning("和风地点解析失败：%s", res.error)
            return []
        return [
            LocationInfo(
                location_id=str(loc.get("id", "")),
                name=str(loc.get("name", "")),
                lat=str(loc.get("lat", "")),
                lon=str(loc.get("lon", "")),
                adm1=str(loc.get("adm1", "")),
                adm2=str(loc.get("adm2", "")),
            )
            for loc in res.data.get("location", [])
        ]

    def current(self, lat: str, lon: str) -> WeatherResult:
        """实时天气（``/weather/v1/current/{lat}/{lon}``）。"""
        return self._request(f"/weather/v1/current/{lat}/{lon}")

    def daily(self, lat: str, lon: str, *, days: int = 7) -> WeatherResult:
        """逐日预报（``/weather/v1/daily/{lat}/{lon}``）。"""
        return self._request(f"/weather/v1/daily/{lat}/{lon}", {"days": str(days)})

    # ------------------------------------------------------------
    # 训练地点固化（E21，T2.1b）
    # ------------------------------------------------------------
    def resolve_training_location(self, location: str | None = None) -> LocationInfo | None:
        """解析训练地点为 ``LocationInfo``（默认取配置的训练区域，E21）。

        返回 ``None`` 表示解析失败（**不编造坐标**）。
        """
        target = location or self._settings.qweather_location
        # 若配置已缓存解析结果，直接复用
        s = self._settings
        if (
            location is None
            and s.qweather_location_id
            and s.qweather_lat
            and s.qweather_lon
        ):
            return LocationInfo(
                location_id=s.qweather_location_id,
                name=target,
                lat=s.qweather_lat,
                lon=s.qweather_lon,
            )

        candidates = self.lookup_city(target, adm="安庆", number=3)
        if not candidates:
            return None
        # 优先精确匹配名称，否则取第一条
        exact = [c for c in candidates if target.endswith(c.name)]
        return exact[0] if exact else candidates[0]