"""Shared fixtures for tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kb_audit.models import Document


@pytest.fixture
def recent_doc() -> Document:
    return Document(
        id="doc-1",
        title="API Reference v3",
        content="This is the current API reference for v3.0.",
        source_type="notion",
        url="https://notion.so/doc-1",
        last_modified=datetime.now(timezone.utc),
    )


@pytest.fixture
def old_doc() -> Document:
    return Document(
        id="doc-2",
        title="API Reference v2",
        content="This is the old API reference for v2.0.",
        source_type="notion",
        url="https://notion.so/doc-2",
        last_modified=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def duplicate_doc(recent_doc: Document) -> Document:
    return Document(
        id="doc-3",
        title="API Reference v3 (copy)",
        content=recent_doc.content,
        source_type="notion",
        url="https://notion.so/doc-3",
        last_modified=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
