import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Lab  # noqa: E402


@pytest.fixture
def lab(tmp_path):
    lab = Lab(tmp_path)
    yield lab
    lab.close()


@pytest.fixture
def lab_noworker(tmp_path):
    lab = Lab(tmp_path, with_worker=False)
    yield lab
    lab.close()


@pytest.fixture
def key():
    return lambda: str(uuid.uuid4())
