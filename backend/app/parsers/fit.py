"""FIT 解析引擎（P4）。

设计约束（用户已定，不再讨论）：

- **Q32=B**：`.fit` 是唯一真源，`fit_json` 不参与解析。
- **Q33=A**：官方 ``garmin-fit-sdk`` 主解析，``fitdecode`` 交叉校验。
- **Q34=A**：双写 —— ``jsonl.gz`` 全量归档 + SQLite 逐秒表。
- **Q36=A**：关键字段严格比对（相对容差 ``1e-6``），超差阻断入库。

三条容易踩的坑（基线实测得出）：

1. **数字字段号不猜语义。** 官方 Profile 只有 125 个消息，``record`` 里的
   107/135/136/143、``set`` 里的 2/14、``lap`` 里的 145 都不在官方定义内。
   有 ``fit-file-parser`` 侧定义（仅 79/140 等少数）的按定义换算，
   没有的一律保留为 ``private_<字段号>``。
2. **私有消息带 scale。** 例如 ``user_metrics(79).field0 = vo2_max``，
   ``scale=2048/7``。官方 SDK 不认识该消息，只返回 raw int，
   不换算会差 292 倍。
3. **同一字段类型不稳定。** 实测 ``device_index`` 有 int 也有 str，
   ``local_device_type`` 同理。逐秒表只收数值通道，其余进 jsonl。

换算公式按 FIT 规范：``actual = raw / scale - offset``。
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fitdecode
from garmin_fit_sdk import Decoder, Stream

# 关键字段比对容差（Q36=A）
REL_TOL = 1e-6

OVERLAY_PATH = Path(__file__).with_name("_profile_overlay.json")


def _load_overlay() -> tuple[dict[int, dict[str, dict]], dict[str, int]]:
    """加载 fit-file-parser 的字段定义（只抄定义，不抄解析逻辑）。"""
    raw: dict[str, Any] = json.loads(OVERLAY_PATH.read_text(encoding="utf-8"))
    by_num: dict[int, dict[str, dict]] = {}
    name_to_num: dict[str, int] = {}
    for num_str, mesg in raw.items():
        num = int(num_str)
        by_num[num] = mesg["fields"]
        if mesg.get("name"):
            name_to_num[mesg["name"]] = num
    return by_num, name_to_num


_FIELDS_BY_MESG, _NUM_BY_NAME = _load_overlay()


class FitParseError(RuntimeError):
    """FIT 文件无法解析（头不合法、解码异常、空文件）。"""


class FitCrossCheckError(RuntimeError):
    """双解码器结果不一致，按 Q36=A 阻断入库。"""

    def __init__(self, message: str, detail: dict[str, Any]) -> None:
        super().__init__(message)
        self.detail = detail


# ============================================================
# 数据结构
# ============================================================
@dataclass(slots=True)
class RecordPoint:
    """逐秒表的一行（7 通道）。"""

    seq: int
    t: int  # UTC epoch 秒
    hr: int | None = None
    speed: float | None = None
    power: float | None = None
    cadence: float | None = None
    altitude: float | None = None
    distance: float | None = None


@dataclass(slots=True)
class ParsedFit:
    activity_id: str
    file_sha256: str
    file_bytes: int
    records: list[RecordPoint] = field(default_factory=list)
    laps: list[dict[str, Any]] = field(default_factory=list)
    sets: list[dict[str, Any]] = field(default_factory=list)
    zones: list[dict[str, Any]] = field(default_factory=list)
    splits: list[dict[str, Any]] = field(default_factory=list)
    messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def record_count(self) -> int:
        return len(self.records)


# ============================================================
# 归一化
# ============================================================
def _mesg_name(key: str) -> tuple[str, int | None]:
    """SDK 的 ``record_mesgs`` / ``"233"`` → (``record`` / ``private_233``, 消息号)。"""
    if key.isdigit():
        return f"private_{key}", int(key)
    name = key[: -len("_mesgs")] if key.endswith("_mesgs") else key
    return name, _NUM_BY_NAME.get(name)


def _resolve_field(
    mesg_num: int | None, field_key: str
) -> tuple[str, dict[str, Any] | None]:
    """字段键 → (输出名, 定义)。数字字段号查 overlay，查不到就保留原号。"""
    if not field_key.isdigit():
        return field_key, None
    defn: dict[str, Any] | None = None
    if mesg_num is not None:
        defn = _FIELDS_BY_MESG.get(mesg_num, {}).get(field_key)
    if defn and defn.get("field"):
        return str(defn["field"]), defn
    return f"private_{field_key}", None


def _apply_scale(value: Any, defn: dict[str, Any] | None) -> Any:
    """按 FIT 规范换算：``actual = raw / scale - offset``。

    只对官方 SDK 未定义的字段调用 —— 官方已命名的字段，SDK 已经换算过。
    """
    if defn is None or isinstance(value, bool):
        return value
    if not isinstance(value, (int, float)):
        return value
    scale = defn.get("scale")
    if not scale:
        return value
    offset = defn.get("offset") or 0
    return value / float(scale) - float(offset)


def _jsonable(value: Any) -> Any:
    """jsonl 序列化：datetime → ISO8601(UTC)，bytes → hex，嵌套结构递归。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value


def _epoch(value: Any) -> int | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp())


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# ============================================================
# 主流程
# ============================================================
def _decode(path: Path) -> tuple[dict[str, Any], int]:
    """官方 SDK 解码，返回 (原始消息字典, 错误数)。"""
    data = path.read_bytes()
    if len(data) < 14:
        raise FitParseError(f"{path.name}: 文件过短（{len(data)} 字节）")
    if data[8:12] != b".FIT":
        raise FitParseError(f"{path.name}: FIT 头不合法（{data[8:12]!r}）")

    msgs, errs = Decoder(Stream.from_byte_array(bytearray(data))).read()
    if not msgs:
        raise FitParseError(f"{path.name}: 解析出 0 条消息")
    return msgs, len(errs)


def _build_records(rows: list[dict[str, Any]]) -> list[RecordPoint]:
    """record 消息 → 7 通道逐秒行。

    ``cadence`` 与 ``fractional_cadence`` 合并（后者是小数部分），
    否则骑行/跑步的 0.5 步频会丢。
    """
    out: list[RecordPoint] = []
    for i, row in enumerate(rows):
        t = _epoch(row.get("timestamp"))
        if t is None:
            continue
        cad = _num(row.get("cadence"))
        frac = _num(row.get("fractional_cadence"))
        if cad is not None and frac:
            cad = cad + frac
        speed = _num(row.get("enhanced_speed"))
        if speed is None:
            speed = _num(row.get("speed"))
        altitude = _num(row.get("enhanced_altitude"))
        if altitude is None:
            altitude = _num(row.get("altitude"))
        hr = row.get("heart_rate")
        out.append(
            RecordPoint(
                seq=i,
                t=t,
                hr=int(hr) if isinstance(hr, int) and not isinstance(hr, bool) else None,
                speed=speed,
                power=_num(row.get("power")),
                cadence=cad,
                altitude=altitude,
                distance=_num(row.get("distance")),
            )
        )
    return out


def _normalize(
    msgs: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    """把 SDK 原始输出归一化成 ``{消息名: [字段字典]}``。"""
    out: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for raw_key, rows in msgs.items():
        name, mesg_num = _mesg_name(str(raw_key))
        bucket: list[dict[str, Any]] = []
        for row in rows:
            item: dict[str, Any] = {}
            for fkey, value in row.items():
                if value is None:
                    continue
                fname, defn = _resolve_field(mesg_num, str(fkey))
                item[fname] = _apply_scale(value, defn)
            bucket.append(item)
        out[name] = bucket
        counts[name] = len(bucket)
    return out, counts


def _cross_check(path: Path, normalized: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """fitdecode 交叉校验（Q33=A）。

    比对三项，任一不一致即视为失败：
      1. 消息类型集合
      2. 各类消息条数
      3. record 的 (timestamp, heart_rate) 逐条值，以及 session 的关键总量
    """
    fd_counts: dict[str, int] = {}
    fd_records: list[tuple[int | None, int | None]] = []
    fd_session: dict[str, Any] = {}

    with fitdecode.FitReader(
        io.BytesIO(path.read_bytes()), check_crc=fitdecode.CrcCheck.DISABLED
    ) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            name = frame.name
            fd_counts[name] = fd_counts.get(name, 0) + 1
            if name == "record":
                fd_records.append(
                    (
                        _epoch(frame.get_value("timestamp", fallback=None)),
                        frame.get_value("heart_rate", fallback=None),
                    )
                )
            elif name == "session" and not fd_session:
                for k in ("total_distance", "total_elapsed_time", "avg_heart_rate"):
                    fd_session[k] = frame.get_value(k, fallback=None)

    detail: dict[str, Any] = {"mismatches": []}
    mm: list[str] = detail["mismatches"]

    # 1) 类型集合（fitdecode 会把未知消息叫 unknown_N，两边都做一次归一）
    sdk_names = {k for k in normalized if not k.startswith("private_")}
    fd_known = {k for k in fd_counts if not k.startswith("unknown_")}
    if sdk_names != fd_known:
        only_sdk = sorted(sdk_names - fd_known)
        only_fd = sorted(fd_known - sdk_names)
        mm.append(f"消息类型不一致 sdk_only={only_sdk} fitdecode_only={only_fd}")

    # 2) 条数
    for name in sorted(sdk_names & fd_known):
        a, b = len(normalized[name]), fd_counts[name]
        if a != b:
            mm.append(f"{name} 条数不一致 sdk={a} fitdecode={b}")

    # 3) record 逐条
    sdk_records = normalized.get("record", [])
    if len(sdk_records) != len(fd_records):
        mm.append(f"record 条数不一致 sdk={len(sdk_records)} fitdecode={len(fd_records)}")
    else:
        bad = 0
        first_bad = None
        for i, (row, (fd_ts, fd_hr)) in enumerate(zip(sdk_records, fd_records, strict=True)):
            sdk_ts = _epoch(row.get("timestamp"))
            sdk_hr = row.get("heart_rate")
            if sdk_ts != fd_ts or sdk_hr != fd_hr:
                bad += 1
                if first_bad is None:
                    first_bad = {
                        "seq": i,
                        "sdk": (sdk_ts, sdk_hr),
                        "fitdecode": (fd_ts, fd_hr),
                    }
        if bad:
            mm.append(f"record 值不一致 {bad}/{len(sdk_records)} 条，首例={first_bad}")

    # 4) session 关键总量
    sdk_session = (normalized.get("session") or [{}])[0]
    for key, fd_val in fd_session.items():
        sdk_val = sdk_session.get(key)
        if fd_val is None and sdk_val is None:
            continue
        if fd_val is None or sdk_val is None:
            mm.append(f"session.{key} 一边缺失 sdk={sdk_val} fitdecode={fd_val}")
            continue
        if not isinstance(sdk_val, (int, float)) or not isinstance(fd_val, (int, float)):
            continue
        denom = max(abs(float(fd_val)), 1.0)
        if abs(float(sdk_val) - float(fd_val)) / denom > REL_TOL:
            mm.append(
                f"session.{key} 超差 sdk={sdk_val} fitdecode={fd_val} "
                f"rel={abs(float(sdk_val) - float(fd_val)) / denom:.3e}"
            )

    detail["ok"] = not mm
    detail["sdk_counts"] = {k: v for k, v in sorted(normalized.items())}
    detail["fitdecode_counts"] = dict(sorted(fd_counts.items()))
    return detail


def parse_fit(path: Path, *, cross_check: bool = True) -> ParsedFit:
    """解析单个 FIT 文件。

    Args:
        path: FIT 文件路径。
        cross_check: 是否执行 fitdecode 交叉校验（测试里可关掉提速）。

    Raises:
        FitParseError: 文件结构损坏。
        FitCrossCheckError: 两解码器结果不一致（Q36=A 阻断）。
    """
    msgs, errs = _decode(path)
    if errs:
        raise FitParseError(f"{path.name}: SDK 解码报错 {errs} 处")

    data = path.read_bytes()
    normalized, counts = _normalize(msgs)

    if cross_check:
        detail = _cross_check(path, normalized)
        if not detail["ok"]:
            raise FitCrossCheckError(
                f"{path.name}: 交叉校验失败 —— " + "；".join(detail["mismatches"][:3]),
                detail,
            )

    activity_id = path.stem.split("_")[0]
    return ParsedFit(
        activity_id=activity_id,
        file_sha256=hashlib.sha256(data).hexdigest(),
        file_bytes=len(data),
        records=_build_records(msgs.get("record_mesgs", [])),
        laps=normalized.get("lap", []),
        sets=normalized.get("set", []),
        zones=normalized.get("time_in_zone", []),
        splits=normalized.get("split", []),
        messages=normalized,
        counts=counts,
    )


def write_jsonl_gz(parsed: ParsedFit, out_path: Path) -> int:
    """全量归档：一条消息一行，gzip 压缩。返回写出的行数。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = 0
    with gzip.open(out_path, "wt", encoding="utf-8", compresslevel=6) as fh:
        for name in sorted(parsed.messages):
            for row in parsed.messages[name]:
                fh.write(
                    json.dumps(
                        {"m": name, "d": _jsonable(row)},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                fh.write("\n")
                lines += 1
    return lines
