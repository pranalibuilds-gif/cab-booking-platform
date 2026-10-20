from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from math import asin, ceil, cos, radians, sin, sqrt
from uuid import UUID

from fastapi import HTTPException, status
from geoalchemy2 import Geography, Geometry
from geoalchemy2.elements import WKTElement
from sqlalchemy import cast, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AdminAuditLog,
    AssignmentAttemptStatus,
    DriverAvailability,
    DriverLocation,
    DriverProfile,
    DriverVerification,
    Ride,
    RideAssignmentAttempt,
    RideStatus,
    RideStatusHistory,
    User,
)
from app.schemas import Coordinate, FareEstimateResponse, RideResponse

ACTIVE_RIDE_STATUSES = (
    RideStatus.ACCEPTED,
    RideStatus.DRIVER_ARRIVING,
    RideStatus.DRIVER_ARRIVED,
    RideStatus.IN_PROGRESS,
)
TERMINAL_RIDE_STATUSES = {
    RideStatus.COMPLETED,
    RideStatus.CANCELLED_BY_RIDER,
    RideStatus.CANCELLED_BY_DRIVER,
    RideStatus.CANCELLED_BY_ADMIN,
    RideStatus.NO_DRIVER_FOUND,
    RideStatus.EXPIRED,
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalized_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def make_point(latitude: float, longitude: float) -> WKTElement:
    return WKTElement(f"POINT({longitude} {latitude})", srid=4326)


def point_for_database(db: Session, latitude: float, longitude: float) -> WKTElement | str:
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        return f"POINT({longitude} {latitude})"
    return make_point(latitude, longitude)


def haversine_distance_meters(
    pickup_latitude: float,
    pickup_longitude: float,
    dropoff_latitude: float,
    dropoff_longitude: float,
) -> Decimal:
    earth_radius_meters = 6_371_008.8
    lat1, lat2 = radians(pickup_latitude), radians(dropoff_latitude)
    delta_lat = lat2 - lat1
    delta_lon = radians(dropoff_longitude - pickup_longitude)
    haversine = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    distance = 2 * earth_radius_meters * asin(sqrt(haversine))
    return Decimal(str(distance))


def calculate_fare_paise(distance_meters: Decimal, base_paise: int, per_km_paise: int) -> int:
    variable_fare = distance_meters * Decimal(per_km_paise) / Decimal(1000)
    return base_paise + int(variable_fare.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def estimate_fare(pickup: Coordinate, dropoff: Coordinate, settings: Settings) -> FareEstimateResponse:
    if pickup.latitude == dropoff.latitude and pickup.longitude == dropoff.longitude:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Pickup and drop-off must differ")
    exact_distance = haversine_distance_meters(
        pickup.latitude, pickup.longitude, dropoff.latitude, dropoff.longitude
    )
    return FareEstimateResponse(
        distance_meters=max(int(exact_distance.quantize(Decimal("1"), rounding=ROUND_HALF_UP)), 1),
        fare_paise=calculate_fare_paise(exact_distance, settings.fare_base_paise, settings.fare_per_km_paise),
        currency="INR",
        fare_base_paise=settings.fare_base_paise,
        fare_per_km_paise=settings.fare_per_km_paise,
    )


def record_status(
    db: Session,
    ride: Ride,
    new_status: RideStatus,
    actor_id: UUID | None,
    reason: str | None = None,
) -> None:
    previous_status = ride.status
    ride.status = new_status
    ride.updated_at = utc_now()
    db.add(
        RideStatusHistory(
            ride_id=ride.id,
            previous_status=previous_status,
            new_status=new_status,
            actor_id=actor_id,
            reason=reason,
            created_at=utc_now(),
        )
    )


def ensure_nonterminal(ride: Ride) -> None:
    if RideStatus(ride.status) in TERMINAL_RIDE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Terminal rides cannot be modified")


def coordinates_for_ride(db: Session, ride: Ride) -> tuple[Coordinate, Coordinate]:
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        pickup_latitude, pickup_longitude = parse_wkt_point(str(ride.pickup_location))
        dropoff_latitude, dropoff_longitude = parse_wkt_point(str(ride.dropoff_location))
        return (
            Coordinate(latitude=pickup_latitude, longitude=pickup_longitude),
            Coordinate(latitude=dropoff_latitude, longitude=dropoff_longitude),
        )
    pickup_geometry = cast(ride.__class__.pickup_location, Geometry(srid=4326))
    dropoff_geometry = cast(ride.__class__.dropoff_location, Geometry(srid=4326))
    values = db.execute(
        select(
            func.ST_Y(pickup_geometry),
            func.ST_X(pickup_geometry),
            func.ST_Y(dropoff_geometry),
            func.ST_X(dropoff_geometry),
        ).where(Ride.id == ride.id)
    ).one()
    return (
        Coordinate(latitude=values[0], longitude=values[1]),
        Coordinate(latitude=values[2], longitude=values[3]),
    )


def ride_response(db: Session, ride: Ride) -> RideResponse:
    pickup, dropoff = coordinates_for_ride(db, ride)
    driver = (
        db.execute(
            select(User.full_name, DriverProfile.vehicle_make, DriverProfile.vehicle_model)
            .join(DriverProfile, DriverProfile.user_id == User.id)
            .where(User.id == ride.driver_id)
        ).one_or_none()
        if ride.driver_id is not None
        else None
    )
    vehicle = (
        " ".join(part for part in (driver.vehicle_make, driver.vehicle_model) if part)
        if driver is not None
        else None
    )
    return RideResponse(
        id=ride.id,
        rider_id=ride.rider_id,
        driver_id=ride.driver_id,
        driver_name=driver.full_name if driver is not None else None,
        driver_vehicle=vehicle or None,
        status=ride.status,
        pickup=pickup,
        dropoff=dropoff,
        distance_meters=ride.distance_meters,
        fare_paise=ride.fare_paise,
        currency="INR",
        fare_base_paise=ride.fare_base_paise,
        fare_per_km_paise=ride.fare_per_km_paise,
        request_deadline=normalized_utc(ride.request_deadline),
        created_at=normalized_utc(ride.created_at),
        updated_at=normalized_utc(ride.updated_at),
        started_at=normalized_utc(ride.started_at) if ride.started_at else None,
        completed_at=normalized_utc(ride.completed_at) if ride.completed_at else None,
    )


def offer_seconds_remaining(attempt: RideAssignmentAttempt, now: datetime | None = None) -> int:
    return max(0, ceil((normalized_utc(attempt.expires_at) - (now or utc_now())).total_seconds()))


def _finish_unmatched(db: Session, ride: Ride, settings: Settings, now: datetime) -> None:
    attempts = (
        db.scalar(
            select(func.count(RideAssignmentAttempt.id)).where(RideAssignmentAttempt.ride_id == ride.id)
        )
        or 0
    )
    if normalized_utc(ride.request_deadline) <= now:
        record_status(db, ride, RideStatus.EXPIRED, None, "Overall request lifetime elapsed")
    elif attempts >= settings.ride_max_offer_attempts:
        record_status(db, ride, RideStatus.NO_DRIVER_FOUND, None, "Maximum offer attempts exhausted")
    else:
        record_status(db, ride, RideStatus.NO_DRIVER_FOUND, None, "No eligible drivers found")


def _select_nearest_driver(
    db: Session, ride: Ride, settings: Settings, now: datetime
) -> tuple[UUID, int] | None:
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return None
    pickup, _ = coordinates_for_ride(db, ride)
    existing_driver_ids = select(RideAssignmentAttempt.driver_id).where(
        RideAssignmentAttempt.ride_id == ride.id
    )
    pickup_geography = cast(
        func.ST_SetSRID(func.ST_MakePoint(pickup.longitude, pickup.latitude), 4326),
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
    )
    active_location = DriverLocation.location
    candidates = (
        select(DriverProfile.user_id, func.ST_Distance(active_location, pickup_geography).label("distance"))
        .join(DriverLocation, DriverLocation.driver_id == DriverProfile.user_id)
        .join(User, User.id == DriverProfile.user_id)
        .where(
            DriverProfile.availability == DriverAvailability.AVAILABLE,
            DriverProfile.is_online.is_(True),
            DriverProfile.verification_status == DriverVerification.VERIFIED,
            User.is_active.is_(True),
            DriverLocation.updated_at >= now - timedelta(seconds=settings.driver_location_freshness_seconds),
            func.ST_DWithin(active_location, pickup_geography, settings.driver_search_radius_meters),
            DriverProfile.user_id.not_in(existing_driver_ids),
        )
        .order_by("distance", DriverProfile.user_id)
        .limit(1)
    )
    row = db.execute(candidates).first()
    if row is None:
        return None
    return row.user_id, int(Decimal(str(row.distance)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def offer_next_driver(
    db: Session, ride: Ride, settings: Settings, now: datetime
) -> RideAssignmentAttempt | None:
    if ride.status != RideStatus.REQUESTED:
        return None
    if normalized_utc(ride.request_deadline) <= now:
        record_status(db, ride, RideStatus.EXPIRED, None, "Overall request lifetime elapsed")
        return None
    attempt_count = (
        db.scalar(
            select(func.count(RideAssignmentAttempt.id)).where(RideAssignmentAttempt.ride_id == ride.id)
        )
        or 0
    )
    if attempt_count >= settings.ride_max_offer_attempts:
        record_status(db, ride, RideStatus.NO_DRIVER_FOUND, None, "Maximum offer attempts exhausted")
        return None
    candidate = _select_nearest_driver(db, ride, settings, now)
    if candidate is None:
        _finish_unmatched(db, ride, settings, now)
        return None
    driver_id, _ = candidate
    attempt = RideAssignmentAttempt(
        ride_id=ride.id,
        driver_id=driver_id,
        attempt_number=attempt_count + 1,
        status=AssignmentAttemptStatus.OFFERED,
        offered_at=now,
        expires_at=now + timedelta(seconds=settings.ride_offer_timeout_seconds),
    )
    db.add(attempt)
    db.flush()
    return attempt


def create_ride(
    db: Session, rider: User, pickup: Coordinate, dropoff: Coordinate, settings: Settings
) -> tuple[Ride, RideAssignmentAttempt | None]:
    if rider.role != "rider":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only riders can request rides")
    quote = estimate_fare(pickup, dropoff, settings)
    now = utc_now()
    ride = Ride(
        rider_id=rider.id,
        pickup_location=point_for_database(db, pickup.latitude, pickup.longitude),
        dropoff_location=point_for_database(db, dropoff.latitude, dropoff.longitude),
        status=RideStatus.REQUESTED,
        currency="INR",
        distance_meters=quote.distance_meters,
        fare_base_paise=quote.fare_base_paise,
        fare_per_km_paise=quote.fare_per_km_paise,
        fare_paise=quote.fare_paise,
        request_deadline=now + timedelta(seconds=settings.ride_request_lifetime_seconds),
        created_at=now,
        updated_at=now,
    )
    db.add(ride)
    db.flush()
    db.add(
        RideStatusHistory(
            ride_id=ride.id,
            previous_status=None,
            new_status=RideStatus.REQUESTED,
            actor_id=rider.id,
            created_at=now,
        )
    )
    attempt = offer_next_driver(db, ride, settings, now)
    db.commit()
    db.refresh(ride)
    if attempt is not None:
        db.refresh(attempt)
    return ride, attempt


def lock_ride(db: Session, ride_id: UUID) -> Ride:
    ride = db.scalar(select(Ride).where(Ride.id == ride_id).with_for_update())
    if ride is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ride not found")
    return ride


def available_after_ride(db: Session, driver_id: UUID) -> DriverProfile:
    profile = db.scalar(select(DriverProfile).where(DriverProfile.user_id == driver_id).with_for_update())
    if profile is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Assigned driver profile is missing")
    profile.availability = DriverAvailability.AVAILABLE if profile.is_online else DriverAvailability.OFFLINE
    return profile


def release_offer(db: Session, attempt: RideAssignmentAttempt, status_value: AssignmentAttemptStatus) -> None:
    attempt.status = status_value
    attempt.resolved_at = utc_now()


def write_admin_audit(
    db: Session, admin: User, action: str, target_type: str, target_id: UUID, reason: str
) -> None:
    db.add(
        AdminAuditLog(
            admin_id=admin.id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
        )
    )


def parse_wkt_point(point: str) -> tuple[float, float]:
    parts = point.removeprefix("POINT(").removesuffix(")").split()
    if len(parts) != 2:
        raise ValueError("Stored driver location is not a valid point")
    return float(parts[1]), float(parts[0])


def driver_location_from_point(db: Session, driver_id: UUID) -> Coordinate | None:
    if db.bind is None or db.bind.dialect.name != "postgresql":
        location = db.get(DriverLocation, driver_id)
        if location is None:
            return None
        latitude, longitude = parse_wkt_point(str(location.location))
        return Coordinate(latitude=latitude, longitude=longitude)
    row = db.execute(
        select(
            func.ST_Y(cast(DriverLocation.location, Geometry(srid=4326))),
            func.ST_X(cast(DriverLocation.location, Geometry(srid=4326))),
        ).where(DriverLocation.driver_id == driver_id)
    ).first()
    return Coordinate(latitude=row[0], longitude=row[1]) if row else None


def expire_due_offers(session_factory, settings: Settings) -> list[tuple[UUID, UUID | None]]:
    now = utc_now()
    changed: list[tuple[UUID, UUID | None]] = []
    with session_factory() as db:
        ride_ids = db.scalars(
            select(RideAssignmentAttempt.ride_id)
            .where(
                RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
                RideAssignmentAttempt.expires_at <= now,
            )
            .order_by(RideAssignmentAttempt.expires_at)
            .limit(100)
        ).all()
        for ride_id in ride_ids:
            ride = db.scalar(select(Ride).where(Ride.id == ride_id).with_for_update(skip_locked=True))
            if ride is None or ride.status != RideStatus.REQUESTED:
                continue
            attempt = db.scalar(
                select(RideAssignmentAttempt)
                .where(
                    RideAssignmentAttempt.ride_id == ride.id,
                    RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
                )
                .with_for_update()
            )
            if attempt is None or normalized_utc(attempt.expires_at) > now:
                continue
            old_driver_id = attempt.driver_id
            release_offer(db, attempt, AssignmentAttemptStatus.EXPIRED)
            next_attempt = offer_next_driver(db, ride, settings, now)
            db.commit()
            changed.append((ride.id, next_attempt.driver_id if next_attempt else old_driver_id))

        expired_ids = db.scalars(
            select(Ride.id)
            .where(Ride.status == RideStatus.REQUESTED, Ride.request_deadline <= now)
            .order_by(Ride.request_deadline)
            .limit(100)
        ).all()
        for ride_id in expired_ids:
            ride = db.scalar(select(Ride).where(Ride.id == ride_id).with_for_update(skip_locked=True))
            if ride is None or ride.status != RideStatus.REQUESTED:
                continue
            attempt = db.scalar(
                select(RideAssignmentAttempt)
                .where(
                    RideAssignmentAttempt.ride_id == ride.id,
                    RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
                )
                .with_for_update()
            )
            if attempt is not None:
                release_offer(db, attempt, AssignmentAttemptStatus.EXPIRED)
                driver_id = attempt.driver_id
            else:
                driver_id = None
            record_status(db, ride, RideStatus.EXPIRED, None, "Overall request lifetime elapsed")
            db.commit()
            changed.append((ride.id, driver_id))
    return changed


def current_offer(db: Session, driver_id: UUID) -> RideAssignmentAttempt | None:
    now = utc_now()
    return db.scalar(
        select(RideAssignmentAttempt)
        .join(Ride, Ride.id == RideAssignmentAttempt.ride_id)
        .where(
            RideAssignmentAttempt.driver_id == driver_id,
            RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
            RideAssignmentAttempt.expires_at > now,
            Ride.status == RideStatus.REQUESTED,
        )
        .order_by(RideAssignmentAttempt.offered_at)
        .limit(1)
    )


def accessible_ride(db: Session, ride_id: UUID, user: User) -> Ride:
    ride = db.get(Ride, ride_id)
    if ride is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ride not found")
    if user.role != "admin" and user.id not in (ride.rider_id, ride.driver_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ride not found")
    return ride
