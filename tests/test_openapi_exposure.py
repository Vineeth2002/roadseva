"""
tests/test_openapi_exposure.py — /docs, /redoc, /openapi.json gating
========================================================================
Proves the FastAPI app object is constructed with docs disabled when
ENVIRONMENT="production" (or unset -- the existing repo-wide default,
matching security.py's own ENVIRONMENT check) and enabled otherwise,
while normal application routes are unaffected either way.

The app object is created once at import time from main.py's module-level
os.getenv() read, so each test that needs a specific environment
reimports main fresh with that environment variable set first.

Run with: pytest tests/test_openapi_exposure.py -v
"""

import importlib
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_app(environment_value):
    original = os.environ.get("ENVIRONMENT")
    if environment_value is None:
        os.environ.pop("ENVIRONMENT", None)
    else:
        os.environ["ENVIRONMENT"] = environment_value
    try:
        if "main" in sys.modules:
            importlib.reload(sys.modules["main"])
        else:
            import main  # noqa
        import main
        return main.app
    finally:
        if original is None:
            os.environ.pop("ENVIRONMENT", None)
        else:
            os.environ["ENVIRONMENT"] = original


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    import security
    security._limiter._data.clear()
    yield
    security._limiter._data.clear()


class TestProductionDisablesDocs:

    def test_docs_returns_404_in_production(self):
        app = get_app("production")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/docs")
        assert resp.status_code == 404

    def test_redoc_returns_404_in_production(self):
        app = get_app("production")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/redoc")
        assert resp.status_code == 404

    def test_openapi_json_returns_404_in_production(self):
        app = get_app("production")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/openapi.json")
        assert resp.status_code == 404

    def test_unset_environment_defaults_to_production_behavior(self):
        app = get_app(None)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/docs")
        assert resp.status_code == 404

    def test_normal_route_unaffected_in_production(self):
        app = get_app("production")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/privacy")
        assert resp.status_code == 200


class TestDevelopmentEnablesDocs:

    def test_docs_returns_200_in_development(self):
        app = get_app("development")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/docs")
        assert resp.status_code == 200

    def test_redoc_returns_200_in_development(self):
        app = get_app("development")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/redoc")
        assert resp.status_code == 200

    def test_openapi_json_returns_200_in_development(self):
        app = get_app("development")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        body = resp.json()
        assert "openapi" in body

    def test_normal_route_unaffected_in_development(self):
        app = get_app("development")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/privacy")
        assert resp.status_code == 200


def teardown_module(module):
    os.environ.pop("ENVIRONMENT", None)
    if "main" in sys.modules:
        importlib.reload(sys.modules["main"])