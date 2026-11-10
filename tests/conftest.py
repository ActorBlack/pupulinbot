"""Keep protocol tests independent of real time; pacing has dedicated integration tests."""

from unittest.mock import AsyncMock

import pytest

from pupulinbot.paipuya_gate import PaipuyaGate


@pytest.fixture(autouse=True)
def isolate_api_pacing(request, monkeypatch):
    if not request.node.get_closest_marker("paipuya_pacing"):
        monkeypatch.setattr(PaipuyaGate, "wait", AsyncMock())
