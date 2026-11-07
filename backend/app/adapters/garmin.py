"""Garmin 适配器（TA.1/TA.2/TA.11）：鉴权、同步、标准化、课程工作流。

鉴权策略（V1 已验证）：
- python-garminconnect 0.3.17 的 tokenstore 格式为 ``{di_token, di_refresh_token, di_client_id}``。
- 插件 ``garmin-connect-plugin-for-dsh`` 的 CN 区 session 为
  ``{tokens.accessToken, tokens.refreshToken, clientId}``，与前者一一对应。
- 因此可用插件 session **引导** tokenstore，无需账号密码；之后由
  python-garminconnect 自动做 DI OAuth 刷新（同源流程，指向 diauth.garmin.cn）。

原始数据保护（REQ-DATA-001）：
- 原始抓取结果按 ``raw/garmin/<dataset>/<YYYY-MM-DD>.json`` 落盘；
- 文件已存在时**不覆盖**（首次写入即视为权威快照），保证可回溯。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings
from app.domain import load as load_domain
from app.domain.load import compute_day
from app.errors import AppError
from app.schemas.domain import RawDailyRef, StandardDaily
from app.schemas.enums import ErrorCode
from app.storage.activity_source import load_laps

logger = logging.getLogger("hs.adapters.garmin")

# 需要同步的数据集（与现有 garmin-sync.js 主题对齐，便于 V4 对拍）
DATASETS: tuple[str, ...] = (
    "daily_summary",
    "sleep",
    "hrv",
    "heart_rate",
    "body_battery",
    "stress",
    "spo2",
    "respiration",
    # 2026-10-21 补齐：体重此前从未进入同步清单，导致手动录入的称重永远拉不到
    "weight",
    "lifestyle_logging",  # 生活方式打卡（Garmin「习惯」）
    "hydration",  # 饮水
    # 2026-10-21 决策 Q4：个人健康/训练数据全集（按日）
    "training_readiness",
    "training_status",
    "max_metrics",
    "fitness_age",
    "endurance_score",
    "hill_score",
    "rhr_daily",
    "steps_data",
    "floors",
    "intensity_minutes",
    "calories_daily",
    "all_day_events",
    "activities",  # 负荷来源（按日）；休息日为空属正常
)

# ``standardize()`` 实际取值的来源数据集（用于 ``source_refs`` 可回溯，REQ-DATA-004）。
# 注意：heart_rate / body_battery / stress 虽被同步，但**不参与** StandardDaily 取值，故不列入。
_STANDARDIZE_SOURCES: tuple[str, ...] = (
    "daily_summary",
    "sleep",
    "hrv",
    "spo2",
    "respiration",
    "weight",
)

# 可选数据集：空载荷属**正常**（如休息日无活动 / 当天没称重 / 没打卡 / 无训练准备度），
# 不计入 missing / 不判 partial。
OPTIONAL_DATASETS: frozenset[str] = frozenset(
    {
        "activities",
        "weight",
        "lifestyle_logging",
        "hydration",
        "training_readiness",
        "training_status",
        "max_metrics",
        "fitness_age",
        "endurance_score",
        "hill_score",
        "rhr_daily",
        "steps_data",
        "floors",
        "intensity_minutes",
        "calories_daily",
        "all_day_events",
    }
)

# 原始层「滚动窗口」（REQ-DATA-001 修订，2026-10-19）：
# 最近 N 天允许被云端覆盖（旧版存 ``_superseded/``），更早的日期**冻结**。
#
# 为什么是 5（实测结论，2026-10-19）：
# - 抽查 2025-08-01 / 2025-11-15 / 2026-03-10 / 2026-07-20 / 2026-09-25，
#   历史日期与云端**逐字段完全一致** —— 历史数据是稳定的；
# - 差异全部落在最近几天（实测 10-13~10-18 共 6 天有差异），且本地值一律**偏小**
#   （如 totalSteps 37 vs 7180），说明是「当天没结束就拉、之后被 append-only 冻结」；
# - 窗口取 5 天：覆盖「当天 + 移动端延迟回填的若干天」，留出安全余量，
#   同时不必每晚重拉整周（5 天 × 9 数据集 = 45 请求，约 30 秒）。
RAW_REFRESH_WINDOW_DAYS = 5

@dataclass(slots=True)
class GarminSyncResult:
    """单日同步结果（用于任务状态机判定 partial / success）。"""

    date: dt.date
    fetched: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # 已存在，未覆盖
    missing: list[str] = field(default_factory=list)  # 端上无该日数据
    errors: dict[str, str] = field(default_factory=dict)
    superseded: list[str] = field(default_factory=list)  # 窗口内被覆盖（旧版已留痕）
    frozen: list[str] = field(default_factory=list)  # 超出窗口，历史冻结未覆盖

    @property
    def ok(self) -> bool:
        return not self.errors and not self.missing

    @property
    def status(self) -> str:
        if self.errors and not self.written:
            return "failed"
        if self.errors or self.missing:
            return "partial"
        return "success"


def _default_plugin_session() -> Path:
    return Path(
        os.environ.get(
            "GARMIN_SESSION",
            os.path.expanduser(
                "~/AppData/Local/dsh-plugin-garmin-connect/accounts/personal-cn.session.json"
            ),
        )
    )


def _merge_activities(old: Any, new: Any) -> Any:
    """按 ``activityId`` 合并两批活动（新覆盖旧、旧中缺失的保留）。

    为什么需要合并而不是替换：同一天可能多次运动（上午跑 + 晚上力量），首次同步时
    可能只有 1 条；再同步时 Garmin 返回 2 条，必须补齐。反之，Garmin 某次只返回部分
    活动时，也绝不能因此丢掉本地已有活动。故取并集（按 id 去重，新值覆盖旧值）。

    ``new`` 非列表（异常/测试桩）时原样返回，保持 ``sync_day`` 的容错语义。
    """
    if not isinstance(new, list):
        return new
    if not isinstance(old, list) or not old:
        return new

    merged: dict[str, Any] = {}
    for item in old:
        if isinstance(item, dict) and item.get("activityId") is not None:
            merged[str(item["activityId"])] = item
    for item in new:
        if isinstance(item, dict) and item.get("activityId") is not None:
            merged[str(item["activityId"])] = item
    return list(merged.values())


class GarminAdapter:
    """Garmin Connect 适配器（CN 区）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.raw_root = Path(settings.raw_data_dir) / "garmin"
        self.tokenstore = Path(settings.garmin_tokenstore) / "tokens.json"
        self._client: Any | None = None
        # 力量 TRIMP 用的心率区段缓存：一次重算会反复查同一批活动，逐条查库太慢
        self._zone_cache: dict[str, dict[str, Any] | None] | None = None

    # ------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------
    def bootstrap_tokenstore(self) -> None:
        """若 tokenstore 不存在，则从插件 session 引导生成。"""
        if self.tokenstore.exists():
            return
        session_path = _default_plugin_session()
        if not session_path.exists():
            raise AppError(
                ErrorCode.EXTERNAL_UNKNOWN,
                "Garmin 鉴权不可用：既无 tokenstore，也未找到插件 session",
                {"session": str(session_path)},
            )
        s = json.loads(session_path.read_text(encoding="utf-8"))
        tokens = s.get("tokens", {})
        store = {
            "di_token": tokens.get("accessToken"),
            "di_refresh_token": tokens.get("refreshToken"),
            "di_client_id": s.get("clientId"),
        }
        if not store["di_refresh_token"] or not store["di_client_id"]:
            raise AppError(ErrorCode.EXTERNAL_UNKNOWN, "插件 session 缺少 refreshToken / clientId")
        self.tokenstore.parent.mkdir(parents=True, exist_ok=True)
        self.tokenstore.write_text(json.dumps(store), encoding="utf-8")
        logger.info("已从插件 session 引导 Garmin tokenstore: %s", self.tokenstore)

    def client(self) -> Any:
        """惰性登录并缓存客户端。"""
        if self._client is not None:
            return self._client
        from garminconnect import Garmin

        self.bootstrap_tokenstore()
        g = Garmin(
            email=self.settings.garmin_email,
            password=(
                self.settings.garmin_password.get_secret_value()
                if self.settings.garmin_password
                else None
            ),
            is_cn=self.settings.garmin_is_cn,
        )
        try:
            g.login(tokenstore=str(self.tokenstore))
        except Exception as e:  # noqa: BLE001 — 归一化为统一错误码
            raise AppError(
                ErrorCode.EXTERNAL_UNKNOWN, f"Garmin 登录失败：{e}", {"type": type(e).__name__}
            ) from e
        self._client = g
        return g

    # ------------------------------------------------------------
    # 原始数据落盘（只读保护）
    # ------------------------------------------------------------
    def _raw_path(self, dataset: str, day: dt.date) -> Path:
        return self.raw_root / dataset / f"{day.isoformat()}.json"

    def _superseded_path(self, dataset: str, day: dt.date) -> Path:
        """被覆盖版本的留痕路径（同一文件多次覆盖会产生多个带时间戳的文件）。"""
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
        return self.raw_root / "_superseded" / dataset / f"{day.isoformat()}.{stamp}.json"

    def _write_payload(self, dataset: str, day: dt.date, payload: Any) -> None:
        p = self._raw_path(dataset, day)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _write_raw(self, dataset: str, day: dt.date, payload: Any) -> bool:
        """写入原始 JSON；已存在则不覆盖。返回是否新写入（严格 append-only）。"""
        if self._raw_path(dataset, day).exists():
            return False
        self._write_payload(dataset, day, payload)
        return True

    def refresh_raw(
        self,
        dataset: str,
        day: dt.date,
        payload: Any,
        *,
        window_days: int = RAW_REFRESH_WINDOW_DAYS,
        today: dt.date | None = None,
    ) -> tuple[str, Any | None]:
        """按「滚动窗口」写入：最近 ``window_days`` 天可覆盖，更早冻结。

        返回 ``(状态, 旧 payload)``，状态取值：

        - ``written``    首次写入（旧值为 ``None``）
        - ``superseded`` 窗口内覆盖（旧值已移入 ``_superseded/``）
        - ``frozen``     超出窗口，历史冻结，**不覆盖**
        - ``same``       窗口内但内容与旧值完全相同，不写盘

        返回旧 payload 是给「本地 ↔ Garmin 云端」校验用的：调用方可以先比再覆盖，
        不必为了比对再拉一次。
        """
        path = self._raw_path(dataset, day)
        if not path.exists():
            self._write_payload(dataset, day, payload)
            return "written", None

        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise AppError(
                ErrorCode.DATA_CORRUPT,
                f"原始数据文件损坏：{dataset}/{day.isoformat()}",
                {"path": str(path), "error": str(e)},
            ) from e

        cutoff = (today or dt.date.today()) - dt.timedelta(days=window_days - 1)
        if day < cutoff:
            return "frozen", old
        if old == payload:
            return "same", old

        dst = self._superseded_path(dataset, day)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)  # 留痕后再覆盖；copy 比 move 安全（写失败仍留原文件）
        self._write_payload(dataset, day, payload)
        return "superseded", old

    def read_raw(self, dataset: str, day: dt.date) -> Any | None:
        """读取原始 JSON。

        原始层为权威快照（REQ-DATA-001），**不自动修复/覆盖**；
        文件损坏时抛出结构化错误 ``E_DATA_CORRUPT``，由调用方决定是否人工介入。
        """
        p = self._raw_path(dataset, day)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise AppError(
                ErrorCode.DATA_CORRUPT,
                f"原始数据文件损坏：{dataset}/{day.isoformat()}",
                {"path": str(p), "error": str(e)},
            ) from e

    # ------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------
    def _fetch(self, g: Any, dataset: str, day: dt.date) -> Any:
        d = day.isoformat()
        if dataset == "daily_summary":
            return g.get_user_summary(d)
        if dataset == "sleep":
            return g.get_sleep_data(d)
        if dataset == "hrv":
            return g.get_hrv_data(d)
        if dataset == "heart_rate":
            return g.get_heart_rates(d)
        if dataset == "body_battery":
            return g.get_body_battery(d, d)
        if dataset == "stress":
            return g.get_stress_data(d)
        if dataset == "spo2":
            return g.get_spo2_data(d)
        if dataset == "respiration":
            return g.get_respiration_data(d)
        if dataset == "weight":
            # 与既有 weight/<date>.json 单条格式兼容：取当日首条称重记录；
            # 当天没称 → None（可选数据集，不计缺失）
            comp = g.get_body_composition(d, d) or {}
            records = comp.get("dateWeightList") or []
            return records[0] if records else None
        if dataset == "lifestyle_logging":
            return g.get_lifestyle_logging_data(d)
        if dataset == "hydration":
            return g.get_hydration_data(d)
        if dataset == "training_readiness":
            return g.get_training_readiness(d)
        if dataset == "training_status":
            return g.get_training_status(d)
        if dataset == "max_metrics":
            return g.get_max_metrics(d)
        if dataset == "fitness_age":
            return g.get_fitnessage_data(d)
        if dataset == "endurance_score":
            return g.get_endurance_score(d, d)
        if dataset == "hill_score":
            return g.get_hill_score(d, d)
        if dataset == "rhr_daily":
            return g.get_rhr_day(d)
        if dataset == "steps_data":
            return g.get_steps_data(d)
        if dataset == "floors":
            return g.get_floors(d)
        if dataset == "intensity_minutes":
            return g.get_intensity_minutes_data(d)
        if dataset == "calories_daily":
            return g.get_calories_daily(d, d)
        if dataset == "all_day_events":
            return g.get_all_day_events(d)
        if dataset == "activities":
            # 按日活动列表（含 duration / averageHR / activityType）；无活动返回 []
            return g.get_activities_by_date(d, d)
        raise AppError(ErrorCode.VALIDATION, f"未知数据集：{dataset}")

    def sync_day(
        self, day: dt.date, *, datasets: list[str] | tuple[str, ...] | None = None
    ) -> GarminSyncResult:
        """同步单日数据集；逐项容错，任一失败不影响其余。

        :param datasets: 只拉指定数据集（回补用）；``None`` = 全部 ``DATASETS``
        """
        result = GarminSyncResult(date=day)
        targets = list(datasets or DATASETS)
        try:
            g = self.client()
        except Exception as e:  # noqa: BLE001 — 认证/网络层失败：逐数据集记错，不中断流水线
            # 2026-10-27 真实故障：client() 位于循环之前，异常会直接穿透到 run_daily，
            # 导致 audit/raw/日报全部缺失且用户零感知。此处把异常吸收为「每个数据集
            # 同步失败」，使下游仍能走完并留下审计与披露。
            msg = f"{type(e).__name__}: {e}"
            for dataset in targets:
                result.errors[dataset] = msg
            logger.error(
                "Garmin 同步 %s: 客户端初始化失败，全部 %d 个数据集标记为失败 —— %s",
                day,
                len(targets),
                msg,
            )
            return result
        for dataset in targets:
            try:
                payload = self._fetch(g, dataset, day)
            except Exception as e:  # noqa: BLE001
                result.errors[dataset] = f"{type(e).__name__}: {e}"
                continue
            if payload is None or payload == {} or payload == []:
                # 可选数据集（如 activities）：休息日为空属正常，不计缺失
                if dataset in OPTIONAL_DATASETS:
                    result.fetched.append(dataset)
                else:
                    result.missing.append(dataset)
                continue
            result.fetched.append(dataset)
            if dataset == "activities":
                # 同一天可能多次运动（真实故障 2026-10-27）：首次写盘后新增的活动
                # 会被 append-only 永久挡掉。改为「按 activityId 合并 + 滚动窗口刷新」，
                # 既补齐新增活动，又不因 Garmin 少返回而丢本地已有活动。
                try:
                    merged = _merge_activities(self.read_raw("activities", day), payload)
                    state, _ = self.refresh_raw("activities", day, merged)
                except AppError as e:
                    result.errors[dataset] = f"{type(e).__name__}: {e}"
                    continue
                if state in ("written", "superseded"):
                    result.written.append(dataset)
                else:
                    result.skipped.append(dataset)
            elif self._write_raw(dataset, day, payload):
                result.written.append(dataset)
            else:
                result.skipped.append(dataset)
        logger.info(
            "Garmin 同步 %s: status=%s written=%d skipped=%d missing=%d errors=%d",
            day,
            result.status,
            len(result.written),
            len(result.skipped),
            len(result.missing),
            len(result.errors),
        )
        return result

    def refresh_day(
        self, day: dt.date, *, window_days: int = RAW_REFRESH_WINDOW_DAYS
    ) -> tuple[GarminSyncResult, dict[str, tuple[Any, Any]]]:
        """重拉单日并按**滚动窗口**覆盖；返回 ``(结果, {dataset: (旧, 新)})``。

        与 ``sync_day`` 的唯一区别是写盘语义：这里走 ``refresh_raw``，
        最近 ``window_days`` 天的文件允许被云端覆盖（旧版留痕），更早的冻结。

        第二个返回值是「本地 ↔ Garmin 云端」校验的原始材料：
        ``{dataset: (旧 payload, 新 payload)}``，只包含**确实变了**的数据集
        （含超出窗口但云端已改的 ``frozen`` 项）。
        """
        result = GarminSyncResult(date=day)
        changed: dict[str, tuple[Any, Any]] = {}
        try:
            g = self.client()
        except Exception as e:  # noqa: BLE001 — 认证/网络层失败：逐数据集记错，不中断
            msg = f"{type(e).__name__}: {e}"
            for dataset in DATASETS:
                result.errors[dataset] = msg
            logger.error(
                "Garmin 重拉 %s: 客户端初始化失败，全部 %d 个数据集标记为失败 —— %s",
                day,
                len(DATASETS),
                msg,
            )
            return result, changed

        for dataset in DATASETS:
            try:
                payload = self._fetch(g, dataset, day)
            except Exception as e:  # noqa: BLE001 — 逐项容错
                result.errors[dataset] = f"{type(e).__name__}: {e}"
                continue
            if payload is None or payload == {} or payload == []:
                if dataset in OPTIONAL_DATASETS:
                    result.fetched.append(dataset)
                else:
                    result.missing.append(dataset)
                continue

            result.fetched.append(dataset)
            if dataset == "activities":
                # 与 sync_day 同口径：同日多次运动按 id 合并，避免刷新时降级
                try:
                    payload = _merge_activities(self.read_raw("activities", day), payload)
                except AppError as e:
                    result.errors[dataset] = f"{type(e).__name__}: {e}"
                    continue
            state, old = self.refresh_raw(dataset, day, payload, window_days=window_days)
            if state == "written":
                result.written.append(dataset)
            elif state == "superseded":
                result.superseded.append(dataset)
                changed[dataset] = (old, payload)
            elif state == "frozen":
                result.frozen.append(dataset)
                if old != payload:
                    changed[dataset] = (old, payload)
            else:  # same
                result.skipped.append(dataset)

        logger.info(
            "Garmin 重拉 %s: written=%d superseded=%d frozen=%d same=%d missing=%d errors=%d",
            day,
            len(result.written),
            len(result.superseded),
            len(result.frozen),
            len(result.skipped),
            len(result.missing),
            len(result.errors),
        )
        return result, changed

    # ------------------------------------------------------------
    # 标准化（TA.4）
    # ------------------------------------------------------------
    def _source_refs(self, day: dt.date) -> list[RawDailyRef]:
        """构造标准化取值的**原始文件引用**（REQ-DATA-004 可回溯）。

        - 仅收录 ``_STANDARDIZE_SOURCES`` 中**实际存在**的原始文件；
        - ``path`` 记**相对 ``raw_data_dir``** 的路径（POSIX 正斜杠），
          如 ``garmin/daily_summary/2026-10-14.json``；跨机可移植
          （开发机与 NAS 容器的 raw 根不同，绝对路径会失效）；
        - ``fetched_at`` 取该原始文件的**落盘时间**（mtime），即「写入本项目 raw 层的时刻」。
        """
        refs: list[RawDailyRef] = []
        for dataset in _STANDARDIZE_SOURCES:
            p = self._raw_path(dataset, day)
            if not p.exists():
                continue
            try:
                rel = p.relative_to(self.settings.raw_data_dir)
            except ValueError:  # 理论上不会发生（raw_root 在 raw_data_dir 下）
                rel = p
            refs.append(
                RawDailyRef(
                    source="garmin",
                    dataset=dataset,
                    date=day,
                    # POSIX 正斜杠，跨平台/跨机（开发机 Windows → NAS Linux）可移植
                    path=rel.as_posix(),
                    fetched_at=dt.datetime.fromtimestamp(p.stat().st_mtime),
                )
            )
        return refs

    def standardize(self, day: dt.date) -> StandardDaily:
        """把原始 JSON 归一化为 ``StandardDaily``（单位/时区/跨日睡眠口径统一）。"""
        summary = self.read_raw("daily_summary", day) or {}
        sleep = self.read_raw("sleep", day) or {}
        hrv = self.read_raw("hrv", day) or {}
        spo2 = self.read_raw("spo2", day) or {}
        resp = self.read_raw("respiration", day) or {}
        weight_raw = self.read_raw("weight", day) or {}

        dto = sleep.get("dailySleepDTO", {}) if isinstance(sleep, dict) else {}
        hrv_summary = hrv.get("hrvSummary", {}) if isinstance(hrv, dict) else {}
        # Garmin 自带 HRV 基线区间（E24）：onboarding 期 baseline 为 null → 全部留空，
        # 上层据此走保守分支（不判定），**不得**用自算值顶替。
        hrv_base = hrv_summary.get("baseline")
        hrv_base = hrv_base if isinstance(hrv_base, dict) else {}

        return StandardDaily(
            date=day,
            resting_heart_rate=_int(summary.get("restingHeartRate")),
            min_heart_rate=_int(summary.get("minHeartRate")),
            max_heart_rate=_int(summary.get("maxHeartRate")),
            sleep_duration_sec=_int(dto.get("sleepTimeSeconds")),
            sleep_start_ts=_epoch_ms_to_local(dto.get("sleepStartTimestampLocal")),
            sleep_end_ts=_epoch_ms_to_local(dto.get("sleepEndTimestampLocal")),
            hrv_last_night_avg=_float(hrv_summary.get("lastNightAvg")),
            hrv_low_upper=_float(hrv_base.get("lowUpper")),
            hrv_balanced_low=_float(hrv_base.get("balancedLow")),
            hrv_balanced_high=_float(hrv_base.get("balancedUpper")),
            hrv_weekly_avg=_float(hrv_summary.get("weeklyAvg")),
            hrv_status=(hrv_summary.get("status") or None),
            body_battery_wake=_int(summary.get("bodyBatteryMostRecentValue")),
            # 风险引擎口径（Q11）：醒时电量与睡眠得分，缺失留空（不填 0）
            body_battery_morning=_int(summary.get("bodyBatteryAtWakeTime")),
            sleep_score=_int((dto.get("sleepScores") or {}).get("overall", {}).get("value")),
            weight_kg=_grams_to_kg(summary.get("weight") or weight_raw.get("weight")),
            steps=_int(summary.get("totalSteps")),
            active_calories=_int(summary.get("activeKilocalories")),
            intensity_moderate_min=_int(summary.get("moderateIntensityMinutes")),
            intensity_vigorous_min=_int(summary.get("vigorousIntensityMinutes")),
            # 口径修正：Garmin 的 spo2 载荷**没有** `spO2Values` 键，日均值在 `averageSpO2`
            # （实测 469 天载荷均如此，旧代码取 `spO2Values` 导致 spo2_avg 恒为 None）。
            spo2_avg=_float(
                (spo2.get("averageSpO2") if isinstance(spo2, dict) else None)
                or (_first(spo2.get("spO2Values")) if isinstance(spo2, dict) else None)
            ),
            respiration_avg=_float(
                summary.get("avgWakingRespirationValue")
                or (resp.get("avgRespirationRate") if isinstance(resp, dict) else None)
            ),
            source_refs=self._source_refs(day),
        )

    def standardize_activities(
        self,
        day: dt.date,
        sd: StandardDaily | None = None,
        *,
        body_weight_kg: float | None = None,
    ) -> load_domain.DailyLoad:
        """当日活动 → 负荷（原始负荷 → 标准化 0–100 → 按类型汇总）。

        口径（需求 4.1 / 4.4，用户 2026-10-20 确认）：
        - **逐活动先标准化，再按类型求和**，不先合并原始负荷（量纲不同不可加）；
        - 运动分类表决定是否计入：``walking`` 仅记录、``meditation``/``breathwork`` 不纳入；
        - 力量用 volume load，其余用 Banister TRIMP；
        - 必需字段缺失 → 标记不可计算，**不填 0、不估算**（需求 4.5）。

        体重优先级：显式传入的 ``body_weight_kg`` > 当日 ``weight_kg``。
        调用方应传入「最近有效体重」（体重是慢变量，前向沿用不算估算）；
        两者都没有时自重动作标为不可计算，器械动作仍可用 Garmin volume 计算。
        """
        raw = self.read_raw("activities", day)
        if not isinstance(raw, list) or not raw:
            return load_domain.aggregate.aggregate([], day)
        sd = sd or self.standardize(day)
        # 2026-10-25（U-8）：有氧类按 FIT 圈分段算 TRIMP；缺 FIT/单圈时公式层回退整次。
        laps_by_activity = {
            str(act.get("activityId")): load_laps(self.raw_root, str(act.get("activityId")))
            for act in raw
            if act.get("activityId") is not None
        }
        return compute_day(
            raw,
            day,
            max_hr=load_domain.hr_mod.MAX_HR,
            rest_hr=load_domain.hr_mod.REST_HR_BASELINE,
            body_weight_kg=body_weight_kg if body_weight_kg is not None else sd.weight_kg,
            laps_by_activity=laps_by_activity,
        )

    # ------------------------------------------------------------
    # 课程工作流（TA.11，首期必做）
    # ------------------------------------------------------------
    def upload_workout(self, payload: dict) -> int:
        """创建课程，返回 Garmin workoutId。"""
        try:
            res = self.client().upload_workout(payload)
        except Exception as e:  # noqa: BLE001
            raise AppError(ErrorCode.EXTERNAL_UNKNOWN, f"Garmin 课程上传失败：{e}") from e
        return int(res["workoutId"])

    def update_workout(self, workout_id: int, payload: dict) -> int:
        """整替换更新课程（PUT 保留 id → 已排期不失效）。"""
        try:
            res = self.client().update_workout(str(workout_id), payload)
        except Exception as e:  # noqa: BLE001
            raise AppError(ErrorCode.EXTERNAL_UNKNOWN, f"Garmin 课程更新失败：{e}") from e
        return int(res.get("workoutId", workout_id))

    def delete_workout(self, workout_id: int) -> None:
        """删除课程模板；**先取消其排期**，避免留下「孤儿排期」。

        Garmin **不级联**：若课程已排期却直接删除模板，排期记录会残留成孤儿——
        App 首页会继续显示「已安排训练」，但课程库/日历都查不到，且因服务端
        「找不到对应课程」而**永远无法删除**（实测 `DELETE /workout-service/schedule/{id}`
        恒 404）。故删除前必须先把能查到的排期取消掉。
        """
        for sid in self._scheduled_ids(workout_id):
            try:
                self.unschedule_workout(sid)
                logger.info("删除课程 %s 前已取消排期 %s", workout_id, sid)
            except AppError as e:
                logger.warning("取消排期 %s 失败（继续删除课程）：%s", sid, e.message)
        try:
            self.client().delete_workout(str(workout_id))
        except Exception as e:  # noqa: BLE001
            raise AppError(ErrorCode.EXTERNAL_UNKNOWN, f"Garmin 课程删除失败：{e}") from e

    def _scheduled_ids(self, workout_id: int) -> list[int]:
        """该课程**当前可查到**的排期 id（``upcoming`` + 当月/次月日历两个来源）。

        局限：Garmin 没有「按课程列全部排期」的端点，故只能覆盖近端排期
        （``upcoming`` 含当天及未来；日历含当月与次月）。远端历史排期不在覆盖范围。
        """
        ids: list[int] = []
        for it in self.upcoming_schedules():
            if _int(it.get("workoutId")) == workout_id and _int(it.get("scheduledWorkoutId")):
                ids.append(int(it["scheduledWorkoutId"]))
        today = dt.date.today()
        months = [(today.year, today.month)]
        if today.month == 12:
            months.append((today.year + 1, 1))
        else:
            months.append((today.year, today.month + 1))
        for year, month in months:
            try:
                items = self.get_scheduled_workouts(year, month)
            except AppError as e:
                logger.warning("查询 %s-%02d 排期失败：%s", year, month, e.message)
                continue
            for it in items:
                if (
                    str(it.get("itemType")) == "workout"
                    and _int(it.get("workoutId")) == workout_id
                    and _int(it.get("id"))
                ):
                    ids.append(int(it["id"]))
        return sorted(set(ids))

    def upcoming_schedules(self) -> list[dict]:
        """查询「即将到来」的排期（``/workout-service/schedule/upcoming``）。

        为什么需要它：**日历端点看不到孤儿排期**（课程模板已删的排期），只有这个
        端点会返回它们（含 ``scheduledWorkoutId`` / ``workoutId`` / ``workoutName`` /
        ``scheduleDate``）。失败时返回空列表（排期查询不应阻断主流程）。
        """
        try:
            res = self.client().connectapi("/workout-service/schedule/upcoming")
        except Exception as e:  # noqa: BLE001
            logger.warning("查询 upcoming 排期失败：%s", e)
            return []
        return list(res) if isinstance(res, list) else []

    def schedule_workout(self, workout_id: int, day: dt.date) -> int:
        """把课程排期到某天，返回 ``workoutScheduleId``。

        注意：Garmin 排期响应键为 ``workoutScheduleId``（非 ``id``）。
        """
        try:
            res = self.client().schedule_workout(str(workout_id), day.isoformat())
        except Exception as e:  # noqa: BLE001
            raise AppError(ErrorCode.EXTERNAL_UNKNOWN, f"Garmin 排期失败：{e}") from e
        return int(res.get("workoutScheduleId") or res.get("id") or 0)

    def get_scheduled_workouts(self, year: int, month: int) -> list[dict]:
        """查询某月已排期课程。

        注意：python-garminconnect 内部已做 ``month - 1``（Garmin 月为 0-index），
        此处必须传**自然月**，不可重复减 1。
        """
        try:
            res = self.client().get_scheduled_workouts(year, month)
        except Exception as e:  # noqa: BLE001
            raise AppError(ErrorCode.EXTERNAL_UNKNOWN, f"Garmin 排期查询失败：{e}") from e
        items = res.get("calendarItems", res) if isinstance(res, dict) else list(res)
        return list(items or [])

    def unschedule_workout(self, scheduled_id: int) -> None:
        try:
            self.client().unschedule_workout(str(scheduled_id))
        except Exception as e:  # noqa: BLE001
            raise AppError(ErrorCode.EXTERNAL_UNKNOWN, f"Garmin 取消排期失败：{e}") from e


# ------------------------------------------------------------
# 归一化小工具
# ------------------------------------------------------------
def load_raw_dataset(raw_root: Path, dataset: str) -> dict[str, Any]:
    """读 ``raw/<dataset>/<date>.json`` 全部 → ``{YYYY-MM-DD: payload}``。

    ``sync_tredict.py``（上传）与 ``verify_sync.py``（在线滞后比对）共用同一份读取口径，
    避免两边对「本地有哪些天」的理解出现分歧。
    """
    root = raw_root / dataset
    out: dict[str, Any] = {}
    if not root.exists():
        return out
    for p in sorted(root.glob("*.json")):
        try:
            out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("跳过损坏文件 %s: %s", p, e)
    return out


def load_raw_weight_list(raw_root: Path) -> list[dict]:
    """``weight`` 数据集 → ``[{weight, timestampGMT}]``。

    这是 ``build_bodyvalue_records`` 需要的形态。
    """
    root = raw_root / "weight"
    out: list[dict] = []
    if not root.exists():
        return out
    for p in sorted(root.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if d.get("weight") is not None and d.get("timestampGMT") is not None:
            out.append({"weight": d["weight"], "timestampGMT": d["timestampGMT"]})
    return out


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _grams_to_kg(v: Any) -> float | None:
    f = _float(v)
    return round(f / 1000.0, 1) if f is not None else None


def _epoch_ms_to_local(v: Any) -> dt.datetime | None:
    ms = _int(v)
    if ms is None:
        return None
    # Garmin 的 sleepStartTimestampLocal 名义为"本地时间戳"，此处按 UTC 还原墙钟时间。
    return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.UTC).replace(tzinfo=None)


def _first(v: Any) -> Any:
    if isinstance(v, list) and v:
        item = v[0]
        return item.get("value") if isinstance(item, dict) else item
    return None
