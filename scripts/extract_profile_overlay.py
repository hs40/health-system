"""从 fit-file-parser 的 profile.js 提取「官方 profile 未定义」的字段定义。

背景（P4）：
  官方 garmin-fit-sdk 的 Profile 只有 125 个消息，`record` 里的 107/135/136/143、
  以及私有消息 79/140 等字段，SDK 一律只给数字字段号 + 原始整数值。
  但 fit-file-parser 自带一份更宽的 profile（126 消息，含 79/140），
  里面有字段名、type、scale、offset、units。

  **只抄定义，不抄解析逻辑**：数值仍是官方 SDK 解出来的 raw int，
  我们按 `actual = raw / scale - offset` 换算（FIT 规范）。

输出 `scripts/_profile_overlay.json`：
    { "<mesg_num>": { "name": "...", "<field_num>": {"field","type","scale","offset","units"} } }

只读外部文件，不改 `F:/WorkBuddy AI/Garmin_Tredict` 下任何内容。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SRC = Path(
    r"F:/WorkBuddy AI/Garmin_Tredict/_tools/node_modules/fit-file-parser/dist/profile.js"
)
OUT = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "app"
    / "parsers"
    / "_profile_overlay.json"
)


def brace_block(src: str, open_idx: int) -> tuple[str, int]:
    """从 open_idx 处的 '{' 返回配平块内容与结束位置。"""
    depth = 0
    i = open_idx
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx + 1 : i], i
        i += 1
    raise ValueError("unbalanced braces")


def read_str_field(src: str, key: str) -> str:
    """读取 `key: 'xxx',` 形式的字符串值（跳过冒号与空白）。"""
    marker = key + ":"
    pos = src.find(marker)
    if pos < 0:
        return ""
    rest = src[pos + len(marker) :]
    i = 0
    while i < len(rest) and rest[i] not in "'\"":
        if rest[i] == "\n":
            return ""
        i += 1
    if i >= len(rest):
        return ""
    quote = rest[i]
    end = rest.find(quote, i + 1)
    return rest[i + 1 : end] if end > 0 else ""


def read_number(src: str, key: str) -> float | int | None:
    """读取 `key: 123.45,` 形式的数值。"""
    marker = key + ":"
    pos = src.find(marker)
    if pos < 0:
        return None
    rest = src[pos + len(marker) :].lstrip()
    end = 0
    while end < len(rest) and (rest[end].isdigit() or rest[end] in ".-+eE"):
        end += 1
    text = rest[:end]
    if not text:
        return None
    try:
        val = float(text)
    except ValueError:
        return None
    return int(val) if val.is_integer() else val


def split_top_level(block: str) -> list[tuple[str, str]]:
    """把消息块按顶层 `<num>: { ... }` 切开，返回 [(key, inner)]。"""
    out: list[tuple[str, str]] = []
    depth = 0
    i = 0
    n = len(block)
    while i < n:
        ch = block[i]
        if ch == "{":
            if depth == 0:
                # 回溯找键名
                j = i - 1
                while j >= 0 and block[j] in " \t\r\n":
                    j -= 1
                if j >= 0 and block[j] == ":":
                    j -= 1
                    while j >= 0 and block[j] in " \t\r\n":
                        j -= 1
                    end = j + 1
                    while j >= 0 and (block[j].isdigit() or block[j] == "_"):
                        j -= 1
                    key = block[j + 1 : end]
                    inner, close = brace_block(block, i)
                    out.append((key, inner))
                    i = close + 1
                    continue
            depth += 1
        i += 1
    return out


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"找不到 profile.js：{SRC}")
    src = SRC.read_text(encoding="utf-8", errors="replace")

    anchor = src.find("PROFILE_MESSAGES")
    if anchor < 0:
        raise SystemExit("PROFILE_MESSAGES not found")
    body, _ = brace_block(src, src.find("{", anchor))

    overlay: dict[str, dict] = {}
    for key, inner in split_top_level(body):
        if not key.isdigit():
            continue
        fields: dict[str, dict] = {}
        for fkey, finner in split_top_level(inner):
            if not fkey.isdigit():
                continue
            fields[fkey] = {
                "field": read_str_field(finner, "field"),
                "type": read_str_field(finner, "type"),
                "scale": read_number(finner, "scale"),
                "offset": read_number(finner, "offset"),
                "units": read_str_field(finner, "units"),
            }
        # 消息名是普通键值对（不是块），在整块里找
        name = read_str_field(inner, "name")
        overlay[key] = {"name": name, "fields": fields}

    OUT.write_text(json.dumps(overlay, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[overlay] 消息 {len(overlay)} 类 -> {OUT}")
    for num in ("20", "79", "140", "233"):
        if num in overlay:
            m = overlay[num]
            print(f"  {num:<4} name={m['name']!r:<22} 字段={len(m['fields'])}")
        else:
            print(f"  {num:<4} 不存在")

    # 抽查 vo2_max
    if "79" in overlay:
        f0 = overlay["79"]["fields"].get("0")
        print(f"\n[check] mesg79 field0 = {f0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
