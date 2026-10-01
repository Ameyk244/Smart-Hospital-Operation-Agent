"""audit log for hospital-data mutations (observability Phase 6)

Revision ID: 5d3e9a1b7c20
Revises: c8aa734ca08a
Create Date: 2026-10-01 00:00:00.000000

Adds `audit_log`: an append-only record of every successful hospital-data
mutation (today only `reassign_scanner`). Additive only: one new table,
two indexes, and a trigger plus function that reject UPDATE and DELETE on
that table. No existing table is altered. Downgrade drops exactly what this
adds.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.models.audit import AUDIT_LOG_IMMUTABLE_DDL, AUDIT_LOG_IMMUTABLE_DROP_DDL


# revision identifiers, used by Alembic.
revision: str = '5d3e9a1b7c20'
down_revision: Union[str, Sequence[str], None] = 'c8aa734ca08a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('audit_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=80), nullable=False),
    sa.Column('action', sa.String(length=60), nullable=False),
    sa.Column('entity_type', sa.String(length=40), nullable=False),
    sa.Column('entity_code', sa.String(length=40), nullable=False),
    sa.Column('before_json', sa.JSON(), nullable=False),
    sa.Column('after_json', sa.JSON(), nullable=False),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('trace_id', sa.String(length=32), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_log_entity_code'), 'audit_log', ['entity_code'], unique=False)
    op.create_index(op.f('ix_audit_log_occurred_at'), 'audit_log', ['occurred_at'], unique=False)
    for statement in AUDIT_LOG_IMMUTABLE_DDL:
        op.execute(statement)


def downgrade() -> None:
    """Downgrade schema."""
    for statement in AUDIT_LOG_IMMUTABLE_DROP_DDL:
        op.execute(statement)
    op.drop_index(op.f('ix_audit_log_occurred_at'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_entity_code'), table_name='audit_log')
    op.drop_table('audit_log')
