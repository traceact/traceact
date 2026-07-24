# conftest.py — shared pytest fixtures for the TraceAct test suite.
#
# reset_config() mutates module-level package state (traceact.config's
# _package_config / _package_budget / _package_sinks). Without resetting it
# between tests, one test's configure() call would leak into the next test
# that doesn't call configure() itself. The autouse fixture below guarantees
# every test starts and ends with a clean slate, regardless of whether it
# calls configure() or not.

import json

import pytest

from traceact import reset_config


@pytest.fixture(autouse=True)
def _clean_config():
    reset_config()
    yield
    reset_config()


def read_traces(path) -> list:
    """Read every JSON line from a JSONL file into a list of dicts."""
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def read_last_trace(path) -> dict:
    """Read the most recently written trace record from a JSONL file."""
    records = read_traces(path)
    assert records, f"expected at least one trace in {path}, found none"
    return records[-1]
