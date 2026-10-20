from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import Payment, PaymentStatus, Ride, RideStatus

PAYMENT_MAX_ATTEMPTS = 3


def create_payment_for_completed_ride(db: Session, ride: Ride) -> Payment:
    if ride.status != RideStatus.COMPLETED or ride.completed_at is None:
        raise ValueError("A payment can only be created for a completed ride")
    payment = Payment(
        ride_id=ride.id,
        user_id=ride.rider_id,
        amount_paise=ride.fare_paise,
        currency=ride.currency,
        status=PaymentStatus.PENDING,
        reference_id=f"mock-{uuid4().hex}",
    )
    db.add(payment)
    return payment
