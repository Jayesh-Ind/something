"""WebSocket live timeline streaming tests."""

import json

import pytest
from starlette.testclient import TestClient

from src.main import app
from src.timeline.router import ws_manager


def test_websocket_ping_pong():
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live-timeline") as websocket:
            websocket.send_text("ping")
            data = websocket.receive_text()
            assert json.loads(data) == {"type": "pong"}


@pytest.mark.asyncio
async def test_websocket_broadcast():
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live-timeline") as websocket:
            # Broadcast message through manager
            test_msg = {"type": "timeline_event", "data": {"test_id": "123"}}
            await ws_manager.broadcast(test_msg)

            data = websocket.receive_text()
            received = json.loads(data)
            assert received["type"] == "timeline_event"
            assert received["data"]["test_id"] == "123"
