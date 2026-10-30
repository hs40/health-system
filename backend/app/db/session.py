"""数据库连接与会话管理（SQLite + WAL）。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.sqlite_url,
    echo=False,
    connect_args={"check_same_thread": False},
)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    """开启 WAL、外键约束与并发参数。

    2026-10-16 加固：`hs-backend` 与 `hs-scheduler` **共用同一个 SQLite 文件**
    （两容器都挂载 `./data/db`），并发写会争锁，故：
    - `busy_timeout` 由 5s 提到 15s（降低 NAS 慢 I/O 下的 `database is locked`）；
    - `synchronous=NORMAL`（WAL 官方推荐值），减少 fsync 压力，兼顾安全与吞吐。
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def init_db() -> None:
    """建表（幂等）。生产迁移用 Alembic，此函数用于开发与容器首启。"""
    _settings.ensure_dirs()
    # 确保所有模型被注册
    from app.db import models  # noqa: F401

    SQLModel.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI 依赖注入用。"""
    with Session(engine) as session:
        yield session
