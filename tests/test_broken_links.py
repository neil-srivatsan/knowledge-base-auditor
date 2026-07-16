"""Tests for BrokenLinkAnalyzer URL extraction and internal-URL blocking."""

from __future__ import annotations

from kb_audit.analyzers.broken_links import _extract_urls, _is_internal_url
from kb_audit.models import Document


def _doc(content: str = "", metadata: dict | None = None) -> Document:
    return Document(id="test", title="Test Doc", content=content, source_type="test", metadata=metadata)


class TestIsInternalUrl:
    def test_localhost(self):
        assert _is_internal_url("http://localhost/foo")

    def test_localhost_with_port(self):
        assert _is_internal_url("http://localhost:8080/api")

    def test_localhost_subdomain(self):
        assert _is_internal_url("http://my-service.localhost/")

    def test_loopback_ipv4(self):
        assert _is_internal_url("http://127.0.0.1/path")

    def test_loopback_other(self):
        assert _is_internal_url("http://127.255.255.255/path")

    def test_private_class_a(self):
        assert _is_internal_url("http://10.0.0.1/admin")

    def test_private_class_b(self):
        assert _is_internal_url("http://172.16.0.1/admin")

    def test_private_class_c(self):
        assert _is_internal_url("http://192.168.1.100/admin")

    def test_link_local(self):
        assert _is_internal_url("http://169.254.0.1/path")

    def test_public_hostname_not_internal(self):
        assert not _is_internal_url("https://example.com/path")

    def test_public_ip_not_internal(self):
        assert not _is_internal_url("http://8.8.8.8/dns")

    def test_public_ip_93(self):
        assert not _is_internal_url("https://93.184.216.34/index.html")


class TestExtractUrlsInternalFiltering:
    def test_localhost_skipped(self):
        doc = _doc(content="See http://localhost/api for details")
        assert _extract_urls(doc) == []

    def test_private_ip_skipped(self):
        doc = _doc(content="Admin panel at http://192.168.1.1/admin")
        assert _extract_urls(doc) == []

    def test_loopback_skipped(self):
        doc = _doc(content="Debug at http://127.0.0.1:9090/metrics")
        assert _extract_urls(doc) == []

    def test_link_local_skipped(self):
        doc = _doc(content="Check http://169.254.0.1/status")
        assert _extract_urls(doc) == []

    def test_public_url_included(self):
        doc = _doc(content="See https://example.com/guide for help")
        urls = _extract_urls(doc)
        assert "https://example.com/guide" in urls

    def test_mixed_filters_internal_passes_public(self):
        doc = _doc(content="Public: https://example.com internal: http://10.0.0.1/api")
        urls = _extract_urls(doc)
        assert any("example.com" in u for u in urls)
        assert not any("10.0.0.1" in u for u in urls)
