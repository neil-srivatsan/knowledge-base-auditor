"""Tests for the shared title normalization module."""

from kb_audit.titles import normalize_base_title, normalize_title


class TestNormalizeBaseTitle:
    def test_trailing_year(self):
        assert normalize_base_title("Payment Platform Migration Guide 2021") == "payment platform migration guide"

    def test_trailing_version_v(self):
        assert normalize_base_title("API Guide v1") == "api guide"

    def test_trailing_version_word(self):
        assert normalize_base_title("API Guide version 2.0") == "api guide"

    def test_old_suffix(self):
        assert normalize_base_title("API Guide (old)") == "api guide"

    def test_deprecated_suffix(self):
        assert normalize_base_title("Payments Docs (deprecated)") == "payments docs"

    def test_archived_suffix(self):
        assert normalize_base_title("Payments Docs (archived)") == "payments docs"

    def test_copy_suffix(self):
        assert normalize_base_title("My Guide (copy)") == "my guide"

    def test_no_suffix(self):
        assert normalize_base_title("Payment Processing Guide") == "payment processing guide"

    def test_year_and_stale_suffix(self):
        assert normalize_base_title("Migration Guide 2021 (old)") == "migration guide"

    def test_preserves_midword_numbers(self):
        """Ensure '2021' in the middle is not stripped."""
        assert normalize_base_title("Guide for 2021 Compliance") == "guide for 2021 compliance"


class TestNormalizeTitle:
    def test_returns_tuple(self):
        base, ver, stale = normalize_title("API Guide v2")
        assert base == "api guide"
        assert ver == "v2"
        assert stale is None

    def test_year_suffix(self):
        base, ver, stale = normalize_title("Migration Guide 2021")
        assert base == "migration guide"
        assert ver == "2021"

    def test_stale_suffix(self):
        base, ver, stale = normalize_title("API Guide (old)")
        assert base == "api guide"
        assert stale is not None
        assert "old" in stale.lower()

    def test_all_three(self):
        base, ver, stale = normalize_title("Migration Guide 2021 (deprecated)")
        assert base == "migration guide"
        assert ver == "2021"
        assert stale is not None
