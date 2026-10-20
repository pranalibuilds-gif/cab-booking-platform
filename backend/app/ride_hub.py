import logging
from collections import defaultdict
from uuid import UUID

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

logger = logging.getLogger(__name__)


class RideHub:
    def __init__(self) -> None:
        self._ride_connections: dict[UUID, set[WebSocket]] = defaultdict(set)
        self._driver_connections: dict[UUID, set[WebSocket]] = defaultdict(set)

    async def connect_ride(self, ride_id: UUID, websocket: WebSocket) -> None:
        self._ride_connections[ride_id].add(websocket)

    async def connect_driver(self, driver_id: UUID, websocket: WebSocket) -> None:
        self._driver_connections[driver_id].add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        for connections in (*self._ride_connections.values(), *self._driver_connections.values()):
            connections.discard(websocket)

    async def _send(self, connections: set[WebSocket], event: dict[str, object]) -> None:
        for websocket in tuple(connections):
            try:
                await websocket.send_json(event)
            except (WebSocketDisconnect, RuntimeError, OSError):
                self.disconnect(websocket)

    async def publish_ride(self, ride_id: UUID, event: dict[str, object]) -> None:
        await self._send(self._ride_connections.get(ride_id, set()), event)

    async def publish_driver(self, driver_id: UUID, event: dict[str, object]) -> None:
        await self._send(self._driver_connections.get(driver_id, set()), event)


ride_hub = RideHub()
