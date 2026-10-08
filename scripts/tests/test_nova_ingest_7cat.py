#!/usr/bin/env python3
"""7-category gap tests for nova_ingest.auto_select_vector (2026-10-06 change: whole-word keyword
matching, semantic fallback trusted only on a 4/7 majority) plus the recall retry added with it.
The rest of nova_ingest is covered by test_nova_ingest.py. The memory server is mocked.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_ingest_7cat.py
"""
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_ingest as NI  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def recall(*sources, wrap=True):
    mems = [{"source": s, "text": "x"} for s in sources]
    return _Resp({"memories": mems} if wrap else mems)


class _Base(unittest.TestCase):
    def setUp(self):
        p1 = mock.patch.object(NI, "log"); self.log = p1.start(); self.addCleanup(p1.stop)
        p2 = mock.patch.object(NI.time, "sleep"); self.sleep = p2.start(); self.addCleanup(p2.stop)


class TestSecurity(_Base):
    def test_query_is_url_encoded(self):
        with mock.patch.object(NI.urllib.request, "urlopen", return_value=recall()) as u:
            NI.auto_select_vector("a&n=9999&admin=1 #x", "", ["cooking"])
        url = u.call_args.args[0]
        self.assertTrue(url.endswith("&n=7"))
        self.assertNotIn("&admin=1", url)

    def test_vector_names_are_regex_escaped(self):
        with mock.patch.object(NI.urllib.request, "urlopen", return_value=recall()):
            self.assertEqual(NI.auto_select_vector("c++ (lang)", "", ["c++_(lang)", "[a-z]*"]), "c_lang")

    def test_derived_vector_is_safe_identifier(self):
        with mock.patch.object(NI.urllib.request, "urlopen", return_value=recall()):
            v = NI.auto_select_vector("'; DROP x; -- ../etc", "", ["cooking"])
        self.assertRegex(v, r"^[a-z0-9_]+$")

    def test_sample_truncated_before_leaving_process(self):
        with mock.patch.object(NI.urllib.request, "urlopen", return_value=recall()) as u:
            NI.auto_select_vector("Topic", "secret " * 1000, ["cooking"])
        self.assertLess(len(u.call_args.args[0]), 4000)


class TestPerformance(_Base):
    def test_recall_has_timeout_and_bounded_retries(self):
        with mock.patch.object(NI.urllib.request, "urlopen", side_effect=OSError("down")) as u:
            NI.auto_select_vector("Volcanoes", "", ["cooking"])
        self.assertEqual(u.call_count, 3)
        self.assertTrue(all(c.kwargs["timeout"] == 8 for c in u.call_args_list))
        self.assertLessEqual(sum(c.args[0] for c in self.sleep.call_args_list), 5)

    def test_keyword_scoring_many_vectors_fast(self):
        vecs = [f"vector_{i}_topic" for i in range(3000)] + ["horology"]
        t = time.perf_counter()
        self.assertEqual(NI.auto_select_vector("Horology", "omega watches", vecs), "horology")
        self.assertLess(time.perf_counter() - t, 2.0)

    def test_keyword_hit_skips_network(self):
        with mock.patch.object(NI.urllib.request, "urlopen") as u:
            NI.auto_select_vector("Marine Biology", "", ["marine_biology"])
        u.assert_not_called()


class TestRetry(_Base):
    def test_recall_retried_then_majority_used(self):
        with mock.patch.object(NI.urllib.request, "urlopen",
                               side_effect=[OSError("restart"), recall(*["travel"] * 5, "music", "news")]) as u:
            self.assertEqual(NI.auto_select_vector("Seattle trip", "", ["cooking"]), "travel")
        self.assertEqual(u.call_count, 2)
        self.sleep.assert_called_once_with(1)

    def test_final_failure_logged_not_silent(self):
        with mock.patch.object(NI.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(NI.auto_select_vector("Volcanoes", "", ["cooking"]), "volcanoes")
        self.assertTrue(any("failed after 3 tries" in c.args[0] for c in self.log.call_args_list))

    def test_bad_json_retried(self):
        bad = mock.MagicMock(); bad.__enter__.return_value = bad; bad.read.return_value = b"<html>"
        with mock.patch.object(NI.urllib.request, "urlopen", side_effect=[bad, recall(*["film"] * 4)]):
            self.assertEqual(NI.auto_select_vector("Movie night", "", ["cooking"]), "film")


class TestUnit(_Base):
    def test_whole_word_only_no_substring_hits(self):
        # "part", "start", "the man", "fired" must not file car content under art / he_man / fire
        with mock.patch.object(NI.urllib.request, "urlopen", return_value=recall()):
            v = NI.auto_select_vector("Truck build part 3", "we started; the man fired up the engine",
                                      ["art", "he_man", "fire"])
        self.assertEqual(v, "truck_build_part")

    def test_full_name_match_wins(self):
        self.assertEqual(NI.auto_select_vector("x", "all about marine biology", ["marine_biology", "biology_lab"]),
                         "marine_biology")

    def test_short_words_ignored_for_partial_score(self):
        with mock.patch.object(NI.urllib.request, "urlopen", return_value=recall()):
            self.assertEqual(NI.auto_select_vector("The car", "", ["car_art"]), "the_car")

    def test_three_of_seven_is_not_a_majority(self):
        with mock.patch.object(NI.urllib.request, "urlopen",
                               return_value=recall("a", "a", "a", "b", "b", "c", "d")):
            self.assertEqual(NI.auto_select_vector("Odd Topic", "", ["cooking"]), "odd_topic")

    def test_four_of_seven_is(self):
        with mock.patch.object(NI.urllib.request, "urlopen",
                               return_value=recall("a", "a", "a", "a", "b", "c", "d", wrap=False)):
            self.assertEqual(NI.auto_select_vector("Odd Topic", "", ["cooking"]), "a")

    def test_malformed_recall_shapes_tolerated(self):
        for payload in (42, "str", {"memories": ["x", None, {"no": "source"}]}):
            with mock.patch.object(NI.urllib.request, "urlopen", return_value=_Resp(payload)):
                self.assertEqual(NI.auto_select_vector("Odd Topic", "", ["cooking"]), "odd_topic")

    def test_no_existing_vectors_derives(self):
        self.assertEqual(NI.auto_select_vector("Hello World Again Twice", "", []), "hello_world_again")


class TestIntegration(_Base):
    def test_yt_subs_audio_uses_classifier_not_auto_select(self):
        src = (SCRIPTS / "nova_yt_subs_audio.py").read_text()
        self.assertNotIn("auto_select_vector", src)

    def test_selected_vector_logged(self):
        NI.auto_select_vector("x", "great cooking tips", ["cooking"])
        self.assertTrue(any("Auto-selected vector: 'cooking'" in c.args[0] for c in self.log.call_args_list))


class TestFunctional(_Base):
    def test_golden_keyword_semantic_and_derive_paths(self):
        existing = ["automotive", "horology", "music"]
        self.assertEqual(NI.auto_select_vector("Automotive", "", existing), "automotive")
        with mock.patch.object(NI.urllib.request, "urlopen", return_value=recall(*["horology"] * 6, "music")):
            self.assertEqual(NI.auto_select_vector("Omega Seamaster review", "", existing), "horology")
        with mock.patch.object(NI.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(NI.auto_select_vector("Seattle Travel Vlog", "", existing), "seattle_travel_vlog")


class TestFrame(unittest.TestCase):
    def test_callable(self):
        self.assertTrue(callable(NI.auto_select_vector) and callable(NI._derive))

    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPTS / "nova_ingest.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
