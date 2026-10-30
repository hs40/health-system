"""stage10 acwr 增加口径版本列（G-16）

2026-10-24 用户决策：**只标记、不回填**。

- 新写入的 ``acwr`` 行盖当前 ``ACWR_VERSION``；
- 历史行保持 ``NULL``（读作 ``legacy``），不重算——重算会抹掉「当时用旧口径」
  的事实，与追溯目标相反。

Revision ID: c9d2e4f6a8b1
Revises: b3f7c1d8e6a2
Create Date: 2026-10-24 17:35:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9d2e4f6a8b1"
down_revision: str | None = "b3f7c1d8e6a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("acwr", schema=None) as batch_op:
        batch_op.add_column(sa.Column("acwr_version", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("acwr", schema=None) as batch_op:
        batch_op.drop_column("acwr_version")
