# test_decorators.py — @traced_action's capture_inputs resolution.
#
# Regression coverage for a bug where package-level
# configure(config=TraceConfig(capture_inputs=True)) was silently ignored
# unless @traced_action also repeated capture_inputs=True itself. The fix
# folds the decorator's capture_inputs= shorthand into its TraceConfig
# override at decoration time, so there is one resolution path
# (_resolve_config in trace.py) instead of two independent ones. See
# decorators.py's module docstring for the full explanation.

import asyncio

from traceact import ConsoleSink, JsonlSink, TraceConfig, configure, traced_action

from conftest import read_last_trace


def _decorated(capture_inputs=None, config=None):
    @traced_action(
        action="test.action",
        kind="app",
        capture_inputs=capture_inputs,
        config=config,
    )
    def do_thing(username, password):
        return {"ok": True}

    return do_thing


def test_no_package_config_no_decorator_override_captures_nothing(tmp_path):
    # Package default is "no capture" when nothing is configured anywhere.
    path = tmp_path / "traces.jsonl"
    configure(config=TraceConfig(sink_mode="blocking"), sinks=[JsonlSink(str(path))])

    _decorated()("mo", "hunter2")

    assert read_last_trace(path)["inputs"] == {}


def test_package_level_capture_inputs_true_is_honoured_by_bare_decorator(tmp_path):
    # The bug: this used to produce {} because the wrapper only ever looked at
    # the decorator's own local capture_inputs parameter, never the resolved
    # package config. capture_inputs defaults to None on the decorator now,
    # so it correctly defers to the package setting below.
    path = tmp_path / "traces.jsonl"
    configure(
        config=TraceConfig(sink_mode="blocking", capture_inputs=True),
        sinks=[JsonlSink(str(path))],
    )

    _decorated()("mo", "hunter2")

    inputs = read_last_trace(path)["inputs"]
    assert inputs["username"] == "mo"
    assert inputs["password"] == "[redacted]"  # baseline redaction still applies


def test_decorator_level_false_overrides_package_level_true(tmp_path):
    # An explicit per-decorator False should still be able to opt a specific
    # trace out, even when the package default is True.
    path = tmp_path / "traces.jsonl"
    configure(
        config=TraceConfig(sink_mode="blocking", capture_inputs=True),
        sinks=[JsonlSink(str(path))],
    )

    _decorated(capture_inputs=False)("mo", "hunter2")

    assert read_last_trace(path)["inputs"] == {}


def test_package_level_false_is_a_kill_switch_decorator_cannot_override(tmp_path):
    # Documented behaviour in config.py: capture_inputs=False at the package
    # level cannot be re-enabled by a decorator, even one that explicitly asks
    # for capture_inputs=True.
    path = tmp_path / "traces.jsonl"
    configure(
        config=TraceConfig(sink_mode="blocking", capture_inputs=False),
        sinks=[JsonlSink(str(path))],
    )

    _decorated(capture_inputs=True)("mo", "hunter2")

    assert read_last_trace(path)["inputs"] == {}


def test_decorator_level_true_works_without_any_package_config(tmp_path):
    # The one path that already worked before the fix — must keep working.
    path = tmp_path / "traces.jsonl"
    configure(config=TraceConfig(sink_mode="blocking"), sinks=[JsonlSink(str(path))])

    _decorated(capture_inputs=True)("mo", "hunter2")

    inputs = read_last_trace(path)["inputs"]
    assert inputs["username"] == "mo"
    assert inputs["password"] == "[redacted]"


def test_decorator_level_list_captures_only_named_fields(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(config=TraceConfig(sink_mode="blocking"), sinks=[JsonlSink(str(path))])

    _decorated(capture_inputs=["username"])("mo", "hunter2")

    assert read_last_trace(path)["inputs"] == {"username": "mo"}


def test_capture_inputs_shorthand_wins_over_separately_passed_config_object(tmp_path):
    # capture_inputs= is shorthand for config=TraceConfig(capture_inputs=...).
    # If a caller passes both, the shorthand wins for capture_inputs
    # specifically, but other fields on the explicit config still apply.
    path = tmp_path / "traces.jsonl"
    configure(config=TraceConfig(sink_mode="blocking"), sinks=[JsonlSink(str(path))])

    do_thing = _decorated(
        capture_inputs=True,
        config=TraceConfig(capture_inputs=False, redact_by_default=False),
    )
    do_thing("mo", "hunter2")

    inputs = read_last_trace(path)["inputs"]
    assert inputs["username"] == "mo"
    # redact_by_default=False from the explicit config= object was preserved —
    # only capture_inputs itself was overridden by the shorthand.
    assert inputs["password"] == "hunter2"


def test_async_decorator_honours_package_level_capture_inputs_true(tmp_path):
    # The bug and its fix live in both _sync_wrapper and _async_wrapper —
    # confirm the async path independently rather than assuming symmetry.
    path = tmp_path / "traces.jsonl"
    configure(
        config=TraceConfig(sink_mode="blocking", capture_inputs=True),
        sinks=[JsonlSink(str(path))],
    )

    @traced_action(action="test.async_action", kind="app")
    async def do_thing_async(username, password):
        return {"ok": True}

    asyncio.run(do_thing_async("mo", "hunter2"))

    inputs = read_last_trace(path)["inputs"]
    assert inputs["username"] == "mo"
    assert inputs["password"] == "[redacted]"
