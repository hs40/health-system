"""部署脚本的逐秒心率补齐接缝（v3.4.5）。

Seam: 部署配置文件 —— 读文件内容断言关键约束，不依赖运行时环境。
风格对齐 ``test_deploy_config.py``。

v3.4.4 的自检只告警「请把本机 fit_jsonl 同步到 NAS」。v3.4.5 起容器自足，
部署脚本必须在启动容器后触发一次立即补齐，且不再要求人工搬运。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _read(path: Path) -> str:
    assert path.exists(), f"文件不存在: {path}"
    return path.read_text(encoding="utf-8")


class TestFitBackfillTrigger:
    """v3.4.5：容器启动后必须触发一次立即补齐。"""

    @staticmethod
    def _code_lines() -> list[str]:
        """只看指令行：注释里的说明文字不该让断言假绿。"""
        return [
            ln
            for ln in _read(REPO_ROOT / "deploy" / "nas_deploy_single.sh").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]

    def test_script_triggers_backfill_after_container_is_up(self) -> None:
        code = "\n".join(self._code_lines())
        up_at = code.find("up -d")
        trigger_at = code.find("app.services.fit_backfill")
        assert up_at != -1, "找不到容器启动指令"
        assert trigger_at != -1, "部署脚本未触发容器内的逐秒心率补齐"
        assert up_at < trigger_at, "补齐必须在容器启动之后"

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
