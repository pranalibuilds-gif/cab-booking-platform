from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class UserRole(StrEnum):
    RIDER = "rider"
    DRIVER = "driver"
    ADMIN = "admin"


class DriverAvailability(StrEnum):
    OFFLINE = "offline"
    AVAILABLE = "available"
    RESERVED = "reserved"
    ON_TRIP = "on_trip"


class DriverVerification(StrEnum):
    UNVERIFIED = "unverified"
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class RideStatus(StrEnum):
    REQUESTED = "REQUESTED"
    ACCEPTED = "ACCEPTED"
    DRIVER_ARRIVING = "DRIVER_ARRIVING"
    DRIVER_ARRIVED = "DRIVER_ARRIVED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED_BY_RIDER = "CANCELLED_BY_RIDER"
    CANCELLED_BY_DRIVER = "CANCELLED_BY_DRIVER"
    CANCELLED_BY_ADMIN = "CANCELLED_BY_ADMIN"
    NO_DRIVER_FOUND = "NO_DRIVER_FOUND"
    EXPIRED = "EXPIRED"


class AssignmentAttemptStatus(StrEnum):
    OFFERED = "OFFERED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('rider', 'driver', 'admin')", name="ck_users_role"),
        UniqueConstraint("email", name="uq_users_email"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    rider_profile: Mapped["RiderProfile | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    driver_profile: Mapped["DriverProfile | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    sessions: Mapped[list["AuthSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    rides_as_rider: Mapped[list["Ride"]] = relationship(back_populates="rider", foreign_keys="Ride.rider_id")
    rides_as_driver: Mapped[list["Ride"]] = relationship(
        back_populates="driver", foreign_keys="Ride.driver_id"
    )


class RiderProfile(Base):
    __tablename__ = "rider_profiles"

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    phone: Mapped[str | None] = mapped_column(String(32))
    user: Mapped[User] = relationship(back_populates="rider_profile")


class DriverProfile(Base):
    __tablename__ = "driver_profiles"
    __table_args__ = (
        CheckConstraint(
            "availability IN ('offline', 'available', 'reserved', 'on_trip')",
            name="ck_driver_profiles_availability",
        ),
        CheckConstraint(
            "verification_status IN ('unverified', 'pending', 'verified', 'rejected')",
            name="ck_driver_profiles_verification_status",
        ),
        UniqueConstraint("license_plate", name="uq_driver_profiles_license_plate"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    vehicle_make: Mapped[str | None] = mapped_column(String(80))
    vehicle_model: Mapped[str | None] = mapped_column(String(80))
    license_plate: Mapped[str | None] = mapped_column(String(24))
    availability: Mapped[str] = mapped_column(String(16), nullable=False, default=DriverAvailability.OFFLINE)
    is_online: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DriverVerification.UNVERIFIED
    )
    user: Mapped[User] = relationship(back_populates="driver_profile")
    location: Mapped["DriverLocation | None"] = relationship(
        back_populates="driver", cascade="all, delete-orphan", uselist=False
    )


class DriverLocation(Base):
    __tablename__ = "driver_locations"
    __table_args__ = (Index("ix_driver_locations_location_gist", "location", postgresql_using="gist"),)

    driver_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("driver_profiles.user_id", ondelete="CASCADE"), primary_key=True
    )
    location: Mapped[object] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False).with_variant(Text(), "sqlite"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    driver: Mapped[DriverProfile] = relationship(back_populates="location")


class Ride(Base):
    __tablename__ = "rides"
    __table_args__ = (
        CheckConstraint(
            "status IN ('REQUESTED', 'ACCEPTED', 'DRIVER_ARRIVING', 'DRIVER_ARRIVED', "
            "'IN_PROGRESS', 'COMPLETED', 'CANCELLED_BY_RIDER', 'CANCELLED_BY_DRIVER', "
            "'CANCELLED_BY_ADMIN', 'NO_DRIVER_FOUND', 'EXPIRED')",
            name="ck_rides_status",
        ),
        CheckConstraint("fare_paise >= 0", name="ck_rides_fare_nonnegative"),
        CheckConstraint("distance_meters > 0", name="ck_rides_distance_positive"),
        CheckConstraint("fare_base_paise >= 0", name="ck_rides_base_fare_nonnegative"),
        CheckConstraint("fare_per_km_paise >= 0", name="ck_rides_rate_nonnegative"),
        Index("ix_rides_status_deadline", "status", "request_deadline"),
        Index("ix_rides_rider_created", "rider_id", "created_at"),
        Index("ix_rides_driver_status", "driver_id", "status"),
        Index(
            "uq_rides_active_driver",
            "driver_id",
            unique=True,
            postgresql_where=text(
                "driver_id IS NOT NULL AND status IN "
                "('ACCEPTED', 'DRIVER_ARRIVING', 'DRIVER_ARRIVED', 'IN_PROGRESS')"
            ),
            sqlite_where=text(
                "driver_id IS NOT NULL AND status IN "
                "('ACCEPTED', 'DRIVER_ARRIVING', 'DRIVER_ARRIVED', 'IN_PROGRESS')"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    rider_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    driver_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    pickup_location: Mapped[object] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False).with_variant(Text(), "sqlite"),
        nullable=False,
    )
    dropoff_location: Mapped[object] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False).with_variant(Text(), "sqlite"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=RideStatus.REQUESTED, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    distance_meters: Mapped[int] = mapped_column(Integer, nullable=False)
    fare_base_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    fare_per_km_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    fare_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    request_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    rider: Mapped[User] = relationship(back_populates="rides_as_rider", foreign_keys=[rider_id])
    driver: Mapped[User | None] = relationship(back_populates="rides_as_driver", foreign_keys=[driver_id])
    assignment_attempts: Mapped[list["RideAssignmentAttempt"]] = relationship(
        back_populates="ride", cascade="all, delete-orphan"
    )
    status_history: Mapped[list["RideStatusHistory"]] = relationship(
        back_populates="ride", cascade="all, delete-orphan", order_by="RideStatusHistory.created_at"
    )
    payment: Mapped["Payment | None"] = relationship(back_populates="ride", uselist=False)


class RideAssignmentAttempt(Base):
    __tablename__ = "ride_assignment_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OFFERED', 'ACCEPTED', 'REJECTED', 'EXPIRED')",
            name="ck_ride_assignment_attempts_status",
        ),
        CheckConstraint("attempt_number BETWEEN 1 AND 5", name="ck_ride_assignment_attempts_number"),
        UniqueConstraint("ride_id", "driver_id", name="uq_ride_assignment_attempt_ride_driver"),
        UniqueConstraint("ride_id", "attempt_number", name="uq_ride_assignment_attempt_number"),
        Index("ix_ride_assignment_attempts_driver_status", "driver_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    ride_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("rides.id", ondelete="CASCADE"), nullable=False
    )
    driver_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("driver_profiles.user_id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AssignmentAttemptStatus.OFFERED)
    offered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ride: Mapped[Ride] = relationship(back_populates="assignment_attempts")
    driver: Mapped[DriverProfile] = relationship()


class RideStatusHistory(Base):
    __tablename__ = "ride_status_history"
    __table_args__ = (Index("ix_ride_status_history_ride_created", "ride_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    ride_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("rides.id", ondelete="CASCADE"), nullable=False
    )
    previous_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ride: Mapped[Ride] = relationship(back_populates="status_history")


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    admin_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'SUCCEEDED', 'FAILED')",
            name="ck_payments_status",
        ),
        CheckConstraint("amount_paise >= 0", name="ck_payments_amount_nonnegative"),
        CheckConstraint("currency = 'INR'", name="ck_payments_currency_inr"),
        CheckConstraint("attempt_count BETWEEN 0 AND 3", name="ck_payments_attempt_count"),
        UniqueConstraint("ride_id", name="uq_payments_ride"),
        UniqueConstraint("reference_id", name="uq_payments_reference"),
        Index("ix_payments_user_created", "user_id", "created_at"),
        Index("ix_payments_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    ride_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("rides.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PaymentStatus.PENDING)
    reference_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ride: Mapped[Ride] = relationship(back_populates="payment")
    user: Mapped[User] = relationship()


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    user: Mapped[User] = relationship(back_populates="sessions")
