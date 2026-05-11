"""ApiClient helper tests (issue 21 — survey_cost_preview parity + caching)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agents.api_client import ApiClient
from world.action_log import ActionLog
from world.api import create_app
from world.sim import World
from world.subsurface import (
    SEISMIC_MAX_SIZE,
    SEISMIC_MIN_SIZE,
    survey_cost,
)


def _api(tmp_path: Path) -> ApiClient:
    log = ActionLog(root=tmp_path / "runs")
    app = create_app(world=World(), action_log=log)
    return ApiClient(transport=TestClient(app))


@pytest.mark.parametrize("size", list(range(SEISMIC_MIN_SIZE, SEISMIC_MAX_SIZE + 1)))
def test_survey_cost_preview_matches_world_helper_for_every_legal_size(
    tmp_path: Path, size: int
) -> None:
    api = _api(tmp_path)
    assert api.survey_cost_preview(size) == survey_cost(size)


def test_survey_cost_preview_issues_exactly_one_catalog_fetch(tmp_path: Path) -> None:
    """Repeated previews must read /catalog from cache, not re-hit the
    server. The participant's agent may call this on every hover."""

    class CountingTransport:
        def __init__(self, inner: TestClient) -> None:
            self._inner = inner
            self.get_calls: list[str] = []

        def get(self, url, *, params=None):
            self.get_calls.append(url)
            return self._inner.get(url, params=params or {})

        def post(self, url, *, json=None):
            return self._inner.post(url, json=json)

    log = ActionLog(root=tmp_path / "runs")
    inner = TestClient(create_app(world=World(), action_log=log))
    transport = CountingTransport(inner)
    api = ApiClient(transport=transport)

    for size in (4, 8, 12, 16, 8, 4):
        api.survey_cost_preview(size)

    catalog_hits = [u for u in transport.get_calls if u == "/catalog"]
    assert len(catalog_hits) == 1, transport.get_calls


def test_catalog_method_caches_response(tmp_path: Path) -> None:
    """Direct callers of api.catalog() also benefit from the cache."""

    class CountingTransport:
        def __init__(self, inner: TestClient) -> None:
            self._inner = inner
            self.get_calls: list[str] = []

        def get(self, url, *, params=None):
            self.get_calls.append(url)
            return self._inner.get(url, params=params or {})

        def post(self, url, *, json=None):
            return self._inner.post(url, json=json)

    log = ActionLog(root=tmp_path / "runs")
    inner = TestClient(create_app(world=World(), action_log=log))
    transport = CountingTransport(inner)
    api = ApiClient(transport=transport)

    first = api.catalog()
    second = api.catalog()
    assert first is second  # same cached object
    assert transport.get_calls.count("/catalog") == 1
