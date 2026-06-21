#!/usr/bin/env python3
"""
test_nova_correlator.py — Comprehensive tests for nova_correlator.py.

nova_correlator turns alert STORMS into INCIDENTS. It folds downstream symptoms
under their root cause (topology), joins same-host events inside a time window
(temporal), attaches semantically-similar events (embeddings), and asks a local
LLM to summarize the incident.

Covers:
  - _host_of():    meta.host > IP regex > hostname regex > None; jsonb-as-str meta
  - _cosine():     identical / orthogonal / mismatched-length / empty vectors
  - _is_symptom_of(): topology lookups incl. host_down wildcard
  - correlate():
      * standalone for info-level and for events with no resolvable host
      * opens a NEW incident for an unmatched warning/critical
      * topology symptom-folding (gpu -> crash_storm) -> 'attached'/'symptom'
      * same-host same-category temporal join -> 'attached'/'member'
      * critical event escalates an open warning incident's severity
  - llm_summarize(): /api/chat MOCKED -> clean text written to incident;
      <think> stripped; short output -> templated fallback; LLM error -> fallback

HARD SAFETY:
  - The notification bus (nova_notify.notify) is monkeypatched to a no-op recorder
    so NO test can enqueue a telemetry event that the live nova_notifier daemon
    would POST TO SLACK.
  - Ollama HTTP (_http / urllib.request.urlopen) is mocked in every test — the
    suite is deterministic and never needs network or the LLMs to be up.
  - Every DB-touching test runs inside a transaction that is ROLLED BACK in
    teardown. Rows we insert carry source='pytest-correlator' as a belt-and-
    suspenders marker, but rollback guarantees ZERO leftover rows. A final
    fixture asserts the table is clean of our marker.

Run: python3 -m pytest tests/test_nova_correlator.py -q
Written by Jordan Koch.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# nova_correlator lives in scripts/; put it on the path (conftest in scripts/tests
# also does this, but this file lives in tests/ and may run with either rootdir).
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

PG_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
TEST_SOURCE = "pytest-correlator"


def _load_correlator():
    """Import nova_correlator fresh. Skips the whole suite if psycopg2/module
    is unavailable so this never reports a spurious failure."""
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        pytest.skip("psycopg2 not installed")
    if "nova_correlator" in sys.modules:
        del sys.modules["nova_correlator"]
    import nova_correlator
    return nova_correlator


# ── Safety: neutralize the notification bus for the whole module ──────────────

@pytest.fixture(autouse=True)
def _no_slack(monkeypatch):
    """SAFETY: never let a test reach the notification bus.

    nova_correlator does not itself import nova_notify, but several sibling
    modules do, and a recorder here guarantees that if any code path ever calls
    notify() during a test it is a harmless no-op (never written to
    telemetry.events / never POSTed to Slack by the daemon).
    """
    recorder = MagicMock(name="notify_recorder")
    try:
        import nova_notify
        monkeypatch.setattr(nova_notify, "notify", recorder, raising=False)
    except Exception:
        pass
    # Block any accidental real HTTP at the urllib layer as a hard backstop.
    monkeypatch.setattr(
        "urllib.request.urlopen",
        MagicMock(side_effect=AssertionError("real HTTP attempted in a test")),
    )
    return recorder


# ── DB: real connection, always rolled back ──────────────────────────────────

@pytest.fixture
def conn():
    """A real nova_ops connection whose transaction is ROLLED BACK after the
    test. nova_correlator never commits, so nothing it writes can persist."""
    import psycopg2
    try:
        c = psycopg2.connect(PG_DSN)
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"nova_ops not reachable: {e}")
    c.autocommit = False
    try:
        yield c
    finally:
        c.rollback()  # discard everything the test inserted
        c.close()


@pytest.fixture
def mk_event(conn):
    """Factory that inserts a real telemetry.events row (inside the rolled-back
    transaction) and returns the row dict the way nova_notifier hands it to
    correlate(). Marker source='pytest-correlator'."""
    def _make(level="warning", category=None, title="evt", body="", host=None, meta=None):
        cur = conn.cursor()
        import json as _json
        meta = dict(meta or {})
        if host and "host" not in meta:
            meta["host"] = host
        cur.execute(
            "INSERT INTO telemetry.events (source, level, category, title, body, meta) "
            "VALUES (%s,%s,%s,%s,%s,%s::jsonb) RETURNING id",
            (TEST_SOURCE, level, category, title, body, _json.dumps(meta)),
        )
        eid = cur.fetchone()[0]
        return {
            "id": eid, "level": level, "category": category,
            "title": title, "body": body, "meta": meta,
        }
    return _make


@pytest.fixture
def open_incident(conn):
    """Factory that opens a telemetry.incidents row and (optionally) ties a root
    event so root_cat resolves. Returns incident id."""
    def _open(host, title="root incident", severity="warning",
              root_event=None, embedding=None, member_count=1):
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO telemetry.incidents "
            "(status, severity, host, title, root_event, member_count, embedding) "
            "VALUES ('open', %s, %s, %s, %s, %s, %s) RETURNING id",
            (severity, host, title, root_event, member_count, embedding),
        )
        return cur.fetchone()[0]
    return _open


# ═══════════════════════════════════════════════════════════════════════════
# PURE LOGIC — no DB, no network
# ═══════════════════════════════════════════════════════════════════════════

class TestHostOf:
    """_host_of(): meta.host wins, else IP, else known hostname, else None."""

    def setup_method(self):
        self.mod = _load_correlator()

    def test_explicit_meta_host_wins(self):
        ev = {"meta": {"host": "mac-studio"}, "title": "192.168.1.5 down"}
        assert self.mod._host_of(ev) == "mac-studio"

    def test_meta_as_json_string_is_parsed(self):
        ev = {"meta": '{"host": "nova-core"}', "title": "boom"}
        assert self.mod._host_of(ev) == "nova-core"

    def test_bad_json_string_meta_falls_through(self):
        ev = {"meta": "not json", "title": "ping 10.0.0.4 failed"}
        assert self.mod._host_of(ev) == "10.0.0.4"

    def test_ipv4_192_168_from_text(self):
        ev = {"meta": {}, "title": "host 192.168.7.21 unreachable", "body": ""}
        assert self.mod._host_of(ev) == "192.168.7.21"

    def test_ipv4_10_dot_from_body(self):
        ev = {"title": "alert", "body": "client 10.20.30.40 dropped"}
        assert self.mod._host_of(ev) == "10.20.30.40"

    def test_known_hostname_regex(self):
        ev = {"title": "Office-M4-2 GPU wedged"}
        assert self.mod._host_of(ev) == "Office-M4-2"

    def test_no_host_returns_none(self):
        ev = {"title": "something vague happened", "body": "no host here"}
        assert self.mod._host_of(ev) is None

    def test_missing_meta_key_safe(self):
        # ev with no 'meta' at all must not raise.
        assert self.mod._host_of({"title": "x"}) is None


class TestCosine:
    """_cosine(): similarity math + defensive guards."""

    def setup_method(self):
        self.mod = _load_correlator()

    def test_identical_vectors_is_one(self):
        v = [1.0, 2.0, 3.0]
        assert self.mod._cosine(v, v) == pytest.approx(1.0)

    def test_orthogonal_is_zero(self):
        assert self.mod._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_is_negative_one(self):
        assert self.mod._cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_mismatched_length_is_zero(self):
        assert self.mod._cosine([1.0, 2.0], [1.0]) == 0.0

    def test_empty_or_none_is_zero(self):
        assert self.mod._cosine(None, [1.0]) == 0.0
        assert self.mod._cosine([], []) == 0.0
        assert self.mod._cosine([1.0], None) == 0.0

    def test_zero_vector_is_zero(self):
        assert self.mod._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


class TestIsSymptomOf:
    """_is_symptom_of(): topology dependency lookups."""

    def setup_method(self):
        self.mod = _load_correlator()

    def test_gpu_explains_crash_storm(self):
        assert self.mod._is_symptom_of("gpu", "crash_storm") is True

    def test_gpu_explains_inference(self):
        assert self.mod._is_symptom_of("gpu", "inference") is True

    def test_gpu_does_not_explain_wifi(self):
        assert self.mod._is_symptom_of("gpu", "wifi") is False

    def test_host_down_wildcard_explains_anything(self):
        assert self.mod._is_symptom_of("host_down", "literally_anything") is True

    def test_unknown_root_explains_nothing(self):
        assert self.mod._is_symptom_of("not_a_root", "ollama") is False


# ═══════════════════════════════════════════════════════════════════════════
# correlate() — DB-backed, transaction rolled back
# ═══════════════════════════════════════════════════════════════════════════

class TestCorrelateStandalone:
    """Events that should NOT correlate -> 'standalone', no DB writes."""

    def setup_method(self):
        self.mod = _load_correlator()

    def test_info_level_is_standalone(self, conn, mk_event):
        ev = mk_event(level="info", category="gpu", host="mac-studio")
        res = self.mod.correlate(conn, ev)
        assert res == {"action": "standalone", "incident_id": None,
                       "role": None, "suppress": False}

    def test_no_host_is_standalone(self, conn, mk_event):
        # warning but no resolvable host -> FYI only.
        ev = mk_event(level="warning", category="gpu",
                      title="vague problem", body="nowhere", host=None)
        assert ev["meta"].get("host") is None
        res = self.mod.correlate(conn, ev)
        assert res["action"] == "standalone"
        assert res["suppress"] is False


class TestCorrelateOpensIncident:
    """An unmatched warning/critical opens a NEW incident."""

    def setup_method(self):
        self.mod = _load_correlator()

    def test_unmatched_warning_opens_incident(self, conn, mk_event):
        # embed() is mocked to None so the semantic layer is skipped cleanly.
        with patch.object(self.mod, "embed", return_value=None):
            ev = mk_event(level="warning", category="gpu",
                          title="GPU fell off the bus", host="mac-studio")
            res = self.mod.correlate(conn, ev)
        assert res["action"] == "opened"
        assert res["role"] == "root"
        assert res["suppress"] is False
        assert isinstance(res["incident_id"], int)

        cur = conn.cursor()
        cur.execute("SELECT status, host, severity, root_event, member_count, title "
                    "FROM telemetry.incidents WHERE id=%s", (res["incident_id"],))
        status, host, sev, root_event, mcount, title = cur.fetchone()
        assert status == "open"
        assert host == "mac-studio"
        assert sev == "warning"
        assert root_event == ev["id"]
        assert mcount == 1

        # The originating event is now tagged as the incident root.
        cur.execute("SELECT incident_id, corr_role FROM telemetry.events WHERE id=%s",
                    (ev["id"],))
        inc_id, role = cur.fetchone()
        assert inc_id == res["incident_id"]
        assert role == "root"

    def test_title_truncated_to_200(self, conn, mk_event):
        long_title = "X" * 500
        with patch.object(self.mod, "embed", return_value=None):
            ev = mk_event(level="critical", category="storage",
                          title=long_title, host="unas")
            res = self.mod.correlate(conn, ev)
        cur = conn.cursor()
        cur.execute("SELECT title FROM telemetry.incidents WHERE id=%s",
                    (res["incident_id"],))
        assert len(cur.fetchone()[0]) == 200


class TestCorrelateTopologyFold:
    """Layer 1: a known symptom on the same host folds under the root cause."""

    def setup_method(self):
        self.mod = _load_correlator()

    def test_gpu_root_absorbs_crash_storm_symptom(self, conn, mk_event, open_incident):
        host = "Office-M4-2"
        # Root event with category 'gpu' so the incident's root_cat resolves.
        root_ev = mk_event(level="critical", category="gpu",
                           title="GPU hung", host=host)
        inc_id = open_incident(host, title="GPU hung", severity="critical",
                               root_event=root_ev["id"])
        # New symptom: crash_storm on the SAME host -> folded, suppressed.
        sym = mk_event(level="warning", category="crash_storm",
                       title="ollama crash loop", host=host)
        with patch.object(self.mod, "embed", return_value=None):
            res = self.mod.correlate(conn, sym)
        assert res["action"] == "attached"
        assert res["role"] == "symptom"
        assert res["incident_id"] == inc_id
        assert res["suppress"] is True

        cur = conn.cursor()
        cur.execute("SELECT member_count FROM telemetry.incidents WHERE id=%s", (inc_id,))
        assert cur.fetchone()[0] == 2  # started at 1, +1
        cur.execute("SELECT incident_id, corr_role, status FROM telemetry.events WHERE id=%s",
                    (sym["id"],))
        eic, role, status = cur.fetchone()
        assert eic == inc_id and role == "symptom" and status == "suppressed"

    def test_unrelated_category_does_not_fold(self, conn, mk_event, open_incident):
        host = "mac-mini"
        root_ev = mk_event(level="warning", category="gpu",
                           title="GPU slow", host=host)
        open_incident(host, root_event=root_ev["id"])
        # wifi is NOT a symptom of gpu, and category != gpu -> no topology match.
        # With embed mocked to None the semantic layer can't match either,
        # so this opens its OWN incident instead of attaching.
        wifi = mk_event(level="warning", category="wifi",
                        title="AP flapping", host=host)
        with patch.object(self.mod, "embed", return_value=None):
            res = self.mod.correlate(conn, wifi)
        assert res["action"] == "opened"
        assert res["role"] == "root"


class TestCorrelateTemporalJoin:
    """Layer 1b: same category recurring on the same host joins as a member."""

    def setup_method(self):
        self.mod = _load_correlator()

    def test_same_host_same_category_joins(self, conn, mk_event, open_incident):
        host = "nova-core"
        root_ev = mk_event(level="warning", category="postgres",
                           title="pg restarted", host=host)
        inc_id = open_incident(host, severity="warning", root_event=root_ev["id"])
        # Same category 'postgres' on same host -> member of existing incident.
        again = mk_event(level="warning", category="postgres",
                         title="pg restarted again", host=host)
        with patch.object(self.mod, "embed", return_value=None):
            res = self.mod.correlate(conn, again)
        assert res["action"] == "attached"
        assert res["role"] == "member"
        assert res["incident_id"] == inc_id

    def test_critical_member_escalates_incident_severity(self, conn, mk_event, open_incident):
        host = "synology"
        root_ev = mk_event(level="warning", category="storage",
                           title="disk warm", host=host)
        inc_id = open_incident(host, severity="warning", root_event=root_ev["id"])
        crit = mk_event(level="critical", category="storage",
                        title="disk failing", host=host)
        with patch.object(self.mod, "embed", return_value=None):
            res = self.mod.correlate(conn, crit)
        assert res["action"] == "attached"
        cur = conn.cursor()
        cur.execute("SELECT severity FROM telemetry.incidents WHERE id=%s", (inc_id,))
        assert cur.fetchone()[0] == "critical"  # escalated from warning

    def test_different_host_does_not_join(self, conn, mk_event, open_incident):
        root_ev = mk_event(level="warning", category="storage",
                           title="disk warm", host="synology")
        open_incident("synology", root_event=root_ev["id"])
        # Same category but DIFFERENT host -> must not attach; opens its own.
        other = mk_event(level="warning", category="storage",
                         title="other disk", host="unas")
        with patch.object(self.mod, "embed", return_value=None):
            res = self.mod.correlate(conn, other)
        assert res["action"] == "opened"


class TestCorrelateSemantic:
    """Layer 3: embedding similarity attaches even without a topology rule."""

    def setup_method(self):
        self.mod = _load_correlator()

    def test_semantic_match_attaches(self, conn, mk_event, open_incident):
        host = "mac-studio"
        centroid = [0.1, 0.2, 0.3, 0.4]
        # Open incident whose root category ('memory_ingest') is NOT a topology
        # root and differs from the new event's category, so layers 1/1b miss.
        root_ev = mk_event(level="warning", category="memory_ingest",
                           title="ingest stalled", host=host)
        inc_id = open_incident(host, root_event=root_ev["id"], embedding=centroid)
        new_ev = mk_event(level="warning", category="query_error",
                          title="weird thing", host=host)
        # Return a vector nearly identical to the centroid -> cosine ~1 >= 0.62.
        with patch.object(self.mod, "embed", return_value=[0.1, 0.2, 0.3, 0.41]):
            res = self.mod.correlate(conn, new_ev)
        assert res["action"] == "attached"
        assert res["role"] == "member"
        assert res["incident_id"] == inc_id

    def test_semantic_below_threshold_opens_new(self, conn, mk_event, open_incident):
        host = "mac-studio"
        root_ev = mk_event(level="warning", category="memory_ingest",
                           title="ingest stalled", host=host)
        open_incident(host, root_event=root_ev["id"], embedding=[1.0, 0.0, 0.0])
        new_ev = mk_event(level="warning", category="query_error",
                          title="totally different", host=host)
        # Orthogonal vector -> cosine 0 < 0.62 -> no semantic match -> opens new.
        with patch.object(self.mod, "embed", return_value=[0.0, 1.0, 0.0]):
            res = self.mod.correlate(conn, new_ev)
        assert res["action"] == "opened"


# ═══════════════════════════════════════════════════════════════════════════
# llm_summarize() — Ollama /api/chat MOCKED
# ═══════════════════════════════════════════════════════════════════════════

class TestLlmSummarize:
    """llm_summarize(): /api/chat is mocked; assert clean text + DB write."""

    def setup_method(self):
        self.mod = _load_correlator()

    def _incident_with_members(self, conn, mk_event, open_incident, host="mac-studio"):
        root_ev = mk_event(level="critical", category="gpu",
                           title="GPU hung", body="bus fell off", host=host)
        inc_id = open_incident(host, title="GPU hung", severity="critical",
                               root_event=root_ev["id"], member_count=2)
        # Tie the events to the incident so the summary query finds members.
        cur = conn.cursor()
        cur.execute("UPDATE telemetry.events SET incident_id=%s, corr_role='root' WHERE id=%s",
                    (inc_id, root_ev["id"]))
        sym = mk_event(level="warning", category="crash_storm",
                       title="ollama crash loop", host=host)
        cur.execute("UPDATE telemetry.events SET incident_id=%s, corr_role='symptom' WHERE id=%s",
                    (inc_id, sym["id"]))
        return inc_id

    def test_clean_summary_written(self, conn, mk_event, open_incident):
        inc_id = self._incident_with_members(conn, mk_event, open_incident)
        fake = {"message": {"content":
                "Root cause: GPU dropped off the PCIe bus. Symptoms: ollama crash "
                "loop and stalled inference. Action: power-cycle the GPU host."}}
        with patch.object(self.mod, "_http", return_value=fake) as mhttp:
            txt, model = self.mod.llm_summarize(conn, inc_id)
        # Hit /api/chat, not /api/generate.
        assert mhttp.call_args[0][0] == "/api/chat"
        assert model == self.mod.SUMMARY_MODEL
        assert "Root cause" in txt
        assert "<think>" not in txt

        cur = conn.cursor()
        cur.execute("SELECT summary, llm_model FROM telemetry.incidents WHERE id=%s", (inc_id,))
        summary, llm_model = cur.fetchone()
        assert summary == txt
        assert llm_model == self.mod.SUMMARY_MODEL

    def test_think_block_stripped(self, conn, mk_event, open_incident):
        inc_id = self._incident_with_members(conn, mk_event, open_incident)
        fake = {"message": {"content":
                "<think>let me reason about this for a while...</think>"
                "GPU bus drop caused the crash loop; reboot the host now."}}
        with patch.object(self.mod, "_http", return_value=fake):
            txt, model = self.mod.llm_summarize(conn, inc_id)
        assert "<think>" not in txt
        assert "reason about this" not in txt
        assert txt.startswith("GPU bus drop")

    def test_short_output_falls_back_to_template(self, conn, mk_event, open_incident):
        inc_id = self._incident_with_members(conn, mk_event, open_incident)
        # < 20 chars after strip -> templated fallback, model None, no llm_model write.
        with patch.object(self.mod, "_http", return_value={"message": {"content": "ok"}}):
            txt, model = self.mod.llm_summarize(conn, inc_id)
        assert model is None
        assert "correlated events" in txt  # template text
        cur = conn.cursor()
        cur.execute("SELECT llm_model FROM telemetry.incidents WHERE id=%s", (inc_id,))
        assert cur.fetchone()[0] is None

    def test_llm_error_writes_fallback_summary(self, conn, mk_event, open_incident):
        inc_id = self._incident_with_members(conn, mk_event, open_incident)
        with patch.object(self.mod, "_http", side_effect=OSError("ollama down")):
            txt, model = self.mod.llm_summarize(conn, inc_id)
        assert model is None
        assert "correlated events" in txt
        # On error path the fallback IS persisted to summary (without llm_model).
        cur = conn.cursor()
        cur.execute("SELECT summary, llm_model FROM telemetry.incidents WHERE id=%s", (inc_id,))
        summary, llm_model = cur.fetchone()
        assert summary == txt
        assert llm_model is None

    def test_missing_incident_returns_none(self, conn):
        # An id that cannot exist in the rolled-back transaction.
        txt, model = self.mod.llm_summarize(conn, -999999)
        assert (txt, model) == (None, None)


# ═══════════════════════════════════════════════════════════════════════════
# Cleanup guarantee — prove the suite left ZERO rows behind
# ═══════════════════════════════════════════════════════════════════════════

def test_no_pytest_rows_leaked():
    """Final check: after all rollbacks, no pytest-correlator events (and thus no
    incidents from them) survived. This is the audit that the rollback fixture
    actually protected production state."""
    try:
        import psycopg2
    except ImportError:
        pytest.skip("psycopg2 not installed")
    try:
        c = psycopg2.connect(PG_DSN)
    except Exception as e:
        pytest.skip(f"nova_ops not reachable: {e}")
    try:
        cur = c.cursor()
        cur.execute("SELECT count(*) FROM telemetry.events WHERE source=%s", (TEST_SOURCE,))
        leaked = cur.fetchone()[0]
        assert leaked == 0, f"{leaked} pytest-correlator events leaked into telemetry.events"
    finally:
        c.close()
