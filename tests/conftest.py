import pytest

from src.spark import local_session


@pytest.fixture(scope="session")
def spark():
    """One session for the whole run - Spark startup costs ~8s and would otherwise
    dominate a TDD loop."""
    s = local_session(app="silver-tests", cores="2")
    yield s
    s.stop()
