"""Tredict 适配器（TA.5 / V5）：Python 重写，行为对齐 ``tredict-upload.js``。

归档口径 A（**必须严格保持**，否则会与历史数据错位）：
- 把 Garmin 的「本地墙钟」当作 UTC、``timezoneOffsetInSeconds=0``，
  使 timestamp 的 GMT 日期 == 本地日期，消除时区歧义。
- HRV：``timestamp`` = 睡眠中点，``rmssd`` = ``hrvSummary.lastNightAvg``（取整）。
- 睡眠：``start/end`` = 本地时间戳当 UTC，``sleepDurationInSeconds`` = ``sleepTimeSeconds``；
  采用**先删后传**保证幂等（Tredict 按 startTimestamp 判重，纯追加会累积错位记录）。
- 身体：每天一条、固定本地 22:00，含心率三项 + 当日最后一次体重 + 身高；本地未到 22:00 跳过当天。

限速：请求间隔 130ms（≈7.7 req/s，低于 8/s 上限），429/5xx 指数退避。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.errors import AppError
from app.schemas.enums import ErrorCode

logger = logging.getLogger("hs.adapters.tredict")

BASE = "https://www.tredict.com"
API = BASE + "/api/oauth/v2"
HRV_URL = API + "/hrv"
SLEEP_URL = API + "/sleep"
BODYVALUES_URL = API + "/bodyvalues"
ACTIVITY_UPLOAD_URL = API + "/activity/upload"
ACTIVITY_UPDATE_URL = API + "/activity/update"  # + "/<id>"
ACTIVITY_LIST_URL = API + "/activityList"
ACTIVITY_DETAIL_URL = API + "/activity"  # + "/<id>"
# Tredict **自生成**数据（官方文档 https://www.tredict.com/blog/oauth_docs/）
EFFORTS_URL = API + "/efforts"
CAPACITY_URL = API + "/capacity"
ZONES_URL = API + "/zones"
PLANNED_TRAINING_URL = API + "/plannedTrainingList"
EQUIPMENT_URL = API + "/equipmentList"

# 官方允许 50~1000（默认 500）。用 1000：301 条活动从 7 页降到 1 页。
ACTIVITY_PAGE_SIZE = 1000
MAX_LIST_PAGES = 50  # 50 × 1000 = 50,000 条，防死循环
GAP_MS = 130
HEALTH_BATCH = 100
TZ_OFFSET_MS = 8 * 3600 * 1000
HEIGHT_CM = 170
DEFAULT_CONFIG = Path(r"F:/WorkBuddy AI/Garmin_Tredict/_tools/tredict.config.json")

# ``GET /bodyvalues`` 只返回这几个字段；上传时带的 ``hrRestDynamic`` /
# ``hrMaxDynamic`` **读不回来**，逐字段校验必须排除，否则天天假红。
BODYVALUES_READABLE: tuple[str, ...] = (
    "restingHeartrate",
    "bodyHeightInCentimeter",
    "weightInKilograms",
    "bodyFatInPercent",
    "bodyWaterInPercent",
    "muscleMassInPercent",
)

_WALL_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")


# ============================================================
# 纯函数：记录构造（与 Node 逐项对齐，可单测/可对拍）
# ============================================================
def _iso_z(ms: int) -> str:
    """epoch 毫秒 → ISO Z，**保留毫秒**（与 JS ``toISOString()`` 一致）。

    注意：两个整秒时间戳的中点毫秒位可能为 500（如 17.000 与 18.000 的中点），
    因此不可硬编码 ``.000``。
    """
    d = dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC)
    return f"{d.strftime('%Y-%m-%dT%H:%M:%S')}.{ms % 1000:03d}Z"


def wall_to_iso(value: Any) -> str | None:
    """本地墙钟字符串 → ``YYYY-MM-DDTHH:MM:SS.000Z``。"""
    m = _WALL_RE.search(str(value or ""))
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:{m.group(6)}.000Z"


def wall_ms(value: Any) -> int | None:
    iso = wall_to_iso(value)
    if iso is None:
        return None
    parsed = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=dt.UTC)
    return int(parsed.timestamp() * 1000)


def ms_wall_to_iso(ms: Any) -> str | None:
    """epoch 毫秒（Garmin 已编码本地墙钟）→ ISO Z（保留毫秒）。"""
    if ms is None:
        return None
    try:
        n = int(ms)
    except (TypeError, ValueError):
        return None
    return _iso_z(n)


def midpoint_iso(a: Any, b: Any) -> str | None:
    x, y = wall_ms(a), wall_ms(b)
    if x is None or y is None:
        return None
    return _iso_z((x + y) // 2)


def ymd(iso: str) -> str | None:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(iso))
    return f"{m.group(1)}{m.group(2)}{m.group(3)}" if m else None


def norm_ts(iso: str) -> str:
    return str(iso)[:19]


def _data_map(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    d = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    return d if isinstance(d, dict) else {}


def build_hrv_records(hrv_raw: Any) -> tuple[list[dict], int, int]:
    """HRV 记录：``{timestamp=睡眠中点, timezoneOffsetInSeconds=0, rmssd}``。"""
    m = _data_map(hrv_raw)
    out: list[dict] = []
    skipped_null = skipped_no_time = 0
    for k in sorted(m):
        r = m[k] or {}
        rmssd = (r.get("hrvSummary") or {}).get("lastNightAvg")
        if rmssd is None:
            skipped_null += 1
            continue
        ts = midpoint_iso(
            r.get("sleepStartTimestampLocal"), r.get("sleepEndTimestampLocal")
        ) or midpoint_iso(r.get("startTimestampLocal"), r.get("endTimestampLocal"))
        if not ts:
            skipped_no_time += 1
            continue
        out.append({"timestamp": ts, "timezoneOffsetInSeconds": 0, "rmssd": round(rmssd)})
    return out, skipped_null, skipped_no_time


def build_sleep_records(
    sleep_raw: Any, now_ms: int | None = None
) -> tuple[list[dict], int, int]:
    """睡眠记录：``startTimestamp`` / ``endTimestamp`` / ``offset=0`` / ``duration``。

    ``endTimestamp`` 落在未来（归档口径 A 使其比真实 UTC 快约 8h）时跳过，次日重跑补上。
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    m = _data_map(sleep_raw)
    out: list[dict] = []
    skipped = skipped_future = 0
    for k in sorted(m):
        d = (m[k] or {}).get("dailySleepDTO") or {}
        start = ms_wall_to_iso(d.get("sleepStartTimestampLocal"))
        end = ms_wall_to_iso(d.get("sleepEndTimestampLocal"))
        dur = d.get("sleepTimeSeconds")
        if not start or not end or dur is None:
            skipped += 1
            continue
        if _iso_to_ms(end) > now_ms:
            skipped_future += 1
            continue
        out.append(
            {
                "startTimestamp": start,
                "endTimestamp": end,
                "timezoneOffsetInSeconds": 0,
                "sleepDurationInSeconds": dur,
            }
        )
    return out, skipped, skipped_future


def bodyvalues_todo(local: list[dict], cloud: list[dict]) -> list[dict]:
    """本地 vs 云端 bodyvalues 差集（官方语义：同 timestamp 重传 = **合并更新**）。

    2026-10-21 真实故障：体重是「加进已存在的 bodyvalues 记录」的，timestamp 不变，
    旧差集只比键 → 永远判定「已一致」，体重永远传不上去。故必须**比内容**：

    - 云端无该 timestamp → 传；
    - 同 timestamp 但任一**可读字段**不同 → 传（重传是 merge，不产生重复）；
    - 只比 ``BODYVALUES_READABLE``：云端读不回的 ``hrRestDynamic`` /
      ``hrMaxDynamic`` 不得造成永久假差集。
    """

    def _same(a: object, b: object) -> bool:
        if a is None and b is None:
            return True
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(float(a) - float(b)) < 1e-6
        return a == b

    have = {norm_ts(str(r.get("timestamp") or "")): r for r in cloud if isinstance(r, dict)}
    out: list[dict] = []
    for rec in local:
        cur = have.get(norm_ts(str(rec.get("timestamp") or "")))
        if cur is None:
            out.append(rec)
            continue
        if any(not _same(rec.get(f), cur.get(f)) for f in BODYVALUES_READABLE if f in rec):
            out.append(rec)
    return out


def build_bodyvalue_records(
    daily_raw: Any, weight_list: list[dict] | None, now_ms: int | None = None
) -> tuple[list[dict], dict]:
    """身体记录：每天一条、本地 22:00；心率三项 + 当日最后一次体重 + 身高。"""
    daily = _data_map(daily_raw)
    weight_by_date: dict[str, dict] = {}
    for w in weight_list or []:
        if w.get("weight") is None or w.get("timestampGMT") is None:
            continue
        local_iso = _ms_to_iso(int(w["timestampGMT"]) + TZ_OFFSET_MS)
        dk = local_iso[:10]
        prev = weight_by_date.get(dk)
        if prev is None or local_iso > prev["ts"]:
            weight_by_date[dk] = {"weight": w["weight"], "ts": local_iso}

    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    now_local = _ms_to_iso(now_ms + TZ_OFFSET_MS)
    today_local = now_local[:10]
    gate_today = int(now_local[11:13]) < 22  # 本地未到 22:00 → 跳过今天

    date_set = set()
    for k, s in daily.items():
        s = s or {}
        if any(s.get(f) is not None for f in ("restingHeartRate", "minHeartRate", "maxHeartRate")):
            date_set.add(k)
    date_set.update(weight_by_date)

    out: list[dict] = []
    skipped_today = with_weight = with_hr = 0
    earliest = latest = None
    for k in sorted(date_set):
        if gate_today and k == today_local:
            skipped_today += 1
            continue
        s = daily.get(k) or {}
        rec: dict[str, Any] = {"timestamp": f"{k}T22:00:00.000Z", "timezoneOffsetInSeconds": 0}
        has_any = False
        if s.get("restingHeartRate") is not None:
            rec["restingHeartrate"] = s["restingHeartRate"]
            has_any = True
        if s.get("minHeartRate") is not None:
            rec["hrRestDynamic"] = s["minHeartRate"]
            has_any = True
        if s.get("maxHeartRate") is not None:
            rec["hrMaxDynamic"] = s["maxHeartRate"]
            has_any = True
        if has_any:
            with_hr += 1
        w = weight_by_date.get(k)
        if w:
            rec["weightInKilograms"] = round(w["weight"] / 1000, 1)
            with_weight += 1
        if not has_any and not w:
            continue
        rec["bodyHeightInCentimeter"] = HEIGHT_CM
        out.append(rec)
        earliest = earliest or k
        latest = k

    return out, {
        "dailyCount": len(date_set),
        "weightCount": with_weight,
        "withHr": with_hr,
        "earliest": earliest,
        "latest": latest,
        "skippedToday": skipped_today,
        "gateToday": gate_today,
        "todayLocalYmd": today_local,
    }


def sleep_purge_range(records: list[dict]) -> dict | None:
    ms = [_iso_to_ms(r["endTimestamp"]) for r in records if r.get("endTimestamp")]
    if not ms:
        return None
    day = 86400 * 1000
    lo = (min(ms) // day) * day
    hi = (max(ms) // day) * day + day - 1
    return {"start": _ms_to_iso(lo), "end": _ms_to_iso(hi)}


def trim_series_sampled(payload: dict) -> dict:
    """裁掉 ``seriesSampled.data``，保留 ``sampleSize`` / ``endOffset``。

    实测 ``seriesSampled.data`` 占详情响应体积的 **98.4%**
    （16.93 MB → 0.26 MB）。P6 只做匹配与追溯，不需要逐点曲线；
    需要曲线时按 ``_id`` 回源重取即可。
    """
    out = dict(payload)
    ss = out.get("seriesSampled")
    if isinstance(ss, dict):
        out["seriesSampled"] = {k: v for k, v in ss.items() if k != "data"}
    return out


def canonical_sha256(payload: Any) -> str:
    """规范哈希：键排序 + 紧凑分隔符，跨进程稳定。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_utc_iso(value: Any) -> dt.datetime | None:
    """Tredict 的 ``date``（真 UTC，带毫秒）→ 朴素 UTC ``datetime``。

    例：``2026-10-12T09:59:32.156Z`` → ``2026-10-12 09:59:32.156000``。
    朴素时间与 ``garmin_activity.start_time_gmt`` 同型，便于直接相减。
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(dt.UTC).replace(tzinfo=None)
    return d


def _iso_to_ms(iso: str) -> int:
    """ISO Z 字符串 → epoch 毫秒（兼容任意小数秒，如 ``.500Z``）。"""
    s = str(iso).replace("Z", "+00:00")
    return int(dt.datetime.fromisoformat(s).timestamp() * 1000)


def _ms_to_iso(ms: int) -> str:
    return _iso_z(ms)


# ============================================================
# 适配器
# ============================================================
@dataclass(slots=True)
class HealthSyncResult:
    hrv: dict = field(default_factory=dict)
    sleep: dict = field(default_factory=dict)
    bodyvalues: dict = field(default_factory=dict)

    @property
    def status(self) -> str:
        fails = sum(int(x.get("fail", 0)) for x in (self.hrv, self.sleep, self.bodyvalues))
        oks = sum(int(x.get("ok", 0)) for x in (self.hrv, self.sleep, self.bodyvalues))
        if fails and not oks:
            return "failed"
        if fails:
            return "partial"
        return "success"


class TredictAdapter:
    """Tredict 适配器（Personal API Token 鉴权）。"""

    def __init__(self, settings: Settings, *, config_path: Path | None = None) -> None:
        self.settings = settings
        self.config_path = config_path or DEFAULT_CONFIG

    # —— 鉴权 ——
    def token(self) -> str:
        if self.settings.tredict_token:
            return self.settings.tredict_token.get_secret_value().strip()
        env = os.environ.get("TREDICT_TOKEN", "").strip()
        if env:
            return env
        if self.config_path.exists():
            cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            tok = str(cfg.get("token", "")).strip()
            if tok:
                return tok
        raise AppError(ErrorCode.EXTERNAL_UNKNOWN, "未找到 Tredict 令牌")

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Authorization": "Bearer " + self.token(), "Accept": "application/json;charset=UTF-8"}
        if extra:
            h.update(extra)
        return h

    def _request(
        self, method: str, url: str, *, tries: int = 4, backoff_exp: bool = False, **kw: Any
    ) -> httpx.Response:
        """发请求；429 / 5xx 重试，其余状态码（含 4xx）直接返回。

        ``backoff_exp`` 只在写操作（上传/更新）时打开：写操作被限流后线性重试
        容易继续撞墙，改用 1.5s → 3s → 6s 的指数退避（Q6）。
        """

        def _wait(i: int) -> None:
            time.sleep(1.5 * (2**i) if backoff_exp else 1.5 * (i + 1))

        last: httpx.Response | None = None
        for i in range(tries):
            try:
                last = httpx.request(method, url, timeout=30.0, **kw)
            except httpx.HTTPError as e:
                if i == tries - 1:
                    raise AppError(ErrorCode.EXTERNAL_TIMEOUT, f"Tredict 请求失败：{e}") from e
                _wait(i)
                continue
            if last.status_code == 429 or last.status_code >= 500:
                _wait(i)
                continue
            return last
        assert last is not None
        return last

    # —— 健康数据 ——
    def _fetch_series_all(self, url: str, key: str) -> dict:
        u = (
            f"{url}?startDate={dt.datetime.now(tz=dt.UTC).strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
            "&endDate=2000-01-01T00:00:00.000Z"
        )
        r = self._request("GET", u, headers=self._headers())
        if r.status_code != 200:
            raise AppError(ErrorCode.EXTERNAL_UNKNOWN, f"GET {url} 失败 HTTP {r.status_code}")
        return r.json().get(key, {}) or {}

    def fetch_health(self) -> dict:
        """拉取三类健康数据（睡眠/HRV 带日期范围；bodyvalues 全量）。"""
        hrv = self._fetch_series_all(HRV_URL, "hrv")
        sleep = self._fetch_series_all(SLEEP_URL, "sleep")
        r = self._request("GET", BODYVALUES_URL, headers=self._headers())
        bodyvalues = (r.json().get("bodyvalues", []) if r.status_code == 200 else [])
        return {"hrv": hrv, "sleep": sleep, "bodyvalues": bodyvalues}

    # —— 活动（P6：只读回填） ——
    def fetch_activity_list(self, *, extended: bool = False) -> list[dict]:
        """分页拉取全量活动清单。

        ``extended=True`` 时带 ``extendedSummary=1``，每项多出 ``summary``
        （duration / distance / calories / heartrate / heartrateMax …）、
        ``title`` / ``notes`` / ``trainingType`` / ``createdAt``。
        301 条一次请求约 4 秒 —— 校验不需要逐条拉详情。

        ⚠️ ``_links.next`` 返回的 URL **不带** ``extendedSummary``，
        必须手动补回；否则只有第一页有 summary（实测 50/301）。
        """
        query = f"pageSize={ACTIVITY_PAGE_SIZE}"
        if extended:
            query += "&extendedSummary=1"
        url: str | None = f"{ACTIVITY_LIST_URL}?{query}"
        out: list[dict] = []
        pages = 0
        while url and pages < MAX_LIST_PAGES:
            r = self._request("GET", url, headers=self._headers())
            if r.status_code != 200:
                raise AppError(
                    ErrorCode.EXTERNAL_UNKNOWN,
                    f"GET /activityList 失败 HTTP {r.status_code}: {r.text[:150]}",
                )
            body = r.json() or {}
            out.extend((body.get("_embedded") or {}).get("activityList") or [])
            url = ((body.get("_links") or {}).get("next") or {}).get("href")
            if url and extended and "extendedSummary" not in url:
                url += ("&" if "?" in url else "?") + "extendedSummary=1"
            pages += 1
            time.sleep(GAP_MS / 1000)
        logger.info("Tredict activityList：%d 页 / %d 条（extended=%s）", pages, len(out), extended)
        return out

    # —— Tredict 自生成数据（capacity / zones / efforts / planned / equipment） ——
    def _get_json(self, url: str, **params: Any) -> Any:
        """GET 一个 JSON 端点；非 200 抛结构化错误。"""
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}"
        r = self._request("GET", url, headers=self._headers())
        if r.status_code != 200:
            raise AppError(
                ErrorCode.EXTERNAL_UNKNOWN,
                f"GET {url.split('?')[0]} 失败 HTTP {r.status_code}: {r.text[:150]}",
            )
        return r.json()

    def fetch_efforts(self, *, since: dt.date, until: dt.date) -> dict[str, Any]:
        """训练努力值：``{YYYYMMDD: [[effort, {isRace}?], ...]}``。

        官方**不返回 activityId**，同一天多个活动只能得到「当天 N 个 effort」，
        因此落库按日期整组存，不按下标硬对活动（会产生假关联）。

        注意参数方向与直觉相反：``startDate`` 是**较晚**的日期，``endDate`` 是较早的。
        """
        body = self._get_json(
            EFFORTS_URL, startDate=until.isoformat(), endDate=since.isoformat()
        )
        return body.get("trainingEfforts") or {}

    def fetch_capacity(self) -> dict[str, Any]:
        """能力值：``{running: [{timestamp, hrMax, hrLth, ftp, ftpa}]}``（只读）。"""
        return self._get_json(CAPACITY_URL).get("capacity") or {}

    def fetch_zones(self) -> dict[str, Any]:
        """训练区间：``{running: {heartrate: {ts: [...]}, pace: {ts: [...]}}}``（只读）。"""
        return self._get_json(ZONES_URL).get("zones") or {}

    def fetch_planned_trainings(self, start: dt.date, end: dt.date) -> list[dict]:
        """计划训练清单（实测为空；仍保留以便将来不漏）。"""
        body = self._get_json(
            PLANNED_TRAINING_URL, startDate=start.isoformat(), endDate=end.isoformat()
        )
        return (body.get("_embedded") or {}).get("plannedWorkoutList") or []

    def fetch_equipment(self) -> list[dict]:
        """器材清单（实测为空）。"""
        return self._get_json(EQUIPMENT_URL).get("equipment") or []

    def fetch_activity_detail(
        self, activity_id: str, *, extra_values: bool = True
    ) -> dict | None:
        """取单条活动详情；404 返回 ``None``（活动已被删除，不算失败）。

        按 Q18 **不重试**（``tries=1``）：单条失败由调用方记录为 ``failed``，
        继续下一条。重试会把「偶发 500」变成「整批变慢」，而回填可随时重跑。

        ``extra_values=True``（默认）带 ``extraValues=1``：才有 Tredict 自算的
        13 个指标（effort 三源 / vo2max / runningEffectiveness /
        speedAerobicFactor / powerAerobicFactor / speedIndex / stepLength /
        flightTime / groundContactTime / firstSplitDuration /
        intensityDistribution / zonesDistribution / steps）与顶层 ``weather``。
        2026-10-21 实测：本地历史 303 条详情全部是默认参数版，这些指标缺失。
        """
        url = f"{ACTIVITY_DETAIL_URL}/{activity_id}"
        if extra_values:
            url += "?extraValues=1"
        r = self._request(
            "GET", url, headers=self._headers(), tries=1
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise AppError(
                ErrorCode.EXTERNAL_UNKNOWN,
                f"GET /activity/{activity_id} 失败 HTTP {r.status_code}: {r.text[:150]}",
            )
        return r.json()

    # —— 活动（P8：上传 / 补元数据） ——
    def upload_activity(
        self, activity_id: str, fit_bytes: bytes, *, name: str = "", notes: str = ""
    ) -> dict:
        """``POST /activity/upload``（multipart）→ ``{"ok", "status", "exists", "body", "text"}``。

        Tredict 对重复文件返回 ``{"error": "FILE_EXISTS"}``，这不是失败——
        说明该活动早已在云端（可能上次上传成功但本地记账失败），调用方按「已存在」处理。
        """
        files = {
            "file": (f"{activity_id}.fit", fit_bytes, "application/octet-stream"),
        }
        data: dict[str, str] = {}
        if name:
            data["name"] = name
        if notes:
            data["notes"] = notes
        r = self._request(
            "POST",
            ACTIVITY_UPLOAD_URL,
            headers=self._headers(),
            files=files,
            data=data,
            tries=4,
            backoff_exp=True,
        )
        text = r.text
        body: dict | None = None
        try:
            parsed = json.loads(text)
            body = parsed if isinstance(parsed, dict) else None
        except ValueError:
            body = None
        return {
            "ok": 200 <= r.status_code < 300,
            "status": r.status_code,
            "exists": bool(body and body.get("error") == "FILE_EXISTS"),
            "body": body,
            "text": text[:300],
        }

    def update_activity(self, activity_id: str, patch: dict[str, Any]) -> dict:
        """``POST /activity/update/<id>``，body 为 ``{"training": patch}``。"""
        r = self._request(
            "POST",
            f"{ACTIVITY_UPDATE_URL}/{activity_id}",
            headers=self._headers({"Content-Type": "application/json"}),
            content=json.dumps({"training": patch}, ensure_ascii=False),
            tries=4,
            backoff_exp=True,
        )
        return {"ok": 200 <= r.status_code < 300, "status": r.status_code, "text": r.text[:200]}

    def _post_batch(self, url: str, key: str, records: list[dict]) -> dict:
        ok = fail = 0
        errors: list[str] = []
        for i in range(0, len(records), HEALTH_BATCH):
            chunk = records[i : i + HEALTH_BATCH]
            r = self._request(
                "POST",
                url,
                headers=self._headers({"Content-Type": "application/json"}),
                content=json.dumps({key: chunk}),
            )
            if r.status_code < 300:
                ok += len(chunk)
            else:
                fail += len(chunk)
                errors.append(f"HTTP {r.status_code}: {r.text[:150]}")
            time.sleep(GAP_MS / 1000)
        return {"ok": ok, "fail": fail, "errors": errors}

    def sync_sleep(self, records: list[dict], *, force: bool = False) -> dict:
        """睡眠「先删后传」（幂等）。

        ⚠️ 安全护栏：``sleep_purge_range`` 取待传记录的 ``min(end)~max(end)`` 作为删除范围。
        若传入**稀疏**记录（例如只传 2 条、却跨了 2025-11-22 ~ 2026-10-14），
        删除范围会覆盖整段历史，从而误删中间全部睡眠记录（本项目未做云端备份）。
        故默认拒绝「范围天数远多于记录条数」的调用，需显式 ``force=True`` 才放行。
        """
        out: dict[str, Any] = {"purged": False, "deletedCount": 0, "range": None, "upload": None}
        if not records:
            return out
        rng = sleep_purge_range(records)
        out["range"] = rng
        if not rng:
            return out
        if not force:
            span_days = (_iso_to_ms(rng["end"]) - _iso_to_ms(rng["start"])) // 86400000 + 1
            if span_days > max(7, len(records) * 2):
                raise AppError(
                    ErrorCode.VALIDATION,
                    "睡眠删除范围远大于待传记录数，疑似稀疏上传将误删历史记录",
                    {"span_days": span_days, "records": len(records), "range": rng},
                )
        del_url = f"{SLEEP_URL}?startDate={rng['start']}&endDate={rng['end']}"
        dr = self._request("DELETE", del_url, headers=self._headers())
        if dr.status_code >= 300:
            raise AppError(ErrorCode.EXTERNAL_UNKNOWN, f"DELETE /sleep 失败 HTTP {dr.status_code}")
        out["deletedCount"] = (dr.json() or {}).get("deletedCount", 0)
        out["purged"] = True
        time.sleep(GAP_MS / 1000)
        out["upload"] = self._post_batch(SLEEP_URL, "sleep", records)
        return out

    def sync_health(
        self,
        *,
        hrv_raw: Any,
        sleep_raw: Any,
        daily_raw: Any,
        weight_list: list[dict] | None = None,
        now_ms: int | None = None,
    ) -> HealthSyncResult:
        """构建并上传三类健康数据。"""
        hrv_records, _, _ = build_hrv_records(hrv_raw)
        sleep_records, _, _ = build_sleep_records(sleep_raw, now_ms)
        bv_records, _ = build_bodyvalue_records(daily_raw, weight_list, now_ms)

        result = HealthSyncResult()
        result.hrv = self._post_batch(HRV_URL, "hrv", hrv_records)
        # 全量上传（记录密集，范围≈记录数），显式放行护栏
        result.sleep = self.sync_sleep(sleep_records, force=True)
        result.bodyvalues = self._post_batch(BODYVALUES_URL, "bodyvalues", bv_records)
        logger.info(
            "Tredict 健康同步：hrv=%s sleep=%s bodyvalues=%s",
            result.hrv.get("ok"),
            result.sleep.get("upload"),
            result.bodyvalues.get("ok"),
        )
        return result
