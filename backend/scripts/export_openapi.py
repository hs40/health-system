"""离线导出 OpenAPI JSON（供前端 `npm run gen:api` 使用）。

不启动服务、不连数据库——直接 import `app.main:app` 取 `app.openapi()`。
这样前端生成类型时不必先跑起后端，也不会因为本机 8000 端口被占用而失败。

用法（在 backend/ 下）：
    python scripts/export_openapi.py ../frontend/openapi.json

通常不需要手动执行——前端 `npm run gen:api` 会自动调用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 直接以脚本方式运行（而非 -m）时，backend/ 不在 sys.path 上，补一下。
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：python scripts/export_openapi.py <输出路径>", file=sys.stderr)
        return 2

    out = Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)

    from app.main import app

    schema = app.openapi()
    out.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出 {len(schema.get('paths', {}))} 个路径 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
