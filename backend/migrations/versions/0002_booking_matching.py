"""Add PostGIS rides, driver locations, matching attempts, and status history."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects import postgresql

revision: str = "0002_booking_matching"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RIDE_STATUSES = (
    "'REQUESTED', 'ACCEPTED', 'DRIVER_ARRIVING', 'DRIVER_ARRIVED', 'IN_PROGRESS', "
    "'COMPLETED', 'CANCELLED_BY_RIDER', 'CANCELLED_BY_DRIVER', 'CANCELLED_BY_ADMIN', "
    "'NO_DRIVER_FOUND', 'EXPIRED'"
)


def upgrade() -> None:
    op.add_column(
        "driver_profiles",
        sa.Column("is_online", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "driver_locations",
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "location", Geography(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["driver_id"], ["driver_profiles.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("driver_id"),
    )
    op.create_index(
        "ix_driver_locations_location_gist", "driver_locations", ["location"], postgresql_using="gist"
    )
    op.create_index("ix_driver_locations_updated_at", "driver_locations", ["updated_at"])

    op.create_table(
        "rides",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "pickup_location",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column(
            "dropoff_location",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("distance_meters", sa.Integer(), nullable=False),
        sa.Column("fare_base_paise", sa.Integer(), nullable=False),
        sa.Column("fare_per_km_paise", sa.Integer(), nullable=False),
        sa.Column("fare_paise", sa.Integer(), nullable=False),
        sa.Column("request_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"status IN ({RIDE_STATUSES})", name="ck_rides_status"),
        sa.CheckConstraint("fare_paise >= 0", name="ck_rides_fare_nonnegative"),
        sa.CheckConstraint("distance_meters > 0", name="ck_rides_distance_positive"),
        sa.CheckConstraint("fare_base_paise >= 0", name="ck_rides_base_fare_nonnegative"),
        sa.CheckConstraint("fare_per_km_paise >= 0", name="ck_rides_rate_nonnegative"),
        sa.ForeignKeyConstraint(["rider_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["driver_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rides_status", "rides", ["status"])
    op.create_index("ix_rides_status_deadline", "rides", ["status", "request_deadline"])
    op.create_index("ix_rides_rider_created", "rides", ["rider_id", "created_at"])
    op.create_index("ix_rides_driver_status", "rides", ["driver_id", "status"])
    op.create_index(
        "uq_rides_active_driver",
        "rides",
        ["driver_id"],
        unique=True,
        postgresql_where=sa.text(
            "driver_id IS NOT NULL AND status IN "
            "('ACCEPTED', 'DRIVER_ARRIVING', 'DRIVER_ARRIVED', 'IN_PROGRESS')"
        ),
    )

    op.create_table(
        "ride_assignment_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ride_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("offered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('OFFERED', 'ACCEPTED', 'REJECTED', 'EXPIRED')",
            name="ck_ride_assignment_attempts_status",
        ),
        sa.CheckConstraint("attempt_number BETWEEN 1 AND 5", name="ck_ride_assignment_attempts_number"),
        sa.ForeignKeyConstraint(["ride_id"], ["rides.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["driver_id"], ["driver_profiles.user_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ride_id", "driver_id", name="uq_ride_assignment_attempt_ride_driver"),
        sa.UniqueConstraint("ride_id", "attempt_number", name="uq_ride_assignment_attempt_number"),
    )
    op.create_index(
        "ix_ride_assignment_attempts_driver_status",
        "ride_assignment_attempts",
        ["driver_id", "status"],
    )

    op.create_table(
        "ride_status_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ride_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=True),
        sa.Column("new_status", sa.String(length=32), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["ride_id"], ["rides.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ride_status_history_ride_created", "ride_status_history", ["ride_id", "created_at"])

    op.create_table(
        "admin_audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("admin_audit_logs")
    op.drop_index("ix_ride_status_history_ride_created", table_name="ride_status_history")
    op.drop_table("ride_status_history")
    op.drop_index("ix_ride_assignment_attempts_driver_status", table_name="ride_assignment_attempts")
    op.drop_table("ride_assignment_attempts")
    op.drop_index("uq_rides_active_driver", table_name="rides")
    op.drop_index("ix_rides_driver_status", table_name="rides")
    op.drop_index("ix_rides_rider_created", table_name="rides")
    op.drop_index("ix_rides_status_deadline", table_name="rides")
    op.drop_index("ix_rides_status", table_name="rides")
    op.drop_table("rides")
    op.drop_index("ix_driver_locations_updated_at", table_name="driver_locations")
    op.drop_index("ix_driver_locations_location_gist", table_name="driver_locations")
    op.drop_table("driver_locations")
    op.drop_column("driver_profiles", "is_online")
