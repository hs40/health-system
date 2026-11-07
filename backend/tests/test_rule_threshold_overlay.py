"""候选阈值外置为数据文件的契约（ARCHITECTURE_REVIEW A.5.2 剩余）。

``calibration.py`` 解决了「换一套阈值会怎样」，但结论无法固化——只能改源码。
本模块给一条「数据文件 → 运行时生效」的窄通道，测试锁住它的四条语义：

1. 文件不存在 → 与代码默认值**完全一致**（绝大多数部署的常态，不能因缺文件就挂）；
2. 文件非法 → **抛错而非静默忽略**（静默忽略会让人以为「调了但没效果」）；
3. 覆盖只影响 ``thresholds``，``condition`` / ``reason`` 永远是代码；
4. ``RULES`` 保持「代码默认值」语义，生效规则走 ``effective_rules()``——
   否则 ``calibration`` 的「候选 vs 当前」会失去参照点。

顺带验证 CLI ``--file`` 与运行时文件同格式（同一套校验），
避免「CLI 通过、部署时炸」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain import rule_registry as rr
from app.domain.calibration import calibrate
from app.domain.calibration_cli import main as cli_main


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> None:
    """每个用例前后都清缓存，避免上一个用例的文件影响下一个。"""
    rr.reset_threshold_overlay_cache()
    yield
    rr.reset_threshold_overlay_cache()


def _write(tmp_path: Path, payload: object) -> Path:
    file = tmp_path / "rule_thresholds.json"
    file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return file


class TestMissingFile:
    def test_missing_file_yields_no_overlay(self, tmp_path: Path) -> None:
        assert rr.load_threshold_overlay(tmp_path / "nope.json") == {}

    def test_effective_rules_equal_defaults_when_no_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("RULE_THRESHOLDS_FILE", str(tmp_path / "absent.json"))
        rr.reset_threshold_overlay_cache()
        effective = rr.effective_rules()
        assert effective.keys() == rr.RULES.keys()
        for rule_id, spec in rr.RULES.items():
            assert effective[rule_id].thresholds == spec.thresholds


class TestInvalidFileIsRejected:
    """部署产物写错了会在下一次日报静默生效 → 加载阶段必须打回。"""

    def test_unknown_rule_id(self, tmp_path: Path) -> None:
        file = _write(tmp_path, {"P9-NOT-A-RULE": {"x": 1}})
        with pytest.raises(rr.ThresholdOverlayError, match="未知规则"):
            rr.load_threshold_overlay(file)

    def test_unknown_threshold_key(self, tmp_path: Path) -> None:
        file = _write(tmp_path, {"P1-LOAD-HRV": {"not_a_threshold": 1.0}})
        with pytest.raises(rr.ThresholdOverlayError, match="未知阈值键"):
            rr.load_threshold_overlay(file)

    def test_type_mismatch(self, tmp_path: Path) -> None:
        # ``acwr_high`` 默认是 float，给字符串应报错而不是被 str() 掉
        file = _write(tmp_path, {"P1-LOAD-HRV": {"acwr_high": "1.45"}})
        with pytest.raises(rr.ThresholdOverlayError, match="应为"):
            rr.load_threshold_overlay(file)

    def test_bool_is_not_silently_treated_as_int(self, tmp_path: Path) -> None:
        """``bool`` 是 ``int`` 的子类；True 会被当成 1 悄悄生效。单独挡掉。"""
        file = _write(tmp_path, {"P1-LOAD-HRV": {"acwr_high": True}})
        with pytest.raises(rr.ThresholdOverlayError, match="类型与默认值不符"):
            rr.load_threshold_overlay(file)

    def test_top_level_must_be_object(self, tmp_path: Path) -> None:
        file = _write(tmp_path, [1, 2, 3])
        with pytest.raises(rr.ThresholdOverlayError, match="顶层应为对象"):
            rr.load_threshold_overlay(file)

    def test_broken_json(self, tmp_path: Path) -> None:
        file = tmp_path / "broken.json"
        file.write_text("{not json", encoding="utf-8")
        with pytest.raises(rr.ThresholdOverlayError, match="无法读取"):
            rr.load_threshold_overlay(file)


class TestOverlayIsNarrow:
    def test_only_thresholds_change(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        file = _write(tmp_path, {"P1-LOAD-HRV": {"acwr_high": 1.45}})
        monkeypatch.setenv("RULE_THRESHOLDS_FILE", str(file))
        rr.reset_threshold_overlay_cache()

        spec = rr.effective_rules()["P1-LOAD-HRV"]
        default = rr.RULES["P1-LOAD-HRV"]
        assert spec.thresholds["acwr_high"] == 1.45
        # 其余阈值不变
        assert spec.thresholds["balanced_status"] == default.thresholds["balanced_status"]
        # 代码字段全部照旧（同一对象或等值）
        assert spec.condition is default.condition
        assert spec.reason is default.reason
        assert spec.rule_id == default.rule_id

    def test_global_rules_stay_untouched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``RULES`` 必须是「代码默认值」——否则 calibration 失去基线。"""
        file = _write(tmp_path, {"P1-LOAD-HRV": {"acwr_high": 1.45}})
        monkeypatch.setenv("RULE_THRESHOLDS_FILE", str(file))
        rr.reset_threshold_overlay_cache()

        _ = rr.effective_rules()  # 触发加载
        assert rr.RULES["P1-LOAD-HRV"].thresholds["acwr_high"] != 1.45

    def test_ordered_specs_reflects_overlay(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """判定链路走 ``ordered_specs()``，必须能看到覆盖值。"""
        file = _write(tmp_path, {"P1-LOAD-HRV": {"acwr_high": 1.45}})
        monkeypatch.setenv("RULE_THRESHOLDS_FILE", str(file))
        rr.reset_threshold_overlay_cache()

        by_id = {s.rule_id: s for s in rr.ordered_specs()}
        assert by_id["P1-LOAD-HRV"].thresholds["acwr_high"] == 1.45
        # 顺序不能乱（判定依赖书写顺序）
        assert [s.rule_id for s in rr.ordered_specs()] == list(rr.RULES)


class TestCalibrationUsesEffectiveBaseline:
    """校准的「当前」应当是生效值，而不是写死的代码默认值。"""

    def test_candidate_rules_default_base_is_effective(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        file = _write(tmp_path, {"P1-LOAD-HRV": {"acwr_high": 1.45}})
        monkeypatch.setenv("RULE_THRESHOLDS_FILE", str(file))
        rr.reset_threshold_overlay_cache()

        from app.domain.calibration import build_candidate_rules

        candidate = build_candidate_rules({})
        assert candidate["P1-LOAD-HRV"].thresholds["acwr_high"] == 1.45

    def test_explicit_baseline_overrides_effective(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from app.domain.calibration import build_candidate_rules

        candidate = build_candidate_rules({}, base=rr.RULES)
        assert candidate["P1-LOAD-HRV"].thresholds["acwr_high"] == rr.RULES[
            "P1-LOAD-HRV"
        ].thresholds["acwr_high"]


class TestCliReadsSameFormat:
    """``--file`` 走同一套校验：CLI 能过，部署时就不会炸。"""

    def test_cli_rejects_invalid_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        file = _write(tmp_path, {"P9-NOT-A-RULE": {"x": 1}})
        code = cli_main(["--file", str(file), "--limit", "1"])
        assert code == 2
        assert "未知规则" in capsys.readouterr().err

    def test_calibrate_signature_accepts_baseline(self) -> None:
        import inspect

        params = inspect.signature(calibrate).parameters
        assert "baseline" in params
        assert params["baseline"].default is None


class TestExampleFileStaysValid:
    """样例文件本身就是一份部署产物——必须能通过同一套校验。

    否则「照着 example 抄」会在上线时炸，而且是在下一次日报里炸。
    """

    def test_example_file_passes_validation(self) -> None:
        example = Path(__file__).resolve().parents[1] / "rule_thresholds.example.json"
        assert example.exists(), "样例文件丢失"
        overlay = rr.load_threshold_overlay(example)
        assert overlay, "样例文件应当是有效覆盖，而不是空对象"
        for rule_id, patch in overlay.items():
            for key in patch:
                assert key in rr.RULES[rule_id].thresholds
