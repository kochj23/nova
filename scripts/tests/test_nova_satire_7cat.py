#!/usr/bin/env python3
"""Satire image pool + Nova Speaks variety (Jordan 2026-10-19) in the 7 house categories (Security, Performance,
Retry, Unit, Integration, Functional, Frame):
  * nova_satire.py — curated pool (>=100 scenes x styles), per-article inline satirical image, the publish_hugo
    enqueue hook, and the Nova Speaks picker (relevance -> least-recently-used, nothing from the last 10 videos);
  * nova_speaks.py — plan_shots / motion_expr (varied holds + Ken Burns moves, no back-to-back repeat);
  * nova_speaks_sweep.py — dispatch hands the picks to the renderer as --images; uploader run via its interpreter.
ComfyUI, PG, git, YouTube and the LLM are always stubs. Written by Jordan Koch (via Claude)."""
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import nova_satire as sat  # noqa: E402
import nova_speaks as spk  # noqa: E402

MD = ('---\ntitle: "🎹 Robert Miles\' Children Was Written to Get Ravers Home"\ndescription: "rave CDs and infomercials"\n'
      'tags: ["music"]\n---\n\nPara one about rave music.\n\nPara two.\n\n## A heading\n\nPara three.\n\nPara four.\n\nPara five.\n')


class Cur:
    """Minimal psycopg2 cursor fake: answers keyed by SQL substring; records every statement."""
    def __init__(self, answers=None):
        self.answers, self.stmts, self._last = answers or {}, [], None

    def execute(self, sql, params=None):
        self.stmts.append((sql, params))
        self._last = next((v for k, v in self.answers.items() if k in sql), None)

    def fetchall(self):
        return list(self._last or [])

    def fetchone(self):
        r = self._last
        return r[0] if isinstance(r, list) and r else r

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Conn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def close(self):
        pass


def fake_png(tmp):
    p = Path(tmp) / "gen.png"
    p.write_bytes(b"\x89PNG fake")
    return str(p)


def fake_webp(src, dest, width=1200):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest)
    return dest


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.nas = Path(self.tmp) / "nas"
        self.journal = Path(self.tmp) / "journal"
        (self.journal / "content/music").mkdir(parents=True)
        self.p = [patch.object(sat, "NAS_ROOT", self.nas), patch.object(sat, "JOURNAL", self.journal),
                  patch.object(sat, "to_webp", side_effect=fake_webp)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSecurity(Base):
    def test_every_pool_prompt_forbids_text_logos_people_sexual(self):
        for i in range(len(sat.SCENES)):
            _, prompt, _, _ = sat.pool_prompt(i)
            for must in ("No readable text", "no logos", "no real or famous people", "nothing sexual"):
                self.assertIn(must, prompt)

    def test_llm_brief_prompt_is_wrapped_with_safety_and_caption_sanitised(self):
        llm = lambda s, u: '{"prompt": "a goat", "caption": "line1\\nline2 ' + "x" * 300 + '", "tags": ["A","b"]}'
        b = sat.article_brief(MD, llm)
        self.assertNotIn("\n", b["caption"])
        self.assertLessEqual(len(b["caption"]), 120)
        with patch.object(sat, "render", return_value=fake_png(self.tmp)) as r:
            md_path = self.journal / "content/music/2026-10-19-x.md"
            md_path.write_text(MD)
            sat.satirize_article(md_path, llm=llm)
            self.assertIn(sat.NO_TEXT, r.call_args[0][0])

    def test_caption_cannot_break_markdown_link(self):
        out = sat.insert_inline(MD, "/images/satire/x.webp", "evil](http://bad) [x")
        self.assertNotIn("](http://bad)", out)

    def test_enqueue_skips_index_and_art(self):
        with patch.object(sat, "db") as d:
            self.assertFalse(sat.enqueue_article("/j/content/art/2026-x.md", "art"))
            self.assertFalse(sat.enqueue_article("/j/content/music/_index.md", "music"))
            d.assert_not_called()


class TestPerformance(Base):
    def test_rank_is_fast_on_a_large_pool(self):
        rows = [(i, f"p{i}", ["music", "cars"][i % 2:i % 2 + 1], [f"t{i}"], i) for i in range(5000)]
        t = time.perf_counter()
        sat.rank(rows, MD * 20, set(range(0, 5000, 3)), 30)
        self.assertLess(time.perf_counter() - t, 0.5)

    def test_plan_shots_is_fast_for_long_videos(self):
        t = time.perf_counter()
        plan = spk.plan_shots(4000, 30, random.Random(1))
        self.assertEqual(len(plan), 4000)
        self.assertLess(time.perf_counter() - t, 0.5)


class TestRetry(Base):
    def test_wait_idle_yields_then_gives_up(self):
        sleeps = []
        self.assertFalse(sat.wait_idle(max_wait=60, poll=20, sleep=sleeps.append, busy=lambda: True))
        self.assertEqual(sleeps, [20, 20, 20])

    def test_pool_fill_skips_failed_render_and_continues(self):
        cur = Cur({"SELECT prompt_key": [], "count(*)": [(0,)]})
        outs = iter([None, fake_png(self.tmp), fake_png(self.tmp)])
        made = sat.pool_fill(target=2, budget_s=60, gen=lambda *a, **k: next(outs), conn=Conn(cur), busy=lambda: False)
        self.assertEqual(made, 2)
        self.assertEqual(sum("INSERT INTO satire_images" in s for s, _ in cur.stmts), 2)

    def test_article_failure_is_counted_not_fatal(self):
        md_path = self.journal / "content/music/2026-10-19-x.md"
        md_path.write_text(MD)
        cur = Cur({"FROM satire_article_queue": [("2026-10-19-x", str(md_path))], "count(*)": [(0,)]})
        with patch.object(sat, "satirize_article", side_effect=RuntimeError("comfy 500")):
            done = sat.articles_run(conn=Conn(cur), busy=lambda: False, commit=lambda *a: "pushed")
        self.assertEqual(done, 0)
        self.assertTrue(any("attempts=attempts+1" in s for s, _ in cur.stmts))

    def test_brief_falls_back_when_llm_returns_garbage(self):
        b = sat.article_brief(MD, lambda s, u: "no json here")
        self.assertIn("satirical cartoon", b["prompt"])


class TestUnit(Base):
    def test_pool_has_100_plus_unique_scenes_and_all_styles(self):
        self.assertGreaterEqual(len(sat.SCENES), 100)
        self.assertEqual(len({k for k, _, _ in sat.SCENES}), len(sat.SCENES))
        self.assertEqual({sat.scene_style(i)[0] for i in range(len(sat.SCENES))}, {k for k, _ in sat.STYLES})

    def test_insert_inline_is_idempotent_and_mid_article(self):
        out = sat.insert_inline(MD, "/images/satire/x.webp", "cap")
        self.assertEqual(sat.insert_inline(out, "/y", "c"), out)
        body = out.split("---", 2)[2]
        self.assertLess(body.index(sat.MARKER), body.index("Para five"))
        self.assertGreater(body.index(sat.MARKER), body.index("Para one"))

    def test_rank_relevance_then_lru_then_recent_last(self):
        rows = [(1, "a", ["music"], [], 5), (2, "b", ["cars"], [], 1), (3, "c", ["music", "rave"], [], 9), (4, "d", [], [], None)]
        self.assertEqual([r[0] for r in sat.rank(rows, "rave music", {3}, 4, random.Random(0))], [1, 4, 2, 3])

    def test_motion_expr_known_and_default(self):
        for m in spk.MOTIONS:
            self.assertEqual(len(spk.motion_expr(m, 50)), 3)
        self.assertEqual(spk.motion_expr("nope", 50), spk.motion_expr("in-center", 50))

    def test_stills_needed_bounds(self):
        self.assertEqual(sat.stills_needed(10), 8)
        self.assertEqual(sat.stills_needed(10 ** 6), 30)


class TestIntegration(Base):
    def test_pick_for_video_records_use_and_excludes_last_10_videos(self):
        (self.nas / sat.POOL_REL).mkdir(parents=True)
        rows = []
        for i in range(1, 6):
            (self.nas / sat.POOL_REL / f"{i}.webp").write_bytes(b"x")
            rows.append((i, f"{sat.POOL_REL}/{i}.webp", ["music"] if i < 3 else ["cars"], [], None))
        cur = Cur({"FROM satire_images WHERE active": rows, "FROM satire_video_picks": [([1],)]})
        got = sat.pick_for_video("slug", "music night", 3, conn=Conn(cur))
        self.assertEqual(Path(got[0]).name, "2.webp")                 # relevant + not used in the last videos
        self.assertNotIn("1.webp", [Path(g).name for g in got])
        self.assertTrue(any("INSERT INTO satire_video_picks" in s for s, _ in cur.stmts))
        self.assertTrue(any("times_used=times_used+1" in s for s, _ in cur.stmts))

    def test_articles_run_end_to_end_writes_image_inline_and_commits(self):
        md_path = self.journal / "content/music/2026-10-19-x.md"
        md_path.write_text(MD)
        cur = Cur({"FROM satire_article_queue": [("2026-10-19-x", str(md_path))], "count(*)": [(0,)]})
        committed = []
        llm = lambda s, u: '{"prompt": "a dj cat", "caption": "The poster child", "tags": ["music"]}'
        with patch.object(sat, "render", return_value=fake_png(self.tmp)):
            done = sat.articles_run(conn=Conn(cur), llm=llm, busy=lambda: False,
                                    commit=lambda paths, n: committed.append((paths, n)) or "pushed")
        self.assertEqual(done, 1)
        self.assertIn("/images/satire/2026-10-19-x.webp", md_path.read_text())
        self.assertTrue((self.journal / "static/images/satire/2026-10-19-x.webp").exists())
        self.assertTrue((self.nas / sat.POOL_REL / "article-2026-10-19-x.webp").exists())
        self.assertEqual(committed[0][1], 1)

    def test_publish_hook_present_in_nova_journal(self):
        src = (SCRIPTS / "nova_journal.py").read_text()
        self.assertIn('__import__("nova_satire").enqueue_article(output, section)', src)


class TestFunctional(Base):
    def test_backfill_waits_for_100_pool_images(self):
        old = self.journal / "content/music/2026-10-18-old.md"
        old.write_text(MD)
        cur = Cur({"FROM satire_article_queue": [], "count(*)": [(40,)]})
        with patch.object(sat, "satirize_article") as s:
            sat.articles_run(conn=Conn(cur), busy=lambda: False, commit=lambda *a: "x")
            s.assert_not_called()
        cur = Cur({"FROM satire_article_queue": [], "count(*)": [(100,)]})
        with patch.object(sat, "satirize_article", return_value=None) as s:
            sat.articles_run(conn=Conn(cur), busy=lambda: False, commit=lambda *a: "x")
            s.assert_called()

    def test_plan_shots_varies_and_never_repeats_back_to_back(self):
        for n in (2, 5, 16):
            plan = spk.plan_shots(60, n, random.Random(n))
            runs = [s for i, (s, _) in enumerate(plan) if i == 0 or s != plan[i - 1][0]]
            self.assertTrue(all(a != b for a, b in zip(runs, runs[1:])))
            self.assertGreater(len({m for _, m in plan}), 3)

    def test_sweep_passes_satire_picks_as_images_and_uses_upload_interpreter(self):
        import nova_speaks_sweep as sw
        art = self.journal / "content/music/2026-10-19-x.md"
        art.write_text(MD)
        host = {"host": "h", "ssh": None, "enabled": True, "speed": 1, "journal_dir": str(self.journal), "tts_home": str(self.journal / "tts"),
                "scripts_dir": str(SCRIPTS), "python": "python3", "out_dir": "/tmp", "env": ""}
        cur = Cur({"FROM nova_speaks_renders WHERE status='queued'": [("2026-10-19-x", "music", str(art), "https://x/")]})
        fake_sat = MagicMock()
        fake_sat.pick_for_video.return_value = ["/nas/nova-satire/pool/a.webp", "/nas/nova-satire/pool/b.webp"]
        fake_sat.stills_needed.return_value = 8
        cmds = []
        with patch.dict(sys.modules, {"nova_satire": fake_sat}), patch.object(sw, "idle", return_value=True), \
             patch.object(sw, "copy_to"), patch.object(sw, "sh", side_effect=lambda h, c, timeout=60: cmds.append(c) or (0, "123")):
            sw.dispatch(cur, {"h": host})
        self.assertTrue(any("--images" in c and "satire-pool/a.webp" in c for c in cmds), cmds)
        self.assertIn("UPLOAD_PY, str(SCRIPTS / \"nova_speaks_upload.py\")", (SCRIPTS / "nova_speaks_sweep.py").read_text())


class TestFrame(unittest.TestCase):
    def test_selftest_and_help_exit_zero(self):
        for args in (["--selftest"], ["--help"]):
            r = subprocess.run([sys.executable, str(SCRIPTS / "nova_satire.py"), *args], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_has_no_side_effects(self):
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import nova_satire; print('ok')" % str(SCRIPTS)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "ok", r.stderr)

    def test_scheduler_tasks_registered(self):
        import yaml
        tasks = yaml.safe_load((SCRIPTS.parent / "config/scheduler.yaml").read_text())["tasks"]
        for t in ("satire_pool", "satire_articles"):
            self.assertEqual(tasks[t]["script"], "nova_satire.py")
            self.assertTrue(tasks[t].get("gpu_heavy"))


if __name__ == "__main__":
    unittest.main()
