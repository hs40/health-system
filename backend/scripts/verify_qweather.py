#!/usr/bin/env python3
"""和风天气 JWT 连通性验证（E20 / E22）。

用途：公钥上传到和风天气控制台后，一键验证 JWT 认证与 API 连通。

用法：
    cd backend
    ./.venv/Scripts/python.exe scripts/verify_qweather.py

依赖：cryptography（已随 PyJWT[crypto] 安装）。
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# —— 凭据（E22）——
API_HOST = "mh5eggpt58.re.qweatherapi.com"
KID = "KHPN5E55QA"              # 凭据 ID
ISS = "Q17D5568C6"              # 开发者 ID
SUB = "4KTHBX5GGE"              # 项目 ID
LOCATION = "安徽省安庆市宜秀区"    # E21

_PRIVATE_KEY = Path(__file__).resolve().parents[2] / "secrets" / "qweather_ed25519_private.pem"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def build_jwt(ttl_sec: int = 900) -> str:
    """按和风官方规范生成 JWT：EdDSA 签名，header 仅 {alg,kid}（无 typ）。"""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    private_key = load_pem_private_key(_PRIVATE_KEY.read_bytes(), password=None)
    now = int(time.time())
    header = {"alg": "EdDSA", "kid": KID}
    payload = {"iss": ISS, "sub": SUB, "iat": now - 30, "exp": now + ttl_sec}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    signature = _b64url(private_key.sign(signing_input.encode()))
    return f"{signing_input}.{signature}"


def call(path: str, params: dict[str, str], token: str) -> dict:
    """调用和风 API（自动解 gzip）。"""
    url = f"https://{API_HOST}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            with contextlib.suppress(OSError):
                raw = gzip.decompress(raw)
            return json.loads(raw.decode())
    except urllib.error.HTTPError as e:
        raw = e.read()
        with contextlib.suppress(OSError):
            raw = gzip.decompress(raw)
        return {"_http_status": e.code, "_body": raw.decode("utf-8", "replace")}


def _ok(resp: dict) -> bool:
    """v1 新版成功判定：无 error 字段即为成功（成功体含 metadata）。"""
    return "_http_status" not in resp and "error" not in resp


def main() -> int:
    if not _PRIVATE_KEY.exists():
        print(f"[FAIL] 私钥不存在: {_PRIVATE_KEY}")
        return 1

    token = build_jwt()
    print(f"[OK] JWT 生成成功（长度 {len(token)}）")

    # 1) 城市解析（宜秀区）
    geo = call(
        "/geo/v2/city/lookup",
        {"location": LOCATION, "adm": "安庆", "range": "cn", "number": "3"},
        token,
    )
    if _ok(geo):
        print("[OK] 城市解析成功")
        for loc in geo.get("location", [])[:3]:
            print(
                f"     {loc.get('name')} id={loc.get('id')} "
                f"lat={loc.get('lat')} lon={loc.get('lon')} "
                f"{loc.get('adm1')}/{loc.get('adm2')}"
            )
    else:
        print(f"[FAIL] 城市解析失败: {geo}")
        print()
        print("排查提示：")
        print("  1) 公钥是否已上传到 https://console.qweather.com/project 的凭据？")
        print("  2) 凭据的身份认证方式是否选为 JSON Web Token？")
        print("  3) 凭据 ID(kid)/开发者ID(iss)/项目ID(sub)/API Host 是否一致？")
        return 2

    # 2) 实时天气
    first = (geo.get("location") or [{}])[0]
    lat, lon = first.get("lat"), first.get("lon")
    if lat and lon:
        now = call(f"/weather/v1/current/{lat}/{lon}", {}, token)
        if _ok(now):
            cond = (now.get("condition") or {}).get("text")
            temp = (now.get("temperature") or {}).get("value")
            print(f"[OK] 实时天气: {cond} {temp}°C")
        else:
            print(f"[FAIL] 实时天气: {now}")

        daily = call(f"/weather/v1/daily/{lat}/{lon}", {"days": "7"}, token)
        if _ok(daily):
            days = daily.get("days", [])
            print(f"[OK] 逐日预报: days={len(days)}")
            for d in days[:3]:
                tmax = (d.get("temperatureMax") or {}).get("value")
                ctext = ((d.get("daytime") or {}).get("condition") or {}).get("text")
                print(f"     {d.get('forecastStartTime')} {ctext} 最高{tmax}°C")
        else:
            print(f"[FAIL] 逐日预报: {daily}")

    print("\n[完成] 和风天气接入可用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
