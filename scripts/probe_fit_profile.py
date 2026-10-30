"""探查 Garmin FIT 私有消息的可解析性（只读，不改动任何数据）。

用途：为「用 fitdecode + Garmin 私有 profile 重建 FIT 解析 schema」这一决策
（Q32=B）提供事实依据。

输出：
1. fit-file-parser 的 PROFILE_MESSAGES 消息清单（编号 -> 名称 / 字段数）
2. fitdecode 自带 profile 的消息清单
3. 两者的差集，以及实测 FIT 中出现的 unknown_* 消息号是否被任一库覆盖

用法：
    python scripts/probe_fit_profile.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

FFP_PROFILE = Path(
    r"F:/WorkBuddy AI/Garmin_Tredict/_tools/node_modules/fit-file-parser/dist/profile.js"
)

# 实测样本中出现的、两个库都未定义的消息号
OBSERVED_PRIVATE = (22, 113, 141, 233, 288, 325, 326, 327, 394, 499)
# 关键的标准消息，用于确认解析器可用
KEY_STANDARD = (18, 19, 20, 21, 23, 34, 79, 101, 140, 206, 207, 258, 312)


def _brace_block(src: str, open_idx: int) -> tuple[str, int]:
    """从 open_idx 处的 '{' 开始，返回配平的块内容与结束位置。"""
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


def parse_fit_file_parser(path: Path) -> dict[int, dict]:
    """解析 fit-file-parser 的 PROFILE_MESSAGES。

    结构：PROFILE_MESSAGES = { <mesg_num>: { <field_num>: {...}, name: '<mesg_name>' } }
    注意：不能用 r"\\s" 之类的正则跨 shell heredoc 传参（Git Bash 会把 \\s 转成 /s），
    因此这里全部走字符串方法 + 手写配平。
    """
    src = path.read_text(encoding="utf-8", errors="replace")
    anchor = src.find("PROFILE_MESSAGES")
    if anchor < 0:
        raise RuntimeError("PROFILE_MESSAGES not found")
    open_idx = src.find("{", anchor)
    body, _ = _brace_block(src, open_idx)

    result: dict[int, dict] = {}
    depth = 0
    block_start = -1
    pending_key = ""
    for idx, ch in enumerate(body):
        if ch == "{":
            depth += 1
            if depth == 1:
                block_start = idx
                pending_key = body[max(0, idx - 40) : idx]
        elif ch == "}":
            depth -= 1
            if depth == 0 and block_start >= 0:
                block = body[block_start : idx + 1]
                block_start = -1
                # 键形如 "},\n    233: {"，从 pending_key 尾部取连续数字
                stripped = pending_key.rstrip()
                if not stripped.endswith(":"):
                    continue
                head = stripped[:-1].rstrip()
                end = len(head)
                start = end
                while start > 0 and head[start - 1].isdigit():
                    start -= 1
                if start == end:
                    continue
                num = int(head[start:end])
                name = _extract_name(block)
                fields = _extract_field_numbers(block)
                result[num] = {"name": name, "field_count": len(fields), "fields": fields}
    return result


def _extract_name(block: str) -> str:
    marker = "name:"
    pos = block.find(marker)
    if pos < 0:
        return ""
    rest = block[pos + len(marker) :].lstrip()
    if not rest or rest[0] not in "'\"":
        return ""
    quote = rest[0]
    end = rest.find(quote, 1)
    return rest[1:end] if end > 0 else ""


def _extract_field_numbers(block: str) -> list[int]:
    """顶层字段键为 8 空格缩进的 '<num>: {'。"""
    nums: list[int] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped.endswith(": {"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent != 8:
            continue
        head = stripped[: -len(": {")].strip()
        if head.isdigit():
            nums.append(int(head))
    return nums


def parse_fitdecode_profile() -> dict[int, str]:
    import fitdecode.profile as fp

    out: dict[int, str] = {}
    for num, msg in fp.MESSAGE_TYPES.items():
        out[num] = msg.name
    return out


def main() -> int:
    if not FFP_PROFILE.exists():
        print(f"[ERR] 找不到 {FFP_PROFILE}", file=sys.stderr)
        return 2

    ffp = parse_fit_file_parser(FFP_PROFILE)
    fd = parse_fitdecode_profile()

    print("=" * 68)
    print(f"fit-file-parser 消息定义数: {len(ffp)}  最大编号: {max(ffp)}")
    print(f"fitdecode       消息定义数: {len(fd)}   最大编号: {max(fd)}")
    print("=" * 68)

    print("\n[关键标准消息]")
    print(f"{'num':>5}  {'fit-file-parser':<24} {'fitdecode':<24}")
    for num in KEY_STANDARD:
        a = ffp.get(num, {}).get("name", "-")
        b = fd.get(num, "-")
        print(f"{num:>5}  {a:<24} {b:<24}")

    print("\n[实测私有消息]")
    print(f"{'num':>5}  {'fit-file-parser':<24} {'fitdecode':<24}")
    for num in OBSERVED_PRIVATE:
        a = ffp.get(num, {}).get("name", "-")
        b = fd.get(num, "-")
        print(f"{num:>5}  {a:<24} {b:<24}")

    only_ffp = sorted(set(ffp) - set(fd))
    only_fd = sorted(set(fd) - set(ffp))
    print(f"\n仅 fit-file-parser 有: {len(only_ffp)} 个 -> {only_ffp}")
    print(f"仅 fitdecode 有:       {len(only_fd)} 个 -> {only_fd}")

    out_path = Path(os.environ.get("TEMP", ".")) / "fit_profile_compare.json"
    out_path.write_text(
        json.dumps(
            {
                "fit_file_parser": {str(k): v for k, v in sorted(ffp.items())},
                "fitdecode": {str(k): v for k, v in sorted(fd.items())},
                "observed_private": list(OBSERVED_PRIVATE),
                "uncovered_private": [
                    n
                    for n in OBSERVED_PRIVATE
                    if n not in ffp and n not in fd
                ],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n明细已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
