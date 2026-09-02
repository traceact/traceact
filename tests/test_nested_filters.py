# tests/test_nested_filters.py
#
# Dotted-path filtering in TraceLog and over GET /api/query.
#
# The syntax split: dots address a nested path ("errors.code"), the "__"
# suffix stays the operator ("errors.code__contains"). Python kwargs can't
# contain dots, so nested filters arrive via dict unpacking:
# log.filter(**{"errors.code": "rate_limit"}). A path segment landing on a
# list fans out over its elements; a path resolving to nothing matches
# nothing. Top-level (dotless) filtering keeps its original semantics
# unchanged, absent-matches-eq-None included.

import json
import threading
import urllib.request

import pytest

from traceact import TraceLog

_RECORDS = [
    {
        "trace_id": "trc_a", "action": "customer.fetch", "status": "failed",
        "started_at": "2026-09-02T10:00:00.000Z",
        "errors": [{"event_id": "trc_a", "type": "CustomerNotFound",
                    "message": "no customer", "code": "not_found"}],
        "events": [{"kind": "db", "operation": "select", "target": "customers"}],
    },
    {
        "trace_id": "trc_b", "action": "model.generate", "status": "failed",
        "started_at": "2026-09-02T10:00:01.000Z",
        "errors": [{"event_id": "trc_b", "type": "RateLimitError",
                    "message": "429", "code": "rate_limit"}],
        "events": [
            {"kind": "model", "operation": "completion",
             "target": "claude-sonnet-5", "provider": "anthropic",
             "tokens_in": 800},
            {"kind": "model", "operation": "completion",
             "target": "claude-sonnet-5", "provider": "anthropic",
             "tokens_in": 900},
        ],
    },
    {
        "trace_id": "trc_c", "action": "order.checkout", "status": "completed",
        "started_at": "2026-09-02T10:00:02.000Z",
        "errors": [],
        "events": [{"kind": "http", "operation": "POST", "target": "payments"}],
        "meta": {"region": "eu-west-1", "release": None},
    },
]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in _RECORDS),
                    encoding="utf-8")
    return str(path)


class TestDottedPaths:
    def test_list_of_dicts_matches_any_element(self, source):
        got = TraceLog(source).filter(**{"errors.code": "rate_limit"}).all()
        assert [t["trace_id"] for t in got] == ["trc_b"]

    def test_dotted_path_composes_with_operator(self, source):
        got = TraceLog(source).filter(
            **{"events.provider__contains": "ANTHRO"}).all()
        assert [t["trace_id"] for t in got] == ["trc_b"]

    def test_events_fan_out_matches_any_event(self, source):
        got = TraceLog(source).filter(**{"events.tokens_in": 900}).all()
        assert [t["trace_id"] for t in got] == ["trc_b"]

    def test_nested_dict_descent(self, source):
        got = TraceLog(source).filter(**{"meta.region": "eu-west-1"}).all()
        assert [t["trace_id"] for t in got] == ["trc_c"]

    def test_missing_path_matches_nothing(self, source):
        assert TraceLog(source).filter(**{"errors.retryable": True}).all() == []

    def test_missing_path_does_not_match_none(self, source):
        # With nesting, "path absent" is not "value is null": no record
        # matches a path none of them carry, even filtering for None.
        assert TraceLog(source).filter(**{"errors.retryable": None}).all() == []

    def test_present_null_value_matches_none(self, source):
        got = TraceLog(source).filter(**{"meta.release": None}).all()
        assert [t["trace_id"] for t in got] == ["trc_c"]

    def test_combines_with_top_level_filters(self, source):
        got = TraceLog(source).filter(status="failed").filter(
            **{"errors.code": "not_found"}).all()
        assert [t["trace_id"] for t in got] == ["trc_a"]

    def test_typo_operator_still_raises(self, source):
        with pytest.raises(ValueError) as excinfo:
            TraceLog(source).filter(**{"errors.code__regex": "x"})
        assert "contains" in str(excinfo.value)

    def test_top_level_list_equality_unchanged(self, source):
        # A dotless filter on a list field still compares the whole list —
        # the fan-out is a dotted-path behaviour only.
        got = TraceLog(source).filter(errors=[]).all()
        assert [t["trace_id"] for t in got] == ["trc_c"]


class TestOverHttp:
    def test_api_query_accepts_dotted_params(self, source):
        from traceact.viewer.server import ViewerServer, ViewerState

        state = ViewerState()
        name = state.add_source(source)
        server = ViewerServer("127.0.0.1", 0, state)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = (f"http://127.0.0.1:{port}/api/query?source={name}"
                   "&errors.code=rate_limit")
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            assert [t["trace_id"] for t in body["traces"]] == ["trc_b"]
        finally:
            server.shutdown()
            server.server_close()
