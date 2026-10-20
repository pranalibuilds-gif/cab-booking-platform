import hashlib
import secrets
import threading
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel

from app.auth import get_me
from app.models import Ride, User
from app.ride_hub import ride_hub
from app.ride_service import current_offer, offer_seconds_remaining, ride_response

router = APIRouter(prefix="/api/v1/ws", tags=["ride updates"])
TICKET_TTL_SECONDS = 30
MAX_ACTIVE_TICKETS = 10_000


class WebSocketTicketResponse(BaseModel):
    ticket: str
    expires_in: int


CurrentUser = Annotated[User, Depends(get_me)]


class WebSocketTicketStore:
    def __init__(self) -> None:
        self._tickets: dict[str, tuple[UUID, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(ticket: str) -> str:
        return hashlib.sha256(ticket.encode("utf-8")).hexdigest()

    def issue(self, user_id: UUID) -> str:
        now = time.monotonic()
        with self._lock:
            self._tickets = {digest: entry for digest, entry in self._tickets.items() if entry[1] > now}
            if len(self._tickets) >= MAX_ACTIVE_TICKETS:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "WebSocket ticket capacity reached; retry shortly",
                )
            ticket = secrets.token_urlsafe(32)
            self._tickets[self._digest(ticket)] = (user_id, now + TICKET_TTL_SECONDS)
            return ticket

    def consume(self, ticket: str | None) -> UUID | None:
        if not ticket:
            return None
        with self._lock:
            entry = self._tickets.pop(self._digest(ticket), None)
        if entry is None or entry[1] <= time.monotonic():
            return None
        return entry[0]


@router.post("/tickets", response_model=WebSocketTicketResponse)
async def create_websocket_ticket(user: CurrentUser, request: Request) -> WebSocketTicketResponse:
    if not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    return WebSocketTicketResponse(
        ticket=request.app.state.ws_ticket_store.issue(user.id),
        expires_in=TICKET_TTL_SECONDS,
    )


def authenticated_user(websocket: WebSocket, user_id: UUID | None) -> User | None:
    if user_id is None:
        return None
    with websocket.app.state.session_factory() as db:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            return None
        db.expunge(user)
        return user


async def receive_heartbeat(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive_text()
        if message == "ping":
            await websocket.send_json({"type": "pong"})


@router.websocket("/rides/{ride_id}")
async def ride_updates(websocket: WebSocket, ride_id: UUID) -> None:
    await websocket.accept()
    user_id = websocket.app.state.ws_ticket_store.consume(websocket.query_params.get("ticket"))
    user = authenticated_user(websocket, user_id)
    if user is None:
        await websocket.close(code=4401, reason="Authentication required")
        return
    with websocket.app.state.session_factory() as db:
        ride = db.get(Ride, ride_id)
        if ride is None:
            await websocket.close(code=4404, reason="Ride not found")
            return
        if user.role != "admin" and user.id not in (ride.rider_id, ride.driver_id):
            await websocket.close(code=4403, reason="Not authorized for this ride")
            return
        snapshot = ride_response(db, ride).model_dump(mode="json")
    await ride_hub.connect_ride(ride_id, websocket)
    await websocket.send_json({"type": "ride_state", "ride": snapshot})
    try:
        await receive_heartbeat(websocket)
    except WebSocketDisconnect:
        pass
    finally:
        ride_hub.disconnect(websocket)


@router.websocket("/drivers/offers")
async def driver_offer_updates(websocket: WebSocket) -> None:
    await websocket.accept()
    user_id = websocket.app.state.ws_ticket_store.consume(websocket.query_params.get("ticket"))
    user = authenticated_user(websocket, user_id)
    if user is None:
        await websocket.close(code=4401, reason="Authentication required")
        return
    if user.role != "driver":
        await websocket.close(code=4403, reason="Driver role required")
        return
    await ride_hub.connect_driver(user.id, websocket)
    with websocket.app.state.session_factory() as db:
        offer = current_offer(db, user.id)
        if offer is not None:
            ride = db.get(Ride, offer.ride_id)
            if ride is not None:
                await websocket.send_json(
                    {
                        "type": "ride_offer",
                        "ride_id": str(ride.id),
                        "attempt_id": str(offer.id),
                        "attempt_number": offer.attempt_number,
                        "expires_at": offer.expires_at.isoformat(),
                        "expires_in_seconds": offer_seconds_remaining(offer),
                        "ride": ride_response(db, ride).model_dump(mode="json"),
                    }
                )
    try:
        await receive_heartbeat(websocket)
    except WebSocketDisconnect:
        pass
    finally:
        ride_hub.disconnect(websocket)
