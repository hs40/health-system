"""同步校验引擎（P7）——包门面。

三层数据（云端 / 本地 raw / 本地 DB）是否仍然一致，由这里判定；
``scripts/verify_sync.py`` 负责 IO，本包只做比较，便于单测与事后复核。

四条设计约束（来自 P7 设计树）
------------------------------
1. **只读**：本包不写任何文件、不写库。发现缺失只报告，不自动补拉（O2=C）——
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

模块划分（ARCHITECTURE_REVIEW A.1.2 / C.4.2）
---------------------------------------------
===============  ==================================================
``states``       状态常量、失败状态集合、容差、参与比对的字段清单
``core``         JSON 序列化、规范哈希、值相等、差异列表、行还原
``fingerprint``  三段算法的源码指纹（唯一向上依赖 adapters/parsers 的模块）
``result``       ``CheckResult`` 与内部构造器
``channels``     四条本地链路的逐条判定（活动 ×3 + 健康 ×1）
``online``       在线侧：Garmin / Tredict / 健康键集合与递归叶子比对
``fields``       逐字段值比对（健康三类、活动云端对、时长口径分流）
``raw``          Garmin 健康 raw 只比参与计算的字段
``summary``      汇总统计与退出码
===============  ==================================================

**兼容性**：本门面把上述 9 个模块的全部公共符号原样 re-export，
``from app.domain.verify import X`` 的调用方（5 个测试文件 + 2 个 scripts）
无需任何改动。``__all__`` 是必需的——没有它，ruff 的 F401 会把 re-export
当作未使用导入删掉。
"""

from __future__ import annotations

from app.domain.verify.channels import (
    check_activity_records,
    check_garmin_activity,
    check_standard_daily,
    check_tredict_activity,
)
from app.domain.verify.core import (
    canonical_sha256,
    diff_fields,
    rehydrate_record_row,
    same_value,
)
from app.domain.verify.fields import (
    ACTIVITY_DISTANCE_ABS_TOL,
    ACTIVITY_DISTANCE_FIELD,
    ACTIVITY_DISTANCE_REL_TOL,
    ACTIVITY_DURATION_TOL,
    ACTIVITY_SUMMARY_TOL,
    BODYVALUES_COMPARABLE,
    SWIM_SUB_SPORTS,
    compare_cloud_pair,
    compare_health_fields,
    duration_fields,
)
from app.domain.verify.fingerprint import code_fingerprint
from app.domain.verify.online import (
    HEALTH_KINDS,
    NON_DATA_FIELDS,
    activity_local_date,
    compare_activity_coverage,
    compare_online_garmin,
    compare_online_health,
    compare_online_tredict,
    compare_tredict_details,
    deep_diff,
    ids_of_day,
)
from app.domain.verify.raw import (
    HEALTH_RAW_ALIASES,
    HEALTH_RAW_COMPARABLE,
    compare_health_raw,
    health_field_value,
)
from app.domain.verify.result import CheckResult
from app.domain.verify.states import (
    ABS_TOL,
    FAIL_STATES,
    RECORD_FIELDS,
    REL_TOL,
    STANDARD_DAILY_FIELDS,
    STATE_AMBIGUOUS,
    STATE_MISMATCH,
    STATE_MISSING_LOCAL,
    STATE_MISSING_REMOTE,
    STATE_OK,
)
from app.domain.verify.summary import exit_code, summarize

__all__ = [
    "ABS_TOL",
    "ACTIVITY_DISTANCE_ABS_TOL",
    "ACTIVITY_DISTANCE_FIELD",
    "ACTIVITY_DISTANCE_REL_TOL",
    "ACTIVITY_DURATION_TOL",
    "ACTIVITY_SUMMARY_TOL",
    "BODYVALUES_COMPARABLE",
    "CheckResult",
    "FAIL_STATES",
    "HEALTH_KINDS",
    "HEALTH_RAW_ALIASES",
    "HEALTH_RAW_COMPARABLE",
    "NON_DATA_FIELDS",
    "RECORD_FIELDS",
    "REL_TOL",
    "STANDARD_DAILY_FIELDS",
    "STATE_AMBIGUOUS",
    "STATE_MISMATCH",
    "STATE_MISSING_LOCAL",
    "STATE_MISSING_REMOTE",
    "STATE_OK",
    "SWIM_SUB_SPORTS",
    "activity_local_date",
    "canonical_sha256",
    "check_activity_records",
    "check_garmin_activity",
    "check_standard_daily",
    "check_tredict_activity",
    "code_fingerprint",
    "compare_activity_coverage",
    "compare_cloud_pair",
    "compare_health_fields",
    "compare_health_raw",
    "compare_online_garmin",
    "compare_online_health",
    "compare_online_tredict",
    "compare_tredict_details",
    "deep_diff",
    "diff_fields",
    "duration_fields",
    "exit_code",
    "health_field_value",
    "ids_of_day",
    "rehydrate_record_row",
    "same_value",
    "summarize",
]
