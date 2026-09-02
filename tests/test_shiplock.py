# tests/test_shiplock.py
#
# The release gate as a test: run shiplock's deterministic docs-vs-code
# checks (shiplock.toml at the repo root) inside the suite, so the check
# that blocks a release is the one every pytest run and CI job already ran.
#
# Skips when shiplock isn't installed — it's a dev extra, and the package
# under test must not grow a dependency on its own release tooling.

from pathlib import Path

import pytest

shiplock = pytest.importorskip("shiplock")

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_release_gate_is_clean():
    report = shiplock.run_checks(shiplock.load_config(REPO_ROOT))
    details = "\n".join(
        f"{f.check}  {f.path}:{f.line}  {f.message}" for f in report.findings
    )
    assert report.ok, f"shiplock findings:\n{details}"
