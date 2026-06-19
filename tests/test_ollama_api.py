"""Tests for allowlisted native Ollama API proxying."""

import base64
import json

import httpx
import pytest
import pytest_asyncio

import proxy


TEST_CLIENT_IP = "203.0.113.10"


@pytest_asyncio.fixture
async def native_client(monkeypatch, authenticated_key_store):
    """ASGI client without app lifespan, so tests do not require a real Ollama."""
    monkeypatch.setattr(proxy, "_key_store", authenticated_key_store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app, client=(TEST_CLIENT_IP, 12345)),
        base_url="http://testserver",
    ) as client:
        yield client


class _FakeRequestClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def request(self, method, target, content=None, headers=None):
        self.calls.append(
            {
                "method": method,
                "target": target,
                "content": content or b"",
                "headers": headers or {},
            }
        )
        if callable(self.response):
            return self.response(self.calls[-1])
        return self.response


class _FakeLabelledMetric:
    def __init__(self):
        self.calls = []

    def labels(self, **labels):
        metric = self

        class _BoundMetric:
            def inc(self, value=1):
                metric.calls.append(("inc", labels, value))

            def observe(self, value):
                metric.calls.append(("observe", labels, value))

        return _BoundMetric()


class _FakeGauge:
    def __init__(self):
        self.value = 0

    def inc(self):
        self.value += 1

    def dec(self):
        self.value -= 1


class TestOllamaNativeAuthAndPolicy:
    @pytest.mark.asyncio
    async def test_native_ollama_requires_auth(self, native_client):
        response = await native_client.post(
            "/api/chat",
            json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 401
        message = response.json()["error"]
        assert "native Ollama API" in message
        assert "ANTHROPIC_AUTH_TOKEN" not in message

    @pytest.mark.asyncio
    async def test_native_ollama_keeps_non_public_metadata_authenticated(self, native_client):
        response = await native_client.get("/api/version")

        assert response.status_code == 401
        message = response.json()["error"]
        assert "native Ollama API" in message
        assert "ANTHROPIC_AUTH_TOKEN" not in message

    @pytest.mark.asyncio
    async def test_native_ollama_reports_invalid_aproxy_token(self, native_client):
        response = await native_client.post(
            "/api/chat",
            headers={"Authorization": "Bearer wrong-token"},
            json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert response.status_code == 401
        message = response.json()["error"]
        assert "Invalid aproxy token" in message
        assert "Ollama" in message
        assert "ANTHROPIC_AUTH_TOKEN" not in message

    @pytest.mark.asyncio
    async def test_native_ollama_accepts_basic_auth_username_token(
        self, native_client, test_token, monkeypatch
    ):
        upstream = _FakeRequestClient(httpx.Response(200, json={"version": "test"}))
        monkeypatch.setattr(proxy, "client", upstream, raising=False)
        encoded = base64.b64encode(f"{test_token}:".encode()).decode()

        response = await native_client.get(
            "/api/version",
            headers={"Authorization": f"Basic {encoded}"},
        )

        assert response.status_code == 200
        assert response.json() == {"version": "test"}

    @pytest.mark.asyncio
    async def test_native_ollama_accepts_basic_auth_password_token(
        self, native_client, test_token, monkeypatch
    ):
        upstream = _FakeRequestClient(httpx.Response(200, json={"version": "test"}))
        monkeypatch.setattr(proxy, "client", upstream, raising=False)
        encoded = base64.b64encode(f"aproxy:{test_token}".encode()).decode()

        response = await native_client.get(
            "/api/version",
            headers={"Authorization": f"Basic {encoded}"},
        )

        assert response.status_code == 200
        assert response.json() == {"version": "test"}

    @pytest.mark.asyncio
    async def test_admin_routes_are_blocked_before_upstream(
        self, native_client, auth_headers, monkeypatch
    ):
        audit_records = []
        upstream = _FakeRequestClient(httpx.Response(200, json={"unexpected": True}))
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))
        monkeypatch.setattr(proxy, "client", upstream, raising=False)

        response = await native_client.request(
            "DELETE",
            "/api/delete",
            headers=auth_headers,
            json={"model": "test"},
        )

        assert response.status_code == 403
        assert "disabled" in response.json()["error"]
        assert upstream.calls == []
        assert len(audit_records) == 1
        assert audit_records[0][0][:3] == ("tester", "DELETE", "/api/delete")
        assert audit_records[0][1]["status"] == 403
        assert audit_records[0][1]["api_family"] == "ollama"
        assert audit_records[0][1]["route_class"] == "admin_blocked"

    @pytest.mark.asyncio
    async def test_unknown_native_routes_are_denied(self, native_client, auth_headers, monkeypatch):
        audit_records = []
        upstream = _FakeRequestClient(httpx.Response(200, json={"unexpected": True}))
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))
        monkeypatch.setattr(proxy, "client", upstream, raising=False)

        response = await native_client.post("/api/unknown", headers=auth_headers, json={"model": "test"})

        assert response.status_code == 404
        assert "Unsupported Ollama API route" in response.json()["error"]
        assert upstream.calls == []
        assert len(audit_records) == 1
        assert audit_records[0][0][:3] == ("tester", "POST", "/api/unknown")
        assert audit_records[0][1]["route_class"] == "unsupported"


class TestOllamaNativeProxy:
    @pytest.mark.asyncio
    async def test_tags_uses_client_ip_for_unauthenticated_model_listing(
        self, native_client, monkeypatch
    ):
        audit_records = []
        upstream = _FakeRequestClient(httpx.Response(200, json={"models": [{"name": "test"}]}))
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))
        monkeypatch.setattr(proxy, "client", upstream, raising=False)

        response = await native_client.get("/api/tags")

        assert response.status_code == 200
        assert response.json() == {"models": [{"name": "test"}]}
        assert upstream.calls[0]["method"] == "GET"
        assert upstream.calls[0]["target"] == "/api/tags"
        assert len(audit_records) == 1
        assert audit_records[0][0][:3] == (TEST_CLIENT_IP, "GET", "/api/tags")
        assert audit_records[0][1]["status"] == 200
        assert audit_records[0][1]["api_family"] == "ollama"
        assert audit_records[0][1]["route_class"] == "public_metadata"

    @pytest.mark.asyncio
    async def test_tags_ignores_invalid_auth_and_audits_client_ip(self, native_client, monkeypatch):
        audit_records = []
        upstream = _FakeRequestClient(httpx.Response(200, json={"models": [{"name": "test"}]}))
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))
        monkeypatch.setattr(proxy, "client", upstream, raising=False)

        response = await native_client.get(
            "/api/tags",
            headers={"Authorization": "Bearer wrong-token"},
        )

        assert response.status_code == 200
        assert response.json() == {"models": [{"name": "test"}]}
        assert len(audit_records) == 1
        assert audit_records[0][0][:3] == (TEST_CLIENT_IP, "GET", "/api/tags")
        assert audit_records[0][1]["route_class"] == "public_metadata"

    @pytest.mark.asyncio
    async def test_tags_uses_authenticated_user_when_valid_auth_is_present(
        self, native_client, auth_headers, monkeypatch
    ):
        audit_records = []
        upstream = _FakeRequestClient(httpx.Response(200, json={"models": [{"name": "test"}]}))
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))
        monkeypatch.setattr(proxy, "client", upstream, raising=False)

        response = await native_client.get("/api/tags", headers=auth_headers)

        assert response.status_code == 200
        assert len(audit_records) == 1
        assert audit_records[0][0][:3] == ("tester", "GET", "/api/tags")
        assert audit_records[0][1]["route_class"] == "public_metadata"

    def test_public_tags_audit_suppresses_repeated_ip_entries(self, tmp_path, monkeypatch):
        audit_log = tmp_path / "audit.jsonl"
        now = {"value": 1000.0}
        monkeypatch.setattr(proxy, "AUDIT_LOG", str(audit_log))
        monkeypatch.setattr(proxy, "AUDIT_ENABLED", True)
        monkeypatch.setattr(proxy, "PUBLIC_TAGS_LOG_SUPPRESS_SECONDS", 600.0)
        monkeypatch.setattr(proxy.time, "time", lambda: now["value"])

        proxy._PUBLIC_TAGS_LOG_LAST.clear()
        try:
            for _ in range(3):
                proxy.audit(
                    TEST_CLIENT_IP,
                    "GET",
                    "/api/tags",
                    status=200,
                    api_family="ollama",
                    route_class="public_metadata",
                )
            now["value"] += 601.0
            proxy.audit(
                TEST_CLIENT_IP,
                "GET",
                "/api/tags",
                status=200,
                api_family="ollama",
                route_class="public_metadata",
            )
        finally:
            proxy._PUBLIC_TAGS_LOG_LAST.clear()

        records = [json.loads(line) for line in audit_log.read_text().splitlines()]
        assert len(records) == 2
        assert [record["key"] for record in records] == [TEST_CLIENT_IP, TEST_CLIENT_IP]
        assert all(record["route_class"] == "public_metadata" for record in records)

    @pytest.mark.asyncio
    async def test_metadata_route_proxies_query_and_audits_status(
        self, native_client, auth_headers, monkeypatch
    ):
        audit_records = []
        upstream = _FakeRequestClient(httpx.Response(200, json={"models": [{"name": "test"}]}))
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))
        monkeypatch.setattr(proxy, "client", upstream, raising=False)

        response = await native_client.get("/api/ps?verbose=true", headers=auth_headers)

        assert response.status_code == 200
        assert response.json() == {"models": [{"name": "test"}]}
        assert upstream.calls[0]["method"] == "GET"
        assert upstream.calls[0]["target"] == "/api/ps?verbose=true"
        assert len(audit_records) == 1
        assert audit_records[0][0][:3] == ("tester", "GET", "/api/ps")
        assert audit_records[0][1]["status"] == 200
        assert audit_records[0][1]["api_family"] == "ollama"
        assert audit_records[0][1]["route_class"] == "metadata"

    @pytest.mark.asyncio
    async def test_metadata_get_does_not_forward_request_body(
        self, native_client, auth_headers, monkeypatch
    ):
        upstream = _FakeRequestClient(httpx.Response(200, json={"models": []}))
        monkeypatch.setattr(proxy, "client", upstream, raising=False)

        response = await native_client.request(
            "GET",
            "/api/tags",
            headers={**auth_headers, "Content-Type": "application/json"},
            content=b'{"unexpected":true}',
        )

        assert response.status_code == 200
        assert upstream.calls[0]["method"] == "GET"
        assert upstream.calls[0]["target"] == "/api/tags"
        assert upstream.calls[0]["content"] == b""

    @pytest.mark.asyncio
    async def test_chat_non_streaming_extracts_usage_and_forwards_internal_auth(
        self, native_client, auth_headers, monkeypatch
    ):
        audit_records = []
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))

        def chat_response(call):
            assert call["headers"]["Authorization"] == "Bearer ollama"
            assert json.loads(call["content"])["stream"] is False
            return httpx.Response(
                200,
                json={
                    "model": "test",
                    "message": {"role": "assistant", "content": "Hello"},
                    "done": True,
                    "prompt_eval_count": 7,
                    "eval_count": 3,
                },
            )

        upstream = _FakeRequestClient(chat_response)
        monkeypatch.setattr(proxy, "client", upstream, raising=False)

        response = await native_client.post(
            "/api/chat",
            headers=auth_headers,
            json={
                "model": "test",
                "stream": False,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert response.status_code == 200
        assert response.json()["message"]["content"] == "Hello"
        assert len(audit_records) == 1
        assert audit_records[0][0][:3] == ("tester", "POST", "/api/chat")
        assert audit_records[0][1]["model"] == "test"
        assert audit_records[0][1]["tokens"] == {"input_tokens": 7, "output_tokens": 3}
        assert audit_records[0][1]["route_class"] == "model_egress"

    @pytest.mark.asyncio
    async def test_generate_streaming_preserves_ndjson_and_extracts_final_usage(
        self, native_client, auth_headers, monkeypatch
    ):
        audit_records = []
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))

        class _LazyResponse:
            status_code = 200
            headers = {"content-type": "application/x-ndjson"}

            async def aiter_lines(self):
                yield '{"model":"test","response":"Hel","done":false}'
                yield '{"model":"test","response":"lo","done":false}'
                yield '{"model":"test","done":true,"prompt_eval_count":9,"eval_count":2}'

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

        response = await native_client.post(
            "/api/generate",
            headers=auth_headers,
            json={"model": "test", "prompt": "hi"},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert '{"model":"test","response":"Hel","done":false}' in response.text
        assert '{"model":"test","done":true,"prompt_eval_count":9,"eval_count":2}' in response.text
        assert upstream.calls[0]["target"] == "/api/generate"
        assert upstream.stream_obj.entered
        assert upstream.stream_obj.exited
        assert len(audit_records) == 1
        assert audit_records[0][0][:3] == ("tester", "POST", "/api/generate")
        assert audit_records[0][1]["tokens"] == {"input_tokens": 9, "output_tokens": 2}
        assert audit_records[0][1]["api_family"] == "ollama"
        assert audit_records[0][1]["route_class"] == "model_egress"

    @pytest.mark.asyncio
    async def test_generate_streaming_upstream_error_returns_plain_response(
        self, native_client, auth_headers, monkeypatch
    ):
        audit_records = []
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))

        class _ErrorResponse:
            status_code = 503
            headers = {"content-type": "application/json"}

            async def aread(self):
                return b'{"error":"upstream down"}'

        class _FakeStream:
            async def __aenter__(self):
                return _ErrorResponse()

            async def __aexit__(self, *args):
                return False

        class _FakeStreamingClient:
            def stream(self, method, target, content=None, headers=None):
                return _FakeStream()

        monkeypatch.setattr(proxy, "client", _FakeStreamingClient(), raising=False)

        response = await native_client.post(
            "/api/generate",
            headers=auth_headers,
            json={"model": "test", "prompt": "hi"},
        )

        assert response.status_code == 503
        assert response.json() == {"error": "upstream down"}
        assert len(audit_records) == 1
        assert audit_records[0][1]["status"] == 503
        assert audit_records[0][1]["route_class"] == "model_egress"

    @pytest.mark.asyncio
    async def test_generate_streaming_uses_stream_status_when_cloud_sends_json_content_type(
        self, native_client, auth_headers, monkeypatch
    ):
        audit_records = []
        request_count = _FakeLabelledMetric()
        request_latency = _FakeLabelledMetric()
        active_connections = _FakeGauge()
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))
        monkeypatch.setattr(proxy, "REQUEST_COUNT", request_count)
        monkeypatch.setattr(proxy, "REQUEST_LATENCY", request_latency)
        monkeypatch.setattr(proxy, "ACTIVE_CONNECTIONS", active_connections)

        class _BrokenResponse:
            status_code = 200
            headers = {"content-type": "application/json"}

            async def aiter_lines(self):
                raise httpx.StreamClosed()
                yield

        class _FakeStream:
            async def __aenter__(self):
                return _BrokenResponse()

            async def __aexit__(self, *args):
                return False

        class _FakeStreamingClient:
            def stream(self, method, target, content=None, headers=None):
                return _FakeStream()

        monkeypatch.setattr(proxy, "client", _FakeStreamingClient(), raising=False)

        response = await native_client.post(
            "/api/generate",
            headers=auth_headers,
            json={"model": "test", "prompt": "hi"},
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert "Ollama closed the stream unexpectedly" in response.text
        assert active_connections.value == 0
        assert audit_records[0][1]["status"] == 500
        assert any(
            call[0] == "inc" and call[1]["status_code"] == "500"
            for call in request_count.calls
        )

    @pytest.mark.asyncio
    async def test_generate_streaming_records_error_after_partial_upstream_data(
        self, native_client, auth_headers, monkeypatch
    ):
        audit_records = []
        request_count = _FakeLabelledMetric()
        monkeypatch.setattr(proxy, "audit", lambda *args, **kwargs: audit_records.append((args, kwargs)))
        monkeypatch.setattr(proxy, "REQUEST_COUNT", request_count)

        class _PartiallyBrokenResponse:
            status_code = 200
            headers = {"content-type": "application/x-ndjson"}

            async def aiter_lines(self):
                yield '{"model":"test","response":"partial","done":false}'
                raise httpx.StreamClosed()

        class _FakeStream:
            async def __aenter__(self):
                return _PartiallyBrokenResponse()

            async def __aexit__(self, *args):
                return False

        class _FakeStreamingClient:
            def stream(self, method, target, content=None, headers=None):
                return _FakeStream()

        monkeypatch.setattr(proxy, "client", _FakeStreamingClient(), raising=False)

        response = await native_client.post(
            "/api/generate",
            headers=auth_headers,
            json={"model": "test", "prompt": "hi"},
        )

        assert response.status_code == 200
        assert '{"model":"test","response":"partial","done":false}' in response.text
        assert "Ollama closed the stream unexpectedly" in response.text
        assert audit_records[0][1]["status"] == 500
        assert audit_records[0][1]["error"] == "stream closed by Ollama"
        assert any(
            call[0] == "inc" and call[1]["status_code"] == "500"
            for call in request_count.calls
        )
