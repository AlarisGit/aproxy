"""Shared pytest fixtures for aproxy tests."""

import json
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

import proxy


@pytest.fixture
def test_user():
    return "tester"


@pytest.fixture
def test_token():
    return "sk-test-token-for-pytest-only"


@pytest.fixture
def authenticated_key_store(test_token, test_user, tmp_path):
    """Provide a key store preloaded with a test token, without touching keys.json."""
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps({"_salt": "abc", "users": {}}))
    store = proxy._KeyStore(str(keys_file))
    store.api_keys = {test_token: test_user}
    store.hashed_users = {}
    store.salt = None
    return store


@pytest.fixture
def client(monkeypatch, authenticated_key_store):
    """Return a synchronous TestClient with a patched key store."""
    monkeypatch.setattr(proxy, "_key_store", authenticated_key_store)
    with TestClient(proxy.app) as c:
        yield c


@pytest_asyncio.fixture
async def async_client(monkeypatch, authenticated_key_store):
    """Return an asynchronous ASGI client with a patched key store.

    The TestClient handles lifespan automatically, but httpx.AsyncClient does not,
    so we explicitly enter the app's lifespan context to create the shared httpx client.
    """
    monkeypatch.setattr(proxy, "_key_store", authenticated_key_store)
    state = {}
    lifespan = proxy.app.router.lifespan_context
    assert lifespan is not None, "app must define a lifespan context"

    @asynccontextmanager
    async def _lifespan():
        async with lifespan(proxy.app) as s:
            state.update(s or {})
            yield state

    async with _lifespan():
        async with AsyncClient(
            transport=ASGITransport(app=proxy.app), base_url="http://testserver"
        ) as c:
            yield c


@pytest.fixture
def auth_headers(test_token):
    return {"Authorization": f"Bearer {test_token}"}
