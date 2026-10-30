"""risk_assessment 表重建：三级风险 → 四级优先级 + 三态数据关卡

2026-10-22 风险引擎重设计（Q9 / Q10）。用户决策为「**清空全表 + 原地改表结构**」：

- 旧列 ``level``（normal / needsAdjustment / high）、``data_sufficiency``、
  ``hard_blocks`` **整体删除**，不做数据迁移；
- 新列 ``priority``（P1–P4）、``data_state``（A/B/C）以及 P2/P3 的输出动作字段；
- 表内全部历史行**清空**。这不是数据丢失事故：历史判定结论仍完整保存在
  ``daily_report.payload`` 中（S6 之后该字段同样按用户决策删除），
  且旧三级语义在新契约下不再有意义。

**不可逆**：downgrade 只能恢复表结构，无法恢复被清空的历史行。

Revision ID: a7c3e9b5d2f4
Revises: f2a8c4d6e9b1
Create Date: 2026-10-22 15:35:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e9b5d2f4"
down_revision: str | None = "f2a8c4d6e9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ① 清空全表（用户明确决策：不保留旧三级判定）
    op.execute("DELETE FROM risk_assessment")

    with op.batch_alter_table("risk_assessment", schema=None) as batch_op:
        # ② 删除旧索引（否则 SQLite 重建表时会因引用已删列而失败）
        batch_op.drop_index("ix_risk_assessment_level")
        batch_op.drop_index("ix_risk_assessment_date")
        # ③ 删除旧列
        batch_op.drop_column("level")
        batch_op.drop_column("data_sufficiency")
        batch_op.drop_column("hard_blocks")
        # ③ 新增新契约列
        batch_op.add_column(sa.Column("priority", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("data_state", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("missing_inputs", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("volume_halved", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("intensity_reduced", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column(
                "floored_at_recovery_template",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    # 表已清空，新列可直接设为非空
    with op.batch_alter_table("risk_assessment", schema=None) as batch_op:
        batch_op.alter_column("priority", existing_type=sa.String(), nullable=False)
        batch_op.alter_column("data_state", existing_type=sa.String(), nullable=False)
        batch_op.create_index("ix_risk_assessment_date", ["date"])
        batch_op.create_index("ix_risk_assessment_priority", ["priority"])
        batch_op.create_index("ix_risk_assessment_data_state", ["data_state"])


def downgrade() -> None:
    with op.batch_alter_table("risk_assessment", schema=None) as batch_op:
        batch_op.drop_index("ix_risk_assessment_data_state")
        batch_op.drop_index("ix_risk_assessment_priority")
        batch_op.drop_index("ix_risk_assessment_date")
        batch_op.drop_column("floored_at_recovery_template")
        batch_op.drop_column("intensity_reduced")
        batch_op.drop_column("volume_halved")
        batch_op.drop_column("missing_inputs")
        batch_op.drop_column("data_state")
        batch_op.drop_column("priority")
        batch_op.add_column(sa.Column("level", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("data_sufficiency", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("hard_blocks", sa.JSON(), nullable=True))
        batch_op.create_index("ix_risk_assessment_level", ["level"])
        batch_op.create_index("ix_risk_assessment_date", ["date"])
