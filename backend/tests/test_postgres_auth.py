from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuthSession
from app.security import hash_refresh_token


def test_auth_sessions_work_and_revoke_on_postgres(postgres_client: TestClient) -> None:
    client = postgres_client
    assert client.app.state.engine.dialect.name == "postgresql"

    email = f"postgres-{uuid4()}@example.com"
    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "full_name": "Postgres Test Rider",
            "password": "postgres-integration-123",
            "role": "rider",
        },
    )
    assert registration.status_code == 201

    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "postgres-integration-123"},
    )
    assert login.status_code == 200
    assert "cab_refresh" in login.cookies

    old_refresh_token = login.cookies["cab_refresh"]
    refresh = client.post("/api/v1/auth/refresh")
    assert refresh.status_code == 200
    current_refresh_token = refresh.cookies["cab_refresh"]
    assert current_refresh_token != old_refresh_token

    access_token = refresh.json()["access_token"]
    logout = client.post("/api/v1/auth/logout")
    assert logout.status_code == 204
    assert client.post("/api/v1/auth/refresh").status_code == 401
    assert (
        client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        ).status_code
        == 200
    )

    token_hash = hash_refresh_token(current_refresh_token)
    with client.app.state.session_factory() as db:
        session = db.scalar(select(AuthSession).where(AuthSession.refresh_token_hash == token_hash))
        assert session is not None
        assert session.revoked_at is not None
