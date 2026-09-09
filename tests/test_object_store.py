# tests/test_object_store.py
#
# Tests for ObjectStoreSink and S3Backend.
#
# The SigV4 signing core is checked against the published aws-sig-v4-test-suite
# "get-vanilla" vector, so the signature math is validated against an
# authoritative constant rather than a re-implementation of the same logic.
#
# Wire-level tests patch urllib.request.OpenerDirector.open (the method the
# guarded opener ultimately calls), the same offline approach as
# test_http_sink.py — no ports, no threads, no flakiness. Behaviour tests use
# an in-memory backend that records or fails puts.

import gzip
import hashlib
import hmac
import json
import unittest.mock as mock

import pytest

from traceact import ObjectStoreSink, S3Backend
from traceact import _netguard


def _trace(action="note.create", **extra):
    t = {
        "trace_id": "trc_note",
        "action": action,
        "started_at": "2026-07-25T10:00:00Z",
        "status": "completed",
    }
    t.update(extra)
    return t


def _mock_response(status=200):
    resp = mock.MagicMock()
    resp.status = status
    resp.__enter__ = lambda s: resp
    resp.__exit__ = mock.MagicMock(return_value=False)
    return resp


class _CaptureBackend:
    """Records every put so a test can inspect the objects written."""

    def __init__(self):
        self.puts = []

    def put(self, key, body, content_type):
        self.puts.append((key, body, content_type))


class _FailBackend:
    def put(self, key, body, content_type):
        raise OSError("boom")


# ---------------------------------------------------------------------------
# SigV4 signing core
# ---------------------------------------------------------------------------

class TestSigV4:
    def test_matches_get_vanilla_vector(self):
        """The signing key + final HMAC reproduce the published get-vanilla
        signature from the aws-sig-v4-test-suite."""
        backend = S3Backend(
            endpoint="https://example.amazonaws.com",
            bucket="x",
            access_key="AKIDEXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            region="us-east-1",
            network_policy="off",
        )
        backend.service = "service"  # the vector's service name
        canonical_request = "\n".join([
            "GET",
            "/",
            "",
            "host:example.amazonaws.com\nx-amz-date:20150830T123600Z\n",
            "host;x-amz-date",
            hashlib.sha256(b"").hexdigest(),
        ])
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            "20150830T123600Z",
            "20150830/us-east-1/service/aws4_request",
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        signature = hmac.new(
            backend._signing_key("20150830"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert signature == (
            "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
        )

    def test_sign_headers_are_well_formed(self):
        backend = S3Backend(
            endpoint="https://s3.us-east-1.amazonaws.com",
            bucket="bkt",
            access_key="AK",
            secret_key="SK",
            region="us-east-1",
            network_policy="off",
        )
        body = b'{"a":1}\n'
        headers = backend._sign(
            "PUT",
            "https://s3.us-east-1.amazonaws.com/bkt/prod/2026/01/01/1-a.jsonl",
            body,
            "application/x-ndjson",
        )
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AK/")
        assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in headers["Authorization"]
        assert headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest()
        assert headers["Content-Length"] == str(len(body))
        assert headers["Content-Type"] == "application/x-ndjson"


# ---------------------------------------------------------------------------
# S3Backend.put over the wire (offline)
# ---------------------------------------------------------------------------

class TestS3BackendPut:
    def test_puts_path_style_object(self):
        captured = {}

        def fake_open(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.method
            captured["body"] = req.data
            captured["auth"] = req.get_header("Authorization")
            captured["amz_sha"] = req.get_header("X-amz-content-sha256")
            return _mock_response(200)

        backend = S3Backend(
            endpoint="https://s3.us-east-1.amazonaws.com",
            bucket="my-traces",
            access_key="AK",
            secret_key="SK",
            region="us-east-1",
            network_policy="off",
        )
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=fake_open):
            backend.put("prod/2026/01/01/1-a.jsonl", b"line\n", "application/x-ndjson")

        assert captured["url"] == (
            "https://s3.us-east-1.amazonaws.com/my-traces/prod/2026/01/01/1-a.jsonl"
        )
        assert captured["method"] == "PUT"
        assert captured["body"] == b"line\n"
        assert captured["auth"].startswith("AWS4-HMAC-SHA256 ")
        assert captured["amz_sha"] == hashlib.sha256(b"line\n").hexdigest()

    def test_non_2xx_raises(self):
        backend = S3Backend(
            endpoint="https://s3.example.com",
            bucket="b",
            access_key="AK",
            secret_key="SK",
            network_policy="off",
        )
        with mock.patch(
            "urllib.request.OpenerDirector.open",
            side_effect=lambda req, timeout=None: _mock_response(403),
        ):
            with pytest.raises(OSError):
                backend.put("k.jsonl", b"x\n", "application/x-ndjson")

    def test_bad_network_policy_rejected(self):
        with pytest.raises(ValueError):
            S3Backend("https://s3.example.com", "b", "AK", "SK", network_policy="loud")


# ---------------------------------------------------------------------------
# ObjectStoreSink batching, keys, compression
# ---------------------------------------------------------------------------

class TestObjectStoreSink:
    def test_holds_until_flush_records(self):
        cap = _CaptureBackend()
        sink = ObjectStoreSink(cap, prefix="prod/", flush_records=3, flush_seconds=9999)
        sink.write(_trace("a"))
        sink.write(_trace("b"))
        assert cap.puts == []
        sink.write(_trace("c"))
        assert len(cap.puts) == 1

    def test_batch_is_ndjson_with_all_records(self):
        cap = _CaptureBackend()
        sink = ObjectStoreSink(cap, flush_records=2, flush_seconds=9999)
        sink.write(_trace("a"))
        sink.write(_trace("b"))
        key, body, content_type = cap.puts[0]
        assert content_type == "application/x-ndjson"
        assert key.endswith(".jsonl")
        rows = [json.loads(line) for line in body.decode("utf-8").splitlines()]
        assert [r["action"] for r in rows] == ["a", "b"]

    def test_key_is_time_ordered_with_prefix(self):
        cap = _CaptureBackend()
        sink = ObjectStoreSink(cap, prefix="/prod/", flush_records=1)
        sink.write(_trace("a"))
        key = cap.puts[0][0]
        # prefix / YYYY / MM / DD / <epoch_ms>-<uuid>.jsonl
        assert key.startswith("prod/")
        parts = key.split("/")
        assert parts[0] == "prod"
        assert parts[1].isdigit() and len(parts[1]) == 4  # year
        assert parts[-1].endswith(".jsonl")

    def test_flush_drains_partial_buffer(self):
        cap = _CaptureBackend()
        sink = ObjectStoreSink(cap, flush_records=100, flush_seconds=9999)
        sink.write(_trace("a"))
        assert cap.puts == []
        sink.flush()
        assert len(cap.puts) == 1

    def test_close_drains_and_is_idempotent(self):
        cap = _CaptureBackend()
        sink = ObjectStoreSink(cap, flush_records=100, flush_seconds=9999)
        sink.write(_trace("a"))
        sink.close()
        sink.close()  # nothing buffered; no second object
        assert len(cap.puts) == 1

    def test_compress_gzips_and_names_gz(self):
        cap = _CaptureBackend()
        sink = ObjectStoreSink(cap, flush_records=1, compress=True)
        sink.write(_trace("a"))
        key, body, content_type = cap.puts[0]
        assert content_type == "application/gzip"
        assert key.endswith(".jsonl.gz")
        row = json.loads(gzip.decompress(body).decode("utf-8"))
        assert row["action"] == "a"

    def test_rejects_bad_flush_records(self):
        with pytest.raises(ValueError):
            ObjectStoreSink(_CaptureBackend(), flush_records=0)


# ---------------------------------------------------------------------------
# Observable failures
# ---------------------------------------------------------------------------

class TestFailureCounting:
    def test_failed_batch_counts_records_not_raised(self):
        sink = ObjectStoreSink(_FailBackend(), flush_records=2)
        sink.write(_trace("a"))
        sink.write(_trace("b"))  # triggers the failing put
        assert sink.failed == 2

    def test_enforce_blocks_private_endpoint_counted(self):
        # 10.0.0.5 is a private literal — check_destination rejects it under
        # enforce, and the sink counts the batch rather than raising.
        backend = S3Backend(
            "https://10.0.0.5", "b", "AK", "SK", network_policy="enforce",
        )
        sink = ObjectStoreSink(backend, flush_records=1)
        sink.write(_trace("a"))
        assert sink.failed == 1


# ---------------------------------------------------------------------------
# Composition under AsyncSink
# ---------------------------------------------------------------------------

class TestComposition:
    def test_wraps_under_async_sink(self):
        from traceact import AsyncSink

        cap = _CaptureBackend()
        sink = AsyncSink([ObjectStoreSink(cap, flush_records=2, flush_seconds=9999)])
        sink.write(_trace("a"))
        sink.write(_trace("b"))
        sink.close()
        assert len(cap.puts) == 1
        rows = [json.loads(line) for line in cap.puts[0][1].decode("utf-8").splitlines()]
        assert [r["action"] for r in rows] == ["a", "b"]

    def test_async_close_delivers_subthreshold_tail(self):
        """A sub-threshold batch (fewer writes than flush_records, no timeout)
        must still reach the store when the wrapping AsyncSink is closed —
        the documented AsyncSink([ObjectStoreSink(...)]) shutdown must not drop
        the last partial batch silently."""
        from traceact import AsyncSink

        cap = _CaptureBackend()
        inner = ObjectStoreSink(cap, flush_records=500, flush_seconds=9999)
        sink = AsyncSink([inner])
        sink.write(_trace("a"))
        sink.write(_trace("b"))
        sink.write(_trace("c"))
        assert cap.puts == []  # nothing hit the threshold
        sink.close()
        assert len(cap.puts) == 1
        assert inner.failed == 0
        rows = [json.loads(line) for line in cap.puts[0][1].decode("utf-8").splitlines()]
        assert [r["action"] for r in rows] == ["a", "b", "c"]

    def test_async_flush_delivers_subthreshold_tail(self):
        """AsyncSink.flush() cascades to a buffering inner sink without stopping
        the worker, so a partial batch is delivered at a known checkpoint."""
        from traceact import AsyncSink

        cap = _CaptureBackend()
        sink = AsyncSink([ObjectStoreSink(cap, flush_records=500, flush_seconds=9999)])
        sink.write(_trace("a"))
        sink.flush()
        assert len(cap.puts) == 1
        sink.close()

    def test_async_flush_leaves_non_buffering_sinks_untouched(self):
        """A cascaded flush() skips inner sinks that don't expose one, so
        wrapping a per-record sink (the common case) is unaffected."""
        from traceact import AsyncSink

        class _PerRecord:
            def __init__(self):
                self.records = []

            def write(self, record):
                self.records.append(record)

        inner = _PerRecord()
        sink = AsyncSink([inner])
        sink.write(_trace("a"))
        sink.flush()
        sink.close()
        assert [r["action"] for r in inner.records] == ["a"]
