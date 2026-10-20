"""wards, beds and emergency queue

Revision ID: a4c1e7b92d35
Revises: 5d3e9a1b7c20
Create Date: 2026-10-20 00:00:00.000000

Adds `wards`, `beds` and `queue_entries` (docs/EXPANSION.md section 2) and the
three enum types they use. Additive only: no existing table is altered.
Downgrade drops exactly what this adds.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4c1e7b92d35'
down_revision: Union[str, Sequence[str], None] = '5d3e9a1b7c20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('wards',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=20), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('department_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['department_id'], ['departments.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_wards_code'), 'wards', ['code'], unique=True)
    op.create_table('beds',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=20), nullable=False),
    sa.Column('ward_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('AVAILABLE', 'OCCUPIED', 'CLEANING', name='bed_status'), nullable=False),
    sa.Column('patient_id', sa.Integer(), nullable=True),
    sa.Column('occupied_since', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.ForeignKeyConstraint(['ward_id'], ['wards.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_beds_code'), 'beds', ['code'], unique=True)
    op.create_table('queue_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=20), nullable=False),
    sa.Column('patient_id', sa.Integer(), nullable=False),
    sa.Column('urgency', sa.Enum('CRITICAL', 'URGENT', 'STANDARD', name='queue_urgency'), nullable=False),
    sa.Column('status', sa.Enum('WAITING', 'ADMITTED', 'DISCHARGED', name='queue_status'), nullable=False),
    sa.Column('arrived_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('triaged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('bed_id', sa.Integer(), nullable=True),
    sa.Column('discharged_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['bed_id'], ['beds.id'], ),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_queue_entries_code'), 'queue_entries', ['code'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_queue_entries_code'), table_name='queue_entries')
    op.drop_table('queue_entries')
    op.drop_index(op.f('ix_beds_code'), table_name='beds')
    op.drop_table('beds')
    op.drop_index(op.f('ix_wards_code'), table_name='wards')
    op.drop_table('wards')
    sa.Enum(name='queue_status').drop(op.get_bind(), checkfirst=False)
    sa.Enum(name='queue_urgency').drop(op.get_bind(), checkfirst=False)
    sa.Enum(name='bed_status').drop(op.get_bind(), checkfirst=False)
