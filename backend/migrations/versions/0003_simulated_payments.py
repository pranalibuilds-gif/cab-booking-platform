"""Add idempotent simulated ride payments."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_simulated_payments"
down_revision: str | None = "0002_booking_matching"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ride_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reference_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'SUCCEEDED', 'FAILED')",
            name="ck_payments_status",
        ),
        sa.CheckConstraint("amount_paise >= 0", name="ck_payments_amount_nonnegative"),
        sa.CheckConstraint("currency = 'INR'", name="ck_payments_currency_inr"),
        sa.CheckConstraint("attempt_count BETWEEN 0 AND 3", name="ck_payments_attempt_count"),
        sa.ForeignKeyConstraint(["ride_id"], ["rides.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ride_id", name="uq_payments_ride"),
        sa.UniqueConstraint("reference_id", name="uq_payments_reference"),
    )
    op.create_index("ix_payments_user_created", "payments", ["user_id", "created_at"])
    op.create_index("ix_payments_status_created", "payments", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_payments_status_created", table_name="payments")
    op.drop_index("ix_payments_user_created", table_name="payments")
    op.drop_table("payments")
