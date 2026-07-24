# test_doctor.py — the shared health-check logic behind `traceact doctor`
# (CLI) and the viewer's Settings > "Run diagnostics" button (GET /api/doctor).

from traceact.viewer.doctor import run_checks


def test_no_source_reports_ok_with_info_only_checks():
    result = run_checks()

    assert result["ok"] is True
    labels = [c["label"] for c in result["checks"]]
    assert labels == ["python_version", "traceact_version", "state_dir", "viewer_running"]
    # python_version and state_dir are expected to pass on any machine running
    # this test; the other two are purely informational.
    by_label = {c["label"]: c for c in result["checks"]}
    assert by_label["python_version"]["status"] == "pass"
    assert by_label["traceact_version"]["status"] == "info"
    assert by_label["state_dir"]["status"] == "pass"
    assert by_label["viewer_running"]["status"] == "info"


def test_missing_source_fails_with_a_hint(tmp_path):
    missing = tmp_path / "does-not-exist.jsonl"

    result = run_checks(str(missing))

    assert result["ok"] is False
    source_check = next(c for c in result["checks"] if c["label"] == "source")
    assert source_check["status"] == "fail"
    assert "does not exist" in source_check["message"]
    assert "hint" in source_check  # every fail check carries remediation guidance


def test_empty_folder_source_fails_no_jsonl_files(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    result = run_checks(str(empty_dir))

    source_check = next(c for c in result["checks"] if c["label"] == "source")
    assert source_check["status"] == "fail"
    assert "no .jsonl files" in source_check["message"]


def test_valid_source_passes(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text(
        '{"trace_id": "trc_1", "action": "a", "started_at": "2026-01-01T00:00:00Z"}\n'
    )

    result = run_checks(str(path))

    source_check = next(c for c in result["checks"] if c["label"] == "source")
    assert source_check["status"] == "pass"
    assert "1/1" in source_check["message"]
    assert result["ok"] is True


def test_source_with_no_matching_trace_lines_fails(tmp_path):
    path = tmp_path / "not-traces.jsonl"
    path.write_text('{"unrelated": "data"}\n')

    result = run_checks(str(path))

    source_check = next(c for c in result["checks"] if c["label"] == "source")
    assert source_check["status"] == "fail"
    assert "hint" in source_check


def test_freshly_created_empty_source_is_info_not_fail(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text("")

    result = run_checks(str(path))

    source_check = next(c for c in result["checks"] if c["label"] == "source")
    assert source_check["status"] == "info"
    assert result["ok"] is True


def test_pass_check_never_carries_a_hint():
    result = run_checks()
    for check in result["checks"]:
        if check["status"] in ("pass", "info"):
            assert "hint" not in check
