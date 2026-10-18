"""Tests for nova_ingest_scripture.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import nova_ingest_scripture as S  # noqa: E402

SRC = (SCRIPTS / "nova_ingest_scripture.py").read_text()

HYMN = """<HTML><HEAD><TITLE>Rig Veda: Rig-Veda Book 1: HYMN I. Agni.</TITLE></HEAD><BODY>
<CENTER><A HREF="../index.htm">Hinduism</A> <A HREF="rv01002.htm">Next</A></CENTER><HR>
<P ALIGN="CENTER"><FONT SIZE="-1" COLOR="GREEN"><I>Rig Veda</I>, tr. by Ralph T.H. Griffith, [1896], at sacred-texts.com</FONT></P><HR>
<h3>HYMN I. Agni.</h3>
<p>1 I Laud Agni, the chosen Priest, God, minister of sacrifice,<br> The hotar, lavishest of wealth.<A NAME="fr_1"></A><A HREF="#fn_1"><FONT SIZE="1">1</FONT></A><br>
<A NAME="page_2"><FONT SIZE="1" COLOR="GREEN">p. 2</FONT></A>2 Worthy is Agni to be praised by living as by ancient seers.</p>
<h3 ALIGN="CENTER">Footnotes</h3><p>1 A commentator's note that must not be stored.</p>
<p><HR><CENTER><A HREF="rv01002.htm">Next: HYMN II.</A></CENTER></BODY></HTML>"""

INDEX = """<HTML><HEAD><TITLE>Rig Veda Index</TITLE></HEAD><BODY>
<A HREF="../index.htm">Hinduism</A> <A HREF="http://example.com/x.htm">ext</A>
<A HREF="rv01001.htm">HYMN I. Agni</A><BR><A HREF="rv01002.htm">HYMN II. Vayu</A><BR>
<A HREF="rv01003.htm">HYMN III. Asvins</A><BR><A HREF="rv01001.htm">dup</A><A HREF="pageidx.htm">Page Index</A>
</BODY></HTML>"""


class TestSecurity(unittest.TestCase):
    def test_no_credentials_in_source(self):
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_every_work_has_free_license_and_cache_off_main_ssd(self):
        for w in S.WORKS:
            self.assertTrue(w["license"].startswith(("Public domain", "CC0")), w["key"])
        self.assertTrue(str(S.CACHE).startswith("/Volumes/Data/"))

    def test_external_and_parent_links_never_followed(self):
        links = S.local_links(INDEX, "hin/rigveda/index.htm")
        self.assertTrue(all(l.startswith("hin/rigveda/") for l in links))


class TestPerformance(unittest.TestCase):
    def test_split_one_megabyte_fast(self):
        t0 = time.time()
        out = S.split_passages(("A sentence of scripture here. " * 40 + "\n\n") * 900)
        self.assertLess(time.time() - t0, 3.0)
        self.assertGreater(len(out), 500)


class TestRetry(unittest.TestCase):
    def test_http_get_retries_then_succeeds(self):
        calls = []

        def fake(req, timeout=90):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("reset")
            return type("R", (), {"read": lambda self: b"ok"})()
        with mock.patch.object(S, "log"):
            self.assertEqual(S.http_get("https://x.test/a", min_gap=0, _sleep=lambda s: None, _open=fake), b"ok")
        self.assertEqual(len(calls), 3)

    def test_challenge_pages_rejected(self):
        self.assertTrue(S.is_challenge("<title>Just a moment...</title>"))
        self.assertTrue(S.is_challenge("<link href='/content/plugins/woocommerce/x.css'>"))
        self.assertFalse(S.is_challenge(HYMN))


class TestUnit(unittest.TestCase):
    def test_page_text_strips_nav_pagenos_footnotes(self):
        t = S.page_text(HYMN)
        self.assertIn("I Laud Agni", t)
        self.assertIn("Worthy is Agni", t)
        for bad in ("Footnotes", "commentator", "p. 2", "Next", "sacred-texts.com"):
            self.assertNotIn(bad, t)

    def test_title_and_index_detection(self):
        self.assertEqual(S.page_title(HYMN), "Rig-Veda Book 1: HYMN I. Agni.")
        self.assertEqual(S.page_title("<title>I Ching: XV. The <I>Kh</I>ien Hexagram</title>"), "XV. The Khien Hexagram")
        self.assertTrue(S.is_index_page(INDEX, S.page_text(INDEX)))
        self.assertFalse(S.is_index_page(HYMN, S.page_text(HYMN)))

    def test_local_links_ordered_deduped(self):
        self.assertEqual(S.local_links(INDEX, "hin/rigveda/index.htm")[:3],
                         ["hin/rigveda/rv01001.htm", "hin/rigveda/rv01002.htm", "hin/rigveda/rv01003.htm"])

    def test_split_passages_respects_size(self):
        parts = S.split_passages("\n\n".join(["word " * 100] * 20))
        self.assertTrue(all(len(p) <= S.CHUNK + 5 for p in parts))
        self.assertEqual(" ".join(" ".join(parts).split()), " ".join(("word " * 2000).split()))

    def test_split_units_parent_and_continuation(self):
        txt = "Preface stuff\nBOOK I\nCHAPTER I\n" + "x " * 60 + "\nCHAPTER II\n" + "y " * 60
        u = S.split_units(txt, r"^((BOOK|CHAPTER) [IVX]+)$", parent_re=r"^BOOK ")
        self.assertEqual([h for h, _ in u], ["BOOK I, CHAPTER I", "BOOK I, CHAPTER II"])
        cont = S.split_units("z " * 60, r"^(Book [IVX]+)$", initial="Book III")
        self.assertEqual(cont[0][0], "Book III")

    def test_gzip_bodies_decoded(self):
        self.assertEqual(S.decode(gzip.compress("Ō".encode())), "Ō")

    def test_build_items_metadata_and_uid(self):
        w = S.BY_KEY["rig_veda"]
        items = S.build_items(w, "rv01001", "HYMN I. Agni.", "text " * 50, {"mandala": 1, "hymn": 1})
        uid, text, meta = items[0]
        self.assertEqual(uid, "rig_veda:rv01001:1")
        self.assertTrue(text.startswith("[Rig Veda — HYMN I. Agni. (Ralph T. H. Griffith 1896)] "))
        for k in ("tradition", "work", "translator", "year", "license", "type"):
            self.assertIn(k, meta)
        self.assertEqual(meta["type"], "scripture")

    def test_sutta_units(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "mn1_translation-en-sujato.json"
            p.write_text(json.dumps({"mn1:0.1": "Middle Discourses 1", "mn1:0.2": "The Root of All Things",
                                     "mn1:1.1": "So I have heard.", "mn1:1.2": "At one time"}))
            uid, title, text = S.sutta_units(p)
        self.assertEqual((uid, title), ("mn1", "The Root of All Things"))
        self.assertEqual(text, "So I have heard.\nAt one time")


class TestIntegration(unittest.TestCase):
    def test_uses_shared_ingest_remember_and_tradition_vectors(self):
        self.assertIn("import nova_ingest as ni", SRC)
        self.assertEqual({w["vector"] for w in S.WORKS},
                         {"taoism", "shinto", "hinduism", "buddhism", "confucianism", "jainism", "sikhism",
                          "zoroastrianism"})
        self.assertEqual(len(S.BY_KEY), len(S.WORKS))


class TestFunctional(unittest.TestCase):
    def test_crawl_index_to_hymns_skips_known_and_missing(self):
        pages = {"hin/rigveda/index.htm": INDEX, "hin/rigveda/rv01001.htm": HYMN,
                 "hin/rigveda/rv01002.htm": HYMN.replace("HYMN I. Agni", "HYMN II. Vayu"), "hin/rigveda/rv01003.htm": None}
        with mock.patch.object(S, "log"):
            got = list(S.crawl_st(S.BY_KEY["rig_veda"], fetch=pages.get))
        self.assertEqual([p for p, _t, _x in got], ["hin/rigveda/rv01001.htm", "hin/rigveda/rv01002.htm"])

    def test_run_work_resumes_by_uid(self):
        w = S.BY_KEY["rig_veda"]
        sent = []
        fake_units = [("rv01001", "HYMN I", "a " * 60, {}), ("rv01002", "HYMN II", "b " * 60, {})]
        fake_ni = mock.MagicMock()
        fake_ni.remember.side_effect = lambda t, s, m, d: sent.append(m["uid"]) or True
        with mock.patch.dict(sys.modules, {"nova_ingest": fake_ni}), \
             mock.patch.object(S, "units", return_value=iter(fake_units)), \
             mock.patch.object(S, "existing_uids", return_value={"rig_veda:rv01001:1"}), \
             mock.patch.object(S, "manifest_upsert"), mock.patch.object(S, "memory_count", return_value=2), \
             mock.patch.object(S, "queue_depth", return_value=0), mock.patch.object(S.time, "sleep"), \
             mock.patch.object(S, "log"):
            res = S.run_work(w)
        self.assertEqual(sent, ["rig_veda:rv01002:1"])
        self.assertEqual(res["skipped"], 1)


class TestFrame(unittest.TestCase):
    def test_list_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ingest_scripture.py"), "list"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("rig_veda", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)


if __name__ == "__main__":
    unittest.main()
