"""Tests for the PostgreSQL readiness/preflight helper and CLI command.

No psycopg installation and no running PostgreSQL server are required.
All connection paths are mocked; import discovery is patched where needed.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from kb_audit.cli import cli
from kb_audit.storage.pg_readiness import PgReadinessReport, check_readiness


# ---------------------------------------------------------------------------
# check_readiness — no URL
# ---------------------------------------------------------------------------


class TestCheckReadinessNoUrl:
    def test_url_not_supplied(self):
        report = check_readiness(url=None)
        assert not report.url_supplied

    def test_not_postgres_url(self):
        report = check_readiness(url=None)
        assert not report.is_postgres_url

    def test_not_ready(self):
        report = check_readiness(url=None)
        assert not report.ready

    def test_message_mentions_url(self):
        report = check_readiness(url=None)
        assert any("URL" in m or "url" in m for m in report.messages)

    def test_no_psycopg_message_without_postgres_url(self):
        report = check_readiness(url=None)
        assert not any("psycopg" in m for m in report.messages)


# ---------------------------------------------------------------------------
# check_readiness — non-Postgres URL
# ---------------------------------------------------------------------------


class TestCheckReadinessNonPostgresUrl:
    def test_sqlite_memory(self):
        report = check_readiness(url=":memory:")
        assert report.url_supplied
        assert not report.is_postgres_url
        assert not report.ready

    def test_sqlite_scheme(self):
        report = check_readiness(url="sqlite:///kbaudit.db")
        assert not report.is_postgres_url
        assert not report.ready

    def test_bare_path(self):
        report = check_readiness(url="kbaudit.db")
        assert not report.is_postgres_url

    def test_message_says_not_postgres(self):
        report = check_readiness(url=":memory:")
        assert any("not a PostgreSQL URL" in m or "SQLite" in m for m in report.messages)

    def test_no_psycopg_message_for_non_postgres_url(self):
        report = check_readiness(url=":memory:")
        assert not any("psycopg" in m for m in report.messages)


# ---------------------------------------------------------------------------
# check_readiness — Postgres URL, import probes mocked
# ---------------------------------------------------------------------------


class TestCheckReadinessPostgresUrl:
    PG_URL = "postgresql://localhost/kbaudit"
    PG_SHORT = "postgres://localhost/kbaudit"

    def test_postgresql_scheme_detected(self):
        report = check_readiness(url=self.PG_URL)
        assert report.url_supplied
        assert report.is_postgres_url

    def test_postgres_short_scheme_detected(self):
        report = check_readiness(url=self.PG_SHORT)
        assert report.is_postgres_url

    def test_missing_psycopg_not_ready(self):
        with patch(
            "kb_audit.storage.pg_readiness._module_available",
            side_effect=lambda m: False if m == "psycopg" else True,
        ):
            report = check_readiness(url=self.PG_URL)
        assert not report.psycopg_available
        assert not report.ready

    def test_missing_psycopg_message_present(self):
        with patch(
            "kb_audit.storage.pg_readiness._module_available",
            side_effect=lambda m: False if m == "psycopg" else True,
        ):
            report = check_readiness(url=self.PG_URL)
        assert any("psycopg" in m for m in report.messages)

    def test_missing_alembic_not_ready(self):
        with patch(
            "kb_audit.storage.pg_readiness._module_available",
            side_effect=lambda m: False if m == "alembic" else True,
        ):
            report = check_readiness(url=self.PG_URL)
        assert not report.alembic_available
        assert not report.ready

    def test_all_deps_present_and_migrations_on_disk(self):
        # Real migrations/versions/0001_initial_schema.py exists in the repo.
        with patch(
            "kb_audit.storage.pg_readiness._module_available",
            return_value=True,
        ):
            report = check_readiness(url=self.PG_URL)
        assert report.psycopg_available
        assert report.alembic_available
        assert report.migrations_on_disk
        assert report.migration_count >= 1
        assert report.ready
        assert report.messages == []

    def test_missing_migrations_dir_not_ready(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kb_audit.storage.pg_readiness._migrations_dir",
            lambda: tmp_path / "no_such_dir" / "versions",
        )
        with patch(
            "kb_audit.storage.pg_readiness._module_available",
            return_value=True,
        ):
            report = check_readiness(url=self.PG_URL)
        assert not report.migrations_on_disk
        assert not report.ready
        assert any("not found" in m.lower() or "Migrations" in m for m in report.messages)

    def test_empty_migrations_dir_not_ready(self, tmp_path, monkeypatch):
        empty_versions = tmp_path / "migrations" / "versions"
        empty_versions.mkdir(parents=True)
        monkeypatch.setattr(
            "kb_audit.storage.pg_readiness._migrations_dir",
            lambda: empty_versions,
        )
        with patch(
            "kb_audit.storage.pg_readiness._module_available",
            return_value=True,
        ):
            report = check_readiness(url=self.PG_URL)
        assert report.migration_count == 0
        assert not report.migrations_on_disk
        assert not report.ready


# ---------------------------------------------------------------------------
# PgReadinessReport.ready property
# ---------------------------------------------------------------------------


class TestPgReadinessReportReady:
    def _report(self, **kwargs) -> PgReadinessReport:
        defaults = dict(
            url_supplied=True,
            is_postgres_url=True,
            psycopg_available=True,
            alembic_available=True,
            migrations_on_disk=True,
            migration_count=1,
        )
        defaults.update(kwargs)
        return PgReadinessReport(**defaults)

    def test_ready_when_all_true(self):
        assert self._report().ready

    def test_not_ready_when_not_postgres_url(self):
        assert not self._report(is_postgres_url=False).ready

    def test_not_ready_when_psycopg_missing(self):
        assert not self._report(psycopg_available=False).ready

    def test_not_ready_when_alembic_missing(self):
        assert not self._report(alembic_available=False).ready

    def test_not_ready_when_migrations_missing(self):
        assert not self._report(migrations_on_disk=False).ready


# ---------------------------------------------------------------------------
# connect_check — psycopg not installed (real test-env behaviour)
# ---------------------------------------------------------------------------


class TestConnectCheckMissingPsycopg:
    def test_raises_runtime_error(self):
        from kb_audit.storage.pg_readiness import connect_check

        # Force require_psycopg() to raise regardless of installed packages.
        with patch(
            "kb_audit.storage.postgres_support.psycopg_available", return_value=False
        ):
            with pytest.raises(RuntimeError, match="psycopg"):
                connect_check("postgresql://localhost/test")


# ---------------------------------------------------------------------------
# connect_check — mocked psycopg (success and failure paths)
# ---------------------------------------------------------------------------


def _make_fake_psycopg(fail_with: str | None = None) -> types.ModuleType:
    """Return a minimal fake psycopg module."""
    mod = types.ModuleType("psycopg")
    fake_conn = MagicMock()
    if fail_with:
        mod.connect = MagicMock(side_effect=Exception(fail_with))
    else:
        mod.connect = MagicMock(return_value=fake_conn)
    return mod


class TestConnectCheckMocked:
    def test_success_returns_empty_string(self):
        fake_psycopg = _make_fake_psycopg()
        with (
            patch.dict(sys.modules, {"psycopg": fake_psycopg}),
            patch("kb_audit.storage.postgres_support.psycopg_available", return_value=True),
        ):
            from kb_audit.storage.pg_readiness import connect_check

            err = connect_check("postgresql://localhost/test")
        assert err == ""

    def test_connection_error_returns_message(self):
        fake_psycopg = _make_fake_psycopg(fail_with="Connection refused")
        with (
            patch.dict(sys.modules, {"psycopg": fake_psycopg}),
            patch("kb_audit.storage.postgres_support.psycopg_available", return_value=True),
        ):
            from kb_audit.storage.pg_readiness import connect_check

            err = connect_check("postgresql://localhost/test")
        assert "Connection refused" in err

    def test_connection_closed_after_success(self):
        fake_psycopg = _make_fake_psycopg()
        with (
            patch.dict(sys.modules, {"psycopg": fake_psycopg}),
            patch("kb_audit.storage.postgres_support.psycopg_available", return_value=True),
        ):
            from kb_audit.storage.pg_readiness import connect_check

            connect_check("postgresql://localhost/test")
        # The context-manager __exit__ was called, which closes the connection.
        fake_conn = fake_psycopg.connect.return_value
        fake_conn.__exit__.assert_called_once()


# ---------------------------------------------------------------------------
# CLI postgres-check command — offline checks
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


class TestPostgresCheckCliOffline:
    def _invoke(self, runner, args, env=None):
        return runner.invoke(
            cli, ["postgres-check", *args], env=env or {}, catch_exceptions=False
        )

    def test_no_url_exits_nonzero(self, runner):
        result = self._invoke(runner, [])
        assert result.exit_code != 0

    def test_no_url_output_mentions_status(self, runner):
        result = self._invoke(runner, [])
        assert "PostgreSQL Readiness Check" in result.output

    def test_sqlite_url_exits_nonzero(self, runner):
        result = self._invoke(runner, ["--url", ":memory:"])
        assert result.exit_code != 0

    def test_sqlite_url_output_mentions_sqlite(self, runner):
        result = self._invoke(runner, ["--url", ":memory:"])
        assert "SQLite" in result.output or "not a PostgreSQL" in result.output.lower()

    def test_postgres_url_missing_psycopg_exits_nonzero(self, runner):
        with patch(
            "kb_audit.storage.pg_readiness._module_available",
            side_effect=lambda m: False if m == "psycopg" else True,
        ):
            result = self._invoke(runner, ["--url", "postgresql://localhost/test"])
        assert result.exit_code != 0

    def test_postgres_url_missing_psycopg_output_mentions_no(self, runner):
        with patch(
            "kb_audit.storage.pg_readiness._module_available",
            side_effect=lambda m: False if m == "psycopg" else True,
        ):
            result = self._invoke(runner, ["--url", "postgresql://localhost/test"])
        assert "NO" in result.output or "psycopg" in result.output

    def test_postgres_url_all_deps_present_exits_zero(self, runner):
        with patch(
            "kb_audit.storage.pg_readiness._module_available",
            return_value=True,
        ):
            result = self._invoke(runner, ["--url", "postgresql://localhost/test"])
        assert result.exit_code == 0

    def test_postgres_url_all_deps_present_output_ready(self, runner):
        with patch(
            "kb_audit.storage.pg_readiness._module_available",
            return_value=True,
        ):
            result = self._invoke(runner, ["--url", "postgresql://localhost/test"])
        assert "ready" in result.output.lower() or "passed" in result.output.lower()

    def test_env_var_url_accepted(self, runner):
        with patch(
            "kb_audit.storage.pg_readiness._module_available",
            return_value=True,
        ):
            result = runner.invoke(
                cli,
                ["postgres-check"],
                env={"KB_AUDIT_POSTGRES_TEST_URL": "postgresql://localhost/test"},
                catch_exceptions=False,
            )
        assert result.exit_code == 0

    def test_output_shows_all_check_lines(self, runner):
        result = self._invoke(runner, ["--url", "postgresql://localhost/test"])
        assert "URL supplied" in result.output
        assert "psycopg available" in result.output
        assert "alembic available" in result.output
        assert "migrations on disk" in result.output


# ---------------------------------------------------------------------------
# CLI postgres-check command — --connect flag (mocked)
# ---------------------------------------------------------------------------


class TestPostgresCheckCliConnect:
    def _invoke(self, runner, args, env=None):
        return runner.invoke(
            cli, ["postgres-check", *args], env=env or {}, catch_exceptions=False
        )

    def test_connect_without_url_exits_nonzero(self, runner):
        result = self._invoke(runner, ["--connect"])
        assert result.exit_code != 0

    def test_connect_missing_psycopg_exits_nonzero(self, runner):
        with patch(
            "kb_audit.storage.pg_readiness._module_available",
            return_value=False,
        ):
            result = self._invoke(
                runner, ["--url", "postgresql://localhost/test", "--connect"]
            )
        assert result.exit_code != 0

    def test_connect_success_exits_zero(self, runner):
        with (
            patch("kb_audit.storage.pg_readiness._module_available", return_value=True),
            patch("kb_audit.storage.pg_readiness.connect_check", return_value=""),
        ):
            result = self._invoke(
                runner, ["--url", "postgresql://localhost/test", "--connect"]
            )
        assert result.exit_code == 0
        assert "OK" in result.output

    def test_connect_failure_exits_nonzero(self, runner):
        with (
            patch("kb_audit.storage.pg_readiness._module_available", return_value=True),
            patch(
                "kb_audit.storage.pg_readiness.connect_check",
                return_value="Connection refused",
            ),
        ):
            result = self._invoke(
                runner, ["--url", "postgresql://localhost/test", "--connect"]
            )
        assert result.exit_code != 0
        assert "FAILED" in result.output or "Connection refused" in result.output


# ---------------------------------------------------------------------------
# CLI postgres-check — URL resolution precedence
# ---------------------------------------------------------------------------


class TestPostgresCheckUrlPrecedence:
    """Verify --url > KB_AUDIT_POSTGRES_TEST_URL > DATABASE_URL config fallback."""

    def test_configured_database_url_used_as_fallback(self, runner):
        """DATABASE_URL=postgresql://... with no --url or KB_AUDIT_POSTGRES_TEST_URL."""
        with patch("kb_audit.storage.pg_readiness._module_available", return_value=True):
            result = runner.invoke(
                cli,
                ["postgres-check"],
                env={"DATABASE_URL": "postgresql://localhost/from_config"},
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert "PostgreSQL URL       : yes" in result.output

    def test_url_option_takes_precedence_over_database_url(self, runner):
        """--url wins even when DATABASE_URL is set to a SQLite path."""
        with patch("kb_audit.storage.pg_readiness._module_available", return_value=True):
            result = runner.invoke(
                cli,
                ["postgres-check", "--url", "postgresql://localhost/from_option"],
                env={"DATABASE_URL": ":memory:"},
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert "PostgreSQL URL       : yes" in result.output

    def test_kb_audit_postgres_test_url_takes_precedence_over_database_url(self, runner):
        """KB_AUDIT_POSTGRES_TEST_URL wins when --url is absent but DATABASE_URL is SQLite."""
        with patch("kb_audit.storage.pg_readiness._module_available", return_value=True):
            result = runner.invoke(
                cli,
                ["postgres-check"],
                env={
                    "DATABASE_URL": ":memory:",
                    "KB_AUDIT_POSTGRES_TEST_URL": "postgresql://localhost/from_env",
                },
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert "PostgreSQL URL       : yes" in result.output

    def test_sqlite_database_url_reports_not_postgres_and_exits_nonzero(self, runner):
        """Configured SQLite URL falls through to 'not a PostgreSQL URL' branch."""
        result = runner.invoke(
            cli,
            ["postgres-check"],
            env={"DATABASE_URL": ":memory:"},
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "SQLite" in result.output or "not a PostgreSQL" in result.output.lower()


# ---------------------------------------------------------------------------
# CLI postgres-check — empty / whitespace DATABASE_URL normalization
# ---------------------------------------------------------------------------


class TestPostgresCheckEmptyUrl:
    def test_empty_database_url_treated_as_no_url(self, runner):
        """DATABASE_URL='' is normalized to None → URL supplied: no."""
        result = runner.invoke(
            cli,
            ["postgres-check"],
            env={"DATABASE_URL": ""},
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "URL supplied         : no" in result.output

    def test_whitespace_database_url_treated_as_no_url(self, runner):
        """DATABASE_URL='   ' is normalized to None → URL supplied: no."""
        result = runner.invoke(
            cli,
            ["postgres-check"],
            env={"DATABASE_URL": "   "},
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "URL supplied         : no" in result.output

    def test_sqlite_database_url_still_counts_as_supplied(self, runner):
        """DATABASE_URL=':memory:' is non-empty → URL supplied: yes, PostgreSQL URL: no."""
        result = runner.invoke(
            cli,
            ["postgres-check"],
            env={"DATABASE_URL": ":memory:"},
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "URL supplied         : yes" in result.output
        assert "PostgreSQL URL       : no" in result.output

    def test_url_option_overrides_empty_database_url(self, runner):
        """--url postgresql://... wins even when DATABASE_URL is empty."""
        with patch("kb_audit.storage.pg_readiness._module_available", return_value=True):
            result = runner.invoke(
                cli,
                ["postgres-check", "--url", "postgresql://localhost/from_option"],
                env={"DATABASE_URL": ""},
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert "PostgreSQL URL       : yes" in result.output

    def test_kb_audit_postgres_test_url_overrides_empty_database_url(self, runner):
        """KB_AUDIT_POSTGRES_TEST_URL=postgresql://... wins when DATABASE_URL is empty."""
        with patch("kb_audit.storage.pg_readiness._module_available", return_value=True):
            result = runner.invoke(
                cli,
                ["postgres-check"],
                env={
                    "DATABASE_URL": "",
                    "KB_AUDIT_POSTGRES_TEST_URL": "postgresql://localhost/from_env",
                },
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert "PostgreSQL URL       : yes" in result.output
