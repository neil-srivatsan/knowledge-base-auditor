"""Tests for the PostgreSQL driver availability probe.

No PostgreSQL server is required; no connections are opened.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from kb_audit.storage.postgres_support import psycopg_available, require_psycopg


class TestPsycopgAvailable:
    def test_returns_bool(self):
        result = psycopg_available()
        assert isinstance(result, bool)

    def test_returns_true_when_importable(self, monkeypatch):
        # Simulate psycopg being available by ensuring the real import path works
        # (or by patching it in if psycopg is not installed).
        import importlib

        try:
            importlib.import_module("psycopg")
            # psycopg is actually installed — the function must return True.
            assert psycopg_available() is True
        except ModuleNotFoundError:
            # psycopg is not installed in this environment; patch to simulate availability.
            fake_psycopg = type(sys)("psycopg")
            with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
                assert psycopg_available() is True

    def test_returns_false_when_not_importable(self, monkeypatch):
        with patch.dict(sys.modules, {"psycopg": None}):  # type: ignore[dict-item]
            assert psycopg_available() is False


class TestRequirePsycopg:
    def test_does_not_open_a_connection(self):
        # Calling require_psycopg() must never reach a database.
        # We verify this indirectly: no network/socket side-effects are raised;
        # the call either returns None or raises RuntimeError — nothing else.
        try:
            result = require_psycopg()
            assert result is None
        except RuntimeError:
            pass  # expected when psycopg is absent

    def test_returns_none_when_psycopg_available(self):
        import importlib

        try:
            importlib.import_module("psycopg")
        except ModuleNotFoundError:
            pytest.skip("psycopg not installed in this environment")

        # Should not raise.
        assert require_psycopg() is None

    def test_raises_runtime_error_when_psycopg_absent(self):
        with patch.dict(sys.modules, {"psycopg": None}):  # type: ignore[dict-item]
            with pytest.raises(RuntimeError, match="psycopg"):
                require_psycopg()

    def test_error_message_contains_install_hint(self):
        with patch.dict(sys.modules, {"psycopg": None}):  # type: ignore[dict-item]
            with pytest.raises(RuntimeError) as exc_info:
                require_psycopg()
        msg = str(exc_info.value)
        assert "psycopg" in msg
        assert "pip install" in msg or "install" in msg.lower()
