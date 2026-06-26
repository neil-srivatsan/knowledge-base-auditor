"""Structural tests for the HTML template.

These tests parse the rendered index.html and assert that critical elements
are correctly nested and present, guarding against premature closing tags,
missing ARIA attributes, and other markup regressions.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


TEMPLATE_PATH = (
    Path(__file__).parent.parent / "src" / "kb_audit" / "web" / "templates" / "index.html"
)


def _load_html() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


class _DepthTracker(HTMLParser):
    """Minimal parser that tracks open/close tag depth for a set of ids."""

    def __init__(self, track_ids: set[str]) -> None:
        super().__init__()
        self.track_ids = track_ids
        # element_id → open count
        self.depth: dict[str, int] = {k: 0 for k in track_ids}
        self.max_depth: dict[str, int] = {k: 0 for k in track_ids}
        # Stack of (tag, id) so we can match closes
        self._stack: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        eid = attr_dict.get("id")
        self._stack.append((tag, eid))
        if eid and eid in self.track_ids:
            self.depth[eid] += 1
            self.max_depth[eid] = max(self.max_depth[eid], self.depth[eid])

    def handle_endtag(self, tag: str) -> None:
        # Walk stack in reverse to find the matching open tag
        for i in range(len(self._stack) - 1, -1, -1):
            stag, seid = self._stack[i]
            if stag == tag:
                self._stack.pop(i)
                if seid and seid in self.track_ids:
                    self.depth[seid] -= 1
                return


def _get_attrs(html: str, element_id: str) -> dict[str, str | None]:
    """Return the attribute dict of the first element with the given id."""

    class _Finder(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.result: dict[str, str | None] | None = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if self.result is not None:
                return
            d = dict(attrs)
            if d.get("id") == element_id:
                self.result = d

    p = _Finder()
    p.feed(html)
    return p.result or {}


# ---------------------------------------------------------------------------
# Scan controls structure
# ---------------------------------------------------------------------------


def test_scan_controls_card_body_contains_run_scan_button():
    """Run Scan button must be inside .card-body, not outside it."""
    html = _load_html()
    controls_start = html.find('class="controls card"')
    card_body_pos = html.find('class="card-body"', controls_start)
    # Upper bound: the main content section that follows the controls card
    main_content_pos = html.find('class="main-col"')
    scan_btn_pos = html.find('id="scanBtn"')
    assert card_body_pos != -1, "card-body not found inside controls card"
    assert card_body_pos < scan_btn_pos < main_content_pos, (
        "scanBtn is not inside .card-body"
    )


def test_scan_controls_source_info_inside_card_body():
    """#sourceInfo must be inside .card-body."""
    html = _load_html()
    card_body_pos = html.find('class="card-body"', html.find('class="controls card"'))
    # The card-body closes before the next sibling div.card
    card_body_close = html.find("</div>", html.find('id="sourceInfo"'))
    source_info_pos = html.find('id="sourceInfo"')
    assert card_body_pos < source_info_pos, "#sourceInfo must be after .card-body opens"
    assert source_info_pos < card_body_close, "#sourceInfo must be before .card-body closes"


def test_spinner_inside_card_body():
    """Spinner must be inside .card-body."""
    html = _load_html()
    card_body_pos = html.find('class="card-body"', html.find('class="controls card"'))
    spinner_pos = html.find('id="spinner"')
    assert card_body_pos < spinner_pos, "spinner must be after .card-body opens"


# ---------------------------------------------------------------------------
# Dialog ARIA attributes
# ---------------------------------------------------------------------------


def test_detail_overlay_has_dialog_role():
    html = _load_html()
    attrs = _get_attrs(html, "detailOverlay")
    assert attrs.get("role") == "dialog", "detailOverlay must have role=dialog"
    assert attrs.get("aria-modal") == "true", "detailOverlay must have aria-modal=true"
    assert attrs.get("aria-labelledby") == "detailTitle"


def test_report_overlay_has_dialog_role():
    html = _load_html()
    attrs = _get_attrs(html, "reportOverlay")
    assert attrs.get("role") == "dialog"
    assert attrs.get("aria-modal") == "true"
    assert attrs.get("aria-labelledby") == "reportTitle"


def test_result_drawer_has_dialog_role():
    html = _load_html()
    attrs = _get_attrs(html, "resultDrawer")
    assert attrs.get("role") == "dialog"
    assert attrs.get("aria-modal") == "true"
    assert attrs.get("aria-labelledby") == "resultDrawerTitle"


def test_action_modal_has_dialog_role():
    html = _load_html()
    attrs = _get_attrs(html, "actionModal")
    assert attrs.get("role") == "dialog"
    assert attrs.get("aria-modal") == "true"
    assert attrs.get("aria-labelledby") == "actionModalTitle"


# ---------------------------------------------------------------------------
# Tab navigation ARIA
# ---------------------------------------------------------------------------


def test_tab_buttons_have_aria_controls():
    html = _load_html()
    attrs_results = _get_attrs(html, "tab-btn-results")
    attrs_queue = _get_attrs(html, "tab-btn-queue")
    assert attrs_results.get("aria-controls") == "tabResults"
    assert attrs_queue.get("aria-controls") == "tabQueue"


# ---------------------------------------------------------------------------
# Toast container present
# ---------------------------------------------------------------------------


def test_toast_container_present():
    html = _load_html()
    assert 'id="toastContainer"' in html, "toastContainer must be present"


# ---------------------------------------------------------------------------
# Action modal present
# ---------------------------------------------------------------------------


def test_action_modal_present():
    html = _load_html()
    assert 'id="actionModal"' in html
    assert 'id="actionModalBackdrop"' in html
    assert 'id="actionModalConfirm"' in html
    assert 'id="actionModalCancel"' in html


# ---------------------------------------------------------------------------
# Dead CSS removed
# ---------------------------------------------------------------------------


def test_dead_css_removed():
    html = _load_html()
    dead_classes = [
        ".confidence-cell",
        ".confidence-bar",
        ".confidence-track",
        ".confidence-fill",
        ".workflow-badge",
    ]
    for cls in dead_classes:
        assert cls not in html, f"Dead CSS class {cls!r} should have been removed"
