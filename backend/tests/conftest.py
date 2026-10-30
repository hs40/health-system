"""测试全局配置：把数据目录重定向到临时目录。

背景：部分测试直接构造 ``Settings(_env_file=None)``，会落到默认相对路径
``./data/...``（即 ``backend/data/``），在仓库里留下 audit/db 等垃圾文件，
也会掩盖「数据目录已统一到仓库根 data/」这一事实。

做法：在 **conftest 模块导入期**设置环境变量（pytest 先导入 conftest、再导入各测试模块，
故模块级 ``Settings()`` 常量也会被覆盖）。pydantic-settings 的优先级为
「init 参数 > 环境变量 > .env > 默认值」，因此这里能同时压过 ``backend/.env`` 与默认值。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="hs-test-data-"))

os.environ["RAW_DATA_DIR"] = str(_TMP / "raw")
os.environ["DB_PATH"] = str(_TMP / "db" / "health.db")
os.environ["AUDIT_DIR"] = str(_TMP / "audit")
os.environ["GARMIN_TOKENSTORE"] = str(_TMP / "garmin_tokens")
