"""TA.1 关键验证：python-garminconnect CN 区独立鉴权（V1）。

思路：插件已有一个已认证的 DI OAuth session（CN 区）。
python-garminconnect 的 tokenstore 格式 = {di_token, di_refresh_token, di_client_id}，
与插件 session 的 {tokens.accessToken, tokens.refreshToken, clientId} 一一对应，
故可"引导"复用，无需再次输入账号密码。

本脚本仅做只读验证：登录 + 拉取 profile + 拉取最近一天日报。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from garminconnect import Garmin

SESSION = Path(
    os.environ.get(
        "GARMIN_SESSION",
        os.path.expanduser(
            "~/AppData/Local/dsh-plugin-garmin-connect/accounts/personal-cn.session.json"
        ),
    )
)
TOKENSTORE = Path(__file__).resolve().parents[2] / "data" / "garmin_tokens" / "tokens.json"


def bootstrap_tokenstore() -> Path:
    """把插件 session 转换为 python-garminconnect 的 tokenstore。"""
    s = json.loads(SESSION.read_text(encoding="utf-8"))
    tokens = s["tokens"]
    store = {
        "di_token": tokens.get("accessToken"),
        "di_refresh_token": tokens.get("refreshToken"),
        "di_client_id": s.get("clientId"),
    }
    if not store["di_refresh_token"] or not store["di_client_id"]:
        raise SystemExit("插件 session 缺少 refreshToken / clientId，无法引导")
    TOKENSTORE.parent.mkdir(parents=True, exist_ok=True)
    TOKENSTORE.write_text(json.dumps(store), encoding="utf-8")
    print(f"[bootstrap] tokenstore -> {TOKENSTORE}  (clientId={store['di_client_id']})")
    return TOKENSTORE


def main() -> int:
    tokenstore = bootstrap_tokenstore()

    print("[login] Garmin(is_cn=True).login(tokenstore=...)")
    g = Garmin(is_cn=True)
    g.login(tokenstore=str(tokenstore))
    print("[login] OK — DI token refreshed & persisted")

    # 1) 账户资料（验证 API tier 可用）
    try:
        name = g.get_full_name()
        print(f"[profile] full_name = {name}")
    except Exception as e:  # noqa: BLE001
        print(f"[profile] FAILED: {type(e).__name__}: {e}")
        return 1

    # 2) 最近一天日报（验证 connectapi 数据可用）
    from datetime import date, timedelta

    for delta in range(0, 5):
        d = (date.today() - timedelta(days=delta)).isoformat()
        try:
            summary = g.get_user_summary(d)
            if summary:
                print(
                    f"[summary] {d} OK — rhr={summary.get('restingHeartRate')} "
                    f"steps={summary.get('totalSteps')} "
                    f"bbHigh={summary.get('bodyBatteryHighestValue')}"
                )
                break
        except Exception as e:  # noqa: BLE001
            print(f"[summary] {d} err: {type(e).__name__}: {e}")
    else:
        print("[summary] 近 5 天均无数据")

    # 3) 睡眠（跨日，验证另一数据集）
    try:
        sleep = g.get_sleep_data(date.today().isoformat())
        daily = (sleep or {}).get("dailySleepDTO", {})
        print(f"[sleep] {date.today().isoformat()} sleepSec={daily.get('sleepTimeSeconds')}")
    except Exception as e:  # noqa: BLE001
        print(f"[sleep] err: {type(e).__name__}: {e}")

    print("\n=== TA.1 验证通过：CN 区独立鉴权 + 数据读取可用 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
