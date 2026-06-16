"""End-to-end integration tests that exercise the full Claude Code → aproxy → Ollama path.

These tests require:
  - aproxy running on ANTHROPIC_PROXY_HOST:ANTHROPIC_PROXY_PORT (default 127.0.0.1:4001)
  - Ollama running on OLLAMA_BASE_URL (default http://127.0.0.1:11434) with at least one model
  - the `claude` CLI on PATH

They are skipped unless the environment variable APROXY_RUN_INTEGRATION_TESTS=1 is set.
"""

import os
import secrets
import subprocess
import sys

import httpx
import pytest

import proxy

CLAUDE = "/home/sergey/.local/bin/claude"
APROXY_URL = os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:4001")
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")


@pytest.mark.skipif(
    os.environ.get("APROXY_RUN_INTEGRATION_TESTS") != "1",
    reason="Set APROXY_RUN_INTEGRATION_TESTS=1 to run integration tests",
)
class TestClaudeCodeIntegration:
    @pytest.fixture(scope="class")
    def integration_token(self):
        token = "sk-itest-" + secrets.token_hex(16)
        result = subprocess.run(
            [
                sys.executable,
                "proxy.py",
                "keys",
                "add",
                "integration-tester",
                token,
            ],
            cwd="/home/sergey/Projects/aproxy",
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.returncode == 0, result.stderr
        yield token
        # Cleanup: remove the temporary user
        subprocess.run(
            [sys.executable, "proxy.py", "keys", "remove", "integration-tester"],
            cwd="/home/sergey/Projects/aproxy",
            capture_output=True,
            text=True,
        )

    def test_aproxy_health(self):
        r = httpx.get(f"{APROXY_URL}/health")
        assert r.status_code == 200
        assert r.json().get("status") == "ok"

    def test_ollama_has_models(self):
        r = httpx.get(f"{OLLAMA_URL}/api/tags")
        assert r.status_code == 200
        models = r.json().get("models", [])
        assert len(models) > 0, "Ollama has no models pulled"

    def test_proxy_models_requires_auth(self):
        r = httpx.get(f"{APROXY_URL}/v1/models")
        assert r.status_code == 401

    def test_proxy_models_with_token(self, integration_token):
        r = httpx.get(
            f"{APROXY_URL}/v1/models",
            headers={"Authorization": f"Bearer {integration_token}"},
        )
        assert r.status_code == 200
        assert len(r.json().get("data", [])) > 0

    def test_claude_code_one_shot_through_proxy(self, integration_token):
        """Run Claude Code in non-interactive mode against aproxy and verify output."""
        if not os.path.exists(CLAUDE):
            pytest.skip("Claude Code CLI not found at /home/sergey/.local/bin/claude")

        env = {
            **os.environ,
            "ANTHROPIC_BASE_URL": APROXY_URL,
            "ANTHROPIC_AUTH_TOKEN": integration_token,
            "ANTHROPIC_API_KEY": "",
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
        }
        # Remove any proxy variables that could break local routing
        for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "SOCKS_PROXY", "ALL_PROXY"):
            env.pop(proxy_var, None)
            env.pop(proxy_var.lower(), None)

        # Pick the first available Ollama model for Claude to use
        models = httpx.get(f"{OLLAMA_URL}/api/tags").json().get("models", [])
        model_name = models[0]["name"]

        proc = subprocess.run(
            [
                CLAUDE,
                "-p",
                "Say exactly: aproxy integration test passed",
                "--bare",
                "--dangerously-skip-permissions",
                "--no-session-persistence",
                "--model",
                model_name,
            ],
            env=env,
            cwd="/home/sergey/Projects/aproxy",
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"claude failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert "aproxy integration test passed" in proc.stdout
