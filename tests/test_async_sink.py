# tests/test_async_sink.py
#
# Tests for AsyncSink — the background-thread sink wrapper.
#
# Every test creates a fresh AsyncSink and closes it explicitly so no worker
# threads leak between tests. The flush() call before assertions ensures the
# worker has processed all enqueued records.

import time
import threading
import pytest

from traceact import AsyncSink, JsonlSink, ConsoleSink


# ---------------------------------------------------------------------------
# A minimal in-memory sink for testing — captures records without touching disk.
# ---------------------------------------------------------------------------

class CaptureSink:
    def __init__(self):
        self.records = []
        self._lock = threading.Lock()

    def write(self, record):
        with self._lock:
            self.records.append(record)


class FailingSink:
    """A sink whose write() always raises — to test that the worker survives."""
    def write(self, record):
        raise RuntimeError("deliberate failure")


# ---------------------------------------------------------------------------
# Basic write-through behaviour
# ---------------------------------------------------------------------------

class TestWriteThrough:
    def test_records_reach_inner_sink(self):
        inner = CaptureSink()
        sink = AsyncSink([inner])
        try:
            sink.write({"trace_id": "trc_1", "action": "a"})
            sink.write({"trace_id": "trc_2", "action": "b"})
            sink.flush()
            assert len(inner.records) == 2
        finally:
            sink.close()

    def test_multiple_inner_sinks_all_receive_record(self):
        a, b = CaptureSink(), CaptureSink()
        sink = AsyncSink([a, b])
        try:
            sink.write({"trace_id": "trc_1", "action": "x"})
            sink.flush()
            assert len(a.records) == 1
            assert len(b.records) == 1
        finally:
            sink.close()

    def test_write_returns_immediately(self):
        """write() must not block even with a slow inner sink."""
        class SlowSink:
            def write(self, record):
                time.sleep(0.1)

        sink = AsyncSink([SlowSink()])
        try:
            start = time.monotonic()
            for _ in range(5):
                sink.write({"trace_id": "trc_x", "action": "slow"})
            elapsed = time.monotonic() - start
            # All 5 writes should have returned in far less than 5×0.1 s.
            assert elapsed < 0.2, f"write() blocked for {elapsed:.2f}s"
        finally:
            sink.close()

    def test_empty_sinks_list_is_valid(self):
        sink = AsyncSink([])
        try:
            sink.write({"trace_id": "trc_1", "action": "a"})
            sink.flush()  # should complete without error
        finally:
            sink.close()


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------

class TestWorkerLifecycle:
    def test_worker_not_started_until_first_write(self):
        sink = AsyncSink([CaptureSink()])
        try:
            assert not sink._started
            sink.write({"trace_id": "trc_1", "action": "a"})
            assert sink._started
        finally:
            sink.close()

    def test_close_is_idempotent(self):
        sink = AsyncSink([CaptureSink()])
        sink.write({"trace_id": "trc_1", "action": "a"})
        sink.close()
        sink.close()  # second close must not raise

    def test_flush_before_any_write_is_safe(self):
        sink = AsyncSink([CaptureSink()])
        try:
            sink.flush()  # no worker yet — must not raise or block
        finally:
            sink.close()

    def test_records_flushed_on_close(self):
        inner = CaptureSink()
        sink = AsyncSink([inner])
        sink.write({"trace_id": "trc_1", "action": "a"})
        sink.close()
        # After close(), the worker has drained the queue.
        assert len(inner.records) == 1


# ---------------------------------------------------------------------------
# Backpressure policies
# ---------------------------------------------------------------------------

class TestBackpressurePolicies:
    def test_drop_newest_never_blocks(self):
        """With a full queue and drop_newest, write() returns without raising."""
        sink = AsyncSink([CaptureSink()], max_queue=2, on_full="drop_newest")
        try:
            # Stuff the queue (worker may or may not have drained some).
            for i in range(10):
                sink.write({"trace_id": f"trc_{i}", "action": "a"})
            # Should not raise or block.
        finally:
            sink.close()

    def test_drop_newest_increments_dropped(self):
        """Records dropped under drop_newest are counted."""
        # Use a barrier to hold the worker so the queue fills predictably.
        barrier = threading.Event()

        class BlockingSink:
            def write(self, record):
                barrier.wait()

        sink = AsyncSink([BlockingSink()], max_queue=1, on_full="drop_newest")
        try:
            # First write starts the worker; the worker blocks on barrier.
            sink.write({"trace_id": "trc_0", "action": "first"})
            time.sleep(0.05)  # let the worker pick up the first record

            # Queue is now empty but worker is blocked. Fill it.
            sink.write({"trace_id": "trc_1", "action": "queued"})
            # This one should be dropped (queue is full).
            sink.write({"trace_id": "trc_2", "action": "overflow"})

            assert sink.dropped >= 1
        finally:
            barrier.set()
            sink.close()

    def test_drop_oldest_policy_accepted(self):
        sink = AsyncSink([CaptureSink()], max_queue=2, on_full="drop_oldest")
        try:
            for i in range(5):
                sink.write({"trace_id": f"trc_{i}", "action": "a"})
        finally:
            sink.close()

    def test_block_policy_accepted(self):
        sink = AsyncSink([CaptureSink()], max_queue=10, on_full="block")
        try:
            sink.write({"trace_id": "trc_1", "action": "a"})
            sink.flush()
        finally:
            sink.close()

    def test_invalid_on_full_raises(self):
        with pytest.raises(ValueError, match="on_full must be"):
            AsyncSink([CaptureSink()], on_full="discard")


# ---------------------------------------------------------------------------
# Fault tolerance
# ---------------------------------------------------------------------------

class TestFaultTolerance:
    def test_failing_inner_sink_does_not_crash_worker(self):
        """A sink that always raises must not kill the worker or lose other records."""
        good = CaptureSink()
        sink = AsyncSink([FailingSink(), good])
        try:
            sink.write({"trace_id": "trc_1", "action": "a"})
            sink.write({"trace_id": "trc_2", "action": "b"})
            sink.flush()
            # The worker survived — good sink received both records.
            assert len(good.records) == 2
        finally:
            sink.close()


# ---------------------------------------------------------------------------
# Closed-sink contract
# ---------------------------------------------------------------------------

class TestWriteAfterClose:
    def test_write_after_close_is_counted_dropped_not_enqueued(self):
        inner = CaptureSink()
        sink = AsyncSink([inner])
        sink.write({"trace_id": "trc_1", "action": "a"})
        sink.close()

        sink.write({"trace_id": "trc_late", "action": "b"})
        sink.write({"trace_id": "trc_later", "action": "c"})

        assert sink.dropped == 2
        assert [r["trace_id"] for r in inner.records] == ["trc_1"]

    def test_write_after_close_does_not_restart_worker(self):
        sink = AsyncSink([CaptureSink()])
        sink.write({"trace_id": "trc_1", "action": "a"})
        sink.close()
        sink.write({"trace_id": "trc_late", "action": "b"})
        assert sink._started is False

    def test_close_before_any_write_still_closes(self):
        sink = AsyncSink([CaptureSink()])
        sink.close()
        sink.write({"trace_id": "trc_1", "action": "a"})
        assert sink.dropped == 1

    def test_close_twice_is_safe(self):
        sink = AsyncSink([CaptureSink()])
        sink.write({"trace_id": "trc_1", "action": "a"})
        sink.close()
        sink.close()  # must not raise


# ---------------------------------------------------------------------------
# Exported from public API
# ---------------------------------------------------------------------------

def test_async_sink_importable_from_top_level():
    from traceact import AsyncSink as AS
    assert AS is AsyncSink
