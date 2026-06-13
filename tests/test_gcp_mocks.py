"""CRITICAL: Infrastructure — GCP integrations are exercised with mocks only.

No test in this suite touches the network. The Google client libraries are
injected as fakes via sys.modules so the integration code paths
(BigQuerySink payload formation, VertexGeminiMutationEngine response parsing)
run and are asserted without credentials, billing, or connectivity.
"""

import json
import sys
import types
from unittest import mock

import pytest

from self_harness.tracing.schema import SpanKind, TraceEvent


# --- Fake google.cloud.bigquery ---------------------------------------------

@pytest.fixture
def fake_bigquery(monkeypatch):
    bq = types.ModuleType("google.cloud.bigquery")

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.init_kwargs = kwargs
            # Records (table, rows) of every insert for assertions.
            self.inserts = []
            # Default: empty error list == successful streaming insert.
            self.insert_return = []

        def insert_rows_json(self, table, rows):
            self.inserts.append((table, rows))
            return self.insert_return

    bq.Client = FakeClient
    bq.QueryJobConfig = mock.MagicMock()
    bq.ScalarQueryParameter = mock.MagicMock()

    google = types.ModuleType("google")
    google_cloud = types.ModuleType("google.cloud")
    google_cloud.bigquery = bq
    google.cloud = google_cloud

    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", google_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bq)
    return bq


def test_bigquery_sink_forms_correct_json_payload(fake_bigquery):
    from self_harness.tracing.bigquery_sink import BigQuerySink

    sink = BigQuerySink(project_id="proj", dataset="ds", table="tbl")
    events = [
        TraceEvent(run_id="r1", harness_version="g0001-abc", kind=SpanKind.TOOL_CALL,
                   step=3, duration_s=1.5,
                   payload={"tool": "fetch", "args": {"id": 7}}),
        TraceEvent(run_id="r1", harness_version="g0001-abc", kind=SpanKind.RUN_END,
                   step=4, payload={"success": True, "steps": 4}),
    ]

    sink.write(events)

    # Exactly one streaming insert, to the fully-qualified table.
    assert len(sink.client.inserts) == 1
    table, rows = sink.client.inserts[0]
    assert table == "proj.ds.tbl"
    assert len(rows) == 2

    # kind is serialized to its string value; payload is a JSON string that
    # round-trips back to the original dict.
    first = rows[0]
    assert first["kind"] == "tool_call"
    assert first["run_id"] == "r1"
    assert first["harness_version"] == "g0001-abc"
    assert isinstance(first["payload"], str)
    assert json.loads(first["payload"]) == {"tool": "fetch", "args": {"id": 7}}

    # Every row must be JSON-serializable (the streaming API requirement).
    json.dumps(rows)


def test_bigquery_sink_dead_letters_on_persistent_failure(fake_bigquery, tmp_path):
    from self_harness.tracing.bigquery_sink import BigQuerySink

    dead_letter = tmp_path / "dl.ndjson"
    sink = BigQuerySink(project_id="p", dataset="d", table="t",
                        dead_letter_path=dead_letter)
    # Every insert reports row errors -> retries exhausted -> dead-letter.
    sink.client.insert_return = [{"index": 0, "errors": ["boom"]}]

    with mock.patch("self_harness.tracing.bigquery_sink.time.sleep"):  # no real backoff wait
        sink.write([TraceEvent(run_id="r", harness_version="g", kind=SpanKind.ERROR,
                               payload={"error": "X"})])

    assert dead_letter.exists()
    line = json.loads(dead_letter.read_text().splitlines()[0])
    assert line["run_id"] == "r"


def test_bigquery_sink_empty_batch_is_noop(fake_bigquery):
    from self_harness.tracing.bigquery_sink import BigQuerySink

    sink = BigQuerySink(project_id="p", dataset="d", table="t")
    sink.write([])
    assert sink.client.inserts == []


# --- Fake vertexai -----------------------------------------------------------

@pytest.fixture
def fake_vertex(monkeypatch):
    """Inject a fake vertexai + vertexai.generative_models. The fixture exposes
    a hook to set the next generate_content(...).text response."""
    state = {"response_text": ""}

    vertexai = types.ModuleType("vertexai")
    vertexai.init = mock.MagicMock()

    genmod = types.ModuleType("vertexai.generative_models")

    class FakeResponse:
        def __init__(self, text):
            self.text = text

    class FakeGenerativeModel:
        def __init__(self, *args, **kwargs):
            self.init_args = (args, kwargs)

        def generate_content(self, *args, **kwargs):
            return FakeResponse(state["response_text"])

    genmod.GenerativeModel = FakeGenerativeModel
    vertexai.generative_models = genmod

    monkeypatch.setitem(sys.modules, "vertexai", vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.generative_models", genmod)
    return state


def _patch_payload() -> str:
    from self_harness.core.harness import HarnessSpec
    return json.dumps({
        "hypothesis": "retries mask deterministic tool-schema errors",
        "rationale": "run r3 step 12: same 400 error retried 5x",
        "spec_json": HarnessSpec(version="v2", max_retries=2).model_dump_json(),
        "harness_source": "class Harness:\n    pass\n",
    })


def test_vertex_mutation_engine_parses_valid_response(fake_vertex):
    from self_harness.mutation.vertex_gemini import VertexGeminiMutationEngine
    from self_harness.tracing.analyzer import AnalysisReport

    fake_vertex["response_text"] = _patch_payload()

    engine = VertexGeminiMutationEngine(project_id="proj", location="us-central1")
    patch = engine.propose(
        current_spec_json="{}",
        current_source="class Harness: pass",
        traces=[TraceEvent(run_id="r3", harness_version="g0", kind=SpanKind.ERROR,
                           step=12, payload={"error": "HttpError400"})],
        report=AnalysisReport(harness_version="g0"),
    )

    assert patch is not None
    assert patch.hypothesis.startswith("retries mask")
    assert "r3" in patch.rationale
    assert '"max_retries":2' in patch.spec_json.replace(" ", "")


def test_vertex_engine_returns_none_on_no_change(fake_vertex):
    from self_harness.mutation.vertex_gemini import VertexGeminiMutationEngine
    from self_harness.tracing.analyzer import AnalysisReport

    fake_vertex["response_text"] = json.dumps({"no_change": True})
    engine = VertexGeminiMutationEngine(project_id="p")
    assert engine.propose("{}", "x", [], AnalysisReport(harness_version="g0")) is None


def test_vertex_engine_returns_none_on_malformed_json(fake_vertex):
    from self_harness.mutation.vertex_gemini import VertexGeminiMutationEngine
    from self_harness.tracing.analyzer import AnalysisReport

    fake_vertex["response_text"] = "not json at all {"
    engine = VertexGeminiMutationEngine(project_id="p")
    assert engine.propose("{}", "x", [], AnalysisReport(harness_version="g0")) is None


def test_vertex_engine_returns_none_on_missing_fields(fake_vertex):
    from self_harness.mutation.vertex_gemini import VertexGeminiMutationEngine
    from self_harness.tracing.analyzer import AnalysisReport

    # hypothesis present but spec_json / harness_source missing.
    fake_vertex["response_text"] = json.dumps({"hypothesis": "h"})
    engine = VertexGeminiMutationEngine(project_id="p")
    assert engine.propose("{}", "x", [], AnalysisReport(harness_version="g0")) is None
