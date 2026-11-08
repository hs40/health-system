"""逐秒心率明细补齐（v3.4.5）。

背景：``fit_jsonl/<aid>.jsonl.gz`` 历来由本机 ``scripts/parse_fit.py`` 生成，
再经 ``pack_data.sh`` 人工搬到 NAS。容器镜像只 ``COPY backend/app``
（不含 scripts），scheduler 也没有解析 job。本机 daily 自 2026-10-26 22:30
停跑后，NAS 每天新增的 activities 永远没有对应 jsonl.gz —— 训练详情
``hr_data_state`` 恒为 ``fit_missing``，两张心率图一直占位。

本模块让容器自足：发现缺失 → 取 .fit → 解析 → 写 gz。解析引擎
``app/parsers/fit.py`` 与依赖 ``garmin-fit-sdk`` / ``fitdecode`` 本就在镜像里，
``_index.json`` 对读取方并非必需（``load_fit_messages`` 只看 ``<aid>.jsonl.gz``）。

原则：只补空、幂等、单条失败不阻断其余。Q2 已授权容器无人值守从 Garmin
拉取个人健康数据；运维脚本仍不搬运 raw。

告警（Q4）：逐条失败写 ``_backfill_reports/<date>.json``，汇总进消息中心（同日
去重），钉钉只在「整批失败 / Garmin 登录失效 / 连续 N 天仍有缺口」时报，避免刷屏。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.parsers.fit import ParsedFit, parse_fit, write_jsonl_gz
from app.storage.activity_source import ActivityRef, load_activities

logger = logging.getLogger("hs.fit_backfill")

# 消息中心去重用的 ref_type（同日同一结论只发一条，避免重复跑刷屏）。
ENTITY_REF = "fitBackfill"
# 连续 N 天仍存在缺口才升级到钉钉（Q4：日常单条失败只进消息中心）。
PERSISTENT_GAP_DAYS = 3
# 逐条失败 ledger 落在 garmin_root 下这个子目录。
LEDGER_DIR = "_backfill_reports"


def write_parsed_fit(garmin_root: Path, parsed: ParsedFit) -> int:
    """把解析结果写到 ``fit_jsonl/<aid>.jsonl.gz``，返回写出行数。

    只补空：目标已存在则直接返回 0、不覆盖（幂等）。
    """
    out = Path(garmin_root) / "fit_jsonl" / f"{parsed.activity_id}.jsonl.gz"
    if out.exists():
        return 0
    return write_jsonl_gz(parsed, out)


@dataclass(slots=True)
class BackfillResult:
    """逐条补齐结果：只补空、失败隔离，供上层记 ledger / 告警。"""

    ok: list[str] = field(default_factory=list)
    no_fit: list[str] = field(default_factory=list)
    parse_failed: list[str] = field(default_factory=list)
    skipped: int = 0  # 已有 gz、本次不处理的活动数
    failures: dict[str, str] = field(default_factory=dict)  # aid -> 原因（写 ledger）

    @property
    def attempted(self) -> int:
        return len(self.ok) + len(self.no_fit) + len(self.parse_failed)


def missing_fit_ids(
    garmin_root: Path, refs: Mapping[str, ActivityRef]
) -> list[str]:
    """缺 ``fit_jsonl/<aid>.jsonl.gz`` 的 aid，按 aid 排序（纯函数）。"""
    d = Path(garmin_root) / "fit_jsonl"
    return sorted(aid for aid in refs if not (d / f"{aid}.jsonl.gz").exists())


def backfill_missing(
    garmin_root: Path,
    refs: Mapping[str, ActivityRef],
    fetcher: Callable[[str], bytes | None],
    *,
    parse: Callable[[Path], ParsedFit] | None = None,
) -> BackfillResult:
    """发现缺失 → 取 .fit → 解析 → 写 gz。逐条隔离，不向调用方抛异常。

    ``parse`` 缺省用 ``app.parsers.fit.parse_fit``（镜像内已有）；测试注入 fake，
    避免测试依赖真实 FIT 二进制与 Garmin 网络。
    """
    root = Path(garmin_root)
    parser = parse or parse_fit
    missing = missing_fit_ids(root, refs)
    result = BackfillResult(skipped=len(refs) - len(missing))
    fit_dir = root / "fit"

    for aid in missing:
        try:
            blob = fetcher(aid)
        except Exception as e:  # noqa: BLE001 — 单条失败不阻断其余
            reason = f"取 FIT 异常 {type(e).__name__}: {e}"
            logger.warning("取 FIT 失败 %s: %s", aid, e)
            result.no_fit.append(aid)
            result.failures[aid] = reason
            continue
        if not blob:
            result.no_fit.append(aid)
            result.failures[aid] = "Garmin 未返回 FIT（下载失败或该活动无 FIT）"
            continue
        fit_path = fit_dir / f"{aid}.fit"
        try:
            if not fit_path.exists():  # append-only，绝不覆盖已有 .fit
                fit_path.parent.mkdir(parents=True, exist_ok=True)
                fit_path.write_bytes(blob)
            parsed = parser(fit_path)
            write_parsed_fit(root, parsed)
        except Exception as e:  # noqa: BLE001 — 单条失败不阻断其余
            reason = f"解析/写入失败 {type(e).__name__}: {e}"
            logger.warning("解析/写入 FIT 失败 %s: %s", aid, e)
            result.parse_failed.append(aid)
            result.failures[aid] = reason
            continue
        result.ok.append(aid)
    return result


# ============================================================
# Q4 告警：逐条 ledger + 消息中心（同日去重）+ 钉钉（只在严重情形）
# ============================================================
def write_ledger(garmin_root: Path, summary: dict, today: dt.date) -> Path:
    """把本次逐条结果落到 ``_backfill_reports/<date>.json``，返回文件路径。

    逐条失败原因可查；``dingtalk_reasons`` 也靠历史 ledger 判断「连续缺口」。
    """
    d = Path(garmin_root) / LEDGER_DIR
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{today.isoformat()}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def build_alert(summary: dict, today: dt.date) -> dict | None:
    """汇总成一条消息中心告警；全绿（无失败）返回 ``None``。

    ``ref_id`` 按天定界，供 ``_send_message_center`` 同日去重，避免重复跑刷屏。
    """
    no_fit = summary.get("no_fit") or []
    parse_failed = summary.get("parse_failed") or []
    failures = summary.get("failures") or {}
    if not (no_fit or parse_failed or failures):
        return None
    reasons: list[str] = []
    if no_fit:
        reasons.append(f"缺 FIT {len(no_fit)} 条")
    if parse_failed:
        reasons.append(f"解析失败 {len(parse_failed)} 条")
    if failures:
        reasons.append(f"失败明细 {len(failures)} 条（见 {LEDGER_DIR}/）")
    return {
        "title": "逐秒心率明细补齐异常",
        "body": "；".join(reasons) + "。请查看运维台与容器日志。",
        "ref_type": ENTITY_REF,
        "ref_id": f"{ENTITY_REF}:{today.isoformat()}",
        "link": "/ops",
        "importance": "high",
    }


def _has_gap(summary: dict) -> bool:
    """该日 ledger 是否仍存在缺口（供连续缺口判断）。"""
    return bool(
        summary.get("no_fit")
        or summary.get("parse_failed")
        or summary.get("failures")
    )


def dingtalk_reasons(
    summary: dict, ledger_dir: Path, today: dt.date
) -> list[str]:
    """返回需要推送钉钉的原因；日常单条失败返回空列表（Q4：不刷屏）。

    只在三种严重情形上报：
    1. 整批失败（有尝试且一条都没补齐）；
    2. Garmin 登录失效（failure 含 401/Unauthorized/登录/认证 等）；
    3. 连续 ``PERSISTENT_GAP_DAYS`` 天 ledger 仍有缺口。
    """
    reasons: list[str] = []
    filled = summary.get("filled") or []
    no_fit = summary.get("no_fit") or []
    parse_failed = summary.get("parse_failed") or []
    failures = summary.get("failures") or {}
    attempted = len(filled) + len(no_fit) + len(parse_failed)

    # 1) 整批失败
    if attempted > 0 and not filled:
        reasons.append("整批补齐失败")

    # 2) 登录失效
    auth_keys = (
        "401", "403", "unauthorized", "forbidden", "登录", "认证", "重新授权",
        "token expired", "credential",
    )
    if any(k in str(v).lower() for v in failures.values() for k in auth_keys):
        reasons.append("Garmin 登录失效，请重新授权")

    # 3) 连续缺口：today 及之前连续 N 天的 ledger 都存在缺口
    consecutive = 0
    for i in range(PERSISTENT_GAP_DAYS):
        p = Path(ledger_dir) / f"{(today - dt.timedelta(days=i)).isoformat()}.json"
        if not p.exists():
            break
        try:
            day_summary = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            break
        if not _has_gap(day_summary):
            break
        consecutive += 1
    if consecutive >= PERSISTENT_GAP_DAYS:
        reasons.append(f"连续 {PERSISTENT_GAP_DAYS} 天仍存在逐秒心率缺口")

    return reasons


def _send_message_center(alert: dict) -> None:
    """把告警落进消息中心；同日同 ref_id 去重；失败只记日志、不抛。"""
    try:
        from sqlmodel import select

        from app.db.models import MessageRow
        from app.db.session import session_scope
        from app.schemas.enums import MessageCategory, MessageImportance
        from app.services.notify_service import send_message

        with session_scope() as session:
            exists = session.exec(
                select(MessageRow).where(
                    MessageRow.ref_type == alert["ref_type"],
                    MessageRow.ref_id == alert["ref_id"],
                )
            ).first()
            if exists is not None:
                return
            send_message(
                session,
                category=MessageCategory.NEED_ACTION,
                title=alert["title"],
                body=alert["body"],
                ref_type=alert["ref_type"],
                ref_id=alert["ref_id"],
                link=alert["link"],
                importance=MessageImportance(alert.get("importance", "normal")),
            )
    except Exception as e:  # noqa: BLE001 — 告警失败不阻断补齐主链路
        logger.warning("写消息中心告警失败：%s", e)


def _send_dingtalk(settings: Settings, reasons: list[str]) -> None:
    """把严重原因推到钉钉；未配置 webhook 则跳过；失败只记日志、不抛。"""
    try:
        from app.adapters.dingtalk import DingTalkClient

        client = DingTalkClient(settings)
        if not client.enabled:
            return
        text = "\n".join(f"- {r}" for r in reasons)
        client.send_markdown("逐秒心率补齐告警", f"### 逐秒心率补齐告警\n{text}")
    except Exception as e:  # noqa: BLE001 — 告警失败不阻断补齐主链路
        logger.warning("推钉钉告警失败：%s", e)


def run_backfill(
    settings: Settings,
    *,
    fetcher: Callable[[str], bytes | None] | None = None,
    today: dt.date | None = None,
) -> dict:
    """编排入口：补齐 → 写 ledger → 消息中心 → 钉钉，返回结构化结果。

    ``fetcher`` / ``today`` 供测试注入，缺省时构造真实 Garmin fetcher 与当天日期。
    告警失败不抛，保证「容器自己补齐」这条主链路始终可跑通、可被调度器与部署 CLI 复用。
    """
    if fetcher is None:
        from app.adapters.garmin import GarminAdapter
        from app.services.tredict_sync_service import default_fit_fetcher

        fetcher = default_fit_fetcher(GarminAdapter(settings))
    day = today or dt.date.today()

    garmin_root = Path(settings.raw_data_dir) / "garmin"
    refs = load_activities(garmin_root)
    result = backfill_missing(garmin_root, refs, fetcher)
    summary = {
        "ok": not result.failures,
        "filled": result.ok,
        "no_fit": result.no_fit,
        "parse_failed": result.parse_failed,
        "skipped": result.skipped,
        "failures": result.failures,
    }

    ledger = write_ledger(garmin_root, summary, day)
    alert = build_alert(summary, day)
    if alert is not None:
        _send_message_center(alert)
    reasons = dingtalk_reasons(summary, ledger.parent, day)
    if reasons:
        _send_dingtalk(settings, reasons)
    return summary


__all__ = [
    "BackfillResult",
    "ENTITY_REF",
    "LEDGER_DIR",
    "PERSISTENT_GAP_DAYS",
    "backfill_missing",
    "build_alert",
    "dingtalk_reasons",
    "missing_fit_ids",
    "run_backfill",
    "write_ledger",
    "write_parsed_fit",
]


def main() -> None:
    """CLI 入口：``python -m app.services.fit_backfill``。

    部署脚本在容器启动后调用，立即补齐一次，不必等到当晚 23:30 的调度任务。
    结果以 JSON 打到 stdout 便于部署日志核对；异常不阻断部署（脚本兜底）。
    """
    import json

    from app.config import get_settings

    print(json.dumps(run_backfill(get_settings()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
