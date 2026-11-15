"""Shared test set-up: every test runs offline, with its own empty saved/ folder."""

import pytest

import server
from spherex_viewer import protect
from spherex_viewer import tracker as trackers


@pytest.fixture(autouse=True)
def isolated_site(tmp_path, monkeypatch):
    """A fresh saved-image store and no rate limit, so tests cannot affect each other."""
    monkeypatch.setattr(server, "STORE", trackers.Store(tmp_path / "saved"))
    monkeypatch.setattr(server, "JOB_LIMITER", protect.RateLimiter(10_000))
    yield


@pytest.fixture(scope="session")
def demo_tracker():
    """One simulated tracker shared by the tests that only read it."""
    return trackers.build_demo()
