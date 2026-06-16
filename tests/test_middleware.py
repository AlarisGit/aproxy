"""Tests for middleware-level protections."""

import json
import time

import pytest

import proxy


class TestBodySizeLimit:
    def test_rejects_oversized_post_body(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr("proxy.MAX_BODY_SIZE", 100)
        response = client.post(
            "/v1/messages",
            headers=auth_headers,
            json={"model": "test", "messages": [{"role": "user", "content": "x" * 200}]},
        )
        assert response.status_code == 413
        assert response.json()["error"]["type"] == "request_too_large"

    @pytest.mark.asyncio
    async def test_allows_small_post_body(self, async_client, auth_headers, monkeypatch):
        monkeypatch.setattr("proxy.MAX_BODY_SIZE", 10 * 1024 * 1024)
        response = await async_client.post(
            "/v1/messages",
            headers=auth_headers,
            json={"model": "test", "max_tokens": 1024, "messages": [{"role": "user", "content": "hi"}]},
        )
        # Without mocked upstream it will return an upstream error, but it passed the size gate.
        assert response.status_code not in (401, 413)


class TestKeyReload:
    def test_key_store_reloads_when_mtime_changes(self, test_token, test_user, tmp_path, monkeypatch):
        keys_file = tmp_path / "keys.json"
        keys_file.write_text(json.dumps({test_token: test_user}))

        monkeypatch.setattr("proxy._keys_file", str(keys_file))
        monkeypatch.setattr("proxy.KEY_RELOAD_INTERVAL", 0.0)

        store = proxy._KeyStore()
        monkeypatch.setattr("proxy._key_store", store)
        assert store.api_keys == {test_token: test_user}

        # Change the file on disk
        keys_file.write_text(json.dumps({"sk-new": "other"}))
        time.sleep(0.01)
        store._last_check = 0.0
        store.reload_if_changed()

        assert store.api_keys == {"sk-new": "other"}
        assert proxy.validate_key("sk-new") == "other"
        with pytest.raises(Exception):
            proxy.validate_key(test_token)

    def test_key_store_keeps_cached_keys_on_load_failure(self, test_token, test_user, tmp_path, monkeypatch):
        keys_file = tmp_path / "keys.json"
        keys_file.write_text(json.dumps({test_token: test_user}))

        monkeypatch.setattr("proxy._keys_file", str(keys_file))
        monkeypatch.setattr("proxy.KEY_RELOAD_INTERVAL", 0.0)

        store = proxy._KeyStore()
        monkeypatch.setattr("proxy._key_store", store)
        assert store.api_keys == {test_token: test_user}

        # Corrupt the file
        keys_file.write_text("not json")
        time.sleep(0.01)
        store._last_check = 0.0
        store.reload_if_changed()

        # Old keys should still be valid despite the corruption
        assert store.api_keys == {test_token: test_user}
        assert proxy.validate_key(test_token) == test_user
