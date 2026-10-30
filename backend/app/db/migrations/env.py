"""Alembic 环境配置（T1.1）。

要点：
- 数据库 URL 从 ``app.config.Settings`` 读取，**不在此硬编码**（不同环境路径不同）；
- ``target_metadata`` 指向 ``SQLModel.metadata``，使 ``--autogenerate`` 能感知模型；
- 采用 ``render_as_batch=True``：SQLite 的 ALTER 能力有限，批量渲染支持后续改列。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from app.config import get_settings

# 确保所有模型被注册到 metadata
from app.db import models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# URL 解析优先级：调用方显式注入（如测试）> 运行时 Settings。
# alembic.ini 中的占位 URL 视为未设置。
_PLACEHOLDER = "sqlite:///./data/db/health.db"
if config.get_main_option("sqlalchemy.url") in (None, "", _PLACEHOLDER):
    config.set_main_option("sqlalchemy.url", get_settings().sqlite_url)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL，不连库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接实际数据库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
