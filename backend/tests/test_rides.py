from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.models import (
    AdminAuditLog,
    DriverAvailability,
    DriverProfile,
    Ride,
    RideAssignmentAttempt,
    RideStatus,
    RideStatusHistory,
    User,
)
from app.ride_service import calculate_fare_paise, offer_seconds_remaining
from app.ride_websockets import WebSocketTicketStore
from app.security import hash_password


def create_account(client: TestClient, role: str, email: str | None = None) -> tuple[dict[str, str], str]:
    account = {
        "email": email or f"{role}-{uuid4()}@example.com",
        "full_name": f"Test {role.title()}",
        "password": "test-password-123",
        "role": role,
    }
    assert client.post("/api/v1/auth/register", json=account).status_code == 201
    login = client.post(
        "/api/v1/auth/login",
        json={"email": account["email"], "password": account["password"]},
    )
    assert login.status_code == 200
    return login.json()["user"], login.json()["access_token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_fare_rounding_uses_half_up() -> None:
    assert calculate_fare_paise(Decimal("1"), base_paise=5_000, per_km_paise=500) == 5_001
    assert calculate_fare_paise(Decimal("1"), base_paise=5_000, per_km_paise=499) == 5_000


def make_assigned_ride(
    client: TestClient,
    rider_id: str,
    driver_id: str,
    status: RideStatus = RideStatus.ACCEPTED,
) -> Ride:
    now = datetime.now(UTC)
    ride = Ride(
        rider_id=UUID(rider_id),
        driver_id=UUID(driver_id),
        pickup_location="POINT(-73.99 40.73)",
        dropoff_location="POINT(-73.98 40.74)",
        status=status,
        currency="INR",
        distance_meters=1_500,
        fare_base_paise=5_000,
        fare_per_km_paise=1_500,
        fare_paise=7_250,
        request_deadline=now + timedelta(minutes=5),
        created_at=now,
        updated_at=now,
        started_at=now if status == RideStatus.IN_PROGRESS else None,
    )
    with client.app.state.session_factory() as db:
        profile = db.get(DriverProfile, UUID(driver_id))
        assert profile is not None
        profile.is_online = True
        profile.availability = (
            DriverAvailability.ON_TRIP if status == RideStatus.IN_PROGRESS else DriverAvailability.RESERVED
        )
        db.add(ride)
        db.flush()
        db.add(
            RideStatusHistory(
                ride_id=ride.id,
                previous_status=None,
                new_status=status,
                actor_id=UUID(rider_id),
                created_at=now,
            )
        )
        db.commit()
        db.refresh(ride)
        return ride


def test_fare_estimate_and_ride_request_persist_inputs(client: TestClient) -> None:
    _, token = create_account(client, "rider")
    headers = bearer(token)
    payload = {
        "pickup": {"latitude": 0, "longitude": 0},
        "dropoff": {"latitude": 0, "longitude": 0.01},
    }

    estimate = client.post("/api/v1/rides/estimate", json=payload, headers=headers)
    assert estimate.status_code == 200
    quote = estimate.json()
    assert 1_100 <= quote["distance_meters"] <= 1_120
    assert quote["fare_paise"] == 6_668
    assert quote["currency"] == "INR"
    assert quote["fare_base_paise"] == 5_000
    assert quote["fare_per_km_paise"] == 1_500

    created = client.post("/api/v1/rides", json=payload, headers=headers)
    assert created.status_code == 201
    ride = created.json()
    assert ride["status"] == RideStatus.NO_DRIVER_FOUND
    assert ride["fare_paise"] == quote["fare_paise"]
    assert ride["pickup"] == payload["pickup"]
    assert ride["dropoff"] == payload["dropoff"]

    with client.app.state.session_factory() as db:
        stored = db.get(Ride, UUID(ride["id"]))
        assert stored is not None
        assert stored.fare_base_paise == 5_000
        assert stored.fare_per_km_paise == 1_500
        history = db.scalars(
            select(RideStatusHistory)
            .where(RideStatusHistory.ride_id == stored.id)
            .order_by(RideStatusHistory.created_at, RideStatusHistory.id)
        ).all()
        assert [item.new_status for item in history] == [
            RideStatus.REQUESTED,
            RideStatus.NO_DRIVER_FOUND,
        ]

    assert (
        client.post(
            "/api/v1/rides",
            json={
                "pickup": {"latitude": 1, "longitude": 1},
                "dropoff": {"latitude": 1, "longitude": 1},
            },
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/rides/{ride['id']}/cancel",
            json={"reason": "too late"},
            headers=headers,
        ).status_code
        == 409
    )
    assert (
        client.get(
            f"/api/v1/rides/{ride['id']}",
            headers=bearer(create_account(client, "rider")[1]),
        ).status_code
        == 404
    )


def test_ride_and_driver_endpoints_enforce_roles(client: TestClient) -> None:
    _, rider_token = create_account(client, "rider")
    _, driver_token = create_account(client, "driver")
    pickup_dropoff = {
        "pickup": {"latitude": 0, "longitude": 0},
        "dropoff": {"latitude": 0, "longitude": 0.01},
    }

    assert client.post("/api/v1/rides", json=pickup_dropoff, headers=bearer(driver_token)).status_code == 403
    assert (
        client.post("/api/v1/rides/estimate", json=pickup_dropoff, headers=bearer(driver_token)).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/drivers/me/location",
            json={"latitude": 0, "longitude": 0},
            headers=bearer(rider_token),
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/drivers/me/availability",
            json={"online": True},
            headers=bearer(rider_token),
        ).status_code
        == 403
    )


def test_ride_websocket_checks_ownership_and_recovers_current_state(client: TestClient) -> None:
    _, rider_token = create_account(client, "rider")
    _, other_rider_token = create_account(client, "rider")
    assert client.post("/api/v1/ws/tickets").status_code == 401
    ride = client.post(
        "/api/v1/rides",
        json={
            "pickup": {"latitude": 0, "longitude": 0},
            "dropoff": {"latitude": 0, "longitude": 0.01},
        },
        headers=bearer(rider_token),
    ).json()
    url = f"/api/v1/ws/rides/{ride['id']}"
    with client.websocket_connect(f"{url}?access_token={rider_token}") as websocket:
        with pytest.raises(WebSocketDisconnect) as disconnected:
            websocket.receive_json()
        assert disconnected.value.code == 4401

    ticket_response = client.post("/api/v1/ws/tickets", headers=bearer(rider_token))
    assert ticket_response.status_code == 200
    assert ticket_response.json()["expires_in"] == 30
    ticket = ticket_response.json()["ticket"]

    with client.websocket_connect(f"{url}?ticket={ticket}") as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "ride_state"
        assert snapshot["ride"]["id"] == ride["id"]
        assert snapshot["ride"]["status"] == RideStatus.NO_DRIVER_FOUND

    with client.websocket_connect(f"{url}?ticket={ticket}") as websocket:
        with pytest.raises(WebSocketDisconnect) as disconnected:
            websocket.receive_json()
        assert disconnected.value.code == 4401

    other_ticket = client.post(
        "/api/v1/ws/tickets",
        headers=bearer(other_rider_token),
    ).json()["ticket"]
    with client.websocket_connect(f"{url}?ticket={other_ticket}") as websocket:
        with pytest.raises(WebSocketDisconnect) as disconnected:
            websocket.receive_json()
        assert disconnected.value.code == 4403


def test_ride_websocket_pushes_transition_without_reconnecting(client: TestClient) -> None:
    rider, rider_token = create_account(client, "rider")
    driver, driver_token = create_account(client, "driver")
    ride = make_assigned_ride(client, rider["id"], driver["id"])
    ticket = client.post("/api/v1/ws/tickets", headers=bearer(rider_token)).json()["ticket"]

    with client.websocket_connect(f"/api/v1/ws/rides/{ride.id}?ticket={ticket}") as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["ride"]["status"] == RideStatus.ACCEPTED
        transition = client.post(
            f"/api/v1/rides/{ride.id}/arriving",
            json={"reason": None},
            headers=bearer(driver_token),
        )
        assert transition.status_code == 200
        event = websocket.receive_json()
        assert event["type"] == "ride_state"
        assert event["ride"]["id"] == str(ride.id)
        assert event["ride"]["status"] == RideStatus.DRIVER_ARRIVING
        cancellation = client.post(
            f"/api/v1/rides/{ride.id}/cancel",
            json={"reason": "Regression test cancellation"},
            headers=bearer(rider_token),
        )
        assert cancellation.status_code == 200
        cancelled_event = websocket.receive_json()
        assert cancelled_event["type"] == "ride_state"
        assert cancelled_event["ride"]["status"] == RideStatus.CANCELLED_BY_RIDER


def test_assigned_ride_response_includes_driver_name_and_vehicle(client: TestClient) -> None:
    rider, rider_token = create_account(client, "rider")
    driver, _ = create_account(client, "driver")
    ride = make_assigned_ride(client, rider["id"], driver["id"])
    with client.app.state.session_factory() as db:
        profile = db.get(DriverProfile, UUID(driver["id"]))
        assert profile is not None
        profile.vehicle_make = "Toyota"
        profile.vehicle_model = "Etios"
        profile.license_plate = "NOT-EXPOSED"
        db.commit()

    response = client.get(f"/api/v1/rides/{ride.id}", headers=bearer(rider_token))
    assert response.status_code == 200
    assert response.json()["driver_name"] == "Test Driver"
    assert response.json()["driver_vehicle"] == "Toyota Etios"
    assert "license_plate" not in response.json()


def test_offer_countdown_uses_server_expiry() -> None:
    now = datetime.now(UTC)
    offer = RideAssignmentAttempt(
        ride_id=uuid4(),
        driver_id=uuid4(),
        attempt_number=1,
        status="OFFERED",
        offered_at=now,
        expires_at=now + timedelta(seconds=20),
    )
    assert offer_seconds_remaining(offer, now) == 20
    assert offer_seconds_remaining(offer, now + timedelta(seconds=2)) == 18


def test_websocket_ticket_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    store = WebSocketTicketStore()
    now = 1_000.0
    monkeypatch.setattr("app.ride_websockets.time.monotonic", lambda: now)
    ticket = store.issue(uuid4())
    monkeypatch.setattr("app.ride_websockets.time.monotonic", lambda: now + 31)
    assert store.consume(ticket) is None


def test_driver_state_transitions_and_atomic_history(client: TestClient) -> None:
    rider, _ = create_account(client, "rider")
    driver, driver_token = create_account(client, "driver")
    ride = make_assigned_ride(client, rider["id"], driver["id"])
    headers = bearer(driver_token)

    assert client.post(f"/api/v1/rides/{ride.id}/start", json={}, headers=headers).status_code == 409
    with client.app.state.session_factory() as db:
        unchanged = db.get(Ride, ride.id)
        history = db.scalars(select(RideStatusHistory).where(RideStatusHistory.ride_id == ride.id)).all()
        assert unchanged is not None
        assert unchanged.status == RideStatus.ACCEPTED
        assert [entry.new_status for entry in history] == [RideStatus.ACCEPTED]

    for action in ("arriving", "arrived", "start", "complete"):
        response = client.post(
            f"/api/v1/rides/{ride.id}/{action}",
            json={"reason": None},
            headers=headers,
        )
        assert response.status_code == 200

    assert response.json()["status"] == RideStatus.COMPLETED
    assert client.post(f"/api/v1/rides/{ride.id}/complete", json={}, headers=headers).status_code == 409
    with client.app.state.session_factory() as db:
        profile = db.get(DriverProfile, UUID(driver["id"]))
        assert profile is not None
        assert profile.availability == DriverAvailability.AVAILABLE
        history = db.scalars(
            select(RideStatusHistory)
            .where(RideStatusHistory.ride_id == ride.id)
            .order_by(RideStatusHistory.created_at, RideStatusHistory.id)
        ).all()
        assert [entry.new_status for entry in history] == [
            RideStatus.ACCEPTED,
            RideStatus.DRIVER_ARRIVING,
            RideStatus.DRIVER_ARRIVED,
            RideStatus.IN_PROGRESS,
            RideStatus.COMPLETED,
        ]


def test_rider_cancellation_releases_pretrip_driver(client: TestClient) -> None:
    rider, rider_token = create_account(client, "rider")
    driver, _ = create_account(client, "driver")
    ride = make_assigned_ride(client, rider["id"], driver["id"])
    response = client.post(
        f"/api/v1/rides/{ride.id}/cancel",
        json={"reason": "Changed plans"},
        headers=bearer(rider_token),
    )
    assert response.status_code == 200
    assert response.json()["status"] == RideStatus.CANCELLED_BY_RIDER
    with client.app.state.session_factory() as db:
        profile = db.get(DriverProfile, UUID(driver["id"]))
        assert profile is not None
        assert profile.availability == DriverAvailability.AVAILABLE
        history = db.scalar(
            select(RideStatusHistory)
            .where(RideStatusHistory.ride_id == ride.id)
            .order_by(RideStatusHistory.created_at.desc())
        )
        assert history is not None
        assert history.new_status == RideStatus.CANCELLED_BY_RIDER


def test_admin_in_progress_cancellation_requires_driver_release_confirmation(
    client: TestClient,
) -> None:
    rider, _ = create_account(client, "rider")
    driver, _ = create_account(client, "driver")
    ride = make_assigned_ride(client, rider["id"], driver["id"], RideStatus.IN_PROGRESS)
    with client.app.state.session_factory() as db:
        admin = User(
            email=f"admin-{uuid4()}@example.com",
            full_name="Test Admin",
            password_hash=hash_password("test-password-123"),
            role="admin",
        )
        db.add(admin)
        db.commit()
        admin_id = admin.id
    admin_login = client.post(
        "/api/v1/auth/login",
        json={"email": admin.email, "password": "test-password-123"},
    )
    assert admin_login.status_code == 200
    headers = bearer(admin_login.json()["access_token"])

    url = f"/api/v1/rides/{ride.id}/admin-cancel"
    payload = {"reason": "Safety incident", "confirm_driver_release": False}
    assert client.post(url, json=payload, headers=headers).status_code == 409
    with client.app.state.session_factory() as db:
        current = db.get(Ride, ride.id)
        assert current is not None
        assert current.status == RideStatus.IN_PROGRESS

    payload["confirm_driver_release"] = True
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == RideStatus.CANCELLED_BY_ADMIN
    with client.app.state.session_factory() as db:
        profile = db.get(DriverProfile, UUID(driver["id"]))
        assert profile is not None
        assert profile.availability == DriverAvailability.AVAILABLE
        audit = db.scalar(select(AdminAuditLog).where(AdminAuditLog.admin_id == admin_id))
        assert audit is not None
        assert audit.action == "cancel_ride"
