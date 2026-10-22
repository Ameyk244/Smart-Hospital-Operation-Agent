"""scheduled tasks and alerts

Revision ID: b7d2f4a96c18
Revises: a4c1e7b92d35
Create Date: 2026-10-22 00:00:00.000000

Adds `scheduled_tasks` and `alerts` (docs/SCHEDULER.md section 2) and the three
enum types they use. Additive only: no existing table is altered. Downgrade
drops exactly what this adds.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d2f4a96c18'
down_revision: Union[str, Sequence[str], None] = 'a4c1e7b92d35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('scheduled_tasks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=20), nullable=False),
    sa.Column('kind', sa.Enum('WATCH', name='task_kind'), nullable=False),
    sa.Column('condition', sa.Enum('QUEUE_WAIT_OVER', 'FREE_BEDS_UNDER', 'DELAYED_APPOINTMENTS_OVER', name='watch_condition'), nullable=False),
    sa.Column('threshold', sa.Integer(), nullable=False),
    sa.Column('scope_code', sa.String(length=20), nullable=True),
    sa.Column('period_seconds', sa.Integer(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_scheduled_tasks_code'), 'scheduled_tasks', ['code'], unique=True)
    op.create_table('alerts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('dedupe_key', sa.String(length=40), nullable=False),
    sa.Column('severity', sa.Enum('INFO', 'WARNING', 'CRITICAL', name='alert_severity'), nullable=False),
    sa.Column('summary', sa.String(length=200), nullable=False),
    sa.Column('entity_codes', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['scheduled_tasks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_alerts_task_id'), 'alerts', ['task_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_alerts_task_id'), table_name='alerts')
    op.drop_table('alerts')
    op.drop_index(op.f('ix_scheduled_tasks_code'), table_name='scheduled_tasks')
    op.drop_table('scheduled_tasks')
    sa.Enum(name='alert_severity').drop(op.get_bind(), checkfirst=False)
    sa.Enum(name='watch_condition').drop(op.get_bind(), checkfirst=False)
    sa.Enum(name='task_kind').drop(op.get_bind(), checkfirst=False)
