# tests/test_viewer_base_path.py
#
# Tests for serving the viewer under a path prefix, so it can sit behind an
# existing app's reverse proxy on one port instead of exposing a second.
#
# Like test_viewer_query.py, these start a real ViewerServer on an OS-assigned
# port and make real HTTP requests: the whole feature is about request paths
# and the bytes of the served HTML, neither of which a direct handler-method
# call would exercise honestly.

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from traceact.viewer.server import (
    _STATIC_DIR,
    ViewerServer,
    ViewerState,
    _normalise_base_path,
)


def _serve(base_path=""):
    """Start a server on a free port; returns (base_url, state, shutdown)."""
    state = ViewerState()
    server = ViewerServer("127.0.0.1", 0, state, base_path=base_path)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown():
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    return f"http://127.0.0.1:{port}", state, shutdown


@pytest.fixture
def mounted():
    """A viewer mounted at /audit-viewer."""
    url, state, shutdown = _serve("/audit-viewer")
    try:
        yield url, state
    finally:
        shutdown()


@pytest.fixture
def rooted():
    """A viewer at the default root, to prove the default is untouched."""
    url, state, shutdown = _serve()
    try:
        yield url, state
    finally:
        shutdown()


def _get(url, allow_redirects=True):
    """GET a URL, returning (status, headers, body_bytes)."""
    opener = (urllib.request.build_opener()
              if allow_redirects
              else urllib.request.build_opener(_NoRedirect))
    try:
        with opener.open(url, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surfaces a 301 as a result instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TestNormaliseBasePath:
    """The spellings people reach for must all reduce to one canonical form."""

    @pytest.mark.parametrize("raw", ["", None, "/", "///", "   ", "  /  "])
    def test_empty_spellings_mean_root(self, raw):
        assert _normalise_base_path(raw) == ""

    @pytest.mark.parametrize("raw", [
        "audit-viewer",
        "/audit-viewer",
        "/audit-viewer/",
        "audit-viewer/",
        "  /audit-viewer/  ",
    ])
    def test_equivalent_spellings_collapse(self, raw):
        assert _normalise_base_path(raw) == "/audit-viewer"

    def test_nested_prefix_keeps_inner_slashes(self):
        assert _normalise_base_path("/a/b/") == "/a/b"

    def test_result_is_idempotent(self):
        once = _normalise_base_path("audit-viewer/")
        assert _normalise_base_path(once) == once


class TestRootDefaultUnchanged:
    """The default must behave exactly as it did before base paths existed."""

    def test_index_served_at_root(self, rooted):
        url, _ = rooted
        status, _headers, body = _get(f"{url}/")
        assert status == 200
        assert b"TraceAct" in body

    def test_index_is_byte_identical_to_the_shipped_file(self, rooted):
        # An unmounted viewer does no rewriting at all, so nothing can be
        # broken for the overwhelmingly common case by the injection logic.
        url, _ = rooted
        _status, _headers, body = _get(f"{url}/")
        with open(os.path.join(_STATIC_DIR, "index.html"), "rb") as f:
            assert body == f.read()

    def test_no_base_global_declared(self, rooted):
        url, _ = rooted
        _status, _headers, body = _get(f"{url}/")
        assert b"__TRACEACT_BASE__" not in body

    def test_api_served_at_root(self, rooted):
        url, _ = rooted
        status, _headers, body = _get(f"{url}/api/health")
        assert status == 200
        assert json.loads(body)["status"] == "ok"


class TestMountedRouting:
    def test_index_served_under_prefix(self, mounted):
        url, _ = mounted
        status, _headers, body = _get(f"{url}/audit-viewer/")
        assert status == 200
        assert b"TraceAct" in body

    def test_api_served_under_prefix(self, mounted):
        url, _ = mounted
        status, _headers, body = _get(f"{url}/audit-viewer/api/health")
        assert status == 200
        assert json.loads(body)["status"] == "ok"

    def test_static_served_under_prefix(self, mounted):
        url, _ = mounted
        status, headers, body = _get(f"{url}/audit-viewer/static/app.js")
        assert status == 200
        assert headers["Content-Type"].startswith("text/javascript")
        assert len(body) > 0

    def test_root_is_not_served_when_mounted(self, mounted):
        # The whole point is to leave the host app's routes alone: a mounted
        # viewer answering "/" would shadow whatever the host serves there.
        url, _ = mounted
        status, _headers, _body = _get(f"{url}/")
        assert status == 404

    def test_unprefixed_api_is_not_served_when_mounted(self, mounted):
        url, _ = mounted
        status, _headers, _body = _get(f"{url}/api/health")
        assert status == 404

    def test_bare_prefix_redirects_to_trailing_slash(self, mounted):
        url, _ = mounted
        status, headers, _body = _get(f"{url}/audit-viewer",
                                      allow_redirects=False)
        assert status == 301
        assert headers["Location"] == "/audit-viewer/"

    def test_prefix_lookalike_does_not_match(self, mounted):
        # "/audit-viewer-other" shares a string prefix with "/audit-viewer"
        # but is a different mount; a naive startswith() check would serve it.
        url, _ = mounted
        status, _headers, _body = _get(f"{url}/audit-viewer-other/api/health")
        assert status == 404

    def test_prefix_must_be_at_the_start(self, mounted):
        url, _ = mounted
        status, _headers, _body = _get(f"{url}/x/audit-viewer/api/health")
        assert status == 404

    def test_unknown_route_under_prefix_is_404(self, mounted):
        url, _ = mounted
        status, _headers, _body = _get(f"{url}/audit-viewer/api/nonexistent")
        assert status == 404

    def test_static_path_traversal_still_blocked(self, mounted):
        url, _ = mounted
        status, _headers, _body = _get(
            f"{url}/audit-viewer/static/../../../../etc/passwd"
        )
        # Either the traversal is stripped to a basename that doesn't exist,
        # or the client normalises the path off the mount. Both must fail.
        assert status == 404


class TestMountedIndexRewriting:
    def test_asset_urls_are_prefixed(self, mounted):
        url, _ = mounted
        _status, _headers, body = _get(f"{url}/audit-viewer/")
        text = body.decode("utf-8")
        assert 'href="/audit-viewer/static/styles.css"' in text
        assert 'src="/audit-viewer/static/app.js"' in text

    def test_no_unprefixed_asset_urls_remain(self, mounted):
        url, _ = mounted
        _status, _headers, body = _get(f"{url}/audit-viewer/")
        text = body.decode("utf-8")
        assert 'href="/static/' not in text
        assert 'src="/static/' not in text

    def test_base_global_is_declared(self, mounted):
        url, _ = mounted
        _status, _headers, body = _get(f"{url}/audit-viewer/")
        text = body.decode("utf-8")
        assert "__TRACEACT_BASE__" in text
        assert '"/audit-viewer"' in text

    def test_base_global_declared_before_scripts_run(self, mounted):
        # app.js reads the global at load time, so the declaration has to come
        # first in document order or every API call falls back to the root.
        url, _ = mounted
        _status, _headers, body = _get(f"{url}/audit-viewer/")
        text = body.decode("utf-8")
        assert text.index("__TRACEACT_BASE__") < text.index("static/app.js")

    def test_rewritten_html_still_parses_as_the_same_document(self, mounted):
        url, _ = mounted
        _status, _headers, body = _get(f"{url}/audit-viewer/")
        text = body.decode("utf-8")
        # The injection must not have broken the document's structure.
        assert text.count("</head>") == 1
        assert text.count("</body>") == 1
        assert "<html" in text


class TestMountedApiIsFullyUsable:
    """A mount is only useful if every endpoint works through it, not just GET."""

    def test_post_sources_under_prefix(self, mounted, tmp_path):
        url, state = mounted
        src = tmp_path / "traces.jsonl"
        src.write_text('{"trace_id":"t1","action":"a","started_at":"2026-01-01T00:00:00Z"}\n')

        req = urllib.request.Request(
            f"{url}/audit-viewer/api/sources",
            data=json.dumps({"path": str(src)}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            added = json.loads(resp.read())
        assert added["name"] in state.sources

    def test_post_to_unprefixed_path_is_404(self, mounted, tmp_path):
        url, _state = mounted
        req = urllib.request.Request(
            f"{url}/api/sources",
            data=json.dumps({"path": str(tmp_path)}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 404

    def test_query_under_prefix(self, mounted, tmp_path):
        url, state = mounted
        src = tmp_path / "traces.jsonl"
        src.write_text(
            '{"trace_id":"t1","action":"note.create","started_at":"2026-01-01T00:00:00Z"}\n'
        )
        name = state.add_source(str(src))
        status, _headers, body = _get(
            f"{url}/audit-viewer/api/query?source={name}&action=note.create"
        )
        assert status == 200
        assert len(json.loads(body)["traces"]) == 1


class TestNestedPrefix:
    def test_two_segment_prefix_routes(self):
        url, _state, shutdown = _serve("/tools/audit")
        try:
            status, _headers, body = _get(f"{url}/tools/audit/api/health")
            assert status == 200
            assert json.loads(body)["status"] == "ok"

            # The intermediate segment alone is not a mount.
            status, _headers, _body = _get(f"{url}/tools/api/health")
            assert status == 404
        finally:
            shutdown()

    def test_two_segment_prefix_rewrites_assets(self):
        url, _state, shutdown = _serve("/tools/audit")
        try:
            _status, _headers, body = _get(f"{url}/tools/audit/")
            assert b'src="/tools/audit/static/app.js"' in body
        finally:
            shutdown()


class TestServerNormalisesOnConstruction:
    def test_untidy_prefix_is_usable(self):
        # Someone passing "audit-viewer/" should not have to know the canonical
        # form; the server stores one spelling and serves it.
        url, _state, shutdown = _serve("audit-viewer/")
        try:
            status, _headers, _body = _get(f"{url}/audit-viewer/api/health")
            assert status == 200
        finally:
            shutdown()

    def test_slash_only_prefix_serves_at_root(self):
        url, _state, shutdown = _serve("/")
        try:
            status, _headers, _body = _get(f"{url}/api/health")
            assert status == 200
        finally:
            shutdown()
