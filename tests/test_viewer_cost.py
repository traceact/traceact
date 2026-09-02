# tests/test_viewer_cost.py
#
# Tests for cost estimates on model events: the viewer/cost.py module, the
# GET /api/cost endpoint, the /api/health cost_estimates flag, the doctor
# check, and the CLI's app-level tracing configuration.
#
# rates is a soft dependency, so both worlds are covered: most tests run
# against a fake registry (deterministic prices, no rates needed at all),
# the absent-world tests force rates_available() to False, and one
# integration test runs against the installed rates package when present.

import json
import threading
import unittest.mock as mock
import urllib.error
import urllib.request

import pytest

from traceact.viewer import cost
from traceact.viewer.server import ViewerServer, ViewerState


# ---------------------------------------------------------------------------
# Fakes standing in for the rates registry
# ---------------------------------------------------------------------------

class _FakePrice:
    def __init__(self, units, currency="USD"):
        self.units = units
        self.currency = currency


class _FakeModel:
    def __init__(self, price):
        self.price = price


class _FakeResult:
    def __init__(self, models):
        self.models = models


class _FakeRegistry:
    """filter(provider=, model=) over a {(provider, model): [models]} table."""

    snapshot_date = "2026-09-01"

    def __init__(self, table):
        self.table = table

    def filter(self, provider=None, model=None):
        key = ((provider or "").lower(), (model or "").lower())
        return _FakeResult(self.table.get(key, []))


_PRICED = _FakeRegistry({
    ("anthropic", "claude-sonnet-5"): [
        _FakeModel(_FakePrice({"input_mtok": 2, "output_mtok": 10})),
    ],
    ("acme", "in-only"): [
        _FakeModel(_FakePrice({"input_mtok": 5})),
    ],
    ("acme", "ambiguous"): [
        _FakeModel(_FakePrice({"input_mtok": 1, "output_mtok": 2})),
        _FakeModel(_FakePrice({"input_mtok": 9, "output_mtok": 9})),
    ],
    ("acme", "twin-listed"): [
        _FakeModel(_FakePrice({"input_mtok": 3, "output_mtok": 6})),
        _FakeModel(_FakePrice({"input_mtok": 3, "output_mtok": 6})),
    ],
    ("euro", "eu-model"): [
        _FakeModel(_FakePrice({"input_mtok": 4, "output_mtok": 8},
                              currency="EUR")),
    ],
})


@pytest.fixture
def fake_registry():
    """Point the cost module at _PRICED and clear its caches around the test."""
    cost._price_for.cache_clear()
    with mock.patch.object(cost, "_load_registry",
                           return_value=(_PRICED, None)):
        yield _PRICED
    cost._price_for.cache_clear()


@pytest.fixture
def rates_missing():
    """Force the not-installed world regardless of this environment."""
    cost._price_for.cache_clear()
    with mock.patch.object(cost, "rates_available", return_value=False), \
            mock.patch.object(cost, "_registry", None), \
            mock.patch.object(cost, "_load_error", None), \
            mock.patch.object(cost, "_load_attempted", False):
        yield
    cost._price_for.cache_clear()


# ---------------------------------------------------------------------------
# cost.estimate()
# ---------------------------------------------------------------------------

class TestEstimate:
    def test_prices_input_and_output_tokens(self, fake_registry):
        result = cost.estimate("anthropic", "claude-sonnet-5", 800, 200)
        assert result["ok"] is True
        assert result["cost"] == pytest.approx(
            800 / 1e6 * 2 + 200 / 1e6 * 10
        )
        assert result["currency"] == "USD"
        assert result["snapshot_date"] == "2026-09-01"

    def test_matching_is_case_insensitive(self, fake_registry):
        result = cost.estimate("Anthropic", "Claude-Sonnet-5", 100, 0)
        assert result["ok"] is True

    def test_unknown_pair_is_refused_with_both_names(self, fake_registry):
        result = cost.estimate("nobody", "no-model", 100, 100)
        assert result["ok"] is False
        assert "no-model" in result["error"]
        assert "nobody" in result["error"]

    def test_zero_tokens_cost_zero(self, fake_registry):
        result = cost.estimate("anthropic", "claude-sonnet-5", 0, 0)
        assert result["ok"] is True
        assert result["cost"] == 0

    def test_output_tokens_without_output_price_refused(self, fake_registry):
        result = cost.estimate("acme", "in-only", 100, 50)
        assert result["ok"] is False
        assert "output-token price" in result["error"]

    def test_input_only_call_priced_by_input_alone(self, fake_registry):
        result = cost.estimate("acme", "in-only", 100, 0)
        assert result["ok"] is True
        assert result["cost"] == pytest.approx(100 / 1e6 * 5)

    def test_conflicting_duplicate_entries_refused(self, fake_registry):
        result = cost.estimate("acme", "ambiguous", 100, 100)
        assert result["ok"] is False
        assert "more than one price entry" in result["error"]

    def test_agreeing_duplicate_entries_priced(self, fake_registry):
        result = cost.estimate("acme", "twin-listed", 1000, 0)
        assert result["ok"] is True
        assert result["cost"] == pytest.approx(1000 / 1e6 * 3)

    def test_non_usd_currency_passes_through(self, fake_registry):
        result = cost.estimate("euro", "eu-model", 1000, 1000)
        assert result["ok"] is True
        assert result["currency"] == "EUR"

    def test_rates_missing_names_the_install_command(self, rates_missing):
        result = cost.estimate("anthropic", "claude-sonnet-5", 100, 100)
        assert result["ok"] is False
        assert "pip install rates" in result["error"]


@pytest.mark.skipif(not cost.rates_available(),
                    reason="rates isn't installed in this environment")
class TestEstimateAgainstInstalledRates:
    def test_a_registry_model_prices_end_to_end(self, tmp_path):
        # Pull a provider+model pair out of the installed registry itself,
        # so the test tracks whatever snapshot ships rather than pinning a
        # price that changes between rates releases.
        #
        # rates traces its own load with traceact, and this process has no
        # sinks configured — route that trace to a file for the duration so
        # it doesn't print into the pytest output (the test is the app here).
        from traceact import configure
        from traceact.config import reset_config
        from traceact.sinks import JsonlSink
        configure(sinks=[JsonlSink(str(tmp_path / "traces.jsonl"))])
        try:
            registry, error = cost._load_registry()
            assert registry is not None, error
            chosen = None
            for m in registry.models:
                units = m.price.units if m.price else {}
                if "input_mtok" not in units or "output_mtok" not in units:
                    continue
                # One entry only: a pair the snapshot lists twice (say, under
                # two types) is legitimately refused as ambiguous, which is
                # its own test above — this one wants a single-entry pair.
                if len(registry.filter(provider=m.provider,
                                       model=m.id).models) == 1:
                    chosen = m
                    break
            assert chosen is not None, "no model with token prices in snapshot"
            result = cost.estimate(chosen.provider, chosen.id, 1000, 1000)
            assert result["ok"] is True, result.get("error")
            assert result["cost"] == pytest.approx(
                1000 / 1e6 * chosen.price.units["input_mtok"]
                + 1000 / 1e6 * chosen.price.units["output_mtok"]
            )
            assert result["snapshot_date"]
        finally:
            reset_config()
            cost._price_for.cache_clear()


# ---------------------------------------------------------------------------
# The /api/cost endpoint and the /api/health flag
# ---------------------------------------------------------------------------

def _serve(token=None):
    state = ViewerState()
    server = ViewerServer("127.0.0.1", 0, state, token=token)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown():
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    return f"http://127.0.0.1:{port}", shutdown


@pytest.fixture
def server_url():
    url, shutdown = _serve()
    try:
        yield url
    finally:
        shutdown()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class TestCostEndpoint:
    def test_success_shape(self, server_url, fake_registry):
        with mock.patch.object(cost, "rates_available", return_value=True):
            status, body = _get(
                f"{server_url}/api/cost?provider=anthropic"
                "&model=claude-sonnet-5&tokens_in=800&tokens_out=200"
            )
        assert status == 200
        assert body["cost"] == pytest.approx(800 / 1e6 * 2 + 200 / 1e6 * 10)
        assert body["currency"] == "USD"
        assert body["snapshot_date"] == "2026-09-01"
        assert body["provider"] == "anthropic"
        assert body["model"] == "claude-sonnet-5"
        assert "ok" not in body

    def test_missing_params_is_400(self, server_url):
        status, body = _get(f"{server_url}/api/cost?provider=anthropic")
        assert status == 400
        assert "provider" in body["error"] and "model" in body["error"]

    def test_junk_tokens_is_400(self, server_url):
        status, body = _get(
            f"{server_url}/api/cost?provider=a&model=b&tokens_in=lots"
        )
        assert status == 400
        assert "tokens_in" in body["error"]

    def test_negative_tokens_is_400(self, server_url):
        status, body = _get(
            f"{server_url}/api/cost?provider=a&model=b&tokens_in=-5"
        )
        assert status == 400

    def test_absent_token_params_read_as_zero(self, server_url, fake_registry):
        with mock.patch.object(cost, "rates_available", return_value=True):
            status, body = _get(
                f"{server_url}/api/cost?provider=anthropic"
                "&model=claude-sonnet-5"
            )
        assert status == 200
        assert body["cost"] == 0

    def test_rates_missing_is_503(self, server_url, rates_missing):
        status, body = _get(
            f"{server_url}/api/cost?provider=a&model=b&tokens_in=1"
        )
        assert status == 503
        assert "pip install rates" in body["error"]

    def test_unknown_pair_is_404(self, server_url, fake_registry):
        with mock.patch.object(cost, "rates_available", return_value=True):
            status, body = _get(
                f"{server_url}/api/cost?provider=nobody&model=no-model"
            )
        assert status == 404
        assert "no-model" in body["error"]

    def test_token_gated_like_every_api_route(self, fake_registry):
        url, shutdown = _serve(token="secret-token")
        try:
            status, body = _get(
                f"{url}/api/cost?provider=anthropic&model=claude-sonnet-5"
            )
            assert status == 403
            with mock.patch.object(cost, "rates_available",
                                   return_value=True):
                status, body = _get(
                    f"{url}/api/cost?provider=anthropic"
                    "&model=claude-sonnet-5&token=secret-token"
                )
            assert status == 200
        finally:
            shutdown()

    def test_health_reports_cost_estimates_flag(self, server_url):
        for available in (True, False):
            with mock.patch.object(cost, "rates_available",
                                   return_value=available):
                status, body = _get(f"{server_url}/api/health")
            assert status == 200
            assert body["cost_estimates"] is available


# ---------------------------------------------------------------------------
# Doctor check
# ---------------------------------------------------------------------------

class TestDoctorCheck:
    def test_reports_installed_state(self):
        from traceact.viewer.doctor import run_checks
        with mock.patch.object(cost, "rates_available", return_value=True):
            result = run_checks()
        entry = next(c for c in result["checks"]
                     if c["label"] == "cost_estimates")
        assert entry["status"] == "info"
        assert "rates installed" in entry["message"]

    def test_reports_missing_state_with_install_command(self):
        from traceact.viewer.doctor import run_checks
        with mock.patch.object(cost, "rates_available", return_value=False):
            result = run_checks()
        entry = next(c for c in result["checks"]
                     if c["label"] == "cost_estimates")
        assert entry["status"] == "info"
        assert "pip install rates" in entry["message"]


# ---------------------------------------------------------------------------
# The CLI as the app: package tracing routed to a file
# ---------------------------------------------------------------------------

class TestConfigureAppTracing:
    def test_sets_a_jsonl_sink_when_nothing_is_configured(self, tmp_path):
        from traceact.config import get_package_sinks, reset_config
        from traceact.sinks import JsonlSink
        from traceact.viewer.cli import _configure_app_tracing
        reset_config()
        try:
            with mock.patch(
                "traceact.viewer.cli.os.path.expanduser",
                return_value=str(tmp_path / ".traceact" / "viewer-traces.jsonl"),
            ):
                _configure_app_tracing()
            sinks = get_package_sinks()
            assert len(sinks) == 1
            assert isinstance(sinks[0], JsonlSink)
        finally:
            reset_config()

    def test_existing_configuration_wins(self, tmp_path):
        from traceact import configure
        from traceact.config import get_package_sinks, reset_config
        from traceact.sinks import JsonlSink
        from traceact.viewer.cli import _configure_app_tracing
        reset_config()
        try:
            own_sink = JsonlSink(str(tmp_path / "app.jsonl"))
            configure(sinks=[own_sink])
            _configure_app_tracing()
            assert get_package_sinks() == [own_sink]
        finally:
            reset_config()
