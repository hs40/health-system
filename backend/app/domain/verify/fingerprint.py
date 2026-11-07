"""同步校验引擎（P7）——代码指纹（C=A）。

公共入口见 ``app.domain.verify``（包门面）。

注意：本模块**反向依赖** adapters / parsers / domain.matching，是全包唯一一个
向上看的模块。这几个导入放在函数体内（原本就在），因为 ``GarminAdapter``
间接依赖 ``app.domain.*``，放顶层会形成循环导入。
"""

from __future__ import annotations

import hashlib
import inspect


def code_fingerprint() -> dict[str, str]:
    """参与校验的三段逻辑的源码指纹（C=A）。

    没有 git，源码哈希是唯一可用的版本标识。指纹变了而数据「不一致」，
    第一嫌疑就是算法改了，而不是数据坏了。
    """
    from app.adapters.garmin import GarminAdapter
    from app.domain.matching import match_activities
    from app.parsers.fit import _build_records

    targets = {
        "standardize": GarminAdapter.standardize,
        "build_records": _build_records,
        "match_activities": match_activities,
    }
    return {
        name: hashlib.sha256(inspect.getsource(fn).encode("utf-8")).hexdigest()[:16]
        for name, fn in targets.items()
    }

