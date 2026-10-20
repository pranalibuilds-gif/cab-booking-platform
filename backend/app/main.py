import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.admin import router as admin_router
from app.auth import router as auth_router
from app.config import Settings, get_settings
from app.models import Ride
from app.payment_provider import LocalMockPaymentProvider
from app.payments import router as payments_router
from app.ride_hub import ride_hub
from app.ride_service import (
    current_offer,
    expire_due_offers,
    offer_seconds_remaining,
    ride_response,
)
from app.ride_websockets import WebSocketTicketStore
from app.ride_websockets import router as ride_websocket_router
from app.rides import router as rides_router

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    configured = settings or get_settings()
    database_engine = engine or create_engine(configured.database_url, pool_pre_ping=True)
    session_factory = sessionmaker(bind=database_engine, expire_on_commit=False)

    async def offer_expiry_worker() -> None:
        while True:
            try:
                expired = await run_in_threadpool(expire_due_offers, session_factory, configured)
                for ride_id, previous_driver_id in expired:
                    with session_factory() as session:
                        ride = session.get(Ride, ride_id)
                        if ride is None:
                            continue
                        active_offer = current_offer(session, ride_id)
                        if previous_driver_id is not None and (
                            active_offer is None or active_offer.driver_id != previous_driver_id
                        ):
                            await ride_hub.publish_driver(
                                previous_driver_id,
                                {"type": "ride_offer_expired", "ride_id": str(ride_id)},
                            )
                        if active_offer is not None:
                            await ride_hub.publish_driver(
                                active_offer.driver_id,
                                {
                                    "type": "ride_offer",
                                    "ride_id": str(ride.id),
                                    "attempt_id": str(active_offer.id),
                                    "attempt_number": active_offer.attempt_number,
                                    "expires_at": active_offer.expires_at.isoformat(),
                                    "expires_in_seconds": offer_seconds_remaining(active_offer),
                                },
                            )
                        await ride_hub.publish_ride(
                            ride.id,
                            {
                                "type": "ride_state",
                                "ride": ride_response(session, ride).model_dump(mode="json"),
                            },
                        )
            except SQLAlchemyError:
                logger.exception("Ride offer expiry worker could not process a database batch")
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
        worker = None
        if database_engine.dialect.name == "postgresql":
            worker = asyncio.create_task(offer_expiry_worker())
        try:
            yield
        finally:
            if worker is not None:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
            if engine is None:
                database_engine.dispose()

    application = FastAPI(
        title="Cab Booking API",
        version="0.1.0",
        description="Local MVP API for the Cab Booking Platform.",
        lifespan=lifespan,
    )
    application.state.settings = configured
    application.state.engine = database_engine
    application.state.session_factory = session_factory
    application.state.ws_ticket_store = WebSocketTicketStore()
    application.state.payment_provider = LocalMockPaymentProvider()
    application.add_middleware(
        CORSMiddleware,
        allow_origins=configured.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    application.include_router(auth_router)
    application.include_router(rides_router)
    application.include_router(payments_router)
    application.include_router(admin_router)
    application.include_router(ride_websocket_router)

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        try:
            with session_factory() as session:
                session.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="Database is unavailable") from exc
        return {"status": "ok"}

    return application


app = create_app()
