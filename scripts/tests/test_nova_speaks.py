#!/usr/bin/env python3
"""Tests for nova_speaks.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
XTTS (load_tts), ffmpeg/ffprobe (subprocess.run), PIL card rendering and notify are mocked; HOME and
OUT_DIR are tempdirs. No audio is synthesized and nothing is uploaded or posted."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_speaks.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_speaks_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ns = _load()
ns.log = lambda m: None
import nova_notify  # noqa: E402
import nova_speaks_narration as nn  # noqa: E402
nn.log = lambda m: None

MD = """---
title: "🎙️ Machines That Listen: An Essay"
image: "/images/essays/other.webp"
---
*Published 2026-01-01*

Opening paragraph long enough to be read aloud by the voice.

## Chapter One

A [linked](https://x.io) paragraph with a citation [3] and **bold** words in it.

| a | b |

```
code never read aloud at all here
```

short

## References

1. Something long enough to be a paragraph but in references.
"""


class _Tts:
    def __init__(self):
        self.calls = []

    def tts_to_file(self, text, language, file_path, **kw):
        self.calls.append((text, kw))
        Path(file_path).write_bytes(b"RIFF")


def _proc(stdout="2.0"):
    return SimpleNamespace(stdout=stdout, returncode=0, stderr="")


class TestSecurity(unittest.TestCase):
    def test_no_credentials_and_argv_lists(self):
        self.assertIsNone(re.search(r"(password|api[_-]?key|token)\s*=\s*['\"]", SRC, re.I))
        self.assertNotIn("shell=True", SRC)

    def test_urls_and_code_never_spoken(self):
        text = " ".join(p for _, ps in ns.article_to_script(MD, None) for p in ps)
        self.assertNotIn("https://", text)
        self.assertNotIn("code never read", text)
        self.assertNotIn("[3]", text)

    def test_nothing_uploaded(self):
        self.assertNotIn("youtube.com/upload", SRC.lower())
        self.assertNotIn("googleapiclient", SRC)


class TestPerformance(unittest.TestCase):
    def test_script_from_large_article_fast(self):
        md = "---\ntitle: x\n---\n" + "\n\n".join(
            (f"## H{i}" if i % 10 == 0 else f"Paragraph {i} with enough words to count as speech.") for i in range(10_000))
        t0 = time.perf_counter()
        ch = ns.article_to_script(md, None)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(ch), 1000)


class TestRetry(unittest.TestCase):
    def test_ffmpeg_failure_is_loud_not_silent(self):
        # RETRY GAP: speak()/clip()/to_png() — ffmpeg runs once with check=True; failure raises (hand-run job)
        with patch.object(ns.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                ns.to_png("a.webp", "b.png")
        self.assertEqual(run.call_count, 1)

    def test_notify_failure_is_swallowed(self):
        with _Main() as m, patch.object(nova_notify, "notify", side_effect=RuntimeError("slack")):
            m.run()
        self.assertTrue(m.out.endswith(".mp4"))


class TestUnit(unittest.TestCase):
    def test_article_to_script_chapters(self):
        ch = ns.article_to_script(MD, None)
        self.assertEqual([h for h, _ in ch], ["Opening", "Chapter One"])
        # short paragraphs are kept now (2026-10-06): the narration stage merges them into a neighbour
        self.assertEqual(ch[1][1], ["A linked paragraph with a citation and bold words in it.", "short"])

    def test_sections_filter(self):
        self.assertEqual([h for h, _ in ns.article_to_script(MD, ["chapter"])], ["Chapter One"])
        self.assertEqual(ns.article_to_script("", None), [])

    def test_wrap(self):
        self.assertEqual(ns.wrap("aa bb cc dd", 5), ["aa bb", "cc dd"])
        self.assertEqual(ns.wrap("", 5), [])

    def test_speak_splits_long_text_under_limit(self):
        tts = _Tts()
        with tempfile.TemporaryDirectory() as d, patch.object(ns.subprocess, "run", return_value=_proc("7.5")) as run:
            dur = ns.speak(tts, ("word " * 60 + "; ") * 10 + ".", str(Path(d) / "x.wav"))
        self.assertEqual(dur, 7.5)
        self.assertGreater(len(tts.calls), 1)
        self.assertTrue(all(len(t) <= 400 for t, _ in tts.calls))
        self.assertTrue(any(c[0][0][0] == "ffmpeg" and "concat" in c[0][0] for c in run.call_args_list))


class TestIntegration(unittest.TestCase):
    def test_voice_name_vs_reference_wav(self):
        tts = _Tts()
        with tempfile.TemporaryDirectory() as d, patch.object(ns.subprocess, "run", return_value=_proc()):
            ns.speak(tts, "Short sentence.", str(Path(d) / "a.wav"))
            self.assertEqual(tts.calls[-1][1], {"speaker": ns.VOICE, **nn.XTTS_KW})
            with patch.object(ns, "VOICE", "/tmp/ref.wav"):
                ns.speak(tts, "Short sentence.", str(Path(d) / "b.wav"))
        self.assertEqual(tts.calls[-1][1], {"speaker_wav": "/tmp/ref.wav", **nn.XTTS_KW})

    def test_clip_overlay_builds_filter_complex(self):
        with patch.object(ns.subprocess, "run") as run:
            ns.clip("i.png", 2.0, "o.mp4", overlay="lt.png")
        cmd = run.call_args[0][0]
        self.assertIn("-filter_complex", cmd)
        self.assertEqual(cmd[-1], "o.mp4")


class _Main:
    def __init__(self, extra=()):
        self.extra = list(extra)

    def __enter__(self):
        self.home = Path(tempfile.mkdtemp())
        self.st = ExitStack()
        art = self.home / "content" / "essays"
        art.mkdir(parents=True)
        self.article = art / "2026-01-01-machines.md"
        self.article.write_text(MD)
        p = lambda n, **kw: self.st.enter_context(patch.object(ns, n, **kw))
        p("HOME", new=self.home)
        p("OUT_DIR", new=self.home / "review")
        self.tts = p("load_tts", return_value=_Tts())
        self.speak = p("speak", side_effect=lambda t, txt, path, *a, **k: Path(path).write_bytes(b"w"))
        # narration stage offline: no PG cache, no LLM, seed phrasebook, no Whisper
        q = lambda n, **kw: self.st.enter_context(patch.object(nn, n, **kw))
        q("RewriteCache", return_value=None)
        q("load_phrasebook", return_value=nn.SEED_PHRASEBOOK)
        q("llm", side_effect=TimeoutError("offline"))
        q("BackCheck", return_value=SimpleNamespace(backend="none"))
        self.to_png = p("to_png")
        self.clip = p("clip")
        self.card = p("card")
        p("lower_third", side_effect=lambda text, path: Path(path).write_bytes(b"p"))
        self.run_ = self.st.enter_context(patch.object(ns.subprocess, "run", return_value=_proc("3.0")))
        self.st.enter_context(patch.object(ns.os.path, "getsize", return_value=5_000_000))
        self.notify = self.st.enter_context(patch.object(nova_notify, "notify"))
        return self

    def run(self):
        argv = ["x", "--article", str(self.article), "--images", "a.png", "b.png", "c.png", *self.extra]
        with patch.object(sys, "argv", argv), patch("builtins.print") as pr:
            ns.main()
        self.out = pr.call_args[0][0]

    def __exit__(self, *a):
        self.st.close()
        shutil.rmtree(self.home, ignore_errors=True)


class TestFunctional(unittest.TestCase):
    def test_golden_path_renders_and_notifies(self):
        with _Main(["--suffix", "preview"]) as m:
            m.run()
        self.assertTrue(m.out.endswith("review/NovaSpeaks-2026-01-01-machines-preview.mp4"))
        self.assertEqual(m.speak.call_count, 2)
        self.assertEqual(m.to_png.call_count, 3)
        self.assertEqual(m.clip.call_count, 4)                 # title + 2 paragraphs + end
        title_card = m.card.call_args_list[0][0]
        self.assertEqual(title_card[:2], ("NOVA SPEAKS", "Machines That Listen"))
        self.assertIn("https://nova.digitalnoise.net/essays/2026-01-01-machines/", m.card.call_args_list[1][0][1])
        self.assertEqual(m.notify.call_args.kwargs["dedup_key"], "nova-speaks:2026-01-01-machines:preview")

    def test_missing_title_is_an_error(self):
        with _Main() as m:
            m.article.write_text("no frontmatter here")
            with self.assertRaises(AttributeError):
                m.run()
        m.tts.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_clean(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--article", r.stdout)
        r = subprocess.run([sys.executable, "-c", "import nova_speaks"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))


if __name__ == "__main__":
    unittest.main()
