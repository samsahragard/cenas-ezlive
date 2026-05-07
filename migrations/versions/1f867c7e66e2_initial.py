"""initial

Revision ID: 1f867c7e66e2
Revises:
Create Date: 2026-03-26 17:53:56.469749

This migration was rewritten in the Claude edition:
  - All JSON columns use the cross-DB `sa.JSON` type (works on Postgres + SQLite)
    instead of Postgres-specific `JSONB`.
  - Driver schema reflects the final state from the original migration 2:
    drivers.name is non-unique, with a composite unique on (name, location);
    driver_logs has no FK to drivers.name; driver_logs.order_link is nullable.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1f867c7e66e2'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'drivers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('location', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', 'location', name='uq_driver_name_location'),
    )
    op.create_index(op.f('ix_drivers_location'), 'drivers', ['location'], unique=False)
    op.create_index(op.f('ix_drivers_name'), 'drivers', ['name'], unique=False)

    op.create_table(
        'orders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('source_filename', sa.String(length=255), nullable=True),
        sa.Column('external_order_id', sa.String(length=100), nullable=True),
        sa.Column('client', sa.String(length=255), nullable=True),
        sa.Column('upon_delivery_ask_for', sa.String(length=255), nullable=True),
        sa.Column('customer_phone', sa.String(length=50), nullable=True),
        sa.Column('delivery_address', sa.String(length=500), nullable=True),
        sa.Column('delivery_instructions', sa.Text(), nullable=True),
        sa.Column('headcount', sa.Integer(), nullable=True),
        sa.Column('reported_store', sa.String(length=255), nullable=True),
        sa.Column('reported_store_id', sa.String(length=50), nullable=True),
        sa.Column('origin_store_id', sa.String(length=50), nullable=True),
        sa.Column('delivery_date', sa.String(length=50), nullable=True),
        sa.Column('deliver_at', sa.String(length=50), nullable=True),
        sa.Column('delivery_window', sa.JSON(), nullable=True),
        sa.Column('setup_required', sa.Boolean(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('needs_review', sa.Boolean(), nullable=False),
        sa.Column('warning_count', sa.Integer(), nullable=False),
        sa.Column('flags', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_orders_external_order_id'), 'orders', ['external_order_id'], unique=False)
    op.create_index(op.f('ix_orders_status'), 'orders', ['status'], unique=False)

    op.create_table(
        'processing_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('pdf_count', sa.Integer(), nullable=False),
        sa.Column('success_count', sa.Integer(), nullable=False),
        sa.Column('failure_count', sa.Integer(), nullable=False),
        sa.Column('trigger_source', sa.String(length=50), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_processing_jobs_status'), 'processing_jobs', ['status'], unique=False)

    op.create_table(
        'driver_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('driver_name', sa.String(length=100), nullable=False),
        sa.Column('pickup_date', sa.String(length=20), nullable=False),
        sa.Column('order_link', sa.String(length=100), nullable=True),
        sa.Column('ex_miles', sa.Integer(), nullable=True),
        sa.Column('ex_miles_verified', sa.Integer(), nullable=True),
        sa.Column('on_time', sa.Boolean(), nullable=False),
        sa.Column('tracking', sa.Boolean(), nullable=False),
        sa.Column('picture', sa.Boolean(), nullable=False),
        sa.Column('five_star', sa.Boolean(), nullable=False),
        sa.Column('location', sa.String(length=50), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('logged_by', sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_driver_logs_location'), 'driver_logs', ['location'], unique=False)

    op.create_table(
        'order_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('raw_alias', sa.String(length=255), nullable=False),
        sa.Column('item_key', sa.String(length=100), nullable=True),
        sa.Column('qty', sa.Integer(), nullable=True),
        sa.Column('package_type', sa.String(length=50), nullable=True),
        sa.Column('packaging', sa.String(length=50), nullable=True),
        sa.Column('servings', sa.Integer(), nullable=True),
        sa.Column('choices', sa.JSON(), nullable=True),
        sa.Column('extras', sa.JSON(), nullable=True),
        sa.Column('flags', sa.JSON(), nullable=True),
        sa.Column('source', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_order_items_item_key'), 'order_items', ['item_key'], unique=False)
    op.create_index(op.f('ix_order_items_order_id'), 'order_items', ['order_id'], unique=False)

    op.create_table(
        'processing_orders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('processing_job_id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('source_filename', sa.String(length=255), nullable=True),
        sa.Column('external_order_id', sa.String(length=100), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('stage_failed', sa.String(length=100), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('warning_count', sa.Integer(), nullable=False),
        sa.Column('needs_review', sa.Boolean(), nullable=False),
        sa.Column('processing_seconds', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['processing_job_id'], ['processing_jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_processing_orders_external_order_id'), 'processing_orders', ['external_order_id'], unique=False)
    op.create_index(op.f('ix_processing_orders_order_id'), 'processing_orders', ['order_id'], unique=False)
    op.create_index(op.f('ix_processing_orders_processing_job_id'), 'processing_orders', ['processing_job_id'], unique=False)
    op.create_index(op.f('ix_processing_orders_stage_failed'), 'processing_orders', ['stage_failed'], unique=False)
    op.create_index(op.f('ix_processing_orders_status'), 'processing_orders', ['status'], unique=False)

    op.create_table(
        'failure_snapshots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('processing_order_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('raw_order_json', sa.JSON(), nullable=True),
        sa.Column('normalized_order_json', sa.JSON(), nullable=True),
        sa.Column('traceback_text', sa.Text(), nullable=True),
        sa.Column('text_excerpt', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['processing_order_id'], ['processing_orders.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_failure_snapshots_expires_at'), 'failure_snapshots', ['expires_at'], unique=False)
    op.create_index(op.f('ix_failure_snapshots_processing_order_id'), 'failure_snapshots', ['processing_order_id'], unique=False)

    op.create_table(
        'prep_breakdowns',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_item_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('rules_version', sa.String(length=50), nullable=True),
        sa.Column('breakdown', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['order_item_id'], ['order_items.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_prep_breakdowns_order_item_id'), 'prep_breakdowns', ['order_item_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_prep_breakdowns_order_item_id'), table_name='prep_breakdowns')
    op.drop_table('prep_breakdowns')
    op.drop_index(op.f('ix_failure_snapshots_processing_order_id'), table_name='failure_snapshots')
    op.drop_index(op.f('ix_failure_snapshots_expires_at'), table_name='failure_snapshots')
    op.drop_table('failure_snapshots')
    op.drop_index(op.f('ix_processing_orders_status'), table_name='processing_orders')
    op.drop_index(op.f('ix_processing_orders_stage_failed'), table_name='processing_orders')
    op.drop_index(op.f('ix_processing_orders_processing_job_id'), table_name='processing_orders')
    op.drop_index(op.f('ix_processing_orders_order_id'), table_name='processing_orders')
    op.drop_index(op.f('ix_processing_orders_external_order_id'), table_name='processing_orders')
    op.drop_table('processing_orders')
    op.drop_index(op.f('ix_order_items_order_id'), table_name='order_items')
    op.drop_index(op.f('ix_order_items_item_key'), table_name='order_items')
    op.drop_table('order_items')
    op.drop_index(op.f('ix_driver_logs_location'), table_name='driver_logs')
    op.drop_table('driver_logs')
    op.drop_index(op.f('ix_processing_jobs_status'), table_name='processing_jobs')
    op.drop_table('processing_jobs')
    op.drop_index(op.f('ix_orders_status'), table_name='orders')
    op.drop_index(op.f('ix_orders_external_order_id'), table_name='orders')
    op.drop_table('orders')
    op.drop_index(op.f('ix_drivers_name'), table_name='drivers')
    op.drop_index(op.f('ix_drivers_location'), table_name='drivers')
    op.drop_table('drivers')
