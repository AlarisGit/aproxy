"""Tests for public and authenticated health/metadata endpoints."""

import json

import httpx
import respx


class TestHealth:
    def test_health_returns_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "ollama" in data
        assert data["proxy"].startswith("aproxy/")

    def test_health_is_public(self, client):
        response = client.get("/health")
        assert response.status_code == 200

    @respx.mock
    def test_health_reports_unreachable_when_ollama_down(self, client):
        respx.get("http://127.0.0.1:11434/api/version").mock(side_effect=httpx.ConnectError("refused"))
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["ollama"]["version"] == "unreachable"


class TestMetrics:
    def test_metrics_requires_auth(self, client):
        response = client.get("/metrics")
        assert response.status_code == 401

    def test_metrics_returns_prometheus_with_auth(self, client, auth_headers):
        response = client.get("/metrics", headers=auth_headers)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "aproxy_requests_total" in response.text


class TestModels:
    @respx.mock
    def test_list_models_requires_auth(self, client):
        respx.get("http://127.0.0.1:11434/v1/models").respond(200, json={"models": []})
        response = client.get("/v1/models")
        assert response.status_code == 401

    @respx.mock
    def test_list_models_proxies_with_auth(self, client, auth_headers):
        respx.get("http://127.0.0.1:11434/v1/models").respond(200, json={"models": [{"name": "test"}]})
        response = client.get("/v1/models", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == {"models": [{"name": "test"}]}
