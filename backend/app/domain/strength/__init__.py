"""力量训练 Notes 生成（``strength-notes.js`` 的 Python 移植）。

数据文件 ``moves_zh.json`` / ``corrections.json`` 随包分发（Q13=A）：
它们决定 notes 长什么样，属于算法的一部分，不是运行期配置。
"""

from app.domain.strength.notes import (
    NOTES_HEADER,
    NOTES_LIMIT_UPDATE,
    NOTES_LIMIT_UPLOAD,
    TITLE_LIMIT,
    build_strength_notes,
    data_fingerprints,
    fit_groups,
    fmt_per_set,
    load_corrections,
    load_moves,
    truncate_notes,
)

__all__ = [
    "NOTES_HEADER",
    "NOTES_LIMIT_UPDATE",
    "NOTES_LIMIT_UPLOAD",
    "TITLE_LIMIT",
    "build_strength_notes",
    "data_fingerprints",
    "fit_groups",
    "fmt_per_set",
    "load_corrections",
    "load_moves",
    "truncate_notes",
]
