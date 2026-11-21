"""Real Home Assistant tests (pytest-homeassistant-custom-component).

Run with a venv that has Home Assistant and phacc installed:

    python -m pytest -c tests_ha/pytest.ini tests_ha

The Miele cloud is replaced by FakeCloud: no network is touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest_plugins = ["pytest_homeassistant_custom_component"]

FAB = "000000000001"
LEAVES = {
    name: bytes.fromhex(value)
    for name, value in json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "wsi863.json").read_text()
    ).items()
}

# Status 3, Mobile Start armed (RemoteEnable third value bit 0).
STATE_ARMED_COTTONS = {"Status": 3, "RemoteEnable": [15, 0, 1], "ProgramID": 1}


class FakeCloud:
    """Stands in for api.MieleCloud; tests change FakeCloud.state."""

    state: dict[str, Any] = dict(STATE_ARMED_COTTONS)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def async_login(self) -> None:
        return None

    async def async_get_devices(self) -> list[dict[str, Any]]:
        return [{"fab": FAB, "tech_type": "WSI863", "state": FakeCloud.state}]

    async def async_get_state(self, fab: str) -> dict[str, Any]:
        return dict(FakeCloud.state)

    async def async_read_leaf(
        self, fab: str, unit: int, attr: int, idx1: int = 0, idx2: int = 0
    ) -> bytes | None:
        if (unit, attr) == (2, 1574):
            return LEAVES["cottons_selected_2_1574_0_1"]
        if (unit, attr) == (2, 1584):
            return LEAVES["on_no_program_2_1584_1_1"]
        return None

    async def async_write_leaf(self, *args: Any) -> None:
        raise AssertionError("no writes in these tests")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def fake_cloud():
    FakeCloud.state = dict(STATE_ARMED_COTTONS)
    with patch(
        "custom_components.miele_mobile_control.MieleCloud", FakeCloud
    ), patch(
        "custom_components.miele_mobile_control.config_flow.MieleCloud", FakeCloud
    ):
        yield FakeCloud
