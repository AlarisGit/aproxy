# Repository Guidelines

## Project Structure & Module Organization

This repository is a Python FastAPI reverse proxy for Ollama. The main application, CLI key management, configuration loading, auth middleware, routing, logging, and metrics all live in `proxy.py`. Tests are under `tests/`, with shared fixtures in `tests/conftest.py` and focused suites such as `test_messages.py`, `test_middleware.py`, and `test_cli.py`. Operational files live at the root: `aproxy.service` for systemd, `logrotate.conf` for log rotation, and `ACTIVE_DATA_PROTECTION.md` for design notes. `scripts/` contains manual smoke and Claude Code integration checks.

Root JSON files (`aproxy.json`, `keys.json`, `models.json`) are local runtime configuration. Treat them as sensitive and avoid committing real tokens, user data, or deployment-specific paths.

## Build, Test, and Development Commands

Create a development environment:

```bash
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx prometheus-client pytest pytest-asyncio respx
```

Run the unit test suite:

```bash
.venv/bin/python3 -m pytest tests/ --ignore=tests/test_integration.py
```

Run integration tests only when the aproxy/Ollama environment is configured:

```bash
APROXY_RUN_INTEGRATION_TESTS=1 .venv/bin/python3 -m pytest tests/test_integration.py -v
scripts/integration_claude_code_suite.sh
```

Manage API keys with:

```bash
.venv/bin/python3 proxy.py keys add <username>
.venv/bin/python3 proxy.py keys list
.venv/bin/python3 proxy.py keys remove <username>
```

## Coding Style & Naming Conventions

Use standard Python 3.10+ style with 4-space indentation, descriptive function names, and type hints where they clarify behavior. Follow existing private-helper naming (`_load_config`, `_ModelMapper`) for module-internal APIs. Keep FastAPI route handlers and middleware behavior explicit; avoid broad rewrites unless tests cover the affected API surface. There is no configured formatter or linter in the repo, so keep diffs small and consistent with surrounding code.

## Testing Guidelines

Pytest is configured in `pytest.ini` with `asyncio_mode = auto` and `testpaths = tests`. Name new test files `test_<feature>.py` and test functions `test_<behavior>`. Prefer fixture-based tests that patch config, key stores, and HTTP clients rather than touching real `keys.json`, logs, or Ollama. Add integration coverage only for behavior that cannot be validated with mocked ASGI/httpx tests.

## Commit & Pull Request Guidelines

Recent history uses short imperative subjects, often Conventional Commit style such as `fix(ollama): allow public model listing`. Prefer that format for scoped fixes and keep subjects under about 72 characters. Pull requests should describe behavior changes, list test commands run, call out config or security implications, and include logs or screenshots only when they clarify operational behavior.
