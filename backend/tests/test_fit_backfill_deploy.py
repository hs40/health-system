"""部署脚本的逐秒心率补齐接缝。

Seam: 部署配置文件 —— 读文件内容断言关键约束，不依赖运行时环境。
风格对齐 ``test_deploy_config.py``。

v3.4.4 的自检只告警「请把本机 fit_jsonl 同步到 NAS」；v3.4.5 起容器自足，
曾在部署后触发一次立即补齐（5.1）。v3.4.8 起移除 5.1：稳定态下明细已由
23:30 调度 job + v3.4.6 上传时就地补 gz 保障，5.1 退化为零请求的幂等空跑。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _read(path: Path) -> str:
    assert path.exists(), f"文件不存在: {path}"
    return path.read_text(encoding="utf-8")


class TestFitBackfillTrigger:
    """v3.4.8：部署脚本不再触发立即补齐（由 23:30 job 与上传时就地补 gz 承担）。"""

    @staticmethod
    def _code_lines() -> list[str]:
        """只看指令行：注释里的说明文字不该让断言假绿。"""
        return [
            ln
            for ln in _read(REPO_ROOT / "deploy" / "nas_deploy_single.sh").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]

    def test_script_no_longer_triggers_backfill_on_deploy(self) -> None:
        code = "\n".join(self._code_lines())
        assert "up -d" in code, "找不到容器启动指令"
        assert "app.services.fit_backfill" not in code, (
            "部署脚本仍在触发 5.1 立即补齐 —— v3.4.8 起该步已移除"
        )
        assert "5.1 触发" not in code, "5.1 日志文案仍残留"

    def test_script_no_longer_asks_for_manual_fit_copy(self) -> None:
        code = "\n".join(self._code_lines())
        assert "请把本机" not in code, (
            "仍在要求人工搬运 fit_jsonl —— 与 v3.4.5 容器自足矛盾"
        )
        assert "自动补齐" in code, "自检未说明容器会自动补齐缺失明细"

    def test_script_still_never_moves_health_data(self) -> None:
        """运维脚本仍不搬运 raw（容器内应用按授权自行从 Garmin 拉取，两回事）。"""
        code = "\n".join(self._code_lines())
        for banned in ("cp -r", "cp -a", "rsync", "scp "):
            assert banned not in code, (
                f"部署脚本出现 {banned} —— 运维脚本不应自动搬运健康数据"
            )


class TestFitBackfillCliEntrypoint:
    """``docker exec python -m`` 要求模块有 ``__main__`` 入口。"""

    def test_module_has_cli_entrypoint(self) -> None:
        code = _read(REPO_ROOT / "backend" / "app" / "services" / "fit_backfill.py")
        assert "def main()" in code
        assert '__name__ == "__main__"' in code
