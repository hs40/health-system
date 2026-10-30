"""G-17：删除 ``data_quality`` 死表与死接口（2026-10-24 用户决策）。

事实（2026-10-24 代码级复核）：

- ``DataQualityRow`` 全仓**零写入点**（只有 ``api/data.py`` 一处 select），实测 0 行；
- 唯一读取点 ``GET /api/data/quality`` 因此**恒 404**；
- 前端零调用——日报的质量信息来自 payload 内嵌的 ``quality`` 字段。

保留它只会维持「看起来有质量历史可查」的假象，**比没有更危险**。
用户决策：删（不补写入）。质量历史仍可从 479 天日报 payload 反解。

本文件锁三条：head 不再建该表、应用不再注册该路由、drop 迁移可回滚。
"""

from __future__ import annotations

from alembic import command

from tests.test_migrations import _alembic_config, _table_names

# drop 迁移的上游（删除前的 head）
REVISION_BEFORE_DROP = "a7c3e9b5d2f4"


def test_head_no_longer_creates_data_quality_table(tmp_path, monkeypatch) -> None:
    db = tmp_path / "g17.db"
    cfg = _alembic_config(db, monkeypatch)
    command.upgrade(cfg, "head")
    assert "data_quality" not in _table_names(db)


def test_drop_migration_is_reversible(tmp_path, monkeypatch) -> None:
    """回滚只恢复空表结构（数据无法凭空恢复，本来也没有数据）。"""
    db = tmp_path / "g17_down.db"
    cfg = _alembic_config(db, monkeypatch)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, REVISION_BEFORE_DROP)
    assert "data_quality" in _table_names(db)


def test_quality_route_is_not_registered() -> None:
    """OpenAPI 里不再暴露该路径（路由对象在部分 FastAPI 版本无 ``.path``，
    用 schema 判定更稳）。"""
    from app.main import app

    assert "/api/data/quality" not in app.openapi()["paths"]
