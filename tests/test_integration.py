"""End-to-end integration tests that exercise the full Claude Code → aproxy → Ollama path.

These tests require:
  - aproxy running on the host/port configured in aproxy.json (default 127.0.0.1:4001)
  - Ollama running on the URL configured in aproxy.json (default http://127.0.0.1:11434) with at least one model
  - the `claude` CLI on PATH

They are skipped unless the environment variable APROXY_RUN_INTEGRATION_TESTS=1 is set.
"""

import json
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


def _integration_env(integration_token: str) -> dict:
    """Build environment for Claude Code pointing at aproxy."""
    env = {
        **os.environ,
        "ANTHROPIC_BASE_URL": APROXY_URL,
        "ANTHROPIC_AUTH_TOKEN": integration_token,
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    }
    for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "SOCKS_PROXY", "ALL_PROXY"):
        env.pop(proxy_var, None)
        env.pop(proxy_var.lower(), None)
    return env


def _default_model() -> str:
    """Pick the best available Ollama model for integration tests.

    Prefers larger reasoning-capable models for complex multi-tool prompts,
    falling back to any available model. Override with APROXY_INTEGRATION_MODEL.
    """
    explicit = os.environ.get("APROXY_INTEGRATION_MODEL")
    if explicit:
        return explicit

    r = httpx.get(f"{OLLAMA_URL}/api/tags")
    r.raise_for_status()
    models = r.json().get("models", [])
    assert models, "Ollama has no models pulled"
    names = {m["name"] for m in models}

    preferred = [
        "kimi-k2.7-code:cloud",
        "kimi-k2.5:cloud",
        "deepseek-v4-pro:cloud",
        "deepseek-v4-flash:cloud",
        "glm-5.1:cloud",
        "gemma4:31b-cloud",
        "gemini-3-flash-preview:latest",
        "gpt-oss:120b-cloud",
    ]
    for name in preferred:
        if name in names:
            return name

    return models[0]["name"]


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

        env = _integration_env(integration_token)
        model_name = _default_model()

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

    def test_claude_code_without_model_arg_uses_server_mapping(self, integration_token):
        """Claude Code's default Anthropic model ID is translated by aproxy."""
        if not os.path.exists(CLAUDE):
            pytest.skip("Claude Code CLI not found at /home/sergey/.local/bin/claude")

        env = _integration_env(integration_token)
        # Intentionally omit --model so Claude uses its internal default.
        proc = subprocess.run(
            [
                CLAUDE,
                "-p",
                "Say exactly: server mapping test passed",
                "--bare",
                "--dangerously-skip-permissions",
                "--no-session-persistence",
            ],
            env=env,
            cwd="/home/sergey/Projects/aproxy",
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"claude failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert "server mapping test passed" in proc.stdout


@pytest.mark.skipif(
    os.environ.get("APROXY_RUN_INTEGRATION_TESTS") != "1",
    reason="Set APROXY_RUN_INTEGRATION_TESTS=1 to run integration tests",
)
class TestClaudeCodeSelfDiagnostics:
    """Deep self-diagnostics of Claude Code tool capabilities through aproxy.

    Each test exercises one tool or capability in one-shot mode. They are
    intentionally separate because smaller prompts are more reliable with
    local models than a single giant diagnostic prompt.
    """

    @pytest.fixture(scope="class")
    def integration_token(self):
        token = "sk-itest-" + secrets.token_hex(16)
        result = subprocess.run(
            [
                sys.executable,
                "proxy.py",
                "keys",
                "add",
                "diagnostic-tester",
                token,
            ],
            cwd="/home/sergey/Projects/aproxy",
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.returncode == 0, result.stderr
        yield token
        subprocess.run(
            [sys.executable, "proxy.py", "keys", "remove", "diagnostic-tester"],
            cwd="/home/sergey/Projects/aproxy",
            capture_output=True,
            text=True,
        )

    @pytest.fixture(scope="class")
    def model_name(self):
        return _default_model()

    def _run_claude(self, token: str, prompt: str, model_name: str, extra_args=None, timeout: int = 120) -> subprocess.CompletedProcess:
        if not os.path.exists(CLAUDE):
            pytest.skip("Claude Code CLI not found at /home/sergey/.local/bin/claude")

        env = _integration_env(token)
        cmd = [
            CLAUDE,
            "-p",
            prompt,
            "--bare",
            "--dangerously-skip-permissions",
            "--no-session-persistence",
            "--model",
            model_name,
            "--tools",
            "Bash,WebFetch,WebSearch",
        ]
        if extra_args:
            cmd.extend(extra_args)

        return subprocess.run(
            cmd,
            env=env,
            cwd="/home/sergey/Projects/aproxy",
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_bash_tool(self, integration_token, model_name):
        """Claude can execute local shell commands through aproxy."""
        proc = self._run_claude(
            integration_token,
            "Use Bash to run 'echo bash-tool-ok' and return ONLY the exact command output, no commentary.",
            model_name,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert "bash-tool-ok" in proc.stdout

    def test_webfetch_tool(self, integration_token, model_name):
        """Claude can fetch web pages through aproxy."""
        proc = self._run_claude(
            integration_token,
            "Use WebFetch to fetch https://httpbin.org/get and return ONLY the value of the 'url' field from the JSON response.",
            model_name,
            timeout=180,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert "https://httpbin.org/get" in proc.stdout

    def test_websearch_tool(self, integration_token, model_name):
        """Claude can search the web through aproxy."""
        proc = self._run_claude(
            integration_token,
            "Use WebSearch to search for 'current year' and return ONLY the current year as a 4-digit number.",
            model_name,
            timeout=180,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        current_year = str(2026)  # Tests run in 2026 in this environment
        assert current_year in proc.stdout

    def test_custom_agent(self, integration_token, model_name):
        """Claude can delegate work to a custom background agent through aproxy."""
        agents_json = json.dumps({
            "diagnostician": {
                "description": "Runs a simple local diagnostic",
                "prompt": "You are a local diagnostic agent. Use Bash to run 'echo agent-ok' and return ONLY the word 'agent-ok'.",
            }
        })
        proc = self._run_claude(
            integration_token,
            "Run the /diagnostician agent and return only its output.",
            model_name,
            extra_args=["--agents", agents_json, "--agent", "diagnostician"],
            timeout=180,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        assert "agent-ok" in proc.stdout
