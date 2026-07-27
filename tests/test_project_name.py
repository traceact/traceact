# tests/test_project_name.py
#
# Tests for configure(project=...), project cascading to traces, and the
# warning emitted when a root trace is written without a project name.

import warnings

import pytest

import traceact
from traceact import ActionTrace, configure, reset_config
from traceact.config import get_package_project
from traceact.trace import _create_trace


# ---------------------------------------------------------------------------
# configure(project=...) — package-level state
# ---------------------------------------------------------------------------

class TestConfigureProject:
    def test_project_stored_in_package_state(self):
        configure(project="agora")
        assert get_package_project() == "agora"

    def test_reset_clears_project(self):
        configure(project="agora")
        reset_config()
        assert get_package_project() is None

    def test_configure_without_project_does_not_clear_existing(self):
        configure(project="agora")
        configure()  # no project arg
        assert get_package_project() == "agora"

    def test_configure_project_can_be_updated(self):
        configure(project="alpha")
        configure(project="beta")
        assert get_package_project() == "beta"


# ---------------------------------------------------------------------------
# Project cascading — package → trace
# ---------------------------------------------------------------------------

class TestProjectCascade:
    def test_package_project_stamped_onto_trace(self, tmp_path):
        configure(project="agora")
        with ActionTrace.start(action="test.run") as trace:
            pass
        assert trace.project == "agora"

    def test_per_trace_project_overrides_package(self, tmp_path):
        configure(project="agora")
        with ActionTrace.start(action="test.run", project="override") as trace:
            pass
        assert trace.project == "override"

    def test_child_inherits_package_project(self):
        configure(project="agora")
        with ActionTrace.start(action="parent") as parent:
            with ActionTrace.start(action="child") as child:
                pass
        assert child.project == "agora"

    def test_child_inherits_parent_project_when_package_unset(self):
        with ActionTrace.start(action="parent", project="manual") as parent:
            with ActionTrace.start(action="child") as child:
                pass
        assert child.project == "manual"

    def test_package_project_wins_over_parent_project(self):
        # Package is the top of the cascade — it overrides even an explicit
        # parent project so that configure(project=...) is a single source of
        # truth for the whole app.
        configure(project="package-level")
        with ActionTrace.start(action="parent", project="parent-level") as parent:
            with ActionTrace.start(action="child") as child:
                pass
        assert child.project == "package-level"

    def test_no_project_anywhere_leaves_none(self):
        with ActionTrace.start(action="test.run") as trace:
            pass
        assert trace.project is None

    def test_project_appears_in_serialised_record(self, tmp_path):
        import json
        f = tmp_path / "traces.jsonl"
        traceact.configure(
            project="agora",
            config=traceact.TraceConfig(sink_mode="blocking"),
            sinks=[traceact.JsonlSink(str(f))],
        )
        with ActionTrace.start(action="test.run"):
            pass
        records = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        assert records[-1]["project"] == "agora"


# ---------------------------------------------------------------------------
# Warning on unnamed root traces
# ---------------------------------------------------------------------------

class TestUnnamedTraceWarning:
    def test_warns_when_no_project_set(self, tmp_path):
        import traceact
        f = tmp_path / "traces.jsonl"
        traceact.configure(sinks=[traceact.JsonlSink(str(f))])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with ActionTrace.start(action="test.run"):
                pass
        msgs = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
        assert any("project" in m for m in msgs)

    def test_no_warning_when_project_set_via_configure(self, tmp_path):
        import traceact
        f = tmp_path / "traces.jsonl"
        traceact.configure(project="agora", sinks=[traceact.JsonlSink(str(f))])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with ActionTrace.start(action="test.run"):
                pass
        msgs = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
        assert not any("project" in m for m in msgs)

    def test_no_warning_when_project_set_per_trace(self, tmp_path):
        import traceact
        f = tmp_path / "traces.jsonl"
        traceact.configure(sinks=[traceact.JsonlSink(str(f))])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with ActionTrace.start(action="test.run", project="agora"):
                pass
        msgs = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
        assert not any("project" in m for m in msgs)

    def test_child_trace_does_not_warn(self, tmp_path):
        # Only root traces warn — spamming for every child would be noise.
        import traceact
        f = tmp_path / "traces.jsonl"
        traceact.configure(sinks=[traceact.JsonlSink(str(f))])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with ActionTrace.start(action="parent"):
                with ActionTrace.start(action="child"):
                    pass
        project_warnings = [
            w for w in caught
            if issubclass(w.category, UserWarning) and "project" in str(w.message)
        ]
        # At most one warning (the root), never two.
        assert len(project_warnings) <= 1
