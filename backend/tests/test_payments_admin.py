from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import (
    AdminAuditLog,
    AuthSession,
    DriverAvailability,
    DriverProfile,
    Payment,
    PaymentStatus,
    Ride,
    RideStatus,
    RideStatusHistory,
    User,
)
from app.payment_provider import PaymentOutcome
from app.security import hash_password


def create_account(client: TestClient, role: str) -> tuple[User, str]:
    email = f"{role}-{uuid4()}@example.com"
    password = "test-password-123"
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "full_name": f"Phase 3 {role.title()}",
            "password": password,
            "role": role,
        },
    )
    assert response.status_code == 201
    login = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return User(id=UUID(login.json()["user"]["id"]), email=email, full_name=role, role=role), login.json()[
        "access_token"
    ]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


def make_assigned_ride(client: TestClient, rider: User, driver: User) -> Ride:
    now = datetime.now(UTC)
    ride = Ride(
        rider_id=rider.id,
        driver_id=driver.id,
        pickup_location="POINT(-73.99 40.73)",
        dropoff_location="POINT(-73.98 40.74)",
        status=RideStatus.ACCEPTED,
        currency="INR",
        distance_meters=1_500,
        fare_base_paise=5_000,
        fare_per_km_paise=1_500,
        fare_paise=7_250,
        request_deadline=now + timedelta(minutes=5),
        created_at=now,
        updated_at=now,
    )
    with client.app.state.session_factory() as db:
        profile = db.get(DriverProfile, driver.id)
        assert profile is not None
        profile.is_online = True
        profile.availability = DriverAvailability.RESERVED
        db.add(ride)
        db.flush()
        db.add(
            RideStatusHistory(
                ride_id=ride.id,
                previous_status=None,
                new_status=RideStatus.ACCEPTED,
                actor_id=rider.id,
            )
        )
        db.commit()
        db.refresh(ride)
        return ride


def complete_ride(client: TestClient, rider: User, rider_token: str, driver: User, driver_token: str) -> Ride:
    ride = make_assigned_ride(client, rider, driver)
    headers = auth(driver_token)
    for action in ("arriving", "arrived", "start", "complete"):
        response = client.post(f"/api/v1/rides/{ride.id}/{action}", json={}, headers=headers)
        assert response.status_code == 200
    return ride


class SequenceProvider:
    def __init__(self, outcomes: list[PaymentOutcome]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, int, str]] = []

    def charge(self, reference_id: str, amount_paise: int, currency: str) -> PaymentOutcome:
        self.calls.append((reference_id, amount_paise, currency))
        return self.outcomes.pop(0)


def test_payment_is_created_only_after_completed_ride_and_uses_fare_snapshot(client: TestClient) -> None:
    rider, rider_token = create_account(client, "rider")
    driver, driver_token = create_account(client, "driver")
    ride = make_assigned_ride(client, rider, driver)
    rider_headers = auth(rider_token)
    assert client.get("/api/v1/payments", headers=rider_headers).json() == []

    for action in ("arriving", "arrived", "start"):
        assert (
            client.post(
                f"/api/v1/rides/{ride.id}/{action}",
                json={},
                headers=auth(driver_token),
            ).status_code
            == 200
        )
        assert client.get("/api/v1/payments", headers=rider_headers).json() == []

    completed = client.post(
        f"/api/v1/rides/{ride.id}/complete",
        json={},
        headers=auth(driver_token),
    )
    assert completed.status_code == 200
    payments = client.get("/api/v1/payments", headers=rider_headers)
    assert payments.status_code == 200
    assert len(payments.json()) == 1
    payment = payments.json()[0]
    assert payment["ride_id"] == str(ride.id)
    assert payment["user_id"] == str(rider.id)
    assert payment["amount_paise"] == 7_250
    assert payment["currency"] == "INR"
    assert payment["status"] == PaymentStatus.PENDING
    assert payment["attempt_count"] == 0

    duplicate_completion = client.post(
        f"/api/v1/rides/{ride.id}/complete",
        json={},
        headers=auth(driver_token),
    )
    assert duplicate_completion.status_code == 409
    assert len(client.get("/api/v1/payments", headers=rider_headers).json()) == 1


def test_payment_fail_retry_and_success_are_idempotent_and_owned(client: TestClient) -> None:
    rider, rider_token = create_account(client, "rider")
    driver, driver_token = create_account(client, "driver")
    other_rider, other_rider_token = create_account(client, "rider")
    ride = complete_ride(client, rider, rider_token, driver, driver_token)
    payment = client.get("/api/v1/payments", headers=auth(rider_token)).json()[0]
    provider = SequenceProvider(
        [
            PaymentOutcome(succeeded=False, failure_code="mock_declined"),
            PaymentOutcome(succeeded=True),
        ]
    )
    client.app.state.payment_provider = provider
    payment_url = f"/api/v1/payments/{payment['id']}"

    assert client.get(payment_url, headers=auth(other_rider_token)).status_code == 404
    assert (
        client.post(
            f"{payment_url}/process",
            json={"amount_paise": 1},
            headers=auth(other_rider_token),
        ).status_code
        == 404
    )
    assert client.post(f"{payment_url}/process", headers=auth(driver_token)).status_code == 404

    failed = client.post(
        f"{payment_url}/process",
        json={"amount_paise": 1},
        headers=auth(rider_token),
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == PaymentStatus.FAILED
    assert failed.json()["attempt_count"] == 1
    assert failed.json()["last_error"] == "mock_declined"

    succeeded = client.post(f"{payment_url}/process", headers=auth(rider_token))
    assert succeeded.status_code == 200
    assert succeeded.json()["status"] == PaymentStatus.SUCCEEDED
    assert succeeded.json()["attempt_count"] == 2
    assert succeeded.json()["succeeded_at"] is not None
    duplicate = client.post(f"{payment_url}/process", headers=auth(rider_token))
    assert duplicate.status_code == 200
    assert duplicate.json()["attempt_count"] == 2
    assert len(provider.calls) == 2
    assert {call[1:] for call in provider.calls} == {(7_250, "INR")}
    assert len({call[0] for call in provider.calls}) == 1

    with client.app.state.session_factory() as db:
        stored = db.scalar(select(Payment).where(Payment.ride_id == ride.id))
        assert stored is not None
        assert stored.status == PaymentStatus.SUCCEEDED


def test_payment_processing_rechecks_completed_ride_and_amount(client: TestClient) -> None:
    rider, rider_token = create_account(client, "rider")
    driver, _ = create_account(client, "driver")
    ride = make_assigned_ride(client, rider, driver)
    with client.app.state.session_factory() as db:
        payment = Payment(
            ride_id=ride.id,
            user_id=rider.id,
            amount_paise=1,
            currency="INR",
            status=PaymentStatus.PENDING,
            reference_id=f"mock-{uuid4().hex}",
        )
        db.add(payment)
        db.commit()
        payment_id = payment.id

    response = client.post(
        f"/api/v1/payments/{payment_id}/process",
        headers=auth(rider_token),
    )
    assert response.status_code == 409
    with client.app.state.session_factory() as db:
        stored = db.get(Payment, payment_id)
        assert stored is not None
        assert stored.status == PaymentStatus.PENDING
        assert stored.attempt_count == 0


def test_payment_retry_limit_and_unsuccessful_rides_create_no_payment(client: TestClient) -> None:
    rider, rider_token = create_account(client, "rider")
    driver, driver_token = create_account(client, "driver")
    cancelled_ride = make_assigned_ride(client, rider, driver)
    cancelled = client.post(
        f"/api/v1/rides/{cancelled_ride.id}/cancel",
        json={"reason": "Changed plans"},
        headers=auth(rider_token),
    )
    assert cancelled.status_code == 200
    assert client.get("/api/v1/payments", headers=auth(rider_token)).json() == []

    no_match = client.post(
        "/api/v1/rides",
        json={
            "pickup": {"latitude": 0, "longitude": 0},
            "dropoff": {"latitude": 0, "longitude": 0.01},
        },
        headers=auth(rider_token),
    )
    assert no_match.status_code == 201
    assert no_match.json()["status"] == RideStatus.NO_DRIVER_FOUND
    assert client.get("/api/v1/payments", headers=auth(rider_token)).json() == []

    other_driver, other_driver_token = create_account(client, "driver")
    retry_ride = complete_ride(client, rider, rider_token, other_driver, other_driver_token)
    payment = client.get("/api/v1/payments", headers=auth(rider_token)).json()[0]
    provider = SequenceProvider(
        [PaymentOutcome(succeeded=False, failure_code="mock_declined") for _ in range(3)]
    )
    client.app.state.payment_provider = provider
    for expected_attempt in range(1, 4):
        response = client.post(f"/api/v1/payments/{payment['id']}/process", headers=auth(rider_token))
        assert response.status_code == 200
        assert response.json()["status"] == PaymentStatus.FAILED
        assert response.json()["attempt_count"] == expected_attempt
    exhausted = client.post(f"/api/v1/payments/{payment['id']}/process", headers=auth(rider_token))
    assert exhausted.status_code == 409
    assert len(provider.calls) == 3
    assert retry_ride.id == UUID(payment["ride_id"])


def test_admin_dashboard_inspection_and_driver_summary_are_role_protected(
    client: TestClient,
) -> None:
    rider, rider_token = create_account(client, "rider")
    driver, driver_token = create_account(client, "driver")
    ride = complete_ride(client, rider, rider_token, driver, driver_token)
    with client.app.state.session_factory() as db:
        admin = User(
            email=f"admin-{uuid4()}@example.com",
            full_name="Phase 3 Admin",
            password_hash=hash_password("test-password-123"),
            role="admin",
        )
        db.add(admin)
        db.commit()
        db.add(
            AdminAuditLog(
                admin_id=admin.id,
                action="verify_driver",
                target_type="driver",
                target_id=driver.id,
                reason="Test audit",
            )
        )
        db.commit()
        admin_email = admin.email

    admin_login = client.post(
        "/api/v1/auth/login",
        json={"email": admin_email, "password": "test-password-123"},
    )
    assert admin_login.status_code == 200
    admin_headers = auth(admin_login.json()["access_token"])

    assert client.get("/api/v1/admin/dashboard", headers=auth(rider_token)).status_code == 403
    summary = client.get("/api/v1/admin/dashboard", headers=admin_headers)
    assert summary.status_code == 200
    assert summary.json()["users_by_role"]["rider"] == 1
    assert summary.json()["drivers_total"] == 1
    assert summary.json()["rides_by_status"][RideStatus.COMPLETED] == 1
    assert summary.json()["completed_fare_paise"] == 7_250
    assert client.get("/api/v1/admin/users", headers=auth(driver_token)).status_code == 403
    users = client.get("/api/v1/admin/users", headers=admin_headers)
    assert users.status_code == 200
    assert all("password_hash" not in item and "sessions" not in item for item in users.json())
    assert len(client.get("/api/v1/admin/drivers", headers=admin_headers).json()) == 1
    assert len(client.get("/api/v1/admin/rides", headers=admin_headers).json()) == 1
    payments = client.get("/api/v1/admin/payments", headers=admin_headers)
    assert payments.status_code == 200
    assert len(payments.json()) == 1
    assert payments.json()[0]["ride_id"] == str(ride.id)

    earnings = client.get("/api/v1/drivers/me/summary", headers=auth(driver_token))
    assert earnings.status_code == 200
    assert earnings.json() == {
        "completed_rides": 1,
        "gross_fare_paise": 7_250,
        "currency": "INR",
    }
    assert client.get("/api/v1/drivers/me/summary", headers=auth(rider_token)).status_code == 403


def test_admin_user_deactivation_revokes_sessions_and_records_audit(client: TestClient) -> None:
    target, target_token = create_account(client, "rider")
    admin_email = f"admin-{uuid4()}@example.com"
    with client.app.state.session_factory() as db:
        admin = User(
            email=admin_email,
            full_name="Account Admin",
            password_hash=hash_password("test-password-123"),
            role="admin",
        )
        db.add(admin)
        db.commit()
    admin_login = client.post(
        "/api/v1/auth/login",
        json={"email": admin_email, "password": "test-password-123"},
    )
    admin_headers = auth(admin_login.json()["access_token"])

    deactivated = client.post(
        f"/api/v1/admin/users/{target.id}/activation",
        json={"is_active": False, "reason": "Account review"},
        headers=admin_headers,
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["is_active"] is False
    assert client.get("/api/v1/auth/me", headers=auth(target_token)).status_code == 401
    with client.app.state.session_factory() as db:
        sessions = db.scalars(select(AuthSession).where(AuthSession.user_id == target.id)).all()
        assert sessions
        assert all(session.revoked_at is not None for session in sessions)
        audit = db.scalar(
            select(AdminAuditLog).where(
                AdminAuditLog.target_id == target.id,
                AdminAuditLog.action == "deactivate_user",
            )
        )
        assert audit is not None
        assert audit.reason == "Account review"

    reactivated = client.post(
        f"/api/v1/admin/users/{target.id}/activation",
        json={"is_active": True, "reason": "Review complete"},
        headers=admin_headers,
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["is_active"] is True


def test_admin_cannot_deactivate_user_with_unfinished_ride(client: TestClient) -> None:
    rider, _ = create_account(client, "rider")
    driver, _ = create_account(client, "driver")
    ride = make_assigned_ride(client, rider, driver)
    admin_email = f"admin-{uuid4()}@example.com"
    with client.app.state.session_factory() as db:
        admin = User(
            email=admin_email,
            full_name="Ride Admin",
            password_hash=hash_password("test-password-123"),
            role="admin",
        )
        db.add(admin)
        db.commit()
    admin_login = client.post(
        "/api/v1/auth/login",
        json={"email": admin_email, "password": "test-password-123"},
    )
    response = client.post(
        f"/api/v1/admin/users/{driver.id}/activation",
        json={"is_active": False, "reason": "Account review"},
        headers=auth(admin_login.json()["access_token"]),
    )
    assert response.status_code == 409
    with client.app.state.session_factory() as db:
        current_driver = db.get(User, driver.id)
        current_ride = db.get(Ride, ride.id)
        audit = db.scalar(
            select(AdminAuditLog).where(
                AdminAuditLog.target_id == driver.id,
                AdminAuditLog.action == "deactivate_user",
            )
        )
        assert current_driver is not None and current_driver.is_active
        assert current_ride is not None and current_ride.status == RideStatus.ACCEPTED
        assert audit is None
