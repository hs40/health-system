"""架构分层约束测试（ARCHITECTURE_REVIEW A.1.1 / A.2.2 / A.10.1）。

这些测试把 AGENTS.md 里「domain 禁止 IO」「调度依赖必须显式」等约定
变成可执行的断言，避免靠人工 review 守规矩。

Seam：源码文本 / AST。不使用 import 副作用，只静态分析。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND = REPO_ROOT / "backend"
APP = BACKEND / "app"


def _parse(rel: str) -> ast.Module:
    return ast.parse((APP / rel).read_text(encoding="utf-8"))


def _function_level_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """收集所有「位于函数体内」的 import，返回 (行号, 模块名)。

    模块顶层 import 的 col_offset 为 0；函数体内的 import 缩进更深。
    用 lineno 配合 parent 深度判断不可靠，改用「导入语句的语句链路里存在 FunctionDef」。
    """
    found: list[tuple[int, str]] = []

    class V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.depth = 0

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
            if self.depth > 0 and node.module and node.module.startswith("app."):
                found.append((node.lineno, node.module))
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
            if self.depth > 0:
                for alias in node.names:
                    if alias.name.startswith("app."):
                        found.append((node.lineno, alias.name))
            self.generic_visit(node)

    V().visit(tree)
    return found


class TestSchedulerImportsAreExplicit:
    """scheduler.py 的依赖必须在模块顶层可见（ARCHITECTURE_REVIEW A.2.2）。

    原状：15 条函数内 ``from app... import ...``，导致依赖图无法静态分析、
    导入错误要到运行时才暴露。实测这些导入**不存在真实循环依赖**
    （唯一反向引用 plan_service → orchestrator.task_state 只依赖 db/schemas），
    因此可以全部提到模块顶层。
    """

    def test_no_function_level_app_imports(self):
        tree = _parse("orchestrator/scheduler.py")
        leftovers = _function_level_imports(tree)
        assert leftovers == [], (
            "scheduler.py 仍存在函数内延迟导入（应提到模块顶层）：\n"
            + "\n".join(f"  行 {ln}: from {mod} import ..." for ln, mod in leftovers)
        )

    def test_top_level_imports_cover_job_dependencies(self):
        """顶层必须真的导入了各 job 用到的符号，而不是删掉延迟导入后改成别处调用。"""
        source = (APP / "orchestrator" / "scheduler.py").read_text(encoding="utf-8")
        required = [
            "sync_daily",
            "generate_report",
            "analyze_daily",
            "build_today_plan",
            "push_daily_report",
            "upload_session",
            "generate_weekly",
            "generate_monthly",
            "analyze_monthly",
            "run_tredict_sync",
            "report_fault",
        ]
        missing = [name for name in required if name not in source]
        assert missing == [], f"scheduler.py 顶层缺少这些依赖符号：{missing}"


class TestNoLayeringInversion:
    """服务层不得反向依赖编排层（ARCHITECTURE_REVIEW A.1.1 / A.2.2）。

    分层约定：api / orchestrator → services → domain / adapters / storage。
    ``services/*`` 导入 ``app.orchestrator.*`` 属层级倒置：它让 services 无法在
    调度器之外单独复用，也让依赖图出现看似循环的假象（实测无真循环，只是倒置）。

    任务状态机（task_state）只依赖 db/schemas，是纯基础设施，
    应被下移到 ``app/storage/`` 或独立模块，供 services 与 orchestrator 平等使用。
    """

    def test_services_do_not_import_orchestrator(self):
        offenders: list[str] = []
        for path in (APP / "services").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mod = None
                if isinstance(node, ast.ImportFrom):
                    mod = node.module
                elif isinstance(node, ast.Import):
                    mod = node.names[0].name if node.names else None
                if mod and (mod == "app.orchestrator" or mod.startswith("app.orchestrator.")):
                    rel = path.relative_to(APP).as_posix()
                    offenders.append(f"{rel}:{node.lineno} -> {mod}")
        assert offenders == [], (
            "services 层存在对 orchestrator 的反向依赖（层级倒置）：\n  "
            + "\n  ".join(offenders)
        )


class TestModuleSizeBudget:
    """单文件体量预算（ARCHITECTURE_REVIEW A.1.2）。

    大文件不是罪，但 700+ 行的单文件意味着「装配 + 计算 + 渲染」混在一处，
    认知负担和并行修改冲突都会上升。这里设一个可执行的预算，
    并把已拆分的模块列入白名单（禁止新增超预算文件）。
    """

    # 软预算：超过即需要拆分或显式豁免
    MAX_LINES = 700

    # 已评估并接受现状的文件 → 理由
    ACCEPTED: dict[str, str] = {
        # verify.py 的 4 个测试文件直接 import 15 个内部符号与私有常量，
        # 拆分需同步改动测试与指纹断言（canonical_sha256 依赖代码结构），
        # 风险远高于收益 → 待单独立项（见 ARCHITECTURE_REVIEW A.1.2）。
        "domain/verify.py": "指纹与 15 个内部符号被测试锁定，拆分需单独立项",
    }

    def test_no_unaccepted_oversized_service_module(self):
        oversized: list[str] = []
        for path in (APP / "services").rglob("*.py"):
            rel = path.relative_to(APP).as_posix()
            if rel in self.ACCEPTED:
                continue
            lines = len(path.read_text(encoding="utf-8").splitlines())
            if lines > self.MAX_LINES:
                oversized.append(f"{rel}: {lines} 行 > {self.MAX_LINES}")
        assert oversized == [], (
            "以下 services 模块超出体量预算且未豁免（应拆分或显式登记理由）：\n  "
            + "\n  ".join(oversized)
        )


class TestLoggingConventions:
    """日志配置与命名规范（ARCHITECTURE_REVIEW A.8.1）。

    此前 ``main.py`` 与 ``scheduler.py`` 各写一份 ``basicConfig``，格式字符串重复；
    logger 命名并存 ``health-system`` / ``hs.scheduler`` / 裸 ``__name__`` 三种风格；
    ``Settings.log_level`` 定义了却无人读取。三件事都由 ``app/logging_conf.py`` 收口，
    这里把「不得回退」固化为断言。
    """

    def test_basic_config_only_in_logging_conf(self):
        offenders: list[str] = []
        for path in APP.rglob("*.py"):
            rel = path.relative_to(APP).as_posix()
            if rel == "logging_conf.py":
                continue
            if "logging.basicConfig" in path.read_text(encoding="utf-8"):
                offenders.append(rel)
        assert offenders == [], (
            "日志配置必须只在 app/logging_conf.py 里做一次，以下模块重复配置：\n  "
            + "\n  ".join(offenders)
        )

    def test_loggers_use_hs_namespace(self):
        """所有 ``logging.getLogger("字面量")`` 必须落在 ``hs.`` 命名空间。

        用 ``__name__`` 的模块会被解析成 ``app.*``，父级 ``hs.*`` 配置无法继承
        ——统一改成 ``hs.<子系统>.<模块>``。
        """
        allowed = {"apscheduler"}  # 第三方库降噪
        offenders: list[str] = []
        for path in APP.rglob("*.py"):
            rel = path.relative_to(APP).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and node.args):
                    continue
                fn = node.func
                is_getlogger = (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "getLogger"
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "logging"
                )
                if not is_getlogger:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Name) and arg.id == "__name__":
                    offenders.append(f"{rel}:{node.lineno} 使用 __name__")
                elif (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and arg.value not in allowed
                    and not arg.value.startswith("hs.")
                ):
                    offenders.append(f"{rel}:{node.lineno} 使用 {arg.value!r}")
        assert offenders == [], (
            "logger 必须使用 hs.* 命名空间（见 app/logging_conf.LOGGER_NAMESPACES）：\n  "
            + "\n  ".join(offenders)
        )
