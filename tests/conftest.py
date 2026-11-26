"""Shared test setup."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_app_data(monkeypatch, tmp_path_factory) -> None:
    """Never let a test write logs, profiles or settings into the real app folder.

    Tests that need a specific folder still set APPDATA themselves; this is
    only the default.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path_factory.mktemp("appdata")))
