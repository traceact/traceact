# tests/test_quantum_kinds.py
#
# Tests for the "gate" and "qstate" standard event kinds, added for quantum
# circuit instrumentation (Qlens and similar). Two kinds, deliberately: a
# gate event is an operation applied to qubits, a qstate event is an
# observation recorded about them — different consumers (circuit rendering
# vs state visualisation), different capture policies (batched vs sparse).
# Both derive "qubit" touches, so a trace's touch list answers "which qubits
# were involved" regardless of how they were involved.

import json

import pytest

from traceact import ActionTrace, JsonlSink, TraceConfig, configure, reset_config


@pytest.fixture
def sink_file(tmp_path):
    path = tmp_path / "traces.jsonl"
    configure(project="qtest",
              config=TraceConfig(sink_mode="blocking"),
              sinks=[JsonlSink(str(path))])
    yield path
    reset_config()


def _record(path):
    return json.loads(path.read_text().splitlines()[-1])


class TestGateKind:
    def test_gate_event_records_and_derives_qubit_touch(self, sink_file):
        with ActionTrace.start(action="circuit.run") as t:
            t.event(kind="gate", operation="apply", target="q0",
                    gate="H", position=0, qubits=[0])
        rec = _record(sink_file)
        event = rec["events"][0]
        assert event["kind"] == "gate"
        assert event["gate"] == "H"          # arbitrary kwargs survive
        assert event["qubits"] == [0]
        assert {"kind": "qubit", "target": "q0"} in rec["touches"]

    def test_multi_qubit_gates_touch_each_named_target(self, sink_file):
        with ActionTrace.start(action="circuit.run") as t:
            t.event(kind="gate", operation="apply", target="q0",
                    gate="H", position=0)
            t.event(kind="gate", operation="apply", target="q1",
                    gate="CNOT", position=1, control=0)
        touches = _record(sink_file)["touches"]
        assert {"kind": "qubit", "target": "q0"} in touches
        assert {"kind": "qubit", "target": "q1"} in touches

    def test_repeated_gates_on_one_qubit_dedupe_to_one_touch(self, sink_file):
        # A thousand-gate circuit must not produce a thousand-entry touch
        # list; touches record involvement, events record the operations.
        with ActionTrace.start(action="circuit.run") as t:
            for i in range(5):
                t.event(kind="gate", operation="apply", target="q0",
                        gate="X", position=i)
        rec = _record(sink_file)
        qubit_touches = [x for x in rec["touches"] if x["kind"] == "qubit"]
        assert len(qubit_touches) == 1
        assert len([e for e in rec["events"] if e["kind"] == "gate"]) == 5


class TestQstateKind:
    def test_qstate_event_records_reference_not_payload(self, sink_file):
        # The intended pattern: heavy state spools to a sidecar and the
        # event carries the reference. The reference passes through intact.
        with ActionTrace.start(action="circuit.run") as t:
            t.event(kind="qstate", operation="snapshot", target="q0",
                    position=3, statevector_ref="qstates/trc_abc/evt_3.npz",
                    norm_check=1.0)
        event = _record(sink_file)["events"][0]
        assert event["kind"] == "qstate"
        assert event["statevector_ref"] == "qstates/trc_abc/evt_3.npz"

    def test_qstate_also_derives_qubit_touch(self, sink_file):
        with ActionTrace.start(action="circuit.run") as t:
            t.event(kind="qstate", operation="snapshot", target="q2")
        assert {"kind": "qubit", "target": "q2"} in _record(sink_file)["touches"]

    def test_gate_and_qstate_on_same_qubit_share_one_touch(self, sink_file):
        # Both kinds map to "qubit" precisely so the touch list unifies:
        # involvement is involvement, whether by operation or observation.
        with ActionTrace.start(action="circuit.run") as t:
            t.event(kind="gate", operation="apply", target="q0", gate="H")
            t.event(kind="qstate", operation="snapshot", target="q0")
        rec = _record(sink_file)
        qubit_touches = [x for x in rec["touches"] if x["kind"] == "qubit"]
        assert qubit_touches == [{"kind": "qubit", "target": "q0"}]

    def test_kinds_stay_distinct_for_filtering(self, sink_file):
        # The reason there are two kinds at all: a circuit renderer filters
        # for gates and must not receive snapshots, and vice versa.
        with ActionTrace.start(action="circuit.run") as t:
            t.event(kind="gate", operation="apply", target="q0", gate="H")
            t.event(kind="qstate", operation="snapshot", target="q0")
            t.event(kind="gate", operation="apply", target="q1", gate="X")
        events = _record(sink_file)["events"]
        assert len([e for e in events if e["kind"] == "gate"]) == 2
        assert len([e for e in events if e["kind"] == "qstate"]) == 1


class TestOtlpMapping:
    def test_gate_and_qstate_export_as_internal_spans(self):
        # Deliberate absence from the kind map: simulation runs in-process,
        # and a submission to remote hardware is the client library's own
        # HTTP call. This pins the absence so adding a mapping later is a
        # conscious decision, not an accident.
        from traceact.sinks import _KIND_TO_OTLP, _OTLP_KIND_INTERNAL
        assert _KIND_TO_OTLP.get("gate", _OTLP_KIND_INTERNAL) == _OTLP_KIND_INTERNAL
        assert _KIND_TO_OTLP.get("qstate", _OTLP_KIND_INTERNAL) == _OTLP_KIND_INTERNAL
