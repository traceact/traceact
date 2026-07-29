# tests/test_payload_hostility.py
#
# Tests that hostile payloads cannot crash the traced application, that sink
# failures are visible, and that the default sink_mode writes immediately.
#
# The contract under test is TraceAct's core promise: with strict=False (the
# default), nothing the tracing layer does may raise into the app it is
# observing — and nothing it drops may vanish without a signal. Every payload
# here previously either crashed the caller (RecursionError, a hostile
# __str__) or disappeared in silence (sink failure, buffered-default flush
# with no sinks).

import json
import os

import pytest

from traceact import (
    ActionTrace,
    JsonlSink,
    TraceConfig,
    configure,
    reset_config,
)
from traceact.sinks import flush_buffer


@pytest.fixture
def sink_file(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(project="hostility",
              config=TraceConfig(sink_mode="blocking"),
              sinks=[JsonlSink(str(path))])
    yield path
    reset_config()


def _last_record(path):
    return json.loads(path.read_text().splitlines()[-1])


class TestCircularStructures:
    def test_circular_dict_does_not_crash_the_app(self, sink_file):
        circ = {"a": 1}
        circ["self"] = circ
        with ActionTrace.start(action="t") as t:
            t.input(circ)  # previously raised RecursionError into the caller
        rec = _last_record(sink_file)
        assert rec["inputs"]["a"] == 1
        assert rec["inputs"]["self"] == "[circular reference]"

    def test_circular_list_does_not_crash_the_app(self, sink_file):
        circ = [1, 2]
        circ.append(circ)
        with ActionTrace.start(action="t") as t:
            t.input({"payload": circ})
        rec = _last_record(sink_file)
        assert rec["inputs"]["payload"][0] == 1
        assert rec["inputs"]["payload"][2] == "[circular reference]"

    def test_indirect_cycle_through_two_dicts(self, sink_file):
        a, b = {}, {}
        a["b"] = b
        b["a"] = a
        with ActionTrace.start(action="t") as t:
            t.input({"root": a})
        rec = _last_record(sink_file)
        assert rec["inputs"]["root"]["b"]["a"] == "[circular reference]"

    def test_diamond_is_not_misread_as_a_cycle(self, sink_file):
        # The same sub-dict under two keys is legitimate sharing, not a
        # cycle; an id-set without path semantics would flag it.
        shared = {"v": 1}
        with ActionTrace.start(action="t") as t:
            t.input({"left": shared, "right": shared})
        rec = _last_record(sink_file)
        assert rec["inputs"]["left"] == {"v": 1}
        assert rec["inputs"]["right"] == {"v": 1}


class TestDepth:
    def test_five_thousand_deep_dict_does_not_crash(self, sink_file):
        deep = {}
        node = deep
        for _ in range(5000):
            node["n"] = {}
            node = node["n"]
        with ActionTrace.start(action="t") as t:
            t.input({"payload": deep})  # previously RecursionError
        rec = _last_record(sink_file)
        assert "payload" in rec["inputs"]
        # The branch was cut with a placeholder somewhere before the stack
        # limit; the record itself is valid JSON (proven by _last_record).
        assert "[nested too deep]" in json.dumps(rec["inputs"])

    def test_reasonable_nesting_is_untouched(self, sink_file):
        payload = {"a": {"b": {"c": {"d": [1, 2, {"e": "deep enough"}]}}}}
        with ActionTrace.start(action="t") as t:
            t.input(payload)
        assert _last_record(sink_file)["inputs"] == payload


class TestHostileObjects:
    def test_object_whose_str_raises_does_not_crash(self, sink_file):
        class Weird:
            def __str__(self):
                raise RuntimeError("str() is a trap")

        with ActionTrace.start(action="t") as t:
            t.input({"obj": Weird()})  # previously RuntimeError into the caller
        assert _last_record(sink_file)["inputs"]["obj"] == "[Weird]"

    def test_hostile_object_inside_a_list(self, sink_file):
        class Weird:
            def __str__(self):
                raise ValueError("no")

        with ActionTrace.start(action="t") as t:
            t.input({"items": [1, Weird(), 3]})
        rec = _last_record(sink_file)
        # The honest neighbours survive; only the hostile element degrades.
        assert rec["inputs"]["items"][0] == 1
        assert rec["inputs"]["items"][2] == 3

    def test_ten_megabyte_blob_is_truncated_not_stored(self, sink_file):
        with ActionTrace.start(action="t") as t:
            t.input({"blob": "x" * 10_000_000})
        stored = _last_record(sink_file)["inputs"]["blob"]
        assert stored.startswith("[truncated:")
        assert len(stored) < 100


class TestRedactionThroughLists:
    def test_secret_inside_list_of_lists_is_redacted(self, sink_file):
        # Recursion now enters lists inside lists, so a dict two list-levels
        # down no longer smuggles a secret past redaction.
        with ActionTrace.start(action="t") as t:
            t.input({"batches": [[{"password": "hunter2", "user": "mo"}]]})
        inner = _last_record(sink_file)["inputs"]["batches"][0][0]
        assert inner["password"] == "[redacted]"
        assert inner["user"] == "mo"


class _FailingSink:
    def write(self, record):
        raise OSError("disk on fire")


class TestSinkFailureObservability:
    def test_failing_sink_does_not_crash_and_reports(self, tmp_path, capsys):
        good = tmp_path / "traces.jsonl"
        configure(project="hostility",
                  config=TraceConfig(sink_mode="blocking"),
                  sinks=[_FailingSink(), JsonlSink(str(good))])
        try:
            with ActionTrace.start(action="t"):
                pass
            err = capsys.readouterr().err
            assert "_FailingSink" in err and "disk on fire" in err
            # The healthy sink still got the record.
            assert "\"action\": \"t\"" in good.read_text()
        finally:
            reset_config()

    def test_strict_mode_still_raises(self):
        configure(project="hostility",
                  config=TraceConfig(sink_mode="blocking", strict=True),
                  sinks=[_FailingSink()])
        try:
            with pytest.raises(OSError):
                with ActionTrace.start(action="t"):
                    pass
        finally:
            reset_config()


class TestBlockingIsTheDefault:
    def test_unconfigured_sink_mode_writes_immediately(self, tmp_path):
        # The failure this guards against: a long-running app that set sinks
        # but never sink_mode wrote nothing until process exit.
        path = tmp_path / "traces.jsonl"
        configure(project="hostility", sinks=[JsonlSink(str(path))])
        try:
            with ActionTrace.start(action="t"):
                pass
            assert path.exists(), "default sink_mode must write during the run"
        finally:
            reset_config()

    def test_no_configuration_at_all_prints_immediately(self, capsys):
        # pip install, decorate, run — with no configure() call the trace
        # must be visible somewhere, not buffered into an exit flush that a
        # crash (or a test harness) never runs.
        with ActionTrace.start(action="visible.anyway", project="hostility"):
            pass
        assert "visible.anyway" in capsys.readouterr().out


class TestBufferedFlushFallback:
    def test_flush_with_no_sinks_falls_back_to_console(self, capsys):
        configure(project="hostility",
                  config=TraceConfig(sink_mode="buffered"))
        try:
            with ActionTrace.start(action="buffered.trace"):
                pass
            # Nothing written yet — buffered.
            assert "buffered.trace" not in capsys.readouterr().out
            flush_buffer([])
            assert "buffered.trace" in capsys.readouterr().out
        finally:
            reset_config()
