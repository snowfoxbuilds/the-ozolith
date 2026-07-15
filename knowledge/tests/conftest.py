from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_knowledge() -> Path:
    return FIXTURES / "sample_knowledge"
