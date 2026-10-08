#!/usr/bin/env python3
"""7-category tests for the Ideal Reader + verbosity-dial wiring inside nova_journal.py:
publish_hugo runs nova_ideal_reader.edit_article after longform_expand (floor = the profile's
article_length min, no model chooser under pytest, NOVA_IDEAL_READER=0 disables, any failure
publishes the draft as written), and article_length() scales with the verbosity dial.
Offline: publish_hugo writes into a temp HUGO_ROOT; PG, weather, memory and the model are stubbed.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import sys
import tempfile
import time
import types
import unittest
import urllib.request  # noqa: F401  (imported before the scoped load; see test_nova_journal.py)
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_journal.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_journal_ir_test_"))


def _stub_modules():
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    iu = types.ModuleType("nova_image_utils"); iu.generate_image = MagicMock(return_value=None)
    oc = types.ModuleType("nova_ops_context")
    oc.get_full_context = lambda hours=24: {}; oc.format_security_brief = lambda c: ""; oc.format_infra_brief = lambda c: ""
    rs = types.ModuleType("nova_resolve"); rs.resolve_url = lambda svc, path="": f"http://127.0.0.1:0{path}"
    return {"nova_notify": nn, "nova_image_utils": iu, "nova_ops_context": oc, "nova_resolve": rs}


def _load():
    spec = importlib.util.spec_from_file_location("nj_ir7", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()), patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
            patch("urllib.request.urlopen", side_effect=RuntimeError("offline")), \
            patch("subprocess.run", side_effect=RuntimeError("offline")), \
            patch("subprocess.Popen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


nj = _load()
nj.LOG_FILE = TMP / "nova_journal.log"
nj.STATE_FILE = TMP / "journal_state.json"
nj.HUGO_ROOT = TMP / "nova-journal"
nj._TASK_TIMEOUT_CACHE[:] = [None]
nj._LONGFORM_OVERRIDE_CACHE[:] = [[]]

PLAIN = ("The scheduler on the storage host restarted on Tuesday and the queue drained by noon. "
         "Each job that had been waiting picked up where it stopped and finished within the hour. ")
LEAD = "Make no mistake, the backup job failed three nights running on the storage host."
DRAFT = "The queue was quiet all morning.\n\n" + LEAD + " " + PLAIN * 10 + "\n\nIt's all for you, Damien!\n"
SOURCES_TAIL = "\n## Sources & Attribution\n\n- [1] memory abc (ops): the scheduler restarted.\n"


def _lazy_stubs():
    wb = types.ModuleType("nova_weather_blurb"); wb.weather_dateline_line = MagicMock(return_value="")
    am = types.ModuleType("nova_articles_to_memory"); am.remember_article = MagicMock(return_value=True)
    cc = types.ModuleType("nova_claude_code"); cc.claude_env = MagicMock(return_value={})
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    return {"nova_weather_blurb": wb, "nova_articles_to_memory": am, "nova_claude_code": cc,
            "nova_notify": nn, "nova_config": nj.nova_config}


def _publish(title, body, profile=None, section="essays", env=None, ir_patch=None):
    """publish_hugo offline; returns (published text, edit_article mock or None)."""
    import nova_ideal_reader as ir
    patches = [patch.dict(sys.modules, _lazy_stubs()), patch("psycopg2.connect", side_effect=RuntimeError("offline")),
               patch.object(nj, "call_openrouter", side_effect=RuntimeError("no model in tests")),
               patch.object(nj.nova_config, "post_both", side_effect=AssertionError("must not post")),
               patch.object(nj, "longform_expand", side_effect=lambda t, b, *a, **k: b),
               patch.object(ir, "load_darlings", return_value=list(ir.BUILTIN_LEADS)),
               patch.dict(os.environ, env or {})]
    if ir_patch is not None:
        patches.append(patch.object(ir, "edit_article", ir_patch))
    with redirect_stdout(io.StringIO()):
        for p in patches:
            p.start()
        try:
            ok = nj.publish_hugo(title, body, section, ["t"], "d", profile=profile)
        finally:
            for p in reversed(patches):
                p.stop()
    assert ok, "publish_hugo refused"
    md = sorted((nj.HUGO_ROOT / f"content/{section}").glob("*.md"), key=lambda p: p.stat().st_mtime)[-1]
    return md.read_text()


class TestSecurity(unittest.TestCase):
    def test_editor_cannot_add_words_to_published_piece(self):
        text = _publish("Scheduler Restart Notes One", DRAFT + SOURCES_TAIL)
        import nova_ideal_reader as ir
        body = text.split("---", 2)[2]
        self.assertLessEqual(ir._content_words(body) - ir._content_words(DRAFT + SOURCES_TAIL),
                             ir._content_words("Published Thursday October PT AM PM 2026 at"
                                               + " ".join(["monday tuesday wednesday thursday friday saturday sunday",
                                                           "january february march april may june july august",
                                                           "september october november december"])))

    def test_sources_tail_survives_publish_byte_identical(self):
        text = _publish("Scheduler Restart Notes Two", DRAFT + SOURCES_TAIL)
        self.assertTrue(text.rstrip("\n").endswith(SOURCES_TAIL.rstrip("\n")))

    def test_no_model_chooser_under_pytest(self):
        seen = {}

        def fake(title, body, floor_words=None, chooser=None):
            seen["chooser"] = chooser
            return body
        _publish("Scheduler Restart Notes Three", DRAFT * 4, ir_patch=fake)
        self.assertIsNone(seen["chooser"])


class TestPerformance(unittest.TestCase):
    def test_long_piece_edit_inside_publish_is_fast(self):
        big = "The queue was quiet.\n\n" + LEAD + " " + PLAIN * 300
        t = time.perf_counter()
        _publish("Scheduler Restart Long Form", big)
        self.assertLess(time.perf_counter() - t, 5.0)

    def test_verbosity_scale_reads_dial_once_per_call(self):
        with patch("nova_voice.dial_scale", return_value=1.0) as ds:
            nj.article_length("essay")
        self.assertEqual(ds.call_count, 1)


class TestRetry(unittest.TestCase):
    def test_editor_exception_publishes_draft(self):
        text = _publish("Scheduler Restart Notes Four", DRAFT, ir_patch=MagicMock(side_effect=RuntimeError("boom")))
        self.assertIn("Make no mistake,", text)

    def test_dial_store_down_falls_back_to_table(self):
        with patch("nova_voice.dial_scale", side_effect=RuntimeError("pg down")):
            self.assertEqual(nj._verbosity_scale(), 1.0)
            self.assertEqual(nj.article_length("essay"), nj.ARTICLE_LENGTH["essay"])

    def test_chooser_path_uses_retrying_cli(self):
        # the only model call the editor can make is call_openrouter, which retries 3x with backoff
        self.assertIn("_attempts = 3", SRC)
        self.assertIn("from nova_ideal_reader import edit_article, claude_chooser", SRC)


class TestUnit(unittest.TestCase):
    def test_article_length_scaling(self):
        row = nj.ARTICLE_LENGTH["essay"]
        with patch("nova_voice.dial_scale", return_value=1.4):
            self.assertEqual(nj.article_length("essay"), (int(row[0] * 1.4), int(row[1] * 1.4), row[2]))
        with patch("nova_voice.dial_scale", return_value=1.0):
            self.assertIs(nj.article_length("essay"), row)
        self.assertIsNone(nj.article_length(None))
        self.assertIsNone(nj.article_length("no-such-profile"))


class TestIntegration(unittest.TestCase):
    def test_floor_is_profile_min(self):
        seen = {}

        def fake(title, body, floor_words=None, chooser=None):
            seen["floor"] = floor_words
            return body
        with patch("nova_voice.dial_scale", return_value=1.0):
            _publish("Scheduler Restart Essay Floor", DRAFT, profile="essay", ir_patch=fake)
            self.assertEqual(seen["floor"], nj.ARTICLE_LENGTH["essay"][0])
            _publish("Scheduler Restart No Profile", DRAFT, profile=None, ir_patch=fake)
            self.assertIsNone(seen["floor"])

    def test_editor_runs_after_longform_expand(self):
        i_exp = SRC.index("body = longform_expand(title, body")
        i_ir = SRC.index("from nova_ideal_reader import edit_article")
        i_dl = SRC.index("weather_dateline_line() + body")
        self.assertLess(i_exp, i_ir)
        self.assertLess(i_ir, i_dl)


class TestFunctional(unittest.TestCase):
    def test_golden_lead_in_stripped_damien_kept(self):
        text = _publish("Scheduler Restart Golden Path", DRAFT + SOURCES_TAIL)
        self.assertNotIn("Make no mistake", text)
        self.assertIn("The backup job failed three nights running", text)
        self.assertIn("It's all for you, Damien!", text)

    def test_kill_switch_publishes_as_written(self):
        text = _publish("Scheduler Restart Kill Switch", DRAFT, env={"NOVA_IDEAL_READER": "0"})
        self.assertIn("Make no mistake,", text)


class TestFrame(unittest.TestCase):
    def test_module_loads_offline_and_exposes_hooks(self):
        for name in ("publish_hugo", "article_length", "_verbosity_scale", "longform_expand"):
            self.assertTrue(callable(getattr(nj, name)), name)
        import nova_ideal_reader as ir
        self.assertTrue(callable(ir.edit_article))


if __name__ == "__main__":
    unittest.main()
