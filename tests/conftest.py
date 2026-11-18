"""Test helpers: import the pure-python modules without Home Assistant.

`custom_components/miele_mobile_control/__init__.py` imports homeassistant,
so tests stub the package in sys.modules (skipping its __init__) and import
the pure submodules `dop2` and `washer` through it.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_PKG_NAME = "custom_components.miele_mobile_control"
_PKG_DIR = REPO_ROOT / "custom_components" / "miele_mobile_control"
if _PKG_NAME not in sys.modules:
    _pkg = types.ModuleType(_PKG_NAME)
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules[_PKG_NAME] = _pkg

FIXTURES = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "wsi863.json").read_text()
)


@pytest.fixture(scope="session")
def fixtures() -> dict[str, bytes]:
    """All captured leaves, name -> raw bytes."""
    return {name: bytes.fromhex(hex_str) for name, hex_str in FIXTURES.items()}
