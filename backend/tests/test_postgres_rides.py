from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import (
    AssignmentAttemptStatus,
    DriverAvailability,
    DriverLocation,
    DriverProfile,
    DriverVerification,
    Payment,
    PaymentStatus,
    Ride,
    RideAssignmentAttempt,
    RideStatus,
    RideStatusHistory,
)
from app.payment_provider import PaymentOutcome
from app.ride_service import expire_due_offers


def register_user(client: TestClient, role: str) -> tuple[dict[str, str], str]:
    account = {
        "email": f"pg-{role}-{uuid4()}@example.com",
        "full_name": f"PostgreSQL {role.title()}",
        "password": "postgres-integration-123",
        "role": role,
    }
    assert client.post("/api/v1/auth/register", json=account).status_code == 201
    login = client.post(
        "/api/v1/auth/login",
        json={"email": account["email"], "password": account["password"]},
    )
    assert login.status_code == 200
    return login.json()["user"], login.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_driver(client: TestClient, latitude: float, longitude: float) -> tuple[dict[str, str], str]:
    driver, token = register_user(client, "driver")
    with client.app.state.session_factory() as db:
        profile = db.get(DriverProfile, UUID(driver["id"]))
        assert profile is not None
        profile.verification_status = DriverVerification.VERIFIED
        db.commit()
    location = client.post(
        "/api/v1/drivers/me/location",
        json={"latitude": latitude, "longitude": longitude},
        headers=auth(token),
    )
    assert location.status_code == 200
    availability = client.post(
        "/api/v1/drivers/me/availability",
        json={"online": True},
        headers=auth(token),
    )
    assert availability.status_code == 200
    return driver, token


def request_ride(client: TestClient, token: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/rides",
        json={
            "pickup": {"latitude": 0, "longitude": 0},
            "dropoff": {"latitude": 0, "longitude": 0.01},
        },
        headers=auth(token),
    )
    assert response.status_code == 201
    return response.json()


def test_postgis_freshness_matching_transitions_and_history(postgres_client: TestClient) -> None:
    client = postgres_client
    assert client.app.state.engine.dialect.name == "postgresql"
    _, rider_token = register_user(client, "rider")
    stale_driver, stale_token = make_driver(client, 0, 0.0005)
    fresh_driver, fresh_token = make_driver(client, 0, 0.005)
    next_driver, next_token = make_driver(client, 0, 0.008)
    with client.app.state.session_factory() as db:
        stale_location = db.get(DriverLocation, UUID(stale_driver["id"]))
        assert stale_location is not None
        stale_location.updated_at = datetime.now(UTC) - timedelta(seconds=31)
        db.commit()

    ride = request_ride(client, rider_token)
    assert ride["status"] == RideStatus.REQUESTED
    fresh_offers = client.get("/api/v1/drivers/me/offers", headers=auth(fresh_token))
    stale_offers = client.get("/api/v1/drivers/me/offers", headers=auth(stale_token))
    assert fresh_offers.status_code == 200
    assert fresh_offers.json()[0]["ride"]["id"] == ride["id"]
    assert stale_offers.json() == []

    rejection = client.post(
        f"/api/v1/rides/{ride['id']}/offers/reject",
        headers=auth(fresh_token),
    )
    assert rejection.status_code == 200
    assert rejection.json()["status"] == RideStatus.REQUESTED
    next_offers = client.get("/api/v1/drivers/me/offers", headers=auth(next_token))
    assert next_offers.status_code == 200
    assert next_offers.json()[0]["ride"]["id"] == ride["id"]
    final_rejection = client.post(
        f"/api/v1/rides/{ride['id']}/offers/reject",
        headers=auth(next_token),
    )
    assert final_rejection.status_code == 200
    assert final_rejection.json()["status"] == RideStatus.NO_DRIVER_FOUND

    with client.app.state.session_factory() as db:
        stale_location = db.get(DriverLocation, UUID(stale_driver["id"]))
        stale_profile = db.get(DriverProfile, UUID(stale_driver["id"]))
        assert stale_location is not None and stale_profile is not None
        stale_location.updated_at = datetime.now(UTC)
        stale_profile.availability = DriverAvailability.AVAILABLE
        db.commit()

    next_ride = request_ride(client, rider_token)
    assert next_ride["status"] == RideStatus.REQUESTED
    offers = client.get("/api/v1/drivers/me/offers", headers=auth(stale_token))
    assert offers.json()[0]["ride"]["id"] == next_ride["id"]
    accepted = client.post(
        f"/api/v1/rides/{next_ride['id']}/offers/accept",
        headers=auth(stale_token),
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == RideStatus.ACCEPTED
    assert accepted.json()["driver_id"] == stale_driver["id"]

    for action, expected in (
        ("arriving", RideStatus.DRIVER_ARRIVING),
        ("arrived", RideStatus.DRIVER_ARRIVED),
        ("start", RideStatus.IN_PROGRESS),
        ("complete", RideStatus.COMPLETED),
    ):
        response = client.post(
            f"/api/v1/rides/{next_ride['id']}/{action}",
            json={},
            headers=auth(stale_token),
        )
        assert response.status_code == 200
        assert response.json()["status"] == expected

    with client.app.state.session_factory() as db:
        profile = db.get(DriverProfile, UUID(stale_driver["id"]))
        assert profile is not None
        assert profile.availability == DriverAvailability.AVAILABLE
        history = db.scalars(
            select(RideStatusHistory)
            .where(RideStatusHistory.ride_id == UUID(next_ride["id"]))
            .order_by(RideStatusHistory.created_at, RideStatusHistory.id)
        ).all()
        assert [entry.new_status for entry in history] == [
            RideStatus.REQUESTED,
            RideStatus.ACCEPTED,
            RideStatus.DRIVER_ARRIVING,
            RideStatus.DRIVER_ARRIVED,
            RideStatus.IN_PROGRESS,
            RideStatus.COMPLETED,
        ]


def test_postgres_competing_assignments_allow_only_one_active_ride(postgres_client: TestClient) -> None:
    client = postgres_client
    _, rider_token = register_user(client, "rider")
    driver, driver_token = make_driver(client, 0, 0.0005)
    first_ride = request_ride(client, rider_token)
    second_ride = request_ride(client, rider_token)
    assert first_ride["status"] == RideStatus.REQUESTED
    assert second_ride["status"] == RideStatus.REQUESTED

    def accept(ride_id: str) -> int:
        return client.post(
            f"/api/v1/rides/{ride_id}/offers/accept",
            headers=auth(driver_token),
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(accept, (first_ride["id"], second_ride["id"])))
    assert sorted(results) == [200, 409]

    with client.app.state.session_factory() as db:
        active = db.scalars(
            select(Ride).where(
                Ride.driver_id == UUID(driver["id"]),
                Ride.status.in_(
                    (
                        RideStatus.ACCEPTED,
                        RideStatus.DRIVER_ARRIVING,
                        RideStatus.DRIVER_ARRIVED,
                        RideStatus.IN_PROGRESS,
                    )
                ),
            )
        ).all()
        assert len(active) == 1
        profile = db.get(DriverProfile, UUID(driver["id"]))
        assert profile is not None
        assert profile.availability == DriverAvailability.RESERVED


def test_request_lifetime_expiry_is_distinct_from_matching_exhaustion(
    postgres_client: TestClient,
) -> None:
    client = postgres_client
    _, rider_token = register_user(client, "rider")
    make_driver(client, 0, 0.0005)
    ride = request_ride(client, rider_token)
    with client.app.state.session_factory() as db:
        row = db.get(Ride, UUID(ride["id"]))
        offer = db.scalar(
            select(RideAssignmentAttempt).where(
                RideAssignmentAttempt.ride_id == UUID(ride["id"]),
                RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
            )
        )
        assert row is not None and offer is not None
        row.request_deadline = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    changed = expire_due_offers(client.app.state.session_factory, client.app.state.settings)
    assert changed
    with client.app.state.session_factory() as db:
        row = db.get(Ride, UUID(ride["id"]))
        offer = db.get(RideAssignmentAttempt, offer.id)
        assert row is not None and offer is not None
        assert row.status == RideStatus.EXPIRED
        assert offer.status == AssignmentAttemptStatus.EXPIRED
        history = db.scalars(
            select(RideStatusHistory)
            .where(RideStatusHistory.ride_id == row.id)
            .order_by(RideStatusHistory.created_at, RideStatusHistory.id)
        ).all()
        assert [entry.new_status for entry in history] == [
            RideStatus.REQUESTED,
            RideStatus.EXPIRED,
        ]
        assert db.scalar(select(Payment.id).where(Payment.ride_id == row.id)) is None


def test_offer_timeout_moves_to_next_driver(postgres_client: TestClient) -> None:
    client = postgres_client
    _, rider_token = register_user(client, "rider")
    first_driver, first_token = make_driver(client, 0, 0.0005)
    second_driver, second_token = make_driver(client, 0, 0.005)
    ride = request_ride(client, rider_token)
    first_offers = client.get("/api/v1/drivers/me/offers", headers=auth(first_token))
    assert first_offers.status_code == 200
    assert first_offers.json()[0]["ride"]["id"] == ride["id"]

    with client.app.state.session_factory() as db:
        offer = db.scalar(
            select(RideAssignmentAttempt).where(
                RideAssignmentAttempt.ride_id == UUID(ride["id"]),
                RideAssignmentAttempt.driver_id == UUID(first_driver["id"]),
                RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
            )
        )
        assert offer is not None
        offer.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    changed = expire_due_offers(client.app.state.session_factory, client.app.state.settings)
    assert changed == [(UUID(ride["id"]), UUID(second_driver["id"]))]
    assert client.get("/api/v1/drivers/me/offers", headers=auth(first_token)).json() == []
    second_offers = client.get("/api/v1/drivers/me/offers", headers=auth(second_token))
    assert second_offers.status_code == 200
    assert second_offers.json()[0]["ride"]["id"] == ride["id"]

    with client.app.state.session_factory() as db:
        attempts = db.scalars(
            select(RideAssignmentAttempt)
            .where(RideAssignmentAttempt.ride_id == UUID(ride["id"]))
            .order_by(RideAssignmentAttempt.attempt_number)
        ).all()
        assert [attempt.status for attempt in attempts] == [
            AssignmentAttemptStatus.EXPIRED,
            AssignmentAttemptStatus.OFFERED,
        ]


def test_offer_expiry_worker_pushes_terminal_ride_state_to_rider(
    postgres_client: TestClient,
) -> None:
    client = postgres_client
    rider, rider_token = register_user(client, "rider")
    make_driver(client, 0, 0.0005)
    ride = request_ride(client, rider_token)
    ticket = client.post("/api/v1/ws/tickets", headers=auth(rider_token)).json()["ticket"]

    with client.websocket_connect(f"/api/v1/ws/rides/{ride['id']}?ticket={ticket}") as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["ride"]["status"] == RideStatus.REQUESTED
        with client.app.state.session_factory() as db:
            offer = db.scalar(
                select(RideAssignmentAttempt).where(
                    RideAssignmentAttempt.ride_id == UUID(ride["id"]),
                    RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
                )
            )
            assert offer is not None
            offer.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()

        update = websocket.receive_json()
        assert update["type"] == "ride_state"
        assert update["ride"]["id"] == ride["id"]
        assert update["ride"]["status"] == RideStatus.NO_DRIVER_FOUND


def test_expired_offer_rejects_late_accept_and_finishes_without_candidates(
    postgres_client: TestClient,
) -> None:
    client = postgres_client
    _, rider_token = register_user(client, "rider")
    first_driver, first_token = make_driver(client, 0, 0.0005)
    second_driver, second_token = make_driver(client, 0, 0.005)
    ride = request_ride(client, rider_token)

    with client.app.state.session_factory() as db:
        first_attempt = db.scalar(
            select(RideAssignmentAttempt).where(
                RideAssignmentAttempt.ride_id == UUID(ride["id"]),
                RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
            )
        )
        assert first_attempt is not None
        first_attempt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    late_first_accept = client.post(
        f"/api/v1/rides/{ride['id']}/offers/accept",
        headers=auth(first_token),
    )
    assert late_first_accept.status_code == 409
    assert late_first_accept.json()["detail"] == "Ride offer has expired"
    second_offer = client.get("/api/v1/drivers/me/offers", headers=auth(second_token))
    assert second_offer.status_code == 200
    assert second_offer.json()[0]["ride"]["id"] == ride["id"]

    with client.app.state.session_factory() as db:
        second_attempt = db.scalar(
            select(RideAssignmentAttempt).where(
                RideAssignmentAttempt.ride_id == UUID(ride["id"]),
                RideAssignmentAttempt.status == AssignmentAttemptStatus.OFFERED,
            )
        )
        assert second_attempt is not None
        assert second_attempt.driver_id == UUID(second_driver["id"])
        second_attempt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    late_second_accept = client.post(
        f"/api/v1/rides/{ride['id']}/offers/accept",
        headers=auth(second_token),
    )
    assert late_second_accept.status_code == 409
    assert late_second_accept.json()["detail"] == "Ride offer has expired"

    with client.app.state.session_factory() as db:
        stored_ride = db.get(Ride, UUID(ride["id"]))
        attempts = db.scalars(
            select(RideAssignmentAttempt)
            .where(RideAssignmentAttempt.ride_id == UUID(ride["id"]))
            .order_by(RideAssignmentAttempt.attempt_number)
        ).all()
        assert stored_ride is not None
        assert stored_ride.status == RideStatus.NO_DRIVER_FOUND
        assert [attempt.status for attempt in attempts] == [
            AssignmentAttemptStatus.EXPIRED,
            AssignmentAttemptStatus.EXPIRED,
        ]
    assert client.get("/api/v1/drivers/me/offers", headers=auth(first_token)).json() == []
    assert client.get("/api/v1/drivers/me/offers", headers=auth(second_token)).json() == []


def test_postgres_duplicate_payment_processing_charges_once(postgres_client: TestClient) -> None:
    client = postgres_client
    rider, rider_token = register_user(client, "rider")
    driver, driver_token = make_driver(client, 0, 0.0005)
    ride = request_ride(client, rider_token)
    accepted = client.post(
        f"/api/v1/rides/{ride['id']}/offers/accept",
        headers=auth(driver_token),
    )
    assert accepted.status_code == 200
    for action in ("arriving", "arrived", "start", "complete"):
        transition = client.post(
            f"/api/v1/rides/{ride['id']}/{action}",
            json={},
            headers=auth(driver_token),
        )
        assert transition.status_code == 200
    payments = client.get("/api/v1/payments", headers=auth(rider_token))
    assert payments.status_code == 200
    payment_id = payments.json()[0]["id"]

    class CountingProvider:
        def __init__(self) -> None:
            self.calls = 0

        def charge(self, reference_id: str, amount_paise: int, currency: str) -> PaymentOutcome:
            self.calls += 1
            return PaymentOutcome(succeeded=True)

    provider = CountingProvider()
    client.app.state.payment_provider = provider

    def process_payment(_: int) -> tuple[int, str]:
        response = client.post(
            f"/api/v1/payments/{payment_id}/process",
            headers=auth(rider_token),
        )
        return response.status_code, response.json()["status"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(process_payment, (1, 2)))
    assert results == [(200, PaymentStatus.SUCCEEDED), (200, PaymentStatus.SUCCEEDED)]
    assert provider.calls == 1
    with client.app.state.session_factory() as db:
        payment = db.scalar(select(Payment).where(Payment.ride_id == UUID(ride["id"])))
        assert payment is not None
        assert payment.amount_paise == 6_668
        assert payment.user_id == UUID(rider["id"])
        assert payment.status == PaymentStatus.SUCCEEDED
        assert payment.attempt_count == 1
