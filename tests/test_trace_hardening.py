# tests/test_trace_hardening.py
#
# Tests for three trace-core behaviours:
#
#   1. Failure promotion under sampling: with always_trace_errors=True, a
#      failure inside a sampled-out trace produces a record (sampled_out=true,
#      no steps/events) instead of vanishing. Failure paths are tested at
#      sample_rate=0.0 — every trace suppressed — so a single leak through
#      would fail deterministically, not probabilistically.
#   2. SKIP propagation parity between @traced_action and ActionTrace.start().
#   3. trace.event() keyword arguments cannot overwrite core event fields.

import asyncio
import pytest

from traceact import (
    ActionTrace,
    TraceBudget,
    TraceConfig,
    configure,
    reset_config,
    traced_action,
)


class Mem:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)


@pytest.fixture
def sink():
    s = Mem()
    configure(config=TraceConfig(sink_mode="blocking"), sinks=[s])
    yield s
    reset_config()


def _sampled_out_budget(**overrides):
    kwargs = {"sample_rate": 0.0, "always_trace_errors": True}
    kwargs.update(overrides)
    return TraceBudget(**kwargs)


# ---------------------------------------------------------------------------
# Failure promotion — decorator path
# ---------------------------------------------------------------------------

class TestPromotionDecorator:
    def test_failure_recorded_under_full_suppression(self, sink):
        configure(budget=_sampled_out_budget())

        @traced_action(action="pay.charge", kind="payment", actor="user")
        def boom():
            raise ValueError("declined")

        for _ in range(10):
            with pytest.raises(ValueError):
                boom()

        assert len(sink.records) == 10
        for r in sink.records:
            assert r["status"] == "failed"
            assert r["sampled_out"] is True
            assert r["action"] == "pay.charge"
            assert r["kind"] == "payment"
            assert r["actor"] == "user"

    def test_promoted_record_has_error_and_timing(self, sink):
        configure(budget=_sampled_out_budget())

        @traced_action(action="fail.op")
        def boom():
            raise RuntimeError("the message")

        with pytest.raises(RuntimeError):
            boom()

        r = sink.records[0]
        assert r["errors"][0]["type"] == "RuntimeError"
        assert r["errors"][0]["message"] == "the message"
        assert r["duration_ms"] is not None and r["duration_ms"] >= 0
        assert r["started_at"] <= r["ended_at"]

    def test_promoted_record_has_no_recorded_detail(self, sink):
        configure(budget=_sampled_out_budget())

        @traced_action(action="fail.op", capture_inputs=True)
        def boom(secret_payload):
            raise ValueError("x")

        with pytest.raises(ValueError):
            boom("should not appear anywhere")

        r = sink.records[0]
        assert r["steps"] == []
        assert r["events"] == []
        assert r["touches"] == []
        assert r["inputs"] == {}
        assert r["outputs"] == {}

    def test_successes_still_dropped(self, sink):
        configure(budget=_sampled_out_budget())

        @traced_action(action="ok.op")
        def ok():
            return 1

        for _ in range(50):
            ok()
        assert sink.records == []

    def test_flag_off_suppression_is_absolute(self, sink):
        configure(budget=_sampled_out_budget(always_trace_errors=False))

        @traced_action(action="fail.silent")
        def boom():
            raise ValueError("x")

        for _ in range(10):
            with pytest.raises(ValueError):
                boom()
        assert sink.records == []

    def test_nested_failure_records_one_per_suppressed_frame(self, sink):
        configure(budget=_sampled_out_budget())

        @traced_action(action="inner.op")
        def inner():
            raise RuntimeError("deep")

        @traced_action(action="outer.op")
        def outer():
            inner()

        with pytest.raises(RuntimeError):
            outer()

        assert sorted(r["action"] for r in sink.records) == ["inner.op", "outer.op"]
        assert all(r["sampled_out"] for r in sink.records)

    def test_exception_still_propagates_after_promotion(self, sink):
        configure(budget=_sampled_out_budget())

        @traced_action(action="fail.op")
        def boom():
            raise KeyError("k")

        with pytest.raises(KeyError):
            boom()

    def test_async_decorator_promotes(self, sink):
        configure(budget=_sampled_out_budget())

        @traced_action(action="fail.async")
        async def boom():
            raise ValueError("async failure")

        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(ValueError):
                loop.run_until_complete(boom())
        finally:
            loop.close()

        assert len(sink.records) == 1
        assert sink.records[0]["sampled_out"] is True

    def test_propagation_context_carried_into_promoted_record(self, sink):
        from traceact import propagate

        configure(budget=_sampled_out_budget())

        @traced_action(action="fail.op")
        def boom():
            raise ValueError("x")

        with propagate({"Traceact-Trace-Id": "trc_up",
                        "Traceact-Correlation-Id": "corr_wf"}):
            with pytest.raises(ValueError):
                boom()

        r = sink.records[0]
        assert r["upstream_trace_id"] == "trc_up"
        assert r["correlation_id"] == "corr_wf"


# ---------------------------------------------------------------------------
# Failure promotion — context-manager path
# ---------------------------------------------------------------------------

class TestPromotionContextManager:
    def test_failure_recorded(self, sink):
        configure(budget=_sampled_out_budget())
        with pytest.raises(ValueError):
            with ActionTrace.start(action="fail.ctx"):
                raise ValueError("x")
        assert len(sink.records) == 1
        assert sink.records[0]["sampled_out"] is True
        assert sink.records[0]["action"] == "fail.ctx"

    def test_step_and_event_calls_inside_are_silent_noops(self, sink):
        configure(budget=_sampled_out_budget())
        with pytest.raises(ValueError):
            with ActionTrace.start(action="fail.ctx") as t:
                t.step("never recorded")
                t.event(kind="db", operation="insert", target="notes")
                t.input({"a": 1})
                raise ValueError("x")
        r = sink.records[0]
        assert r["steps"] == [] and r["events"] == [] and r["inputs"] == {}

    def test_success_dropped(self, sink):
        configure(budget=_sampled_out_budget())
        with ActionTrace.start(action="ok.ctx"):
            pass
        assert sink.records == []

    def test_keyboard_interrupt_promoted_and_reraised(self, sink):
        # ActionTrace.__exit__ records BaseExceptions as failures; the
        # suppressed path must match, not narrow the contract to Exception.
        configure(budget=_sampled_out_budget())
        with pytest.raises(KeyboardInterrupt):
            with ActionTrace.start(action="fail.interrupt"):
                raise KeyboardInterrupt()
        assert len(sink.records) == 1
        assert sink.records[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# SKIP propagation parity (decorator vs context manager)
# ---------------------------------------------------------------------------

class TestSkipPropagationParity:
    def test_manual_parent_suppresses_successful_child(self, sink):
        # The child's own budget would keep it; the parent's suppression must
        # win, exactly as it does under the decorator. Before the fix the
        # child recorded as an orphan root.
        configure(budget=None)
        with ActionTrace.start(action="parent.op", budget=_sampled_out_budget()):
            with ActionTrace.start(action="child.op",
                                   budget=TraceBudget(sample_rate=1.0)):
                pass
        assert sink.records == []

    def test_manual_parent_child_failure_still_promoted(self, sink):
        configure(budget=None)
        with pytest.raises(ValueError):
            with ActionTrace.start(action="parent.op", budget=_sampled_out_budget()):
                with ActionTrace.start(action="child.op"):
                    raise ValueError("x")
        actions = sorted(r["action"] for r in sink.records)
        assert actions == ["child.op", "parent.op"]
        assert all(r["sampled_out"] for r in sink.records)

    def test_skip_context_restored_after_block(self, sink):
        configure(budget=None)
        with ActionTrace.start(action="parent.op", budget=_sampled_out_budget()):
            pass
        # Outside the block, tracing works again.
        with ActionTrace.start(action="after.op"):
            pass
        assert [r["action"] for r in sink.records] == ["after.op"]
        assert sink.records[0]["sampled_out"] is False


# ---------------------------------------------------------------------------
# trace.event() kwargs cannot overwrite core fields
# ---------------------------------------------------------------------------

class TestEventKwargsPrecedence:
    def test_core_fields_win_over_kwargs(self, sink):
        # event_id, depth, and action are NOT parameters of event() — they can
        # only arrive via **kwargs, and must not overwrite the event's own
        # values. (status IS an explicit parameter, so passing it is normal
        # API use, covered separately below.)
        with ActionTrace.start(action="evt.op") as t:
            t.event(kind="db", operation="insert", target="notes",
                    event_id="evil", depth=99, action="forged")
        evt = sink.records[0]["events"][0]
        assert evt["event_id"].startswith("evt_")
        assert evt["depth"] == 0
        assert evt["action"] == "evt.op"

    def test_explicit_status_parameter_still_works(self, sink):
        with ActionTrace.start(action="evt.op") as t:
            t.event(kind="db", operation="insert", target="notes",
                    status="failed", error="constraint violation")
        evt = sink.records[0]["events"][0]
        assert evt["status"] == "failed"

    def test_custom_kwargs_still_attach(self, sink):
        with ActionTrace.start(action="evt.op") as t:
            t.event(kind="db", operation="insert", target="notes",
                    rows=7, database="sqlite")
        evt = sink.records[0]["events"][0]
        assert evt["rows"] == 7
        assert evt["database"] == "sqlite"
