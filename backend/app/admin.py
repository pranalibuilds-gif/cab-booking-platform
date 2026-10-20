from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import get_me
from app.db import get_db
from app.models import (
    AdminAuditLog,
    AssignmentAttemptStatus,
    AuthSession,
    DriverAvailability,
    DriverProfile,
    Payment,
    Ride,
    RideAssignmentAttempt,
    RideStatus,
    User,
    utc_now,
)
from app.payments import payment_response
from app.ride_service import TERMINAL_RIDE_STATUSES, ride_response
from app.schemas import (
    AdminDriverResponse,
    AdminUserActivationRequest,
    AdminUserResponse,
    PaymentResponse,
    RideResponse,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
CurrentUser = Annotated[User, Depends(get_me)]
DatabaseSession = Annotated[Session, Depends(get_db)]


def require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access is required")


@router.get("/dashboard", response_model=dict[str, object])
def dashboard(user: CurrentUser, db: DatabaseSession) -> dict[str, object]:
    require_admin(user)
    user_counts = dict(db.execute(select(User.role, func.count()).group_by(User.role)).all())
    ride_counts = dict(db.execute(select(Ride.status, func.count()).group_by(Ride.status)).all())
    payment_counts = dict(db.execute(select(Payment.status, func.count()).group_by(Payment.status)).all())
    completed_fares = db.scalar(
        select(func.coalesce(func.sum(Ride.fare_paise), 0)).where(Ride.status == RideStatus.COMPLETED)
    )
    return {
        "users_by_role": user_counts,
        "drivers_total": db.scalar(select(func.count()).select_from(DriverProfile)) or 0,
        "rides_by_status": ride_counts,
        "payments_by_status": payment_counts,
        "completed_fare_paise": int(completed_fares or 0),
        "currency": "INR",
    }


@router.get("/users", response_model=list[AdminUserResponse])
def list_users(user: CurrentUser, db: DatabaseSession) -> list[AdminUserResponse]:
    require_admin(user)
    rows = db.scalars(select(User).order_by(User.created_at.desc()).limit(200)).all()
    return [AdminUserResponse.model_validate(row) for row in rows]


@router.post("/users/{user_id}/activation", response_model=AdminUserResponse)
def set_user_activation(
    user_id: UUID,
    payload: AdminUserActivationRequest,
    user: CurrentUser,
    db: DatabaseSession,
) -> AdminUserResponse:
    require_admin(user)
    target = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if target.role == "admin":
        raise HTTPException(status.HTTP_409_CONFLICT, "Administrator accounts cannot be managed here")
    if target.is_active == payload.is_active:
        return AdminUserResponse.model_validate(target)
    if not payload.is_active:
        active_ride = db.scalar(
            select(Ride.id)
            .where(
                ((Ride.rider_id == target.id) | (Ride.driver_id == target.id)),
                Ride.status.not_in(TERMINAL_RIDE_STATUSES),
            )
            .limit(1)
        )
        if active_ride is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "User cannot be deactivated while they have an unfinished ride",
            )
        pending_offer = db.scalar(
            select(RideAssignmentAttempt.id)
            .where(
                RideAssignmentAttempt.driver_id == target.id,
                RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
            )
            .limit(1)
        )
        if pending_offer is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Driver cannot be deactivated while a ride offer is pending",
            )
        sessions = db.scalars(
            select(AuthSession).where(AuthSession.user_id == target.id).with_for_update()
        ).all()
        now = utc_now()
        for session in sessions:
            if session.revoked_at is None:
                session.revoked_at = now
        driver_profile = db.get(DriverProfile, target.id)
        if driver_profile is not None:
            driver_profile.is_online = False
            driver_profile.availability = DriverAvailability.OFFLINE
    target.is_active = payload.is_active
    db.add(
        AdminAuditLog(
            admin_id=user.id,
            action="activate_user" if payload.is_active else "deactivate_user",
            target_type="user",
            target_id=target.id,
            reason=payload.reason,
        )
    )
    db.commit()
    db.refresh(target)
    return AdminUserResponse.model_validate(target)


@router.get("/drivers", response_model=list[AdminDriverResponse])
def list_drivers(user: CurrentUser, db: DatabaseSession) -> list[AdminDriverResponse]:
    require_admin(user)
    rows = db.execute(
        select(User, DriverProfile)
        .join(DriverProfile, DriverProfile.user_id == User.id)
        .order_by(User.created_at.desc())
        .limit(200)
    ).all()
    return [
        AdminDriverResponse(
            id=driver.user_id,
            email=account.email,
            full_name=account.full_name,
            is_active=account.is_active,
            is_online=driver.is_online,
            availability=driver.availability,
            verification_status=driver.verification_status,
        )
        for account, driver in rows
    ]


@router.get("/rides", response_model=list[RideResponse])
def list_rides(user: CurrentUser, db: DatabaseSession) -> list[RideResponse]:
    require_admin(user)
    rides = db.scalars(select(Ride).order_by(Ride.created_at.desc()).limit(200)).all()
    return [ride_response(db, ride) for ride in rides]


@router.get("/payments", response_model=list[PaymentResponse])
def list_payments(user: CurrentUser, db: DatabaseSession) -> list[PaymentResponse]:
    require_admin(user)
    payments = db.scalars(select(Payment).order_by(Payment.created_at.desc()).limit(200)).all()
    return [payment_response(payment) for payment in payments]
