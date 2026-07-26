# test_redaction.py — baseline redaction, presets, recursion, and validation.

import pytest

from traceact import (
    ActionTrace,
    ConsoleSink,
    JsonlSink,
    REDACTION_PRESETS,
    TraceConfig,
    configure,
)

from conftest import read_last_trace


def test_baseline_pattern_redacts_by_substring(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(config=TraceConfig(sink_mode="blocking"), sinks=[JsonlSink(str(path))])

    with ActionTrace.start(action="test.action", kind="app") as t:
        t.input({"user_password": "hunter2", "username": "mo"})

    inputs = read_last_trace(path)["inputs"]
    assert inputs["user_password"] == "[redacted]"  # "password" is a substring
    assert inputs["username"] == "mo"


def test_non_sensitive_field_passes_through_unredacted(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(config=TraceConfig(sink_mode="blocking"), sinks=[JsonlSink(str(path))])

    with ActionTrace.start(action="test.action", kind="app") as t:
        t.input({"title": "Hello"})

    assert read_last_trace(path)["inputs"] == {"title": "Hello"}


def test_recursion_redacts_nested_dict_fields(tmp_path):
    # The gap this closed: only top-level keys were checked before. A request
    # body shaped like {"request": {"headers": {"authorization": ...}}} used
    # to leak the nested authorization value untouched.
    path = tmp_path / "traces.jsonl"
    configure(config=TraceConfig(sink_mode="blocking"), sinks=[JsonlSink(str(path))])

    with ActionTrace.start(action="test.action", kind="app") as t:
        t.input({
            "request": {
                "headers": {"authorization": "Bearer abc123"},
                "body": {"user_id": 42},
            },
        })

    inputs = read_last_trace(path)["inputs"]
    assert inputs["request"]["headers"]["authorization"] == "[redacted]"
    assert inputs["request"]["body"]["user_id"] == 42


def test_recursion_redacts_dicts_inside_lists(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(config=TraceConfig(sink_mode="blocking"), sinks=[JsonlSink(str(path))])

    with ActionTrace.start(action="test.action", kind="app") as t:
        t.input({"users": [{"name": "a", "password": "p1"}, {"name": "b", "token": "t2"}]})

    users = read_last_trace(path)["inputs"]["users"]
    assert users[0] == {"name": "a", "password": "[redacted]"}
    assert users[1] == {"name": "b", "token": "[redacted]"}


def test_redact_by_default_false_disables_redaction_entirely(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(
        config=TraceConfig(sink_mode="blocking", redact_by_default=False),
        sinks=[JsonlSink(str(path))],
    )

    with ActionTrace.start(action="test.action", kind="app") as t:
        t.input({"password": "hunter2"})

    assert read_last_trace(path)["inputs"]["password"] == "hunter2"


@pytest.mark.parametrize(
    "preset,field,value",
    [
        ("filesystem_paths", "path", "/Users/mo/secret-project"),
        ("env_vars", "env", {"HOME": "/home/mo"}),
        ("http", "cookie", "session=abc123"),
        ("api_keys", "jwt", "eyJhbGciOi..."),
    ],
)
def test_preset_redacts_its_fields_only_when_active(tmp_path, preset, field, value):
    path = tmp_path / "traces.jsonl"

    # Preset NOT active: field passes through untouched.
    configure(config=TraceConfig(sink_mode="blocking"), sinks=[JsonlSink(str(path))])
    with ActionTrace.start(action="test.inactive", kind="app") as t:
        t.input({field: value})
    assert read_last_trace(path)["inputs"][field] == value

    # Preset active: field is redacted.
    configure(
        config=TraceConfig(sink_mode="blocking", redaction_presets=[preset]),
        sinks=[JsonlSink(str(path))],
    )
    with ActionTrace.start(action="test.active", kind="app") as t:
        t.input({field: value})
    assert read_last_trace(path)["inputs"][field] == "[redacted]"


def test_unknown_preset_name_raises_at_construction():
    with pytest.raises(ValueError, match="Unknown redaction preset"):
        TraceConfig(redaction_presets=["not_a_real_preset"])


def test_redaction_presets_registry_matches_documented_names():
    assert set(REDACTION_PRESETS.keys()) == {
        "api_keys", "http", "filesystem_paths", "env_vars", "ai_prompts",
    }


def test_decorator_level_redaction_presets_replaces_not_merges_package_list(tmp_path):
    # Documented behaviour: a decorator-level redaction_presets list replaces
    # the package-level list rather than adding to it, same as every other
    # TraceConfig field.
    path = tmp_path / "traces.jsonl"
    configure(
        config=TraceConfig(sink_mode="blocking", redaction_presets=["filesystem_paths"]),
        sinks=[JsonlSink(str(path))],
    )

    with ActionTrace.start(
        action="test.action", kind="app",
        config=TraceConfig(redaction_presets=["env_vars"]),
    ) as t:
        t.input({"path": "/Users/mo/x", "env": {"K": "v"}})

    inputs = read_last_trace(path)["inputs"]
    assert inputs["path"] == "/Users/mo/x"       # filesystem_paths no longer active
    assert inputs["env"] == "[redacted]"          # env_vars is now active
