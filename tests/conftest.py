import os
from pathlib import Path

import pytest

os.environ.setdefault("ENABLE_RT_POLLERS", "false")
os.environ.setdefault("ENABLE_STATIC_INGEST", "false")
os.environ.setdefault("ADMIN_TOKEN", "test-token")

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def bogota():
    from app.cities import load_city_file
    return load_city_file(ROOT / "cities" / "bogota.yaml")


@pytest.fixture
def fixtures() -> Path:
    return Path(__file__).parent / "fixtures"
