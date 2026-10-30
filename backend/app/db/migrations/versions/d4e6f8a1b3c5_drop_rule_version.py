"""stage11 删除 rule_version 死表

2026-10-25 用户决策：删除（与 G-17 同型）。

为什么删：``RuleVersionRow`` 全仓**零生产读写**（只有迁移与测试的表清单引用），
表实测 **0 行**——风险规则版本的唯一事实源是 ``rule_registry.RULE_VERSION``，
每日落 ``risk_assessment.rule_version``，从未往本表写过。保留它只会让人
误以为「规则版本已登记在表里」。

替代物：``domain/rule_registry.RULE_VERSION``（常量）+ ``risk_assessment.rule_version``
（逐日落库）+ 报告 payload 的 ``algo_versions`` / ``strategy`` 关联块。

Revision ID: d4e6f8a1b3c5
Revises: c9d2e4f6a8b1
Create Date: 2026-10-25 20:15:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e6f8a1b3c5"
down_revision: str | None = "c9d2e4f6a8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("rule_version")


def downgrade() -> None:
    # 回滚只恢复空表结构（本来也没有数据）
    op.create_table(
        "rule_version",
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("version"),
    )
