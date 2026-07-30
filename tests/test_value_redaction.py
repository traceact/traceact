# tests/test_value_redaction.py
#
# Tests for the three capture-hardening pieces:
#   1. Value-pattern scanning — captured string CONTENT is scanned for known
#      credential formats, closing the hole field-name redaction leaves (a
#      key in a field named "location", or pasted into free text).
#   2. Capture transforms — "field:hash" / "field:last4" / "field:length"
#      store a reduced value instead of the raw one.
#   3. traceact doctor --scan — the same registry run over trace files
#      already on disk.

import json

import pytest

from traceact import (
    ActionTrace,
    JsonlSink,
    TraceConfig,
    configure,
    reset_config,
    traced_action,
)
from traceact.redaction import VALUE_PATTERNS, find_value_patterns, scan_value
from traceact.viewer import doctor

# Credential-shaped strings for tests. Assembled from parts so that this
# test file itself passes a doctor --scan of the repository.
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_SK = "sk-" + "proj4bcd5fgh6jkl7nopq8st"
FAKE_GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0a1B2c3D4e5F6g7H8"
FAKE_SLACK = "xoxb-" + "123456789012-abcdefghijkl"
FAKE_JWT = ("eyJ" + "hbGciOiJIUzI1NiJ9" + "."
            + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "."
            + "TJVA95OrM7E2cBab30RMHrHDcEfxjoYZgeFONFh7HgQ")
FAKE_PEM = "-----BEGIN RSA " + "PRIVATE KEY-----"
FAKE_GOOGLE = "AIza" + "SyD8kZq3vN1xW5tY7uP9rE2wQ4mJ6hL0oFa"
FAKE_BEARER = "Bearer " + "abc123def456ghi789jkl012"
FAKE_URL_CRED = "postgres://admin:hunter2pass@db.internal:5432/app"


@pytest.fixture
def sink_file(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(project="valtest",
              config=TraceConfig(sink_mode="blocking"),
              sinks=[JsonlSink(str(path))])
    yield path
    reset_config()


def _last_inputs(path):
    return json.loads(path.read_text().splitlines()[-1])["inputs"]


class TestPatternRegistry:
    @pytest.mark.parametrize("secret,expected", [
        (FAKE_AWS, "aws-key"),
        (FAKE_SK, "sk-token"),
        (FAKE_GH, "github-token"),
        (FAKE_SLACK, "slack-token"),
        (FAKE_JWT, "jwt"),
        (FAKE_PEM, "pem-key"),
        (FAKE_GOOGLE, "google-api-key"),
        (FAKE_BEARER, "bearer-token"),
        (FAKE_URL_CRED, "url-credentials"),
    ])
    def test_every_registered_format_is_caught(self, secret, expected):
        assert expected in find_value_patterns(secret)
        assert f"[redacted:{expected}]" in scan_value(secret)

    def test_registry_and_tests_cover_each_other(self):
        # A pattern added to the registry without a test here would ship
        # unverified; this pins the two lists together.
        covered = {"aws-key", "sk-token", "github-token", "slack-token",
                   "jwt", "pem-key", "google-api-key", "bearer-token",
                   "url-credentials"}
        assert {name for name, _ in VALUE_PATTERNS} == covered

    def test_prose_around_a_secret_survives(self):
        text = f"the call failed using key {FAKE_AWS} at 10:32"
        out = scan_value(text)
        assert out == "the call failed using key [redacted:aws-key] at 10:32"

    @pytest.mark.parametrize("clean", [
        "an ordinary sentence with nothing in it",
        "AKIA-not-a-key",                    # wrong shape after prefix
        "sk-short",                          # below minimum length
        "postgres://db.internal:5432/app",   # URL without credentials
        "d41d8cd98f00b204e9800998ecf8427e",  # a bare hash is not a match
        "risk-assessment-2026",              # contains "sk-" mid-word only
    ])
    def test_ordinary_values_pass_untouched(self, clean):
        assert scan_value(clean) == clean
        assert find_value_patterns(clean) == []


class TestScanningInTraces:
    def test_secret_in_innocently_named_field_is_redacted(self, sink_file):
        # The exact failure field-name matching cannot catch.
        with ActionTrace.start(action="t") as t:
            t.input({"location": FAKE_AWS})
        assert _last_inputs(sink_file)["location"] == "[redacted:aws-key]"
        assert FAKE_AWS not in sink_file.read_text()

    def test_secret_inside_free_text_is_redacted(self, sink_file):
        with ActionTrace.start(action="t") as t:
            t.input({"note": f"use {FAKE_SK} for the staging env"})
        assert FAKE_SK not in sink_file.read_text()
        assert "[redacted:sk-token]" in _last_inputs(sink_file)["note"]

    def test_secret_in_a_list_of_strings_is_redacted(self, sink_file):
        # List items that aren't dicts skip the recursive sanitiser, so the
        # scan has its own hook in the list branch — this pins it.
        with ActionTrace.start(action="t") as t:
            t.input({"args": ["--api-endpoint", FAKE_GOOGLE, "--verbose"]})
        assert FAKE_GOOGLE not in sink_file.read_text()

    def test_secret_nested_in_a_dict_is_redacted(self, sink_file):
        with ActionTrace.start(action="t") as t:
            t.input({"request": {"headers": {"x-custom": FAKE_BEARER}}})
        assert "abc123def456" not in sink_file.read_text()

    def test_event_results_are_scanned(self, sink_file):
        with ActionTrace.start(action="t") as t:
            t.event(kind="http", operation="get", target="cfg",
                    result={"connection_string": FAKE_URL_CRED})
        assert "hunter2pass" not in sink_file.read_text()

    def test_outputs_are_scanned(self, sink_file):
        with ActionTrace.start(action="t") as t:
            t.output({"debug": FAKE_JWT})
        assert FAKE_JWT not in sink_file.read_text()

    def test_redact_values_false_disables_scanning(self, tmp_path):
        path = tmp_path / "traces.jsonl"
        configure(project="valtest",
                  config=TraceConfig(sink_mode="blocking",
                                     redact_values=False),
                  sinks=[JsonlSink(str(path))])
        try:
            with ActionTrace.start(action="t") as t:
                t.input({"location": FAKE_AWS})
            assert FAKE_AWS in path.read_text()
        finally:
            reset_config()

    def test_scanning_is_on_without_any_configure(self, sink_file):
        # Default-on means default-on: no flag was set in this fixture.
        with ActionTrace.start(action="t") as t:
            t.input({"anything": FAKE_SLACK})
        assert FAKE_SLACK not in sink_file.read_text()

    def test_field_name_redaction_still_wins_where_it_applies(self, sink_file):
        # Value scanning is a second layer, not a replacement: a field the
        # name matcher catches is redacted whole, pattern match or not.
        with ActionTrace.start(action="t") as t:
            t.input({"api_key": "not-a-known-format-but-still-secret"})
        assert _last_inputs(sink_file)["api_key"] == "[redacted]"


class TestCaptureTransforms:
    def test_hash_is_deterministic_and_hides_the_value(self, sink_file):
        @traced_action(action="t", capture_inputs=["user_id:hash"])
        def f(user_id):
            return user_id

        f("user_12345")
        f("user_12345")
        records = [json.loads(l) for l in sink_file.read_text().splitlines()]
        first, second = records[0]["inputs"], records[1]["inputs"]
        assert first["user_id"] == second["user_id"]  # correlatable
        assert first["user_id"].startswith("sha256:")
        assert "user_12345" not in sink_file.read_text()

    def test_different_values_hash_differently(self, sink_file):
        @traced_action(action="t", capture_inputs=["user_id:hash"])
        def f(user_id):
            return user_id

        f("alice")
        f("bob")
        records = [json.loads(l) for l in sink_file.read_text().splitlines()]
        assert records[0]["inputs"]["user_id"] != records[1]["inputs"]["user_id"]

    def test_last4_keeps_only_the_tail(self, sink_file):
        @traced_action(action="t", capture_inputs=["card_number:last4"])
        def f(card_number):
            return True

        f("4242424242424242")
        stored = _last_inputs(sink_file)["card_number"]
        assert stored == "…4242"
        assert "4242424242424242" not in sink_file.read_text()

    def test_transform_beats_name_redaction(self, sink_file):
        # "card_number" matches the sensitive-name patterns; without the
        # explicit transform it would store "[redacted]". The transform is
        # the caller's handling instruction for the field, so it wins.
        @traced_action(action="t", capture_inputs=["card_number:last4"])
        def f(card_number):
            return True

        f("5500005555555559")
        assert _last_inputs(sink_file)["card_number"] == "…5559"

    def test_length_stores_size_only(self, sink_file):
        @traced_action(action="t", capture_inputs=["essay:length"])
        def f(essay):
            return True

        f("a" * 1234)
        assert _last_inputs(sink_file)["essay"] == "[1234 chars]"

    def test_plain_and_transformed_fields_mix(self, sink_file):
        @traced_action(action="t",
                       capture_inputs=["title", "card_number:last4"])
        def f(title, card_number):
            return True

        f("Order 7", "4000056655665556")
        inputs = _last_inputs(sink_file)
        assert inputs["title"] == "Order 7"
        assert inputs["card_number"] == "…5556"

    def test_unknown_transform_fails_at_construction(self):
        with pytest.raises(ValueError, match="Unknown capture transform"):
            TraceConfig(capture_inputs=["card_number:rot13"])

    def test_unknown_transform_fails_at_decoration(self):
        with pytest.raises(ValueError, match="Unknown capture transform"):
            @traced_action(action="t", capture_inputs=["x:frobnicate"])
            def f(x):
                return x

    def test_non_string_entry_fails_at_construction(self):
        with pytest.raises(ValueError, match="must be strings"):
            TraceConfig(capture_inputs=["ok", 42])


class TestDoctorScan:
    def _write(self, path, lines):
        path.write_text("\n".join(lines) + "\n")

    def test_planted_secret_is_found(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        self._write(f, [
            json.dumps({"action": "a", "inputs": {"note": "clean"}}),
            json.dumps({"action": "b", "inputs": {"note": FAKE_AWS}}),
        ])
        result = doctor.scan_source(str(f))
        assert result["ok"] is False
        assert result["hits"][0]["pattern"] == "aws-key"
        assert result["hits"][0]["line"] == 2

    def test_clean_source_passes(self, tmp_path):
        f = tmp_path / "traces.jsonl"
        self._write(f, [json.dumps({"action": "a", "inputs": {"n": 1}})])
        result = doctor.scan_source(str(f))
        assert result["ok"] is True
        assert result["hits"] == []
        assert result["lines"] == 1

    def test_folder_source_scans_every_file(self, tmp_path):
        (tmp_path / "a.jsonl").write_text(
            json.dumps({"inputs": {"x": "clean"}}) + "\n")
        (tmp_path / "b.jsonl").write_text(
            json.dumps({"inputs": {"x": FAKE_GH}}) + "\n")
        result = doctor.scan_source(str(tmp_path))
        assert result["ok"] is False
        assert result["files"] == 2
        assert result["hits"][0]["file"].endswith("b.jsonl")

    def test_missing_source_reports_clean_zero_files(self, tmp_path):
        result = doctor.scan_source(str(tmp_path / "nope.jsonl"))
        assert result["ok"] is True
        assert result["files"] == 0

    def test_cli_exit_codes(self, tmp_path, capsys):
        from traceact.viewer import cli

        dirty = tmp_path / "dirty.jsonl"
        self._write(dirty, [json.dumps({"inputs": {"k": FAKE_SK}})])
        clean = tmp_path / "clean.jsonl"
        self._write(clean, [json.dumps({"inputs": {"k": "fine"}})])

        parser = cli._build_parser()
        assert cli._run_doctor(
            parser.parse_args(["doctor", str(dirty), "--scan"])) == 1
        assert "sk-token" in capsys.readouterr().out
        assert cli._run_doctor(
            parser.parse_args(["doctor", str(clean), "--scan"])) == 0

    def test_scan_without_source_is_an_error(self, capsys):
        from traceact.viewer import cli

        parser = cli._build_parser()
        assert cli._run_doctor(parser.parse_args(["doctor", "--scan"])) == 1
        assert "SOURCE" in capsys.readouterr().err
