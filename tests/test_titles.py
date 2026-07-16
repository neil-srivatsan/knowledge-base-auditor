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

    def test_arbitrary_numeric_suffix_not_stripped(self):
        """Four-digit suffix outside 1900-2099 must not be treated as a year."""
        assert normalize_base_title("Error 4004") == "error 4004"

    def test_version_and_year_both_stripped(self):
        """Both a trailing version and a trailing year should be stripped."""
        assert normalize_base_title("Guide v1 2021") == "guide"

    def test_short_label_year_preserved(self):
        """A trailing year after a single-word label must not be stripped."""
        assert normalize_base_title("HTTP 2000") == "http 2000"


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

    def test_arbitrary_numeric_suffix_not_stripped(self):
        base, ver, stale = normalize_title("Error 4004")
        assert base == "error 4004"
        assert ver is None
        assert stale is None

    def test_short_label_year_preserved(self):
        """Single-word-before-year must not be stripped."""
        base, ver, stale = normalize_title("HTTP 2000")
        assert base == "http 2000"
        assert ver is None
        assert stale is None

    def test_version_and_year_base(self):
        """normalize_title strips both version and year, returning base 'guide'."""
        base, ver, stale = normalize_title("Guide v1 2021")
        assert base == "guide"
        assert ver == "2021"
        assert stale is None

    def test_normalize_title_agrees_with_normalize_base_title(self):
        """normalize_title[0] and normalize_base_title must agree on the base title."""
        title = "Guide v1 2021"
        base_from_tuple, _, _ = normalize_title(title)
        assert base_from_tuple == normalize_base_title(title)
