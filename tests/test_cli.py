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
        monkeypatch.setenv("APROXY_CONFIG", str(tmp_path / "aproxy.json"))
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = "/home/sergey/Projects/aproxy/.venv/lib/python3.14/site-packages"
        env["APROXY_CONFIG"] = str(tmp_path / "aproxy.json")
        # CLI needs keys_file to know where to write, but we removed API_KEYS_FILE env.
        # We pass APROXY_CONFIG pointing to a config that specifies keys_file.
        config = {
            "keys_file": str(keys_file),
            "models_file": str(tmp_path / "models.json"),
        }
        (tmp_path / "aproxy.json").write_text(json.dumps(config))
        result = subprocess.run(
            ["/home/sergey/Projects/aproxy/.venv/bin/python3", "/home/sergey/Projects/aproxy/proxy.py", "keys", "add", "alice"],
            cwd="/home/sergey/Projects/aproxy",
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "alice" in result.stdout
        data = json.loads(keys_file.read_text())
        assert "_salt" in data
        assert "alice" in data["users"].values()
        # permissions should be owner-only
        assert oct(keys_file.stat().st_mode)[-3:] == "600"

    def test_cli_remove_user(self, tmp_path, monkeypatch):
        keys_file = tmp_path / "keys.json"
        keys_file.write_text(json.dumps({"sk-legacy": "bob"}))
        monkeypatch.setenv("APROXY_CONFIG", str(tmp_path / "aproxy.json"))
        env = os.environ.copy()
        env["LD_LIBRARY_PATH"] = "/home/sergey/Projects/aproxy/.venv/lib/python3.14/site-packages"
        env["APROXY_CONFIG"] = str(tmp_path / "aproxy.json")
        config = {
            "keys_file": str(keys_file),
            "models_file": str(tmp_path / "models.json"),
        }
        (tmp_path / "aproxy.json").write_text(json.dumps(config))
        result = subprocess.run(
            ["/home/sergey/Projects/aproxy/.venv/bin/python3", "/home/sergey/Projects/aproxy/proxy.py", "keys", "remove", "bob"],
            cwd="/home/sergey/Projects/aproxy",
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(keys_file.read_text())
        assert "bob" not in (data.get("users") or data).values()
