"""日任务编排：Garmin → 本地 → Tredict，按 profile 分档。

为什么拆成「两个同步任务 + 一个校验任务」（2026-10-19 用户定）
-------------------------------------------------------------
校验必须晚于上传（否则「本地有云端无」是必然的，天天误报），但校验又要覆盖
两条链的全部结果。所以：

| profile | 计划任务 | 内容 |
|---|---|---|
| ``health``   | 每日 08:00 | 拉健康（5 天滚动）→ 传健康 |
| ``activity`` | 每日 23:00 | 拉 FIT → 解析 → 传活动 → 拉 Tredict 自生成 → 刷新匹配 |
| ``verify``   | 手动 / 被前两者触发 | 云端三路校验（活动 + 健康）→ 写 MD |
| ``all``      | 手动 | 三条链顺序执行 |

**同步完成后立即触发校验**：``--chain`` 让 ``health`` / ``activity`` 跑完后
自动接 ``verify``（同一进程串行，不依赖事件日志触发器，也不怕任务被禁用）。
计划任务里用 ``daily.cmd --profile health --chain``。

自动修复一轮
------------
云端校验差异分两种：**能靠重传修好的**（云端缺记录 / 字段被改写）与**修不好的**
（Garmin 侧删了活动、转码前就错）。``verify_cloud.py`` 用退出码区分：

- ``0`` 一致
- ``1`` 校验没跑成（网络/鉴权）→ 不重试，直接红
- ``3`` 有差异，**可尝试修复** → 重跑上传步骤一次，再验一次；仍 ``3`` 则红

修复目标从**全量步骤表**查找（``verify`` 档里没有 upload 步，但可以调它们）。
只重试一轮，避免「上传坏了 → 无限重传」把日志刷爆。

用法
----
    python scripts/daily_sync.py --profile health --chain
    python scripts/daily_sync.py --profile activity --chain
    python scripts/daily_sync.py --profile verify
    python scripts/daily_sync.py --profile all
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
LOG_DIR = ROOT / "data" / "logs"
LAST_PATH = LOG_DIR / "daily-last.json"

STEP_TIMEOUT_SEC = 3600
# 单步「无输出」判定：子进程已启动但这么久没有任何新输出 → 打印卡住提示
STEP_STALL_WARN_SEC = 90
EXIT_REPAIRABLE = 3


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    args: tuple[str, ...]
    critical: bool = False
    # 该步返回可修复差异（exit 3）时，需要重跑的上传步骤
    repair: tuple[str, ...] = ()


STEPS_HEALTH: tuple[Step, ...] = (
    Step("health_pull", "拉取 Garmin 健康数据（5 天滚动）", ("pull_garmin_health.py",)),
    Step("health_upload", "健康数据 → Tredict", ("sync_tredict.py",)),
)

STEPS_ACTIVITY: tuple[Step, ...] = (
    Step("garmin_pull", "拉取 Garmin 活动 FIT", ("pull_garmin_fit.py",), critical=True),
    Step("fit_parse", "解析 FIT → jsonl + 逐秒表", ("parse_fit.py",), critical=True),
    Step(
        "activity_upload",
        "活动 → Tredict（只传缺失）",
        ("upload_tredict_activity.py", "--only-missing", "--apply"),
    ),
    Step("tredict_pull", "拉取 Tredict 自生成数据", ("pull_tredict_generated.py",)),
    Step(
        "tredict_match",
        "刷新 Tredict 清单 + 重算匹配",
        ("backfill_tredict_activity.py", "--list-only"),
    ),
    # 活动数据最终落地后重算派生层：分类表 / 负荷算法一改，历史日报不会自动跟着变
    #
    # 2026-10-20：原 ``recompute_reports.py`` 已在负荷重构中**删除**
    # （需求 10.1：不设独立手动重算入口，改由 ``POST /api/data/change`` 自动触发）。
    # 这里改为调用应用内的重算编排，否则每天固定在这一步 exit 2 失败。
    Step(
        "recompute_reports",
        "重算负荷/ACWR（复用重算编排）",
        ("-m", "app.orchestrator.recompute_cli"),
    ),
)

STEPS_VERIFY: tuple[Step, ...] = (
    Step(
        "verify_cloud",
        "云端校验（活动 + 健康，三路）",
        ("verify_cloud.py", "--scope", "all"),
        repair=("activity_upload", "health_upload"),
    ),
    Step("md_report", "更新同步状态文档", ("render_status_doc.py", "--profile", "verify")),
)

STEPS_WEEKLY: tuple[Step, ...] = (
    Step(
        "verify_activity_full",
        "云端校验（活动全量）",
        ("verify_cloud.py", "--scope", "activity"),
    ),
    Step(
        "verify_health_full",
        "云端校验（健康全量 473 天）",
        ("verify_cloud.py", "--scope", "health", "--full"),
    ),
    Step("md_report", "更新同步状态文档", ("render_status_doc.py", "--profile", "weekly")),
)

STEPS_ALL: tuple[Step, ...] = (
    *STEPS_ACTIVITY,
    *STEPS_HEALTH,
    *STEPS_VERIFY,
)

PROFILES: dict[str, tuple[Step, ...]] = {
    "activity": STEPS_ACTIVITY,
    "health": STEPS_HEALTH,
    "verify": STEPS_VERIFY,
    "weekly": STEPS_WEEKLY,
    "all": STEPS_ALL,
}

# 自动修复时跨档查找目标步骤（verify 档里没有 upload 步，但要能调它们）
ALL_STEPS: dict[str, Step] = {s.key: s for s in (*STEPS_ACTIVITY, *STEPS_HEALTH, *STEPS_VERIFY)}


@dataclass
class Result:
    key: str
    label: str
    ok: bool
    exit_code: int
    seconds: float
    tail: list[str] = field(default_factory=list)
    attempts: int = 1
    repaired: bool = False

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "seconds": round(self.seconds, 1),
            "attempts": self.attempts,
            "repaired": self.repaired,
            "tail": self.tail,
        }


def _cmd_for(step: Step) -> list[str]:
    """步骤 → 命令行。

    ``args[0] == "-m"`` 时按**模块**调用（``python -m app.xxx``），
    否则按 ``scripts/`` 下的脚本调用。
    """
    if step.args[0] == "-m":
        return [sys.executable, "-u", *step.args]
    return [sys.executable, "-u", str(BACKEND / "scripts" / step.args[0]), *step.args[1:]]


def _fmt_cmd(step: Step) -> str:
    if step.args[0] == "-m":
        return f"python -u {' '.join(step.args)}"
    return f"python {' '.join(step.args)}"


def run_step(step: Step, log, *, dry_run: bool) -> Result:
    cmd = _cmd_for(step)
    print(f"\n=== [{step.key}] {step.label} ===", flush=True)
    print(f"    $ {_fmt_cmd(step)}", flush=True)
    # 把「正在跑」也写进日志：否则进程卡住/被杀时，日志停在上一行，看不出卡在哪
    log.write(f"\n=== [{step.key}] {step.label}\n    $ {_fmt_cmd(step)}\n")
    log.flush()
    if dry_run:
        return Result(step.key, step.label, True, 0, 0.0, ["(dry-run)"])

    started = time.monotonic()
    # 子进程 stdout 是管道 → Windows 下默认用 cp936(GBK) 编码：
    # 中文会变乱码，遇到 ✅ 这类字符还会直接 UnicodeEncodeError 崩掉。
    # 计划任务没有控制台，这个问题只在「被调度」时才暴露，故显式锁死 UTF-8。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    out_lines: list[str] = []
    err_lines: list[str] = []
    timed_out = False
    # 必须流式读：``subprocess.run(capture_output=True)`` 在子进程卡死时
    # 一个字都拿不到，用户只能干等到 STEP_TIMEOUT_SEC（1 小时）。这里改为
    # 边读边写日志 + 心跳提示，卡住时能立刻看出卡在哪一步。
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=BACKEND,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            bufsize=1,
        )
    except OSError as e:
        seconds = time.monotonic() - started
        log.write(f"    无法启动：{e}\n")
        log.flush()
        print(f"    [error] {e}", flush=True)
        return Result(step.key, step.label, False, -2, seconds, [f"spawn failed: {e}"])

    def _drain(stream, sink: list[str], tag: str, logfile) -> None:
        for line in stream:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            sink.append(line)
            logfile.write(line + tag + "\n")

    t_out = threading.Thread(target=_drain, args=(proc.stdout, out_lines, "", log), daemon=True)
    t_err = threading.Thread(
        target=_drain, args=(proc.stderr, err_lines, "\t(stderr)", log), daemon=True
    )
    t_out.start()
    t_err.start()

    last_report = time.monotonic()
    reported_lines = 0
    while proc.poll() is None:
        time.sleep(1.0)
        # 有新输出 → 实时打屏 + 落盘
        if len(out_lines) > reported_lines:
            for ln in out_lines[reported_lines:]:
                if ln.strip():
                    print(f"    {ln}", flush=True)
            reported_lines = len(out_lines)
            log.flush()
            last_report = time.monotonic()
        elif time.monotonic() - last_report >= STEP_STALL_WARN_SEC:
            wait = int(time.monotonic() - last_report)
            print(f"    [stall] 已 {wait}s 无输出，仍在运行…", flush=True)
            log.write(f"    [stall] 已 {wait}s 无输出\n")
            log.flush()
            last_report = time.monotonic()
        if time.monotonic() - started > STEP_TIMEOUT_SEC:
            timed_out = True
            proc.kill()
            break

    t_out.join(timeout=5)
    t_err.join(timeout=5)
    returncode = proc.poll() if proc.poll() is not None else -1
    seconds = time.monotonic() - started

    bar = "=" * 70
    log.write(f"\n{bar}\n=== {step.key} ({seconds:.1f}s, exit={returncode})\n{bar}\n")
    log.flush()

    if timed_out:
        print(f"    [timeout] 超过 {STEP_TIMEOUT_SEC}s，已终止", flush=True)
        log.write(f"--- {step.key} TIMEOUT after {seconds:.0f}s ---\n")
        log.flush()
        return Result(step.key, step.label, False, -1, seconds, tail_of(out_lines, 12))

    tail = tail_of(out_lines, 12)
    ok = returncode == 0
    print(f"    → {'OK' if ok else f'FAILED (exit {returncode})'}  {seconds:.1f}s", flush=True)
    return Result(step.key, step.label, ok, returncode, seconds, tail)


def tail_of(lines: list[str], n: int) -> list[str]:
    return [ln for ln in lines if ln.strip()][-n:]


def main() -> int:
    ap = argparse.ArgumentParser(description="日任务：Garmin → 本地 → Tredict")
    ap.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default="all",
        help="跑哪一档（默认 all）",
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印计划")
    ap.add_argument("--only", default="", help="只跑这些步骤（逗号分隔）")
    ap.add_argument("--skip", default="", help="跳过这些步骤（逗号分隔）")
    ap.add_argument("--keep-going", action="store_true", help="关键步骤失败也继续")
    ap.add_argument("--no-repair", action="store_true", help="校验有差异时不自动重传")
    ap.add_argument(
        "--chain",
        action="store_true",
        help="本档跑完后立即接 verify 档（同步任务完成后马上校验）",
    )
    args = ap.parse_args()

    steps = list(PROFILES[args.profile])
    if args.chain and args.profile in ("activity", "health", "weekly"):
        have = {s.key for s in steps}
        steps += [s for s in STEPS_VERIFY if s.key not in have]
    by_key = dict(ALL_STEPS)

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    skip = {x.strip() for x in args.skip.split(",") if x.strip()}
    plan = [s for s in steps if (not only or s.key in only) and s.key not in skip]

    unknown = (only | skip) - {s.key for s in steps}
    if unknown:
        print(f"[warn] 未知步骤已忽略：{sorted(unknown)}（可选：{[s.key for s in steps]}）")

    started_at = dt.datetime.now()
    print(
        f"[daily] {started_at:%Y-%m-%d %H:%M:%S}  profile={args.profile}"
        + (" +chain" if args.chain else "")
        + f"  共 {len(plan)} 步"
        + ("（dry-run）" if args.dry_run else "")
    )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"daily-{args.profile}-{started_at:%Y%m%d-%H%M%S}.log"
    results: list[Result] = []
    aborted = False

    def snapshot() -> dict:
        failed_now = [r for r in results if not r.ok]
        return {
            "schema": 2,
            "profile": args.profile,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
            "dry_run": args.dry_run,
            "aborted": aborted,
            "ok": not failed_now,
            "log": str(log_path.relative_to(ROOT)),
            "steps": [r.as_dict() for r in results],
        }

    with log_path.open("w", encoding="utf-8") as log:
        log.write(
            f"daily_sync started {started_at.isoformat(timespec='seconds')} "
            f"profile={args.profile}\n"
        )
        # 立即落盘：否则进程被杀/卡死时日志停在缓冲区里，事后一无所知
        log.flush()
        for step in plan:
            result = run_step(step, log, dry_run=args.dry_run)
            results.append(result)

            if result.exit_code == EXIT_REPAIRABLE and step.repair and not args.no_repair:
                print(f"\n[daily] {step.key} 发现可修复差异，重跑 {step.repair} 后复验")
                log.write(f"\n--- repair triggered by {step.key} ---\n")
                for repair_key in step.repair:
                    target = by_key.get(repair_key)
                    if target is None or args.dry_run:
                        continue
                    fix = run_step(target, log, dry_run=args.dry_run)
                    results.append(fix)
                again = run_step(step, log, dry_run=args.dry_run)
                again.attempts = 2
                again.repaired = again.exit_code == 0
                results.append(again)
                result = again

            # 每步结束就落一次快照：``md_report`` 步骤要读它，
            # 若只在全部跑完后写，文档里显示的就是「上一次运行」。
            if not args.dry_run:
                LAST_PATH.write_text(
                    json.dumps(snapshot(), ensure_ascii=False, indent=1), encoding="utf-8"
                )

            if not result.ok and step.critical and not args.keep_going:
                print(f"\n[daily] 关键步骤 {step.key} 失败，中止后续步骤（--keep-going 可继续）")
                aborted = True
                break

    summary = snapshot()
    failed = [r for r in results if not r.ok]
    if not args.dry_run:
        LAST_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print(
        f"\n[daily] {'全部完成' if not failed else f'{len(failed)} 步失败'}  "
        f"耗时 {(dt.datetime.now() - started_at).total_seconds():.0f}s"
    )
    if not args.dry_run:
        print(f"[daily] 日志 {summary['log']}")
    for r in failed:
        print(f"  [x] {r.key} (exit {r.exit_code})")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
