from fastapi.testclient import TestClient


def register(client: TestClient, payload: dict[str, str]) -> dict:
    response = client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201
    return response.json()


def login(client: TestClient, email: str, password: str):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def test_registration_login_and_current_user(client: TestClient, rider_payload: dict[str, str]) -> None:
    user = register(client, rider_payload)
    assert user["role"] == "rider"

    response = login(client, rider_payload["email"], rider_payload["password"])
    assert response.status_code == 200
    assert response.json()["expires_in"] == 900
    assert "cab_refresh" in response.cookies

    current = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {response.json()['access_token']}"},
    )
    assert current.status_code == 200
    assert current.json()["id"] == user["id"]


def test_registration_rejects_admin_role(client: TestClient, rider_payload: dict[str, str]) -> None:
    rider_payload["role"] = "admin"
    assert client.post("/api/v1/auth/register", json=rider_payload).status_code == 422


def test_duplicate_email_is_rejected(client: TestClient, rider_payload: dict[str, str]) -> None:
    register(client, rider_payload)
    assert client.post("/api/v1/auth/register", json=rider_payload).status_code == 409


def test_invalid_password_does_not_authenticate(client: TestClient, rider_payload: dict[str, str]) -> None:
    register(client, rider_payload)
    assert login(client, rider_payload["email"], "wrong-password").status_code == 401


def test_refresh_rotates_cookie_and_logout_revokes_session(
    client: TestClient, rider_payload: dict[str, str]
) -> None:
    register(client, rider_payload)
    first_login = login(client, rider_payload["email"], rider_payload["password"])
    first_refresh = first_login.cookies["cab_refresh"]

    refresh_response = client.post("/api/v1/auth/refresh")
    assert refresh_response.status_code == 200
    rotated_refresh = refresh_response.cookies["cab_refresh"]
    assert rotated_refresh != first_refresh

    logout_response = client.post("/api/v1/auth/logout")
    assert logout_response.status_code == 204
    assert client.post("/api/v1/auth/refresh").status_code == 401
    assert (
        client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {first_login.json()['access_token']}"},
        ).status_code
        == 200
    )


def test_health_reports_database_connection(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
