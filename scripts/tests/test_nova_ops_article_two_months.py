#!/usr/bin/env python3
"""Tests for nova_ops_article_two_months.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script is a one-off that runs top-to-bottom at import (two LLM passes, title, cover image, Hugo
publish, git push, Slack DM). Every test executes it under a fake $HOME with all four collaborator
modules stubbed, and inspects what the stubs received."""
import datetime as dt
import importlib.util
import io
import itertools
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
SCRIPT = SCRIPTS / "nova_ops_article_two_months.py"
SRC = SCRIPT.read_text()
_seq = itertools.count()


class _Stubs:
    def __init__(self, part1, part2, title, image, image_exc, publish_exc):
        self.nj = types.ModuleType("nova_journal")
        self.nj.publish_hugo = MagicMock(side_effect=publish_exc)
        self.nj.git_push = MagicMock(); self.nj.notify_slack = MagicMock()
        self.nj.generate_image = MagicMock(return_value=image, side_effect=image_exc)
        self.nj.call_openrouter = MagicMock(return_value=title)
        self.nv = types.ModuleType("nova_voice")
        self.nv.CONTEXT_JOURNAL_OPS = "[ops-context]"
        self.nv.system_prompt = lambda ctx: "SYS:" + ctx
        self.rd = types.ModuleType("nova_rando_daily_ops")
        self.rd.call_llm = MagicMock(side_effect=[part1, part2])
        self.cfg = types.ModuleType("nova_config")
        self.cfg.post_both = MagicMock(); self.cfg.JORDAN_DM = "D_JORDAN"

    def modules(self):
        return {"nova_journal": self.nj, "nova_voice": self.nv, "nova_rando_daily_ops": self.rd, "nova_config": self.cfg}


def _run(home, part1="part one text", part2="part two text", title="Two Months of Chaos", image="/img/c.png",
         image_exc=None, publish_exc=None):
    """Execute the script once under fake $HOME `home`; returns (module, stubs, stdout, log text)."""
    (Path(home) / ".openclaw/logs").mkdir(parents=True, exist_ok=True)
    st = _Stubs(part1, part2, title, image, image_exc, publish_exc)
    spec = importlib.util.spec_from_file_location(f"ops2m_{next(_seq)}", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    out = io.StringIO()
    with patch.dict(sys.modules, st.modules()), patch("pathlib.Path.home", return_value=Path(home)), redirect_stdout(out):
        spec.loader.exec_module(mod)
    log = (Path(home) / ".openclaw/logs/ops_article_two_months.log").read_text()
    return mod, st, out.getvalue(), log


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("subprocess", SRC); self.assertNotIn("os.system", SRC); self.assertNotIn("shell=True", SRC)

    def test_hostile_title_is_slugged_before_it_reaches_the_url(self):
        with tempfile.TemporaryDirectory() as td:
            _, st, _, _ = _run(td, title='"; rm -rf / # <script>Evil Title</script>')
        msg, kw = st.cfg.post_both.call_args.args[0], st.cfg.post_both.call_args.kwargs
        url = msg.split()[-1]
        self.assertRegex(url, r"^https://nova\.digitalnoise\.net/operations/\d{4}-\d\d-\d\d-[a-z0-9-]{1,60}/$")
        self.assertEqual(kw["slack_channel"], "D_JORDAN")          # the DM, never a public channel

    def test_dm_is_the_only_direct_post(self):
        self.assertEqual(SRC.count("post_both("), 1)
        self.assertIn("slack_channel=nova_config.JORDAN_DM", SRC)


class TestPerformance(unittest.TestCase):
    def test_stitching_two_10k_word_parts_is_fast(self):
        p1 = " ".join(f"w{i}" for i in range(10_000)); p2 = " ".join(f"v{i}" for i in range(10_000))
        with tempfile.TemporaryDirectory() as td:
            t0 = time.perf_counter()
            _, st, _, _ = _run(td, part1=p1, part2=p2, title="T " * 500)
            self.assertLess(time.perf_counter() - t0, 2.0)
        body = st.nj.publish_hugo.call_args.args[1]
        self.assertEqual(len(body.split()), 20_001)                 # the "---" separator counts as a word
        self.assertIn("~20001 words", st.nj.publish_hugo.call_args.args[4])


class TestRetry(unittest.TestCase):
    def test_llm_returning_none_is_not_retried_and_still_publishes(self):
        # RETRY GAP: call_llm — each part is requested exactly once; None collapses to "" and the stitch goes out
        with tempfile.TemporaryDirectory() as td:
            _, st, _, log = _run(td, part1=None, part2=None)
        self.assertEqual(st.rd.call_llm.call_count, 2)
        self.assertEqual(st.nj.publish_hugo.call_args.args[1], "\n\n---\n\n")
        self.assertIn("part1: 0 chars", log)

    def test_title_failure_falls_back_to_the_default(self):
        # RETRY GAP: call_openrouter — one attempt; None -> the baked-in fallback title
        with tempfile.TemporaryDirectory() as td:
            _, st, _, _ = _run(td, title=None)
        self.assertEqual(st.nj.call_openrouter.call_count, 1)
        self.assertEqual(st.nj.publish_hugo.call_args.args[0], "Two Months, One Nova, Zero Chill")

    def test_image_failure_is_non_fatal(self):
        # RETRY GAP: generate_image — one attempt, exception swallowed, article ships without a cover
        with tempfile.TemporaryDirectory() as td:
            _, st, _, log = _run(td, image_exc=RuntimeError("swarm down"))
        self.assertIsNone(st.nj.publish_hugo.call_args.kwargs["image_path"])
        self.assertIn("image gen failed (non-fatal): swarm down", log)
        st.cfg.post_both.assert_called_once()

    def test_publish_failure_escapes_before_the_dm(self):
        # RETRY GAP: publish_hugo — one attempt; the exception propagates and no push/notify/DM follows
        with tempfile.TemporaryDirectory() as td, self.assertRaises(RuntimeError):
            _run(td, publish_exc=RuntimeError("hugo"))


class TestUnit(unittest.TestCase):
    def test_log_appends_a_timestamped_line_and_prints(self):
        with tempfile.TemporaryDirectory() as td:
            mod, _, _, _ = _run(td)
            with redirect_stdout(io.StringIO()) as out:
                mod.log("hello there")
            text = mod.LOG.read_text()
        self.assertEqual(out.getvalue(), "hello there\n")
        self.assertRegex(text.splitlines()[-1], r"^\[\d\d:\d\d:\d\d\] hello there$")

    def test_dossiers_carry_the_named_beats(self):
        for beat in ("nova_notify central bus", "Cloudflare", "dnsmasq", "150 resolved queue tickets", "Little Mister"):
            self.assertIn(beat, SRC.split("DOSSIER2")[0])
        for beat in ("NOVA_GW_STANDBY", "posting list tuple with 3 items", "raw_classification", "Anonymous Diffie-Hellman"):
            self.assertIn(beat, SRC.split("DOSSIER2", 1)[1])

    def test_title_is_stripped_of_quotes_and_hashes(self):
        with tempfile.TemporaryDirectory() as td:
            _, st, _, _ = _run(td, title=' "#Loud Title#" \n')
        self.assertEqual(st.nj.publish_hugo.call_args.args[0], "Loud Title")


class TestIntegration(unittest.TestCase):
    def test_publishing_goes_through_nova_journal_helpers(self):
        for name in ("publish_hugo", "git_push", "notify_slack", "generate_image", "call_openrouter"):
            self.assertIn(f"nj.{name}(", SRC)
            self.assertNotIn(f"def {name}", SRC)
        self.assertNotIn("def call_llm", SRC)

    def test_both_passes_use_the_ops_journal_voice_and_stitch_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            _, st, _, _ = _run(td, part1="  ALPHA  ", part2="  OMEGA  ")
        systems = [c.args[0] for c in st.rd.call_llm.call_args_list]
        self.assertTrue(all(s.startswith("SYS:[ops-context]") for s in systems))
        self.assertIn("PART ONE", systems[0]); self.assertIn("PART TWO", systems[1])
        self.assertIn("MID-TO-LATE MAY", st.rd.call_llm.call_args_list[0].args[1])
        self.assertIn("TONIGHT (7/19)", st.rd.call_llm.call_args_list[1].args[1])
        self.assertEqual([c.kwargs["max_tokens"] for c in st.rd.call_llm.call_args_list], [16000, 18000])
        self.assertEqual(st.nj.publish_hugo.call_args.args[1], "ALPHA\n\n---\n\nOMEGA")

    def test_everything_lands_in_the_operations_section(self):
        with tempfile.TemporaryDirectory() as td:
            _, st, _, _ = _run(td)
        self.assertEqual(st.nj.publish_hugo.call_args.args[2], "operations")
        self.assertEqual(st.nj.generate_image.call_args.kwargs["section"], "operations")
        self.assertEqual(st.nj.git_push.call_args.args[0], "operations")
        self.assertEqual(st.nj.notify_slack.call_args.args[0], "operations")


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_pushes_notifies_and_dms(self):
        with tempfile.TemporaryDirectory() as td:
            _, st, out, log = _run(td, part1="one " * 10, part2="two " * 10, title="Two Months of Chaos", image="/img/c.png")
        args, kw = st.nj.publish_hugo.call_args.args, st.nj.publish_hugo.call_args.kwargs
        self.assertEqual(args[0], "Two Months of Chaos")
        self.assertIn("retrospective", args[3]); self.assertIn("~21 words", args[4])
        self.assertEqual((kw["image_path"], kw["emoji"]), ("/img/c.png", "🏛️"))
        st.nj.git_push.assert_called_once_with("operations", "Two Months of Chaos")
        self.assertEqual(st.nj.notify_slack.call_args.args[1], "🏛️ Two Months of Chaos")
        msg = st.cfg.post_both.call_args.args[0]
        self.assertIn("*The two-month retrospective is live*", msg)
        self.assertIn(f"https://nova.digitalnoise.net/operations/{dt.date.today().isoformat()}-two-months-of-chaos/", msg)
        self.assertIn("PUBLISHED: Two Months of Chaos (~21 words)", log)
        self.assertIn("Slack DM sent: https://", out)
        self.assertEqual(st.nj.generate_image.call_args.kwargs, {"width": 1536, "height": 1024, "section": "operations"})

    def test_error_path_image_down_still_ships_the_article(self):
        with tempfile.TemporaryDirectory() as td:
            _, st, _, _ = _run(td, image_exc=OSError("no swarm"))
        st.nj.publish_hugo.assert_called_once()
        st.nj.git_push.assert_called_once()
        st.cfg.post_both.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_compiles_and_every_side_effect_goes_through_a_stubbed_collaborator(self):
        # a one-off with no main(): the frame is byte-compilation plus proof the stub surface is complete
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        imported = set(re.findall(r"^(?:import|from)\s+([\w.]+)", SRC, re.M))
        self.assertEqual(imported - {"sys", "time", "pathlib", "re", "datetime"},
                         {"nova_journal", "nova_voice", "nova_rando_daily_ops", "nova_config"})
        self.assertIsNone(re.search(r"^\s*(?:import|from)\s+(?:urllib|requests|psycopg2|subprocess)\b", SRC, re.M))


if __name__ == "__main__":
    unittest.main()
