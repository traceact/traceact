# tests/test_endings.py
#
# How a traced frame's ending is classified and recorded.
#
# Two behaviours under test:
#
#   1. No ending can drop a trace. asyncio.CancelledError stopped deriving
#      from Exception in Python 3.8, so an Exception-only clause in the
#      decorator let a cancelled coroutine's trace vanish unwritten — the
#      defect that motivated this file. Every BaseException now finishes
#      and writes the trace, then re-raises.
#
#   2. Cancellation is a distinct ending: asyncio.CancelledError and
#      KeyboardInterrupt record status="cancelled"; other exceptions,
#      SystemExit included, record status="failed". The exception is
#      recorded on the trace either way.
#
# Cancellations here are produced by an asyncio task cancelled mid-await
# through the event loop's own machinery — not by raising CancelledError,
# except where the by-hand form is itself the case under test.

import asyncio

import pytest

from traceact import ActionTrace, TraceBudget, configure, traced_action


class _CaptureSink:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(record)


@pytest.fixture
def sink():
    s = _CaptureSink()
    configure(project="endings-test", sinks=[s])
    return s


# ---------------------------------------------------------------------------
# The dropped-trace defect: a cancelled decorated coroutine must be recorded
# ---------------------------------------------------------------------------

class TestCancelledCoroutineIsRecorded:
    def test_real_task_cancellation_writes_a_cancelled_record(self, sink):
        @traced_action(action="agent.research", kind="app")
        async def research():
            await asyncio.sleep(30)

        async def main():
            task = asyncio.ensure_future(research())
            await asyncio.sleep(0.01)  # let the coroutine start and block
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(main())

        assert len(sink.records) == 1, "the cancelled trace must be written"
        record = sink.records[0]
        assert record["status"] == "cancelled"
        assert record["action"] == "agent.research"
        assert record["errors"][0]["type"] == "CancelledError"
        assert record["ended_at"] is not None
        assert record["duration_ms"] is not None

    def test_cancellation_reraises(self, sink):
        # The decorator must never swallow the cancellation — asyncio's
        # machinery depends on it propagating.
        @traced_action(action="agent.step")
        async def step():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(step())
        assert sink.records[0]["status"] == "cancelled"

    def test_nested_traces_each_record_their_cancellation(self, sink):
        @traced_action(action="inner")
        async def inner():
            await asyncio.sleep(30)

        @traced_action(action="outer")
        async def outer():
            await inner()

        async def main():
            task = asyncio.ensure_future(outer())
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(main())
        by_action = {r["action"]: r for r in sink.records}
        assert set(by_action) == {"inner", "outer"}
        assert by_action["inner"]["status"] == "cancelled"
        assert by_action["outer"]["status"] == "cancelled"
        assert by_action["inner"]["parent_trace_id"] == by_action["outer"]["trace_id"]


# ---------------------------------------------------------------------------
# Classification across paths
# ---------------------------------------------------------------------------

class TestEndingClassification:
    def test_context_manager_cancellation(self, sink):
        with pytest.raises(asyncio.CancelledError):
            with ActionTrace.start(action="ctx.cancel"):
                raise asyncio.CancelledError()
        assert sink.records[0]["status"] == "cancelled"

    def test_sync_decorator_keyboard_interrupt(self, sink):
        @traced_action(action="job.run")
        def job():
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            job()
        assert sink.records[0]["status"] == "cancelled"
        assert sink.records[0]["errors"][0]["type"] == "KeyboardInterrupt"

    def test_system_exit_records_as_failed_and_reraises(self, sink):
        @traced_action(action="job.exit")
        def job():
            raise SystemExit(3)

        with pytest.raises(SystemExit):
            job()
        assert sink.records[0]["status"] == "failed"
        assert sink.records[0]["errors"][0]["type"] == "SystemExit"

    def test_ordinary_exception_still_fails(self, sink):
        @traced_action(action="job.boom")
        def job():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            job()
        assert sink.records[0]["status"] == "failed"

    def test_success_still_completes(self, sink):
        @traced_action(action="job.ok")
        def job():
            return 7

        assert job() == 7
        assert sink.records[0]["status"] == "completed"


# ---------------------------------------------------------------------------
# Sampled-out promotion covers cancellation too
# ---------------------------------------------------------------------------

class TestSampledOutCancellation:
    def test_cancelled_sampled_out_coroutine_promotes_a_record(self, sink):
        configure(budget=TraceBudget(sample_rate=0.0, always_trace_errors=True))

        @traced_action(action="sampled.cancel")
        async def work():
            await asyncio.sleep(30)

        async def main():
            task = asyncio.ensure_future(work())
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(main())
        assert len(sink.records) == 1
        record = sink.records[0]
        assert record["status"] == "cancelled"
        assert record["sampled_out"] is True
        assert record["steps"] == [] and record["events"] == []
        # A promoted record carries the same package-resolved project a
        # sampled-in run of the same frame would have.
        assert record["project"] == "endings-test"

    def test_keyboard_interrupt_promotes_from_sync_skip_path(self, sink):
        configure(budget=TraceBudget(sample_rate=0.0, always_trace_errors=True))

        @traced_action(action="sampled.interrupt")
        def work():
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            work()
        assert sink.records[0]["status"] == "cancelled"
        assert sink.records[0]["sampled_out"] is True


# ---------------------------------------------------------------------------
# errors= — caller-declared exception codes
# ---------------------------------------------------------------------------

class CustomerNotFound(LookupError):
    pass


class TestErrorCodes:
    def test_decorator_stamps_matching_code(self, sink):
        @traced_action(action="customer.fetch",
                       errors={CustomerNotFound: "not_found",
                               TimeoutError: "timeout"})
        def fetch():
            raise CustomerNotFound("no customer 42")

        with pytest.raises(CustomerNotFound):
            fetch()
        err = sink.records[0]["errors"][0]
        assert err["code"] == "not_found"
        assert err["type"] == "CustomerNotFound"

    def test_first_declared_match_wins(self, sink):
        # Subclass listed before base: the specific code applies.
        @traced_action(action="x", errors={CustomerNotFound: "not_found",
                                           LookupError: "lookup"})
        def specific():
            raise CustomerNotFound("gone")

        with pytest.raises(CustomerNotFound):
            specific()
        assert sink.records[0]["errors"][0]["code"] == "not_found"

    def test_base_class_entry_matches_subclasses(self, sink):
        @traced_action(action="x", errors={LookupError: "lookup"})
        def sub():
            raise CustomerNotFound("gone")

        with pytest.raises(CustomerNotFound):
            sub()
        assert sink.records[0]["errors"][0]["code"] == "lookup"

    def test_unmatched_exception_has_no_code_key(self, sink):
        @traced_action(action="x", errors={TimeoutError: "timeout"})
        def other():
            raise ValueError("nope")

        with pytest.raises(ValueError):
            other()
        assert "code" not in sink.records[0]["errors"][0]

    def test_context_manager_parity(self, sink):
        with pytest.raises(TimeoutError):
            with ActionTrace.start(action="ctx", errors={TimeoutError: "timeout"}):
                raise TimeoutError("too slow")
        assert sink.records[0]["errors"][0]["code"] == "timeout"

    def test_cancelled_ending_still_gets_its_code(self, sink):
        # errors= classifies the ending's exception whatever the status.
        with pytest.raises(KeyboardInterrupt):
            with ActionTrace.start(action="ctx",
                                   errors={KeyboardInterrupt: "user_stop"}):
                raise KeyboardInterrupt()
        record = sink.records[0]
        assert record["status"] == "cancelled"
        assert record["errors"][0]["code"] == "user_stop"

    def test_dict_error_carries_its_own_code(self, sink):
        with ActionTrace.start(action="evt") as t:
            t.event(kind="http", operation="get", target="api",
                    status="failed",
                    error={"type": "RateLimitError",
                           "message": "429", "code": "rate_limit"})
        err = sink.records[0]["errors"][0]
        assert err["code"] == "rate_limit"

    def test_sampled_out_promotion_keeps_the_code(self, sink):
        configure(budget=TraceBudget(sample_rate=0.0, always_trace_errors=True))

        @traced_action(action="sampled.coded",
                       errors={TimeoutError: "timeout"})
        def work():
            raise TimeoutError("slow")

        with pytest.raises(TimeoutError):
            work()
        assert sink.records[0]["errors"][0]["code"] == "timeout"

    def test_validation_rejects_non_type_key_at_decoration(self):
        with pytest.raises(TypeError):
            @traced_action(action="x", errors={"TimeoutError": "timeout"})
            def bad():
                pass

    def test_validation_rejects_non_string_code_at_decoration(self):
        with pytest.raises(TypeError):
            @traced_action(action="x", errors={TimeoutError: 7})
            def bad():
                pass

    def test_validation_rejects_non_dict(self):
        with pytest.raises(TypeError):
            @traced_action(action="x", errors=[TimeoutError])
            def bad():
                pass

    def test_start_validates_too(self):
        with pytest.raises(TypeError):
            ActionTrace.start(action="x", errors={7: "nope"})
