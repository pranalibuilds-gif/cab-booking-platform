import re
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_me
from app.db import get_db
from app.models import Payment, PaymentStatus, Ride, RideStatus, User
from app.payment_provider import PaymentOutcome, PaymentProviderError
from app.payment_service import PAYMENT_MAX_ATTEMPTS
from app.schemas import PaymentResponse

router = APIRouter(prefix="/api/v1", tags=["payments"])
CurrentUser = Annotated[User, Depends(get_me)]
DatabaseSession = Annotated[Session, Depends(get_db)]


def payment_response(payment: Payment) -> PaymentResponse:
    return PaymentResponse.model_validate(payment)


def accessible_payment(db: Session, payment_id: UUID, user: User) -> Payment:
    payment = db.get(Payment, payment_id)
    if payment is None or (user.role != "admin" and payment.user_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    return payment


def safe_failure_code(value: str | None) -> str:
    if value and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        return value
    return "payment_failed"


@router.get("/payments", response_model=list[PaymentResponse])
def list_payments(user: CurrentUser, db: DatabaseSession) -> list[PaymentResponse]:
    payments = db.scalars(
        select(Payment).where(Payment.user_id == user.id).order_by(Payment.created_at.desc()).limit(100)
    ).all()
    return [payment_response(payment) for payment in payments]


@router.get("/payments/{payment_id}", response_model=PaymentResponse)
def get_payment(payment_id: UUID, user: CurrentUser, db: DatabaseSession) -> PaymentResponse:
    return payment_response(accessible_payment(db, payment_id, user))


@router.post("/payments/{payment_id}/process", response_model=PaymentResponse)
def process_payment(
    payment_id: UUID,
    request: Request,
    user: CurrentUser,
    db: DatabaseSession,
) -> PaymentResponse:
    payment = db.scalar(select(Payment).where(Payment.id == payment_id).with_for_update())
    if payment is None or payment.user_id != user.id or user.role != "rider":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment not found")
    ride = db.scalar(select(Ride).where(Ride.id == payment.ride_id).with_for_update())
    if (
        ride is None
        or ride.status != RideStatus.COMPLETED
        or ride.rider_id != payment.user_id
        or ride.fare_paise != payment.amount_paise
        or ride.currency != payment.currency
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "Payment does not match a completed ride")
    if payment.status == PaymentStatus.SUCCEEDED:
        return payment_response(payment)
    if payment.status == PaymentStatus.PROCESSING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Payment is already processing")
    if payment.status not in (PaymentStatus.PENDING, PaymentStatus.FAILED):
        raise HTTPException(status.HTTP_409_CONFLICT, "Payment cannot be processed in its current state")
    if payment.attempt_count >= PAYMENT_MAX_ATTEMPTS:
        raise HTTPException(status.HTTP_409_CONFLICT, "Payment retry limit reached")

    payment.status = PaymentStatus.PROCESSING
    payment.attempt_count += 1
    payment.updated_at = datetime.now(UTC)
    provider = request.app.state.payment_provider
    try:
        outcome = provider.charge(payment.reference_id, payment.amount_paise, payment.currency)
    except PaymentProviderError as exc:
        outcome = PaymentOutcome(succeeded=False, failure_code=exc.failure_code)

    if outcome.succeeded:
        payment.status = PaymentStatus.SUCCEEDED
        payment.succeeded_at = datetime.now(UTC)
        payment.last_error = None
    else:
        payment.status = PaymentStatus.FAILED
        payment.last_error = safe_failure_code(outcome.failure_code or "payment_declined")
    payment.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(payment)
    return payment_response(payment)
