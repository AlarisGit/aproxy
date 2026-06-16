"""Tests for POST /v1/messages proxying."""

import httpx
import json
import pytest
import respx

import proxy


class TestMessagesNonStreaming:
    def test_messages_requires_auth(self, client):
        response = client.post("/v1/messages", json={"model": "test"})
        assert response.status_code == 401
        assert response.json()["error"]["type"] == "authentication_error"

    @respx.mock
    def test_messages_forwards_request_to_ollama(self, client, auth_headers):
        respx.post("http://127.0.0.1:11434/v1/messages").respond(
            200,
            json={
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}],
                "model": "test",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )
        response = client.post(
            "/v1/messages",
            headers=auth_headers,
            json={
                "model": "test",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["content"][0]["text"] == "Hello"

    @respx.mock
    def test_messages_maps_anthropic_model_to_ollama(self, client, auth_headers, monkeypatch):
        """When Claude Code sends an Anthropic model ID, aproxy should translate
        it to a configured Ollama model before forwarding the request."""
        monkeypatch.setattr(
            proxy,
            "AVAILABLE_OLLAMA_MODELS",
            {"kimi-k2.7-code:cloud", "devstral-small-2:24b-cloud"},
        )
        proxy._model_mapper.mapping = {"opus": "kimi-k2.7-code:cloud"}
        proxy._model_mapper.default = ""

        captured_body = {}

        def capture_request(request):
            captured_body["model"] = json.loads(request.content).get("model")
            return httpx.Response(
                200,
                json={
                    "id": "msg_01",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello"}],
                    "model": "kimi-k2.7-code:cloud",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 5, "output_tokens": 1},
                },
            )

        respx.post("http://127.0.0.1:11434/v1/messages").mock(side_effect=capture_request)
        response = client.post(
            "/v1/messages",
            headers=auth_headers,
            json={
                "model": "claude-opus-4-7",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert captured_body["model"] == "kimi-k2.7-code:cloud"

    @respx.mock
    def test_messages_leaves_ollama_model_untouched(self, client, auth_headers, monkeypatch):
        """If the client already passes a native Ollama model, no mapping happens."""
        monkeypatch.setattr(
            proxy,
            "AVAILABLE_OLLAMA_MODELS",
            {"devstral-small-2:24b-cloud"},
        )
        proxy._model_mapper.mapping = {"opus": "kimi-k2.7-code:cloud"}
        proxy._model_mapper.default = ""

        respx.post("http://127.0.0.1:11434/v1/messages").respond(
            200,
            json={
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello"}],
                "model": "devstral-small-2:24b-cloud",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )
        response = client.post(
            "/v1/messages",
            headers=auth_headers,
            json={
                "model": "devstral-small-2:24b-cloud",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        # Response should come back with the same model the user requested
        assert response.json()["model"] == "devstral-small-2:24b-cloud"

    @respx.mock
    def test_messages_falls_back_to_default_when_tier_missing(self, client, auth_headers, monkeypatch):
        """If the tier mapping points to a missing Ollama model, default is used."""
        monkeypatch.setattr(
            proxy,
            "AVAILABLE_OLLAMA_MODELS",
            {"kimi-k2.5:cloud", "devstral-small-2:24b-cloud"},
        )
        proxy._model_mapper.mapping = {"opus": "non-existent-model"}
        proxy._model_mapper.default = "kimi-k2.5:cloud"

        captured_body = {}

        def capture_request(request):
            captured_body["model"] = json.loads(request.content).get("model")
            return httpx.Response(
                200,
                json={
                    "id": "msg_01",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello"}],
                    "model": "kimi-k2.5:cloud",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 5, "output_tokens": 1},
                },
            )

        respx.post("http://127.0.0.1:11434/v1/messages").mock(side_effect=capture_request)
        response = client.post(
            "/v1/messages",
            headers=auth_headers,
            json={
                "model": "claude-opus-4-7",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert captured_body["model"] == "kimi-k2.5:cloud"

    @respx.mock
    def test_messages_returns_upstream_status_on_failure(self, client, auth_headers):
        respx.post("http://127.0.0.1:11434/v1/messages").respond(
            502,
            json={"error": {"message": "ollama down"}},
        )
        response = client.post(
            "/v1/messages",
            headers=auth_headers,
            json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 502
        body = response.json()
        assert "ollama" in body.get("detail", body.get("error", "")).get("message", "").lower()


class TestMessagesStreaming:
    @pytest.mark.asyncio
    async def test_streaming_messages_forwards_sse_on_success(
        self, async_client, auth_headers, monkeypatch
    ):
        content = (
            b'data: {"type":"content_block_delta"}\n\n'
            b'data: {"type":"message_delta","usage":{"input_tokens":5,"output_tokens":1}}\n\n'
        )

        class _FakeStream:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, *args):
                return False

        response = httpx.Response(200, content=content)
        response.headers["content-type"] = "text/event-stream"

        monkeypatch.setattr(proxy.client, "stream", lambda *args, **kwargs: _FakeStream(response))

        response = await async_client.post(
            "/v1/messages",
            headers=auth_headers,
            json={"model": "test", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        text = response.text
        assert 'data: {"type":"content_block_delta"}' in text
        assert 'data: {"type":"message_delta"' in text
        assert response.headers["content-type"].startswith("text/event-stream")

    @pytest.mark.asyncio
    async def test_streaming_messages_returns_real_status_on_upstream_error(
        self, async_client, auth_headers, monkeypatch
    ):
        class _FakeStream:
            def __init__(self, response):
                self.response = response

            async def __aenter__(self):
                return self.response

            async def __aexit__(self, *args):
                return False

        error_response = httpx.Response(503, json={"error": "busy"})

        monkeypatch.setattr(
            proxy.client, "stream", lambda *args, **kwargs: _FakeStream(error_response)
        )

        response = await async_client.post(
            "/v1/messages",
            headers=auth_headers,
            json={"model": "test", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 503
        assert "busy" in response.text
        assert response.headers["content-type"] == "application/json"
