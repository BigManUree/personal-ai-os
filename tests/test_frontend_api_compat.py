"""Regression tests: Vue SPA bundle must call correct backend API endpoints.

These tests read the built JS bundle referenced by index.html in
app/static/ and verify that the three previously-mismatched API calls
have been corrected.
"""
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "static"


def _read_bundle() -> str:
    """Read the JS bundle referenced by index.html (not stale old bundles)."""
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # Extract the JS file path from the script tag
    m = re.search(r'src="(/static/assets/[^"]+\.js)"', index_html)
    assert m, "index.html 中未找到 JS bundle 引用"
    js_path = STATIC_DIR / m.group(1).removeprefix("/static/")
    assert js_path.exists(), f"bundle 文件不存在: {js_path}"
    return js_path.read_text(encoding="utf-8")


class TestPersonaApi:
    """Problem 1: /api/persona does not exist; must use /api/profile."""

    def test_bundle_does_not_call_persona_endpoint(self):
        bundle = _read_bundle()
        assert "/api/persona" not in bundle, \
            "前端仍在调用不存在的 /api/persona，应改用 /api/profile"

    def test_bundle_calls_profile_endpoint(self):
        bundle = _read_bundle()
        assert "/api/profile" in bundle, \
            "前端未调用 /api/profile，个人画像页面无法获取数据"


class TestMemoryCandidateReview:
    """Problem 2: must call /approve and /reject, not /review."""

    def test_bundle_does_not_call_candidate_review(self):
        bundle = _read_bundle()
        # The wrong path was /api/memory-candidates/{id}/review
        # We must not see "memory-candidates" immediately followed by "/review"
        # (the approvals endpoint /api/approvals/{id}/review is correct and allowed)
        assert not re.search(r'memory-candidates[^`]*\/review', bundle), \
            "前端仍在调用 /api/memory-candidates/{id}/review，应改为 /approve 或 /reject"

    def test_bundle_calls_approve_endpoint(self):
        bundle = _read_bundle()
        assert "approve" in bundle, \
            "前端未调用候选记忆 approve 接口"

    def test_bundle_calls_reject_endpoint(self):
        bundle = _read_bundle()
        assert "reject" in bundle, \
            "前端未调用候选记忆 reject 接口"


class TestApprovalReviewBody:
    """Problem 3: must send {decision: ...} not {status: ...}."""

    def test_bundle_sends_decision_field(self):
        bundle = _read_bundle()
        # The minified bundle should contain "decision" as a JSON body field
        assert re.search(r'decision\s*:', bundle) or '"decision"' in bundle or 'decision,' in bundle, \
            "前端审批请求体未使用 decision 字段"

    def test_approvals_review_endpoint_exists_in_bundle(self):
        bundle = _read_bundle()
        assert "/api/approvals/" in bundle and "/review" in bundle, \
            "前端未调用审批审核接口 /api/approvals/{id}/review"
