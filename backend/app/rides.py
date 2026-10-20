from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_me
from app.config import Settings
from app.db import get_db
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
from app.payment_service import create_payment_for_completed_ride
from app.ride_hub import ride_hub
from app.ride_service import (
    ACTIVE_RIDE_STATUSES,
    accessible_ride,
    available_after_ride,
    current_offer,
    ensure_nonterminal,
    estimate_fare,
    expire_due_offers,
    normalized_utc,
    offer_next_driver,
    offer_seconds_remaining,
    point_for_database,
    record_status,
    release_offer,
    ride_response,
    utc_now,
    write_admin_audit,
)
from app.ride_service import (
    create_ride as create_ride_record,
)
from app.schemas import (
    AdminRideCancellationRequest,
    DriverAvailabilityResponse,
    DriverAvailabilityUpdate,
    DriverLocationResponse,
    DriverLocationUpdate,
    DriverSummaryResponse,
    DriverVerificationRequest,
    FareEstimateResponse,
    RideCreateRequest,
    RideHistoryResponse,
    RideOfferResponse,
    RideResponse,
    RideStatusHistoryResponse,
    RideTransitionRequest,
)

router = APIRouter(prefix="/api/v1", tags=["rides"])
CurrentUser = Annotated[User, Depends(get_me)]
DatabaseSession = Annotated[Session, Depends(get_db)]


def settings_from(request: Request) -> Settings:
    return request.app.state.settings


def require_role(user: User, *roles: str) -> None:
    if user.role not in roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This operation is not allowed for your role")


def current_offer_for_ride(db: Session, ride_id: UUID) -> RideAssignmentAttempt | None:
    return db.scalar(
        select(RideAssignmentAttempt)
        .where(
            RideAssignmentAttempt.ride_id == ride_id,
            RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
        )
        .with_for_update()
    )


async def publish_ride_state(db: Session, ride: Ride) -> None:
    await ride_hub.publish_ride(
        ride.id,
        {"type": "ride_state", "ride": ride_response(db, ride).model_dump(mode="json")},
    )


async def publish_offer(ride: Ride, attempt: RideAssignmentAttempt | None) -> None:
    if attempt is not None:
        await ride_hub.publish_driver(
            attempt.driver_id,
            {
                "type": "ride_offer",
                "ride_id": str(ride.id),
                "attempt_id": str(attempt.id),
                "attempt_number": attempt.attempt_number,
                "expires_at": normalized_utc(attempt.expires_at).isoformat(),
                "expires_in_seconds": offer_seconds_remaining(attempt),
            },
        )


def get_profile(db: Session, driver_id: UUID, *, lock: bool = False) -> DriverProfile:
    query = select(DriverProfile).where(DriverProfile.user_id == driver_id)
    if lock:
        query = query.with_for_update()
    profile = db.scalar(query)
    if profile is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Driver profile is missing")
    return profile


def _current_location_record(db: Session, driver_id: UUID) -> DriverLocation | None:
    return db.get(DriverLocation, driver_id)


@router.post("/rides", response_model=RideResponse, status_code=status.HTTP_201_CREATED)
async def request_ride(
    payload: RideCreateRequest,
    request: Request,
    user: CurrentUser,
    db: DatabaseSession,
) -> RideResponse:
    ride, attempt = create_ride_record(db, user, payload.pickup, payload.dropoff, settings_from(request))
    await publish_offer(ride, attempt)
    await publish_ride_state(db, ride)
    return ride_response(db, ride)


@router.post("/rides/estimate", response_model=FareEstimateResponse)
def estimate_ride_fare(
    payload: RideCreateRequest, request: Request, user: CurrentUser
) -> FareEstimateResponse:
    require_role(user, "rider")
    return estimate_fare(payload.pickup, payload.dropoff, settings_from(request))


@router.get("/rides", response_model=list[RideResponse])
def list_rides(user: CurrentUser, db: DatabaseSession) -> list[RideResponse]:
    query = select(Ride).order_by(Ride.created_at.desc())
    if user.role == "rider":
        query = query.where(Ride.rider_id == user.id)
    elif user.role == "driver":
        query = query.where(Ride.driver_id == user.id)
    rides = db.scalars(query.limit(100)).all()
    return [ride_response(db, ride) for ride in rides]


@router.get("/drivers/me/summary", response_model=DriverSummaryResponse)
def driver_summary(user: CurrentUser, db: DatabaseSession) -> DriverSummaryResponse:
    require_role(user, "driver")
    completed = db.execute(
        select(func.count(Ride.id), func.coalesce(func.sum(Ride.fare_paise), 0)).where(
            Ride.driver_id == user.id, Ride.status == RideStatus.COMPLETED
        )
    ).one()
    return DriverSummaryResponse(
        completed_rides=int(completed[0]),
        gross_fare_paise=int(completed[1]),
        currency="INR",
    )


@router.get("/rides/{ride_id}", response_model=RideResponse)
def get_ride(ride_id: UUID, user: CurrentUser, db: DatabaseSession) -> RideResponse:
    ride = accessible_ride(db, ride_id, user)
    return ride_response(db, ride)


@router.get("/rides/{ride_id}/history", response_model=RideHistoryResponse)
def ride_history(ride_id: UUID, user: CurrentUser, db: DatabaseSession) -> RideHistoryResponse:
    ride = accessible_ride(db, ride_id, user)
    entries = db.scalars(
        select(RideStatusHistory)
        .where(RideStatusHistory.ride_id == ride.id)
        .order_by(RideStatusHistory.created_at, RideStatusHistory.id)
    ).all()
    return RideHistoryResponse(
        ride=ride_response(db, ride),
        status_history=[
            RideStatusHistoryResponse(
                id=entry.id,
                previous_status=entry.previous_status,
                new_status=entry.new_status,
                actor_id=entry.actor_id,
                reason=entry.reason,
                created_at=normalized_utc(entry.created_at),
            )
            for entry in entries
        ],
    )


@router.post("/drivers/me/location", response_model=DriverLocationResponse)
async def update_driver_location(
    payload: DriverLocationUpdate, user: CurrentUser, db: DatabaseSession
) -> DriverLocationResponse:
    require_role(user, "driver")
    get_profile(db, user.id, lock=True)
    location = _current_location_record(db, user.id)
    now = utc_now()
    if location is None:
        location = DriverLocation(driver_id=user.id)
        db.add(location)
    location.location = point_for_database(db, payload.latitude, payload.longitude)
    location.updated_at = now
    db.commit()
    active_rides = db.scalars(
        select(Ride).where(
            Ride.driver_id == user.id,
            Ride.status.in_(ACTIVE_RIDE_STATUSES),
        )
    ).all()
    for ride in active_rides:
        await ride_hub.publish_ride(
            ride.id,
            {
                "type": "driver_location",
                "ride_id": str(ride.id),
                "driver_id": str(user.id),
                "location": payload.model_dump(mode="json"),
                "updated_at": now.isoformat(),
            },
        )
    return DriverLocationResponse(
        latitude=payload.latitude,
        longitude=payload.longitude,
        updated_at=now,
    )


@router.post("/drivers/me/availability", response_model=DriverAvailabilityResponse)
async def set_driver_availability(
    payload: DriverAvailabilityUpdate,
    request: Request,
    user: CurrentUser,
    db: DatabaseSession,
) -> DriverAvailabilityResponse:
    require_role(user, "driver")
    profile = get_profile(db, user.id, lock=True)
    if payload.online:
        if profile.verification_status != DriverVerification.VERIFIED:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Driver verification is required to go online")
        location = _current_location_record(db, user.id)
        if location is None or normalized_utc(location.updated_at) < utc_now() - timedelta(
            seconds=settings_from(request).driver_location_freshness_seconds
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Update your location before going online; the last location is missing or stale",
            )
        if profile.availability in (DriverAvailability.RESERVED, DriverAvailability.ON_TRIP):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Availability cannot be changed while assigned to a ride"
            )
        profile.is_online = True
        profile.availability = DriverAvailability.AVAILABLE
    else:
        profile.is_online = False
        if profile.availability == DriverAvailability.AVAILABLE:
            profile.availability = DriverAvailability.OFFLINE
            offers = db.scalars(
                select(RideAssignmentAttempt)
                .join(Ride, Ride.id == RideAssignmentAttempt.ride_id)
                .where(
                    RideAssignmentAttempt.driver_id == user.id,
                    RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
                    Ride.status == RideStatus.REQUESTED,
                )
                .with_for_update()
            ).all()
            changed_rides: list[Ride] = []
            new_offers: list[tuple[Ride, RideAssignmentAttempt | None]] = []
            for offer in offers:
                ride = db.scalar(select(Ride).where(Ride.id == offer.ride_id).with_for_update())
                if ride is None or ride.status != RideStatus.REQUESTED:
                    continue
                release_offer(db, offer, AssignmentAttemptStatus.EXPIRED)
                next_offer = offer_next_driver(db, ride, settings_from(request), utc_now())
                changed_rides.append(ride)
                new_offers.append((ride, next_offer))
            db.commit()
            for ride, next_offer in new_offers:
                await publish_offer(ride, next_offer)
                await publish_ride_state(db, ride)
            return DriverAvailabilityResponse(online=False, availability=profile.availability)
    db.commit()
    return DriverAvailabilityResponse(online=profile.is_online, availability=profile.availability)


@router.get("/drivers/me/availability", response_model=DriverAvailabilityResponse)
def get_driver_availability(user: CurrentUser, db: DatabaseSession) -> DriverAvailabilityResponse:
    require_role(user, "driver")
    profile = get_profile(db, user.id)
    return DriverAvailabilityResponse(online=profile.is_online, availability=profile.availability)


@router.get("/drivers/me/offers", response_model=list[RideOfferResponse])
def list_driver_offers(request: Request, user: CurrentUser, db: DatabaseSession) -> list[RideOfferResponse]:
    require_role(user, "driver")
    expire_due_offers(request.app.state.session_factory, settings_from(request))
    offer = current_offer(db, user.id)
    if offer is None:
        return []
    ride = db.get(Ride, offer.ride_id)
    if ride is None:
        return []
    return [
        RideOfferResponse(
            attempt_id=offer.id,
            attempt_number=offer.attempt_number,
            expires_at=normalized_utc(offer.expires_at),
            expires_in_seconds=offer_seconds_remaining(offer),
            ride=ride_response(db, ride),
        )
    ]


@router.post("/rides/{ride_id}/offers/accept", response_model=RideResponse)
async def accept_ride_offer(
    ride_id: UUID, request: Request, user: CurrentUser, db: DatabaseSession
) -> RideResponse:
    require_role(user, "driver")
    ride = db.scalar(select(Ride).where(Ride.id == ride_id).with_for_update())
    if ride is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ride not found")
    if ride.status != RideStatus.REQUESTED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ride is no longer awaiting a driver")
    offer = db.scalar(
        select(RideAssignmentAttempt)
        .where(
            RideAssignmentAttempt.ride_id == ride.id,
            RideAssignmentAttempt.driver_id == user.id,
            RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
        )
        .with_for_update()
    )
    if offer is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No active offer exists for this driver")
    now = utc_now()
    if normalized_utc(offer.expires_at) <= now or normalized_utc(ride.request_deadline) <= now:
        release_offer(db, offer, AssignmentAttemptStatus.EXPIRED)
        next_offer = offer_next_driver(db, ride, settings_from(request), now)
        db.commit()
        await publish_offer(ride, next_offer)
        await publish_ride_state(db, ride)
        raise HTTPException(status.HTTP_409_CONFLICT, "Ride offer has expired")

    profile = get_profile(db, user.id, lock=True)
    location = _current_location_record(db, user.id)
    if (
        profile.availability != DriverAvailability.AVAILABLE
        or not profile.is_online
        or profile.verification_status != DriverVerification.VERIFIED
        or location is None
        or normalized_utc(location.updated_at)
        < now - timedelta(seconds=settings_from(request).driver_location_freshness_seconds)
    ):
        release_offer(db, offer, AssignmentAttemptStatus.REJECTED)
        next_offer = offer_next_driver(db, ride, settings_from(request), now)
        db.commit()
        await publish_offer(ride, next_offer)
        await publish_ride_state(db, ride)
        raise HTTPException(status.HTTP_409_CONFLICT, "Driver is no longer eligible for this offer")

    offer.status = AssignmentAttemptStatus.ACCEPTED
    offer.resolved_at = now
    profile.availability = DriverAvailability.RESERVED
    ride.driver_id = user.id
    record_status(db, ride, RideStatus.ACCEPTED, user.id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Ride or driver was assigned concurrently") from exc
    db.refresh(ride)
    await publish_ride_state(db, ride)
    return ride_response(db, ride)


@router.post("/rides/{ride_id}/offers/reject", response_model=RideResponse)
async def reject_ride_offer(
    ride_id: UUID, request: Request, user: CurrentUser, db: DatabaseSession
) -> RideResponse:
    require_role(user, "driver")
    ride = db.scalar(select(Ride).where(Ride.id == ride_id).with_for_update())
    if ride is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ride not found")
    if ride.status != RideStatus.REQUESTED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ride is no longer awaiting a driver")
    offer = db.scalar(
        select(RideAssignmentAttempt)
        .where(
            RideAssignmentAttempt.ride_id == ride.id,
            RideAssignmentAttempt.driver_id == user.id,
            RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
        )
        .with_for_update()
    )
    if offer is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No active offer exists for this driver")
    release_offer(db, offer, AssignmentAttemptStatus.REJECTED)
    next_offer = offer_next_driver(db, ride, settings_from(request), utc_now())
    db.commit()
    db.refresh(ride)
    await publish_offer(ride, next_offer)
    await publish_ride_state(db, ride)
    return ride_response(db, ride)


def _driver_transition(db: Session, ride: Ride, user: User, target: RideStatus, reason: str | None) -> None:
    allowed = {
        (RideStatus.ACCEPTED, RideStatus.DRIVER_ARRIVING),
        (RideStatus.DRIVER_ARRIVING, RideStatus.DRIVER_ARRIVED),
        (RideStatus.DRIVER_ARRIVED, RideStatus.IN_PROGRESS),
        (RideStatus.IN_PROGRESS, RideStatus.COMPLETED),
    }
    current = RideStatus(ride.status)
    if user.role != "driver" or ride.driver_id != user.id or (current, target) not in allowed:
        raise HTTPException(status.HTTP_409_CONFLICT, "This ride transition is not allowed")
    profile = get_profile(db, user.id, lock=True)
    expected_availability = (
        DriverAvailability.RESERVED if target != RideStatus.COMPLETED else DriverAvailability.ON_TRIP
    )
    if profile.availability != expected_availability:
        raise HTTPException(status.HTTP_409_CONFLICT, "Driver availability is inconsistent with this ride")
    now = utc_now()
    if target == RideStatus.IN_PROGRESS:
        profile.availability = DriverAvailability.ON_TRIP
        ride.started_at = now
    elif target == RideStatus.COMPLETED:
        ride.completed_at = now
        available_after_ride(db, user.id)
    record_status(db, ride, target, user.id, reason)
    if target == RideStatus.COMPLETED:
        create_payment_for_completed_ride(db, ride)


@router.post("/rides/{ride_id}/arriving", response_model=RideResponse)
async def driver_arriving(
    ride_id: UUID, payload: RideTransitionRequest, user: CurrentUser, db: DatabaseSession
) -> RideResponse:
    require_role(user, "driver")
    ride = db.scalar(select(Ride).where(Ride.id == ride_id).with_for_update())
    if ride is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ride not found")
    ensure_nonterminal(ride)
    _driver_transition(db, ride, user, RideStatus.DRIVER_ARRIVING, payload.reason)
    db.commit()
    db.refresh(ride)
    await publish_ride_state(db, ride)
    return ride_response(db, ride)


@router.post("/rides/{ride_id}/arrived", response_model=RideResponse)
async def driver_arrived(
    ride_id: UUID, payload: RideTransitionRequest, user: CurrentUser, db: DatabaseSession
) -> RideResponse:
    require_role(user, "driver")
    ride = db.scalar(select(Ride).where(Ride.id == ride_id).with_for_update())
    if ride is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ride not found")
    ensure_nonterminal(ride)
    _driver_transition(db, ride, user, RideStatus.DRIVER_ARRIVED, payload.reason)
    db.commit()
    db.refresh(ride)
    await publish_ride_state(db, ride)
    return ride_response(db, ride)


@router.post("/rides/{ride_id}/start", response_model=RideResponse)
async def start_ride(
    ride_id: UUID, payload: RideTransitionRequest, user: CurrentUser, db: DatabaseSession
) -> RideResponse:
    require_role(user, "driver")
    ride = db.scalar(select(Ride).where(Ride.id == ride_id).with_for_update())
    if ride is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ride not found")
    ensure_nonterminal(ride)
    _driver_transition(db, ride, user, RideStatus.IN_PROGRESS, payload.reason)
    db.commit()
    db.refresh(ride)
    await publish_ride_state(db, ride)
    return ride_response(db, ride)


@router.post("/rides/{ride_id}/complete", response_model=RideResponse)
async def complete_ride(
    ride_id: UUID, payload: RideTransitionRequest, user: CurrentUser, db: DatabaseSession
) -> RideResponse:
    require_role(user, "driver")
    ride = db.scalar(select(Ride).where(Ride.id == ride_id).with_for_update())
    if ride is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ride not found")
    ensure_nonterminal(ride)
    _driver_transition(db, ride, user, RideStatus.COMPLETED, payload.reason)
    db.commit()
    db.refresh(ride)
    await publish_ride_state(db, ride)
    return ride_response(db, ride)


async def cancel_ride_by_user(
    ride_id: UUID,
    user: User,
    db: Session,
    *,
    as_admin: bool = False,
    admin_payload: AdminRideCancellationRequest | None = None,
    reason: str | None = None,
) -> RideResponse:
    ride = db.scalar(select(Ride).where(Ride.id == ride_id).with_for_update())
    if ride is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ride not found")
    ensure_nonterminal(ride)
    old_status = RideStatus(ride.status)
    if as_admin:
        if user.role != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access is required")
        if admin_payload is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Cancellation reason is required")
        if old_status == RideStatus.IN_PROGRESS and not admin_payload.confirm_driver_release:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Confirm driver release explicitly before cancelling an in-progress ride",
            )
        target = RideStatus.CANCELLED_BY_ADMIN
        reason = admin_payload.reason
        write_admin_audit(db, user, "cancel_ride", "ride", ride.id, reason)
    elif user.role == "rider" and ride.rider_id == user.id:
        if old_status not in {
            RideStatus.REQUESTED,
            RideStatus.ACCEPTED,
            RideStatus.DRIVER_ARRIVING,
            RideStatus.DRIVER_ARRIVED,
        }:
            raise HTTPException(status.HTTP_409_CONFLICT, "Rider cannot cancel this ride at this stage")
        target = RideStatus.CANCELLED_BY_RIDER
    elif user.role == "driver" and ride.driver_id == user.id:
        if old_status not in {
            RideStatus.ACCEPTED,
            RideStatus.DRIVER_ARRIVING,
            RideStatus.DRIVER_ARRIVED,
        }:
            raise HTTPException(status.HTTP_409_CONFLICT, "Driver cannot cancel this ride at this stage")
        target = RideStatus.CANCELLED_BY_DRIVER
    else:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ride not found")

    offer = current_offer_for_ride(db, ride.id)
    if offer is not None:
        release_offer(db, offer, AssignmentAttemptStatus.EXPIRED)
    if ride.driver_id is not None:
        if old_status == RideStatus.IN_PROGRESS and as_admin and not admin_payload.confirm_driver_release:
            raise HTTPException(status.HTTP_409_CONFLICT, "Driver release confirmation is required")
        available_after_ride(db, ride.driver_id)
    record_status(db, ride, target, user.id, reason)
    db.commit()
    db.refresh(ride)
    if offer is not None:
        await ride_hub.publish_driver(
            offer.driver_id, {"type": "ride_offer_expired", "ride_id": str(ride.id)}
        )
    await publish_ride_state(db, ride)
    return ride_response(db, ride)


@router.post("/rides/{ride_id}/cancel", response_model=RideResponse)
async def cancel_ride(
    ride_id: UUID,
    payload: RideTransitionRequest,
    user: CurrentUser,
    db: DatabaseSession,
) -> RideResponse:
    return await cancel_ride_by_user(ride_id, user, db, reason=payload.reason)


@router.post("/rides/{ride_id}/admin-cancel", response_model=RideResponse)
async def admin_cancel_ride(
    ride_id: UUID,
    payload: AdminRideCancellationRequest,
    user: CurrentUser,
    db: DatabaseSession,
) -> RideResponse:
    return await cancel_ride_by_user(
        ride_id,
        user,
        db,
        as_admin=True,
        admin_payload=payload,
    )


@router.post("/admin/drivers/{driver_id}/verification", response_model=dict[str, str])
def set_driver_verification(
    driver_id: UUID,
    payload: DriverVerificationRequest,
    user: CurrentUser,
    db: DatabaseSession,
) -> dict[str, str]:
    require_role(user, "admin")
    profile = get_profile(db, driver_id, lock=True)
    profile.verification_status = (
        DriverVerification.VERIFIED if payload.verified else DriverVerification.REJECTED
    )
    if not payload.verified and profile.availability == DriverAvailability.AVAILABLE:
        profile.availability = DriverAvailability.OFFLINE
        profile.is_online = False
    db.add(
        AdminAuditLog(
            admin_id=user.id,
            action="verify_driver" if payload.verified else "reject_driver",
            target_type="driver",
            target_id=driver_id,
            reason=payload.reason,
        )
    )
    db.commit()
    return {"verification_status": profile.verification_status}
