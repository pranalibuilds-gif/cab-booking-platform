import os
from collections.abc import Iterator
from ipaddress import ip_address
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool


def pytest_configure() -> None:
    os.environ.setdefault("JWT_SECRET_KEY", "test-only-signing-secret-that-is-long-enough")


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.config import Settings
    from app.main import create_app
    from app.models import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url="sqlite://",
        jwt_secret_key="test-only-signing-secret-that-is-long-enough",
    )
    app = create_app(settings, engine)
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def rider_payload() -> dict[str, str]:
    return {
        "email": f"rider-{uuid4()}@example.com",
        "full_name": "Test Rider",
        "password": "test-password-123",
        "role": "rider",
    }


@pytest.fixture
def postgres_client() -> Iterator[TestClient]:
    if os.environ.get("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("Set RUN_POSTGRES_INTEGRATION=1 to run PostgreSQL integration tests.")

    from app.config import get_settings
    from app.main import create_app
    from app.models import Base

    settings = get_settings()
    url = make_url(settings.database_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("PostgreSQL integration tests require a PostgreSQL DATABASE_URL.")
    if settings.environment == "production":
        pytest.fail("PostgreSQL integration tests cannot run with production settings.")
    host = url.host or ""
    try:
        local_host = ip_address(host).is_loopback
    except ValueError:
        local_host = host.lower() == "localhost"
    if not local_host:
        pytest.fail("PostgreSQL integration tests may only run against a loopback database.")

    schema = f"cab_test_{uuid4().hex}"
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={schema},public"},
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

    try:
        Base.metadata.create_all(engine, checkfirst=False)
        application = create_app(settings, engine)
        with TestClient(application) as test_client:
            yield test_client
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()
