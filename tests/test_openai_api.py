"""Tests for OpenAI-compatible POST /v1/chat/completions proxying."""

import json

import httpx
import pytest
import pytest_asyncio

import proxy


@pytest_asyncio.fixture
async def openai_client(monkeypatch, authenticated_key_store):
    """ASGI client without app lifespan, so tests do not require a real Ollama."""
    monkeypatch.setattr(proxy, "_key_store", authenticated_key_store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app),
        base_url="http://testserver",
    ) as client:
        yield client


class _FakePostClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, target, content=None, headers=None):
        self.calls.append(
            {
                "target": target,
                "content": content or b"",
                "headers": headers or {},
            }
        )
        if callable(self.response):
            return self.response(self.calls[-1])
        return self.response


class TestOpenAIChatCompletions:
    @pytest.mark.asyncio
    async def test_chat_completions_requires_auth(self, openai_client):
        response = await openai_client.post(
            "/v1/chat/completions",
            json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 401
        body = response.json()
        assert body["error"]["type"] == "authentication_error"
        assert "Authentication required" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_chat_completions_oversized_body_uses_openai_error_shape(
        self, openai_client, auth_headers, monkeypatch
    ):
        monkeypatch.setattr(proxy, "MAX_BODY_SIZE", 100)

        response = await openai_client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "test",
                "messages": [{"role": "user", "content": "x" * 200}],
            },
        )

        assert response.status_code == 413
        assert response.json()["error"]["type"] == "request_too_large"

    @pytest.mark.asyncio
    async def test_chat_completions_forwards_request_and_audits_usage(
        self, openai_client, auth_headers, monkeypatch
    ):
        audit_records = []
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))

        def chat_response(call):
            assert call["target"] == "/v1/chat/completions"
            assert call["headers"]["Authorization"] == "Bearer ollama"
            assert json.loads(call["content"])["stream"] is False
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "total_tokens": 10,
                    },
                },
            )

        upstream = _FakePostClient(chat_response)
        monkeypatch.setattr(proxy, "client", upstream, raising=False)

        response = await openai_client.post(
            "/v1/chat/completions",
            headers={
                **auth_headers,
                "Content-Type": "text/plain",
                "Accept": "application/json",
            },
            json={
                "model": "test",
                "stream": False,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "Hello"
        assert upstream.calls[0]["headers"]["Content-Type"] == "application/json"
        assert upstream.calls[0]["headers"]["accept"] == "application/json"
        assert len(audit_records) == 1
        assert audit_records[0][0][:3] == ("tester", "POST", "/v1/chat/completions")
        assert audit_records[0][1]["model"] == "test"
        assert audit_records[0][1]["api_family"] == "openai"
        assert audit_records[0][1]["tokens"] == {
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
        }

    @pytest.mark.asyncio
    async def test_chat_completions_maps_anthropic_model_ids(
        self, openai_client, auth_headers, monkeypatch
    ):
        monkeypatch.setattr(proxy, "AVAILABLE_OLLAMA_MODELS", {"glm-5.2:cloud"})
        monkeypatch.setattr(proxy._model_mapper, "mapping", {"sonnet": "glm-5.2:cloud"})
        monkeypatch.setattr(proxy._model_mapper, "default", "")

        def chat_response(call):
            assert json.loads(call["content"])["model"] == "glm-5.2:cloud"
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "model": "glm-5.2:cloud",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        upstream = _FakePostClient(chat_response)
        monkeypatch.setattr(proxy, "client", upstream, raising=False)

        response = await openai_client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "claude-sonnet-4-5",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        assert response.json()["model"] == "glm-5.2:cloud"

    @pytest.mark.asyncio
    async def test_chat_completions_streaming_preserves_sse_and_audits_usage(
        self, openai_client, auth_headers, monkeypatch
    ):
        audit_records = []
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))

        class _LazyResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            async def aiter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"Hel"}}]}'
                yield ""
                yield 'data: {"choices":[{"delta":{"content":"lo"}}],"usage":{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10}}'
                yield ""
                yield "data: [DONE]"

        class _FakeStream:
            def __init__(self):
                self.entered = False
                self.exited = False
                self.response = _LazyResponse()

            async def __aenter__(self):
                self.entered = True
                return self.response

            async def __aexit__(self, *args):
                self.exited = True
                return False

        class _FakeStreamingClient:
            def __init__(self):
                self.stream_obj = _FakeStream()
                self.calls = []

            def stream(self, method, target, content=None, headers=None):
                self.calls.append(
                    {
                        "method": method,
                        "target": target,
                        "content": content or b"",
                        "headers": headers or {},
                    }
                )
                return self.stream_obj

        upstream = _FakeStreamingClient()
        monkeypatch.setattr(proxy, "client", upstream, raising=False)

        response = await openai_client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "test",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert 'data: {"choices":[{"delta":{"content":"Hel"}}]}' in response.text
        assert "data: [DONE]" in response.text
        assert upstream.calls[0]["method"] == "POST"
        assert upstream.calls[0]["target"] == "/v1/chat/completions"
        assert upstream.stream_obj.entered
        assert upstream.stream_obj.exited
        assert len(audit_records) == 1
        assert audit_records[0][0][:3] == ("tester", "POST", "/v1/chat/completions")
        assert audit_records[0][1]["api_family"] == "openai"
        assert audit_records[0][1]["tokens"] == {
            "input_tokens": 8,
            "output_tokens": 2,
            "total_tokens": 10,
        }

    @pytest.mark.asyncio
    async def test_chat_completions_streamclosed_after_partial_data_is_client_disconnect(
        self, openai_client, auth_headers, monkeypatch
    ):
        audit_records = []
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))

        class _PartiallyClosedResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            async def aiter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"partial"}}]}'
                raise httpx.StreamClosed()

        class _FakeStream:
            async def __aenter__(self):
                return _PartiallyClosedResponse()

            async def __aexit__(self, *args):
                return False

        class _FakeStreamingClient:
            def stream(self, method, target, content=None, headers=None):
                return _FakeStream()

        monkeypatch.setattr(proxy, "client", _FakeStreamingClient(), raising=False)

        response = await openai_client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "test",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        assert 'data: {"choices":[{"delta":{"content":"partial"}}]}' in response.text
        assert "Ollama closed the stream unexpectedly" not in response.text
        assert len(audit_records) == 1
        assert audit_records[0][1]["status"] == 200
        assert audit_records[0][1]["error"] == "client disconnected"
