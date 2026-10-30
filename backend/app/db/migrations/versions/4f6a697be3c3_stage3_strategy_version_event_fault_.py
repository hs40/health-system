"""stage3 strategy version event fault recovery message

创建阶段③（T3.1/T3.9/T3.10）4 张表：
- ``strategy_version``：季节策略版本（不可变）
- ``strategy_event``：策略事件
- ``fault_recovery_task``：故障恢复任务
- ``message``：系统消息

Revision ID: 4f6a697be3c3
Revises: a353d4a310d7
Create Date: 2026-10-16 19:16:58.373029

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = '4f6a697be3c3'
down_revision: str | None = 'a353d4a310d7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('fault_recovery_task',
    sa.Column('fault_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('severity', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('impact_scope', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('fix_measures', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('fixed_at', sa.DateTime(), nullable=True),
    sa.Column('precheck_status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('precheck_report', sa.JSON(), nullable=True),
    sa.Column('residual_risk', sa.JSON(), nullable=True),
    sa.Column('needs_user_confirm', sa.Boolean(), nullable=False),
    sa.Column('user_confirmed', sa.Boolean(), nullable=False),
    sa.Column('user_confirmed_at', sa.DateTime(), nullable=True),
    sa.Column('recovery_executed_at', sa.DateTime(), nullable=True),
    sa.Column('final_result', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.PrimaryKeyConstraint('fault_id')
    )
    with op.batch_alter_table('fault_recovery_task', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_fault_recovery_task_severity'), ['severity'], unique=False)

    op.create_table('message',
    sa.Column('message_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.Column('category', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('importance', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('title', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('body', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('ref_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('ref_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('link', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('is_read', sa.Boolean(), nullable=False),
    sa.Column('is_resolved', sa.Boolean(), nullable=False),
    sa.Column('send_status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.PrimaryKeyConstraint('message_id')
    )
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_message_at'), ['at'], unique=False)
        batch_op.create_index(batch_op.f('ix_message_category'), ['category'], unique=False)

    op.create_table('strategy_event',
    sa.Column('event_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('event_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('triggered_at', sa.DateTime(), nullable=False),
    sa.Column('current_version_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('target_version_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('task_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('trigger_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('steps', sa.JSON(), nullable=True),
    sa.Column('validation_result', sa.JSON(), nullable=True),
    sa.Column('failure_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('risk_state', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('notify_status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('related_report_ref', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('follow_up_status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.PrimaryKeyConstraint('event_id')
    )
    with op.batch_alter_table('strategy_event', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_strategy_event_event_type'), ['event_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_strategy_event_task_id'), ['task_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_strategy_event_triggered_at'), ['triggered_at'], unique=False)

    op.create_table('strategy_version',
    sa.Column('strategy_version_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('strategy_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('version_number', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('change_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('parent_version_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('effective_at', sa.DateTime(), nullable=True),
    sa.Column('created_by', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('change_reason', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('evidence_summary', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('expected_effect', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('actual_effect', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.Column('applicability_conditions', sa.JSON(), nullable=True),
    sa.Column('safety_constraints', sa.JSON(), nullable=True),
    sa.Column('validation_status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('validation_result', sa.JSON(), nullable=True),
    sa.Column('limitations', sa.JSON(), nullable=True),
    sa.Column('immutable', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('strategy_version_id'),
    sa.UniqueConstraint('strategy_type', 'version_number', name='uq_strategy_type_ver')
    )
    with op.batch_alter_table('strategy_version', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_strategy_version_parent_version_id'), ['parent_version_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_strategy_version_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_strategy_version_strategy_type'), ['strategy_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_strategy_version_version_number'), ['version_number'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('strategy_version', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_strategy_version_version_number'))
        batch_op.drop_index(batch_op.f('ix_strategy_version_strategy_type'))
        batch_op.drop_index(batch_op.f('ix_strategy_version_status'))
        batch_op.drop_index(batch_op.f('ix_strategy_version_parent_version_id'))

    op.drop_table('strategy_version')
    with op.batch_alter_table('strategy_event', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_strategy_event_triggered_at'))
        batch_op.drop_index(batch_op.f('ix_strategy_event_task_id'))
        batch_op.drop_index(batch_op.f('ix_strategy_event_event_type'))

    op.drop_table('strategy_event')
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_message_category'))
        batch_op.drop_index(batch_op.f('ix_message_at'))

    op.drop_table('message')
    with op.batch_alter_table('fault_recovery_task', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_fault_recovery_task_severity'))

    op.drop_table('fault_recovery_task')
