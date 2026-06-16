"""Tests for CLI key management helpers."""

import json
import os
import subprocess
import sys

import pytest

PYTHON = sys.executable


class TestKeyCLI:
    def test_cli_add_creates_hashed_key(self, tmp_path, monkeypatch):
        keys_file = tmp_path / "keys.json"
        monkeypatch.setenv("API_KEYS_FILE", str(keys_file))
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = "/home/sergey/Projects/aproxy/.venv/lib/python3.14/site-packages"
        result = subprocess.run(
            ["/home/sergey/Projects/aproxy/.venv/bin/python3", "/home/sergey/Projects/aproxy/proxy.py", "keys", "add", "alice"],
            cwd="/home/sergey/Projects/aproxy",
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "alice" in result.stdout
        data = json.loads(keys_file.read_text())
        assert "_salt" in data
        assert "alice" in data["users"].values()
        # permissions should be owner-only
        assert oct(keys_file.stat().st_mode)[-3:] == "600"

    def test_cli_remove_user(self, tmp_path, monkeypatch):
        keys_file = tmp_path / "keys.json"
        keys_file.write_text(json.dumps({"sk-legacy": "bob"}))
        monkeypatch.setenv("API_KEYS_FILE", str(keys_file))
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = "/home/sergey/Projects/aproxy/.venv/lib/python3.14/site-packages"
        result = subprocess.run(
            ["/home/sergey/Projects/aproxy/.venv/bin/python3", "/home/sergey/Projects/aproxy/proxy.py", "keys", "remove", "bob"],
            cwd="/home/sergey/Projects/aproxy",
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(keys_file.read_text())
        assert "bob" not in (data.get("users") or data).values()
