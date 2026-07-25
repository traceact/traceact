# tests/test_tracelog.py
#
# Tests for TraceLog — the programmatic JSONL query interface.
#
# Every test writes its own isolated .jsonl file into a tmp_path fixture so
# tests don't interfere with each other or with live trace files.

import json
import os
import unittest.mock as mock
import pytest

from traceact import TraceLog


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path, records):
    """Write a list of dicts as JSONL to path."""
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _trace(action="note.create", kind="app", status="completed",
           duration_ms=42.0, started_at="2026-07-25T10:00:00Z",
           **extra):
    """Return a minimal valid trace dict."""
    t = {
        "trace_id": f"trc_{action.replace('.', '_')}",
        "action": action,
        "started_at": started_at,
        "kind": kind,
        "status": status,
        "duration_ms": duration_ms,
    }
    t.update(extra)
    return t


# ---------------------------------------------------------------------------
# Basic reads
# ---------------------------------------------------------------------------

class TestAll:
    def test_returns_all_traces(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        records = [_trace("a.create"), _trace("b.update"), _trace("c.delete")]
        _write_jsonl(f, records)
        result = TraceLog(str(f)).all()
        assert len(result) == 3

    def test_empty_file_returns_empty_list(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        assert TraceLog(str(f)).all() == []

    def test_missing_path_returns_empty_list(self, tmp_path):
        assert TraceLog(str(tmp_path / "nonexistent.jsonl")).all() == []

    def test_skips_malformed_lines(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text(
            json.dumps(_trace("good.one")) + "\n"
            "not json at all\n"
            '{"no_trace_id": true}\n'
            + json.dumps(_trace("good.two")) + "\n"
        )
        result = TraceLog(str(f)).all()
        assert len(result) == 2

    def test_folder_source_merges_all_jsonl_files(self, tmp_path):
        _write_jsonl(tmp_path / "a.jsonl", [_trace("a.op")])
        _write_jsonl(tmp_path / "b.jsonl", [_trace("b.op"), _trace("b.op2")])
        result = TraceLog(str(tmp_path)).all()
        assert len(result) == 3

    def test_sorted_oldest_first(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("late",  started_at="2026-07-25T12:00:00Z"),
            _trace("early", started_at="2026-07-25T08:00:00Z"),
            _trace("mid",   started_at="2026-07-25T10:00:00Z"),
        ])
        actions = [t["action"] for t in TraceLog(str(f)).all()]
        assert actions == ["early", "mid", "late"]


# ---------------------------------------------------------------------------
# filter() — exact match
# ---------------------------------------------------------------------------

class TestFilterExact:
    def test_filter_by_status(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("a", status="completed"),
            _trace("b", status="failed"),
            _trace("c", status="completed"),
        ])
        result = TraceLog(str(f)).filter(status="failed").all()
        assert len(result) == 1
        assert result[0]["action"] == "b"

    def test_filter_by_kind(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("a", kind="db"),
            _trace("b", kind="app"),
            _trace("c", kind="db"),
        ])
        result = TraceLog(str(f)).filter(kind="db").all()
        assert len(result) == 2

    def test_filter_multiple_fields_anded(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("a", kind="db", status="failed"),
            _trace("b", kind="db", status="completed"),
            _trace("c", kind="app", status="failed"),
        ])
        result = TraceLog(str(f)).filter(kind="db", status="failed").all()
        assert len(result) == 1
        assert result[0]["action"] == "a"

    def test_filter_no_matches_returns_empty(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("a", status="completed")])
        assert TraceLog(str(f)).filter(status="failed").all() == []

    def test_filter_field_absent_excludes_trace(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        # correlation_id is absent from the trace
        _write_jsonl(f, [_trace("a")])
        assert TraceLog(str(f)).filter(correlation_id="corr_123").all() == []

    def test_filter_bool_field(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("a", budget_hit=True),
            _trace("b", budget_hit=False),
        ])
        result = TraceLog(str(f)).filter(budget_hit=True).all()
        assert len(result) == 1
        assert result[0]["action"] == "a"


# ---------------------------------------------------------------------------
# filter() — lookup operators
# ---------------------------------------------------------------------------

class TestFilterOperators:
    def test_contains(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("note.create"),
            _trace("user.update"),
            _trace("note.delete"),
        ])
        result = TraceLog(str(f)).filter(action__contains="note").all()
        assert len(result) == 2

    def test_contains_case_insensitive(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("NOTE.create")])
        result = TraceLog(str(f)).filter(action__contains="note").all()
        assert len(result) == 1

    def test_startswith(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("note.create"),
            _trace("note.delete"),
            _trace("user.update"),
        ])
        result = TraceLog(str(f)).filter(action__startswith="note").all()
        assert len(result) == 2

    def test_endswith(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("note.create"),
            _trace("user.create"),
            _trace("note.delete"),
        ])
        result = TraceLog(str(f)).filter(action__endswith="create").all()
        assert len(result) == 2

    def test_re_operator(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("note.create"),
            _trace("note.update"),
            _trace("payment.charge"),
        ])
        result = TraceLog(str(f)).filter(action__re=r"note\.(create|update)").all()
        assert len(result) == 2

    def test_unknown_operator_raises(self):
        log = TraceLog("/dev/null")
        with pytest.raises(ValueError, match="Unknown TraceLog filter operator"):
            log.filter(action__bogus="x")


# ---------------------------------------------------------------------------
# filter() immutability
# ---------------------------------------------------------------------------

class TestFilterImmutability:
    def test_original_unaffected_by_derived_filter(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("a", status="completed"),
            _trace("b", status="failed"),
        ])
        log = TraceLog(str(f))
        _ = log.filter(status="failed")  # derive a filtered copy
        # original should still return all traces
        assert log.count() == 2

    def test_chained_filters_are_independent_of_base(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("a", kind="db", status="failed"),
            _trace("b", kind="db", status="completed"),
            _trace("c", kind="app", status="failed"),
        ])
        log = TraceLog(str(f))
        db_only   = log.filter(kind="db")
        db_failed = db_only.filter(status="failed")

        assert db_only.count() == 2    # db_only unaffected by db_failed
        assert db_failed.count() == 1


# ---------------------------------------------------------------------------
# last() and first()
# ---------------------------------------------------------------------------

class TestLastFirst:
    def test_last_returns_n_most_recent(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("a", started_at="2026-07-25T08:00:00Z"),
            _trace("b", started_at="2026-07-25T09:00:00Z"),
            _trace("c", started_at="2026-07-25T10:00:00Z"),
        ])
        result = TraceLog(str(f)).last(2)
        assert [t["action"] for t in result] == ["c", "b"]

    def test_last_fewer_than_n(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("only")])
        assert len(TraceLog(str(f)).last(5)) == 1

    def test_first_returns_n_oldest(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("a", started_at="2026-07-25T08:00:00Z"),
            _trace("b", started_at="2026-07-25T09:00:00Z"),
            _trace("c", started_at="2026-07-25T10:00:00Z"),
        ])
        result = TraceLog(str(f)).first(2)
        assert [t["action"] for t in result] == ["a", "b"]


# ---------------------------------------------------------------------------
# count()
# ---------------------------------------------------------------------------

class TestCount:
    def test_count_all(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("a"), _trace("b"), _trace("c")])
        assert TraceLog(str(f)).count() == 3

    def test_count_filtered(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("a", status="failed"),
            _trace("b", status="completed"),
        ])
        assert TraceLog(str(f)).filter(status="failed").count() == 1


# ---------------------------------------------------------------------------
# render_table()
# ---------------------------------------------------------------------------

class TestRenderTable:
    def test_prints_output(self, tmp_path, capsys):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [_trace("note.create")])
        TraceLog(str(f)).render_table()
        out = capsys.readouterr().out
        assert "note.create" in out
        assert "1 trace shown" in out

    def test_empty_prints_no_traces_message(self, tmp_path, capsys):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        TraceLog(str(f)).render_table()
        out = capsys.readouterr().out
        assert "no traces" in out.lower()

    def test_n_limits_rows(self, tmp_path, capsys):
        f = tmp_path / "traces.jsonl"
        _write_jsonl(f, [
            _trace("a", started_at="2026-07-25T08:00:00Z"),
            _trace("b", started_at="2026-07-25T09:00:00Z"),
            _trace("c", started_at="2026-07-25T10:00:00Z"),
        ])
        TraceLog(str(f)).render_table(n=2)
        out = capsys.readouterr().out
        assert "2 traces shown" in out
        # "a" is the oldest trace (08:00); only the two newest (b, c) should appear
        assert "08:00:00" not in out


# ---------------------------------------------------------------------------
# TraceLog.view() — URL building + browser interaction
# ---------------------------------------------------------------------------
#
# We never actually launch a viewer in tests. We patch launch_or_connect to
# return a fixed base URL, and webbrowser.open to capture what URL was opened.

_FAKE_BASE = "http://127.0.0.1:8765/"

class TestView:
    def _patch_launch(self):
        return mock.patch(
            "traceact.viewer.instance.launch_or_connect",
            return_value=_FAKE_BASE,
        )

    def _patch_browser(self):
        return mock.patch("webbrowser.open")

    # -- URL structure -------------------------------------------------------

    def test_no_filters_returns_base_url(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        log = TraceLog(str(f))
        with self._patch_launch(), mock.patch("webbrowser.open"):
            url = log.view(open_browser=False)
        assert url == "http://127.0.0.1:8765/"

    def test_single_exact_filter(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        log = TraceLog(str(f)).filter(status="failed")
        with self._patch_launch(), mock.patch("webbrowser.open"):
            url = log.view(open_browser=False)
        assert "pf_status=failed" in url

    def test_operator_filter_uses_double_underscore(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        log = TraceLog(str(f)).filter(action__contains="note")
        with self._patch_launch(), mock.patch("webbrowser.open"):
            url = log.view(open_browser=False)
        assert "pf_action__contains=note" in url

    def test_multiple_filters_all_present(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        log = TraceLog(str(f)).filter(status="failed").filter(kind="db")
        with self._patch_launch(), mock.patch("webbrowser.open"):
            url = log.view(open_browser=False)
        assert "pf_status=failed" in url
        assert "pf_kind=db" in url

    def test_startswith_operator(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        log = TraceLog(str(f)).filter(action__startswith="order")
        with self._patch_launch(), mock.patch("webbrowser.open"):
            url = log.view(open_browser=False)
        assert "pf_action__startswith=order" in url

    def test_endswith_operator(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        log = TraceLog(str(f)).filter(action__endswith=".create")
        with self._patch_launch(), mock.patch("webbrowser.open"):
            url = log.view(open_browser=False)
        assert "pf_action__endswith=.create" in url

    def test_re_operator(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        log = TraceLog(str(f)).filter(action__re="^order")
        with self._patch_launch(), mock.patch("webbrowser.open"):
            url = log.view(open_browser=False)
        assert "pf_action__re=" in url

    def test_url_starts_with_base(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        log = TraceLog(str(f)).filter(status="completed")
        with self._patch_launch(), mock.patch("webbrowser.open"):
            url = log.view(open_browser=False)
        assert url.startswith("http://127.0.0.1:8765/")

    def test_query_string_separator(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        log = TraceLog(str(f)).filter(status="failed")
        with self._patch_launch(), mock.patch("webbrowser.open"):
            url = log.view(open_browser=False)
        assert "?" in url

    # -- Source path passed to viewer ----------------------------------------

    def test_source_path_passed_to_launch_or_connect(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        log = TraceLog(str(f))
        with mock.patch(
            "traceact.viewer.instance.launch_or_connect",
            return_value=_FAKE_BASE,
        ) as mock_launch, mock.patch("webbrowser.open"):
            log.view(open_browser=False)
        mock_launch.assert_called_once_with(source=str(f))

    # -- open_browser flag ---------------------------------------------------

    def test_open_browser_false_does_not_open_browser(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        log = TraceLog(str(f))
        with self._patch_launch(), mock.patch("webbrowser.open") as mock_browser:
            log.view(open_browser=False)
        mock_browser.assert_not_called()

    def test_open_browser_true_opens_browser(self, tmp_path):
        import threading
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        log = TraceLog(str(f)).filter(status="failed")
        opened = []
        def fake_timer(delay, fn):
            fn()   # fire immediately in test
            t = mock.MagicMock()
            t.start = lambda: None
            return t
        with self._patch_launch(), \
             mock.patch("webbrowser.open", side_effect=lambda u: opened.append(u)), \
             mock.patch("threading.Timer", side_effect=fake_timer):
            log.view(open_browser=True)
        assert len(opened) == 1
        assert "pf_status=failed" in opened[0]

    # -- Immutability — view() does not mutate the TraceLog ------------------

    def test_view_does_not_mutate_specs(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        f.write_text("")
        log = TraceLog(str(f)).filter(status="failed")
        before = list(log._specs)
        with self._patch_launch(), mock.patch("webbrowser.open"):
            log.view(open_browser=False)
        assert log._specs == before
