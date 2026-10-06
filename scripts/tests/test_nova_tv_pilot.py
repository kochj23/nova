#!/usr/bin/env python3
"""Tests for nova_tv_pilot.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_tv_pilot.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="tv-pilot-test-"))


def _stub_modules():
    cfg = types.ModuleType("nova_config")
    cfg.NOVA_HOST = "127.0.0.1"; cfg.openrouter_api_key = MagicMock(return_value="sk-or-test"); cfg.post_both = MagicMock()
    img = types.ModuleType("nova_image_utils"); img.generate_image = MagicMock(return_value=None)
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock()
    return {"nova_config": cfg, "nova_image_utils": img, "nova_notify": nn}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()), patch.object(Path, "home", classmethod(lambda c: TMP)), \
         patch.dict(os.environ, {"NOVA_FOR_DATE": "2026-10-05"}):
        spec.loader.exec_module(mod)
    return mod


tv = _load("tv_pilot_under_test", SCRIPT)
# Module-level stubs: no git, no OpenRouter, no memory server, no Slack; Hugo + log live in the tempdir.
tv.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=OSError("offline: git stubbed")))
tv.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=tv.urllib.request.Request,
                                                                urlopen=MagicMock(side_effect=OSError("offline"))),
                                  parse=tv.urllib.parse, error=tv.urllib.error)
tv.generate_image = MagicMock(return_value=None)
tv.notify = MagicMock()
assert str(tv.HUGO_ROOT).startswith(str(TMP)) and str(tv.LOG_FILE).startswith(str(TMP))

SCREENPLAY = ("# THE LAST SIGNAL\n\n**Episode:** Pilot\n**Logline:** A radio operator hears the dead.\n\n"
              "FADE IN:\n\nINT. BUNKER - NIGHT\n\n" + "Action line. " * 400 + "\n\nEND OF PILOT")
GENRE = {"name": "Dark Comedy", "tone": "satirical"}


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _llm(content=SCREENPLAY):
    return MagicMock(return_value=_Resp({"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}}))


def _git_ok(returncode=0, pull_rc=0):
    def run(argv, **kw):
        rc = pull_rc if argv[1] == "pull" else (returncode if argv[1] == "push" else 0)
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="boom")
    return MagicMock(side_effect=run)


def _urlopen(memories, llm_content=SCREENPLAY):
    def uo(req, timeout=0):
        url = req if isinstance(req, str) else req.full_url
        if "/recall" in url:
            return _Resp(memories)
        return _llm(llm_content)(req)
    return MagicMock(side_effect=uo)


def _pipeline(memories=None, llm_content=SCREENPLAY, git=None, cover=None):
    mems = memories if memories is not None else [{"text": f"fact {i}"} for i in range(10)]
    uo = _urlopen(mems, llm_content)
    tv.notify = MagicMock(); tv.generate_image = MagicMock(return_value=cover)
    with patch.object(tv.urllib.request, "urlopen", uo), patch.object(tv.subprocess, "run", git or _git_ok()) as g, \
         patch.object(tv.random, "choice", lambda seq: seq[0]), redirect_stdout(io.StringIO()) as out:
        ok = tv.run_pipeline()
    return ok, uo, g, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("sk-or-", SRC)

    def test_api_key_comes_from_nova_config_and_missing_key_aborts(self):
        uo = _llm()
        with patch.object(tv.urllib.request, "urlopen", uo), redirect_stdout(io.StringIO()):
            tv.call_openrouter("s", "u")
        self.assertEqual(uo.call_args[0][0].get_header("Authorization"), "Bearer sk-or-test")
        with patch.object(tv.nova_config, "openrouter_api_key", MagicMock(return_value="")):
            with self.assertRaises(RuntimeError):
                tv.call_openrouter("s", "u")
        uo.assert_called_once()

    def test_git_is_argv_list_never_shell(self):
        g = _git_ok()
        with patch.object(tv.subprocess, "run", g), redirect_stdout(io.StringIO()):
            tv.git_push()
        for c in g.call_args_list:
            self.assertIsInstance(c[0][0], list); self.assertFalse(c[1].get("shell", False))
            self.assertEqual(c[1]["cwd"], tv.HUGO_ROOT)

    def test_slug_is_filesystem_safe(self):
        with patch.object(tv.urllib.request, "urlopen", _llm("# ../../etc/passwd; rm -rf / <b>\n\n" + "x" * 3000)), redirect_stdout(io.StringIO()):
            title, slug, _ = tv.generate_pilot("history", [{"text": "t"}], GENRE)
        self.assertRegex(slug, r"^[a-z0-9-]+$"); self.assertNotIn("/", slug)


class TestPerformance(unittest.TestCase):
    def test_prompt_building_fast_on_10k_memories(self):
        mems = [{"text": "m" * 400} for _ in range(10_000)]
        with patch.object(tv.urllib.request, "urlopen", _llm()), redirect_stdout(io.StringIO()):
            t0 = time.perf_counter()
            for _ in range(20):
                tv.generate_pilot("history", mems, GENRE)
            self.assertLess(time.perf_counter() - t0, 2.0)

    def test_pilot_numbering_fast_with_10k_posts(self):
        d = TMP / "many"; d.mkdir(exist_ok=True)
        for i in range(10_000):
            (d / f"2026-01-01-p{i}.md").write_text("")
        (d / "_index.md").write_text("")
        with patch.object(tv, "CONTENT_DIR", d):
            t0 = time.perf_counter(); n = tv._get_pilot_number()
            self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(n, 10_000)


class TestRetry(unittest.TestCase):
    def test_memory_fetch_fails_open_and_pipeline_tries_fallback_domain_once(self):
        # RETRY GAP: fetch_memories — one urlopen per domain; failure returns [] and the pipeline aborts cleanly
        uo = MagicMock(side_effect=OSError("memory down"))
        tv.notify = MagicMock()
        with patch.object(tv.urllib.request, "urlopen", uo), patch.object(tv.random, "choice", lambda s: s[0]), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(tv.fetch_memories("history"), [])
            self.assertFalse(tv.run_pipeline())
        self.assertEqual(uo.call_count, 3)                                # 1 direct + 2 in the pipeline (primary, fallback)
        self.assertIn("ERROR: Not enough memories", out.getvalue()); tv.notify.assert_not_called()

    def test_openrouter_failure_propagates_once(self):
        # RETRY GAP: call_openrouter — a single urlopen; HTTP failure escapes to __main__'s FATAL handler
        uo = MagicMock(side_effect=OSError("openrouter 502"))
        with patch.object(tv.urllib.request, "urlopen", uo), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                tv.call_openrouter("s", "u")
        self.assertEqual(uo.call_count, 1)

    def test_git_failures_are_swallowed_and_rebase_aborted(self):
        # RETRY GAP: git_push — one attempt; a failed pull --rebase aborts and never pushes, errors never raise
        g = _git_ok(pull_rc=1)
        with patch.object(tv.subprocess, "run", g), redirect_stdout(io.StringIO()) as out:
            tv.git_push()
        cmds = [c[0][0][1] for c in g.call_args_list]
        self.assertEqual(cmds, ["add", "commit", "pull", "rebase"]); self.assertNotIn("push", cmds)
        self.assertIn("Push ABORTED", out.getvalue())
        with patch.object(tv.subprocess, "run", MagicMock(side_effect=OSError("no git"))), redirect_stdout(io.StringIO()) as out:
            tv.git_push()
        self.assertIn("Git push error: no git", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_date_override_is_honored(self):
        self.assertEqual(tv._today_str(), "2026-10-05"); self.assertEqual(tv._now_iso(), "2026-10-05T21:00:00-07:00")

    def test_fetch_memories_handles_list_and_dict_shapes(self):
        with patch.object(tv.urllib.request, "urlopen", MagicMock(return_value=_Resp([{"text": "a"}]))):
            self.assertEqual(tv.fetch_memories("jazz_history", 3), [{"text": "a"}])
        with patch.object(tv.urllib.request, "urlopen", MagicMock(return_value=_Resp({"memories": [1, 2]}))) as uo:
            self.assertEqual(tv.fetch_memories("jazz_history", 3), [1, 2])
        self.assertIn("/recall?q=interesting%20facts%20stories%20details%20about%20jazz%20history&n=3&source=jazz_history", uo.call_args[0][0])
        with patch.object(tv.urllib.request, "urlopen", MagicMock(return_value=_Resp({"results": [9]}))):
            self.assertEqual(tv.fetch_memories("x"), [9])

    def test_generate_pilot_title_extraction(self):
        with patch.object(tv.urllib.request, "urlopen", _llm()), redirect_stdout(io.StringIO()):
            title, slug, sp = tv.generate_pilot("history", [{"text": "t"}, {"content": "c"}, {"nope": 1}], GENRE)
        self.assertEqual((title, slug), ("THE LAST SIGNAL", "the-last-signal")); self.assertEqual(sp, SCREENPLAY)
        with patch.object(tv.urllib.request, "urlopen", _llm("Some Show Name\nmore")), redirect_stdout(io.StringIO()):
            self.assertEqual(tv.generate_pilot("h", [], GENRE)[0], "Some Show Name")
        with patch.object(tv.urllib.request, "urlopen", _llm("# " + "T" * 100)), redirect_stdout(io.StringIO()):
            title, slug, _ = tv.generate_pilot("h", [], GENRE)
        self.assertEqual(len(title), 80); self.assertEqual(len(slug), 60)
        with patch.object(tv.urllib.request, "urlopen", _llm("INT. ROOM - DAY\n*action*")), redirect_stdout(io.StringIO()):
            self.assertEqual(tv.generate_pilot("h", [], GENRE)[0], "Untitled Pilot")

    def test_publish_pilot_frontmatter_and_cover(self):
        cover = TMP / "cover.png"; cover.write_bytes(b"PNG")
        with redirect_stdout(io.StringIO()):
            fp = tv.publish_pilot("The Last Signal", "the-last-signal", SCREENPLAY, "jazz_history", GENRE, str(cover))
        self.assertEqual(fp, tv.CONTENT_DIR / "2026-10-05-the-last-signal.md")
        txt = fp.read_text()
        self.assertIn('title: "📺 The Last Signal"', txt); self.assertIn("date: 2026-10-05T21:00:00-07:00", txt)
        self.assertIn('tags: ["screenplay", "tv", "dark_comedy", "jazz_history"]', txt)
        self.assertIn('description: "A radio operator hears the dead."', txt)
        self.assertIn('image: "/images/pilot/2026-10-05-the-last-signal.png"', txt)
        self.assertTrue((tv.IMAGES_DIR / "2026-10-05-the-last-signal.png").exists())
        self.assertTrue(txt.rstrip().endswith("Source domain: `jazz_history`. Pilot #1.*"))
        with redirect_stdout(io.StringIO()):
            fp2 = tv.publish_pilot("No Cover", "no-cover", "no logline here " * 200, "history", GENRE, None)
        self.assertIn('image: ""', fp2.read_text()); self.assertIn("A dark comedy pilot drawn from Nova's memory archive on history.", fp2.read_text())

    def test_log_writes_to_tempdir(self):
        with redirect_stdout(io.StringIO()):
            tv.log("unit-line")
        self.assertIn("unit-line", tv.LOG_FILE.read_text())


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported_not_reimplemented(self):
        self.assertIn("from nova_image_utils import generate_image", SRC); self.assertIn("from nova_notify import notify", SRC)
        self.assertNotIn("def generate_image", SRC); self.assertNotIn("def notify", SRC)
        self.assertEqual(tv.MEMORY_SERVER, "http://127.0.0.1:18790")

    def test_openrouter_payload_shape(self):
        uo = _llm()
        with patch.object(tv.urllib.request, "urlopen", uo), redirect_stdout(io.StringIO()):
            tv.generate_pilot("world_war_2", [{"text": "fact"}], GENRE)
        req = uo.call_args[0][0]; body = json.loads(req.data)
        self.assertEqual(req.full_url, tv.OPENROUTER_URL); self.assertEqual(body["model"], tv.MODEL)
        self.assertEqual(body["max_tokens"], 16000); self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertIn("Write a 30-minute Dark Comedy TV pilot.", body["messages"][1]["content"])
        self.assertIn("- fact", body["messages"][1]["content"]); self.assertIn("world war 2", body["messages"][1]["content"])
        self.assertEqual(req.get_header("X-title"), "Nova TV Pilot")

    def test_generate_then_publish_chain(self):
        with patch.object(tv.urllib.request, "urlopen", _llm()), redirect_stdout(io.StringIO()):
            title, slug, sp = tv.generate_pilot("history", [{"text": "t"}], GENRE)
            fp = tv.publish_pilot(title, slug, sp, "history", GENRE, None)
        self.assertTrue(fp.name.endswith("-the-last-signal.md")); self.assertIn(SCREENPLAY, fp.read_text())


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_pushes_and_notifies(self):
        cover = TMP / "golden.png"; cover.write_bytes(b"PNG")
        ok, uo, g, out = _pipeline(cover=str(cover))
        self.assertTrue(ok)
        self.assertTrue((tv.CONTENT_DIR / "2026-10-05-the-last-signal.md").exists())
        tv.generate_image.assert_called_once()
        self.assertEqual(tv.generate_image.call_args[1], {"width": 1024, "height": 1024, "section": "art"})
        self.assertIn("'THE LAST SIGNAL'", tv.generate_image.call_args[0][0])
        self.assertEqual([c[0][0][1] for c in g.call_args_list], ["add", "commit", "pull", "push"])
        title, kw = tv.notify.call_args[0][0], tv.notify.call_args[1]
        self.assertEqual(title, 'New TV Pilot: "THE LAST SIGNAL"')
        self.assertIn("Drama • Source: american civil war", kw["body"])
        self.assertIn("https://nova.digitalnoise.net/pilot/2026-10-05-the-last-signal/", kw["body"])
        self.assertEqual((kw["level"], kw["category"]), ("info", "media"))
        self.assertIn('Pipeline complete: "THE LAST SIGNAL"', out)

    def test_short_screenplay_aborts_before_image_and_publish(self):
        ok, uo, g, out = _pipeline(llm_content="# Tiny\n\nFADE IN. FADE OUT.")
        self.assertFalse(ok); self.assertIn("Screenplay too short", out)
        tv.generate_image.assert_not_called(); g.assert_not_called(); tv.notify.assert_not_called()

    def test_fallback_domain_used_when_primary_is_thin(self):
        calls = []
        def uo(req, timeout=0):
            url = req if isinstance(req, str) else req.full_url
            if "/recall" in url:
                calls.append(url); return _Resp([{"text": "f"}] * (8 if "source=history" in url else 2))
            return _llm()(req)
        tv.notify = MagicMock(); tv.generate_image = MagicMock(return_value=None)
        with patch.object(tv.urllib.request, "urlopen", MagicMock(side_effect=uo)), patch.object(tv.subprocess, "run", _git_ok()), \
             patch.object(tv.random, "choice", lambda s: s[0]), redirect_stdout(io.StringIO()) as out:
            self.assertTrue(tv.run_pipeline())
        self.assertEqual(len(calls), 2); self.assertIn("source=history", calls[1])
        self.assertIn("trying fallback", out.getvalue()); self.assertIn("Source: history", tv.notify.call_args[1]["body"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_the_pipeline(self):
        self.assertIn('if __name__ == "__main__":\n    try:\n        success = run_pipeline()', SRC)
        # the real module writes its log under Path.home(); the throwaway process gets a tempdir HOME
        box = TMP / "frame-home"; box.mkdir(exist_ok=True)
        code = ("import sys, types\n"
                "img = types.ModuleType('nova_image_utils'); img.generate_image = lambda *a, **k: None\n"
                "sys.modules['nova_image_utils'] = img\n"
                "import nova_tv_pilot as m\nprint('IMPORT-OK', len(m.GENRES))\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(box)})
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stdout.strip(), "IMPORT-OK 8")
        self.assertFalse((box / "nova-journal").exists())


if __name__ == "__main__":
    unittest.main()
