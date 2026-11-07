"""同步校验引擎（P7）——状态常量与容差。

公共入口见 ``app.domain.verify``（包门面）。

三层数据（云端 / 本地 raw / 本地 DB）是否仍然一致，由这里判定；
``scripts/verify_sync.py`` 负责 IO，本模块只做比较，便于单测与事后复核。

四条设计约束（来自 P7 设计树）
------------------------------
1. **只读**：本模块不写任何文件、不写库。发现缺失只报告，不自动补拉（O2=C）——
   自动补拉会让「校验」变成有副作用的写操作，cron 里跑就成了「每晚自动改数据」。
2. **逐条判定**：每个实体一条 ``CheckResult``，脚本据此回写 ``sync_ledger``（Q4=A）。
3. **三态空值判定**（H2=A）：``都空`` → ok；``表空/重算有`` → mismatch；``表有/重算空`` → mismatch。
   只有这样才能区分「数据源本来就没有」和「有数据但没取到」。
4. **代码指纹**（C=A）：健康链路依赖 ``GarminAdapter.standardize()``，谁改了算法，
   校验就会报「数据不一致」。指纹让「算法变了」和「数据变了」可区分。

已知不可验证项（写进报告 ``notes``，不假装它们不存在）
------------------------------------------------------
- Garmin 活动的 ``payload`` 有 **105 字段**，其中 **17 个是旧 Node 管道算的派生值**
  （``distanceMeters`` / ``durationSeconds`` / ``averageHeartRate`` …），
  线上接口只返回 88 个原生字段，**派生字段无法在线复现**。
- 因此在线校验**不能比哈希**（哈希永远不等），只能比 88 个共同字段的**值**。
  实测 301/301 零差异。
- 健康 raw（``daily_summary`` / ``sleep`` / ``hrv`` …）**不在 ``asset_manifest.json`` 里**，
  没有 sha 可比；``source_refs.fetched_at`` 是文件 mtime，跨机不可比（M=A 故不比对）。
"""

from __future__ import annotations

from app.domain.standard_fields import DB_FIELDS

# ============================================================
# 状态与容差
# ============================================================
STATE_OK = "ok"
STATE_MISMATCH = "mismatch"
STATE_MISSING_LOCAL = "missing_local"
STATE_MISSING_REMOTE = "missing_remote"
STATE_AMBIGUOUS = "ambiguous"

# 判定「失败」的状态（Q5=A）：ambiguous 只警告，不失败
FAIL_STATES = frozenset({STATE_MISMATCH, STATE_MISSING_LOCAL, STATE_MISSING_REMOTE})

# 浮点比较：Garmin 的 float 经过 JSON 往返应逐位相等，留极小容差吸收实现差异
REL_TOL = 1e-9
ABS_TOL = 1e-9

# ``standard_daily`` 参与比对的字段（排除 id / created_at / source_refs）。
# 唯一事实源：``app/domain/standard_fields.py``（② 白名单收敛，2026-10-26）。
# 此前手写 20 项，漏了 sleep_score / body_battery_morning → 云端校验静默跳过这 2 字段。
STANDARD_DAILY_FIELDS: tuple[str, ...] = DB_FIELDS

# 逐秒表参与比对的 8 个通道（与 ``garmin_activity_record`` 列一一对应）
RECORD_FIELDS: tuple[str, ...] = (
    "seq",
    "t",
    "hr",
    "speed",
    "power",
    "cadence",
    "altitude",
    "distance",
)

