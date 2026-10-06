#!/usr/bin/env python3
"""Tests for nova_rando_daily_ops.py — the 7 house categories (Security, Performance, Retry, Unit,
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
SCRIPT = SCRIPTS / "nova_rando_daily_ops.py"
SRC = SCRIPT.read_text()


def _stub_modules():
    cfg = types.ModuleType("nova_config"); cfg.post_both = MagicMock(); cfg.SLACK_BB = "C_CRIT"
    jr = types.ModuleType("nova_journal"); jr.grafana_panel_image = MagicMock(return_value=None); jr.git_push = MagicMock()
    img = types.ModuleType("nova_image_utils"); img.generate_image = MagicMock(return_value=None)
    ctx = types.ModuleType("nova_ops_context")
    ctx.get_full_context = MagicMock(return_value={})
    ctx.format_security_brief = MagicMock(return_value="sec brief"); ctx.format_infra_brief = MagicMock(return_value="infra brief")
    voice = types.ModuleType("nova_voice"); voice.CONTEXT_JOURNAL_OPS = "OPS-CTX\n"
    voice.system_prompt = lambda context="", section="", **k: f"SYSTEM[{section}] {context}"
    hist = types.ModuleType("nova_article_history"); hist.recent_articles_context = MagicMock(return_value="RECENT ARTICLES")
    cc = types.ModuleType("nova_claude_code"); cc.claude_generate = MagicMock(return_value="claude text")
    return {"nova_config": cfg, "nova_journal": jr, "nova_image_utils": img, "nova_ops_context": ctx,
            "nova_voice": voice, "nova_article_history": hist, "nova_claude_code": cc}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()):          # helpers bound at import; sys.modules restored after
        spec.loader.exec_module(mod)
    return mod


ro = _load("rando_daily_ops_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="rando-ops-test-"))
ro.HUGO_ROOT = TMP / "nova-journal"
ro.CONTENT_DIR = ro.HUGO_ROOT / "content" / "operations"
ro.IMAGES_DIR = ro.HUGO_ROOT / "static" / "images" / "operations"

ARTICLE = ("Another day, another cron job that thinks it's special. " * 40).strip()      # > 120 words, no error markers


def _fresh():
    """Re-arm the stubs the module bound at import, and return the call-time stubs for patch.dict."""
    ro.nova_config.post_both = MagicMock()
    ro.nova_journal.grafana_panel_image = MagicMock(return_value=None)
    ro.nova_journal.git_push = MagicMock()
    ro.generate_image = MagicMock(return_value=None)
    ro.get_full_context = MagicMock(return_value={"security": {"security_event_count": 4, "threat_scores": {}, "open_incidents": []},
                                                 "syslog": {"firewall_blocks": 12}})
    ro.format_security_brief = MagicMock(return_value="sec brief"); ro.format_infra_brief = MagicMock(return_value="infra brief")
    stubs = _stub_modules()
    return {k: stubs[k] for k in ("nova_voice", "nova_article_history", "nova_claude_code")}


class _Cur:
    """RealDictCursor stand-in: fetchall -> [] ; fetchone -> {"total": 0} or the weather row."""
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))

    def fetchall(self):
        return [{"observer": "nova", "category": "c", "subject": "s", "observation": "o", "severity": "info", "observed_at": "t"}] \
            if "FROM shared_observations" in self.sql[-1] else []

    def fetchone(self):
        return {"high_f": 91, "low_f": 60} if "telemetry.weather" in self.sql[-1] else {"total": 3}

    def close(self):
        pass


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _run_main(llm, image=None):
    """Drive main() with gather_ops_data stubbed; `llm` is the call_llm side_effect list [article, title]."""
    mods = _fresh()
    ro.generate_image = MagicMock(return_value=image)
    with patch.object(ro, "gather_ops_data", return_value={"queue_completed": [], "observations": []}), \
         patch.object(ro, "call_llm", MagicMock(side_effect=llm)) as cl, patch.object(ro.subprocess, "run", MagicMock()) as sr, \
         patch.dict(sys.modules, mods), redirect_stdout(io.StringIO()) as out:
        rc = ro.main()
    return rc, cl, sr, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_key_comes_from_keychain(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        with patch.object(ro.subprocess, "check_output", return_value="sk-or-abc\n") as co:
            self.assertEqual(ro.get_openrouter_key(), "sk-or-abc")
        self.assertEqual(co.call_args[0][0][:3], ["security", "find-generic-password", "-a"])

    def test_sql_is_parameterized_and_read_only(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+[\w.]+", SRC))

    def test_sanitize_scrubs_ips_macs_hosts_and_paths(self):
        s = ro._sanitize("Jordans-Mac-mini at 192.168.1.6 (aa:bb:cc:dd:ee:ff) read /etc/shadow; Office-M4-2 and Amys-iPhone; 10.0.0.1")
        for leak in ("192.168", "aa:bb", "/etc/shadow", "Office-M4", "Jordan", "Amy", "10.0.0.1"):
            self.assertNotIn(leak, s)
        self.assertIn("a personal device", s); self.assertIn("a workstation", s); self.assertIn("an internal host", s)
        self.assertEqual(ro._sanitize(None), "")

    def test_presence_and_devices_never_reach_the_cloud_prompt(self):
        data = {"presence": [{"room": "bedroom"}], "new_devices": [{"client_name": "Amys-iPhone", "ip": "192.168.1.77"}],
                "observations": [{"observation": "192.168.1.6 rebooted"}], "queue_completed": [{"description": "fixed 192.168.1.9", "context": "Jordans-Mac"}]}
        with patch.object(ro, "call_llm", return_value=ARTICLE) as cl, patch.dict(sys.modules, _fresh()):
            ro.generate_article(data)
        system, user = cl.call_args[0][0], cl.call_args[0][1]
        for leak in ("bedroom", "presence", "Amys-iPhone", "192.168.1", "Jordans-Mac", "new_devices"):
            self.assertNotIn(leak, user)
        self.assertIn("fixed an internal host", user); self.assertIn("SYSTEM[operations] OPS-CTX", system)


class TestPerformance(unittest.TestCase):
    def test_sanitize_and_scrub_fast_on_10k_strings(self):
        rows = [{"observation": f"host-{i} at 192.168.1.{i % 250} mac aa:bb:cc:dd:ee:{i % 99:02x} Jordans-Mac /etc/hosts",
                 "presence": {"room": "x"}, "n": i} for i in range(10_000)]
        t0 = time.perf_counter()
        out = ro._scrub_obj(rows)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(out), 10_000)
        self.assertNotIn("presence", out[0]); self.assertNotIn("192.168", out[9_999]["observation"])
        self.assertEqual(out[5]["n"], 5)

    def test_looks_like_failure_fast_on_long_bodies(self):
        body = ("word " * 5_000) + "failed to authenticate"
        t0 = time.perf_counter()
        for _ in range(2_000):
            ro.looks_like_failure(body)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_llm_falls_back_from_claude_to_openrouter(self):
        # call_llm: attempt 1 = Claude Code Max; on any failure attempt 2 = OpenRouter (2 attempts, then raises)
        mods = _fresh(); mods["nova_claude_code"].claude_generate = MagicMock(side_effect=RuntimeError("cli auth"))
        uo = MagicMock(return_value=_Resp({"choices": [{"message": {"content": "fallback prose"}}]}))
        with patch.dict(sys.modules, mods), patch.object(ro.subprocess, "check_output", return_value="key\n"), \
             patch("urllib.request.urlopen", uo):
            self.assertEqual(ro.call_llm("sys", "usr"), "fallback prose")
        self.assertEqual(mods["nova_claude_code"].claude_generate.call_count, 1); self.assertEqual(uo.call_count, 1)
        req = uo.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer key")
        self.assertEqual(json.loads(req.data)["model"], ro.OPENROUTER_MODEL)
        with patch.dict(sys.modules, mods), patch.object(ro.subprocess, "check_output", return_value="key\n"), \
             patch("urllib.request.urlopen", side_effect=OSError("502")):
            with self.assertRaises(OSError):
                ro.call_llm("sys", "usr")                     # both backends down -> loud failure, main never publishes

    def test_gather_fails_open_per_source(self):
        # RETRY GAP: gather_ops_data — every PG/HTTP source is one attempt; a failure yields that source's safe default
        with patch("psycopg2.connect", side_effect=OSError("pg down")), patch("urllib.request.urlopen", side_effect=OSError("http down")), \
             patch.object(ro.Path, "home", return_value=TMP / "nohome"):
            d = ro.gather_ops_data()
        self.assertEqual(d["observations"], [{"error": "pg down"}]); self.assertEqual(d["scheduler"], {"error": "pg down"})
        self.assertEqual(d["hue"], {"error": "unavailable"}); self.assertEqual(d["memory_count"], 0)
        self.assertEqual((d["queue_completed"], d["queue_remaining"], d["memories_added_today"]), ([], 0, 0))
        self.assertNotIn("unas", d); self.assertNotIn("printers_active", d)

    def test_cwebp_missing_falls_back_to_copy(self):
        # RETRY GAP: publish()/cwebp — one attempt; FileNotFoundError/timeout fall back to a plain copy
        _fresh()
        src = TMP / "cover.png"; src.write_bytes(b"\x89PNG")
        with patch.object(ro.subprocess, "run", side_effect=FileNotFoundError("cwebp")), redirect_stdout(io.StringIO()):
            ro.publish("Fallback Copy", ARTICLE, src, pub_date="2026-10-05")
        self.assertEqual((ro.HUGO_ROOT / "static/images/operations/2026-10-05-fallback-copy.webp").read_bytes(), b"\x89PNG")

    def test_image_failure_does_not_block_publish(self):
        # RETRY GAP: main()/generate_image — one attempt; failure logs and publishes without a cover
        mods = _fresh()
        ro.generate_image = MagicMock(side_effect=RuntimeError("comfy down"))
        with patch.object(ro, "gather_ops_data", return_value={}), patch.object(ro, "call_llm", MagicMock(side_effect=[ARTICLE, "T"])), \
             patch.object(ro, "publish") as pub, patch.dict(sys.modules, mods), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ro.main(), 0)
        self.assertIn("Image generation failed: comfy down", out.getvalue())
        self.assertIsNone(pub.call_args[0][2])


class TestUnit(unittest.TestCase):
    def test_looks_like_failure_gate(self):
        self.assertEqual(ro.looks_like_failure(""), (True, "only 0 words (< 120 floor)"))
        self.assertEqual(ro.looks_like_failure(None)[0], True)
        self.assertEqual(ro.looks_like_failure("Failed to authenticate. " * 70), (True, "leads with an upstream-error signature: 'failed to authenticate'"))
        self.assertEqual(ro.looks_like_failure(ARTICLE), (False, ""))
        self.assertEqual(ro.looks_like_failure(ARTICLE + " Earlier the rate limit bit us, as usual.")[0], False)   # discussed, not led with

    def test_scrub_obj_shapes(self):
        self.assertEqual(ro._scrub_obj({"a": [{"presence": 1, "b": "10.1.2.3"}], "new_devices": []}), {"a": [{"b": "an internal host"}]})
        self.assertEqual(ro._scrub_obj(7), 7); self.assertEqual(ro._scrub_obj(None), None)
        self.assertEqual(ro._scrub_obj({"Jordans-Mac": 1}), {"a personal device": 1})

    def test_generate_title_strips_quotes(self):
        with patch.object(ro, "call_llm", return_value='  "Why Is It Always DNS"  \n'):
            self.assertEqual(ro.generate_title("preview"), "Why Is It Always DNS")

    def test_sanitize_keeps_public_ips_and_plain_text(self):
        self.assertEqual(ro._sanitize("8.8.8.8 is fine, so is nova-core"), "8.8.8.8 is fine, so is nova-core")
        self.assertEqual(ro._sanitize("172.16.4.4 and 172.32.0.1"), "an internal host and 172.32.0.1")
        self.assertEqual(ro._sanitize("10.20.30.40 and 10.1.2.3"), "an internal host and an internal host")   # 10/8 used to leak the last octet


class TestIntegration(unittest.TestCase):
    def test_generate_article_leads_with_queue_block_and_appends_history(self):
        data = {"queue_completed": [{"description": "Rebuilt 15 corrupt indexes", "context": "first clean restore since July"}],
                "scheduler": {"total": 9}}
        with patch.object(ro, "call_llm", return_value=ARTICLE) as cl, patch.dict(sys.modules, _fresh()):
            ro.generate_article(data)
        user, kw = cl.call_args[0][1], cl.call_args[1]
        self.assertLess(user.index("TODAY'S COMPLETED WORK"), user.index("EVERYTHING ELSE"))
        self.assertIn("1. Rebuilt 15 corrupt indexes\n   DETAIL: first clean restore since July", user)
        self.assertNotIn('"queue_completed"', user); self.assertIn('"scheduler"', user)
        self.assertTrue(user.endswith("RECENT ARTICLES")); self.assertEqual(kw, {"max_tokens": 16000})

    def test_publish_uses_shared_journal_push_and_notifications_channel(self):
        _fresh()
        ro.nova_journal.grafana_panel_image = MagicMock(return_value="/images/operations/fleet.png")
        with patch.object(ro.subprocess, "run", MagicMock()), redirect_stdout(io.StringIO()):
            ro.publish('Nova "Quoted" Title', ARTICLE, None, pub_date="2026-10-05")
        ro.nova_journal.grafana_panel_image.assert_called_once_with("fleet-health", 7, "operations", "rando-ops-fleet-health")
        ro.nova_journal.git_push.assert_called_once_with("operations", 'Nova "Quoted" Title')
        msg, kw = ro.nova_config.post_both.call_args[0][0], ro.nova_config.post_both.call_args[1]
        self.assertEqual(kw, {"slack_channel": "#nova-notifications"})
        self.assertIn("https://nova.digitalnoise.net/operations/2026-10-05-nova-quoted-title/", msg)
        post = (ro.CONTENT_DIR / "2026-10-05-nova-quoted-title.md").read_text()
        self.assertIn('title: "Nova Quoted Title"', post); self.assertIn("date: 2026-10-05T12:00:00-07:00", post)
        self.assertIn("![Current fleet health](/images/operations/fleet.png)", post)
        self.assertIn("*Published Monday, October 05, 2026*", post); self.assertNotIn("cover:", post)

    def test_gather_reads_the_expected_tables(self):
        cur = _Cur()
        conn = types.SimpleNamespace(cursor=lambda **k: cur, close=lambda: None)
        with patch("psycopg2.connect", return_value=conn) as pg, patch("urllib.request.urlopen", return_value=_Resp({"count": 42, "ok": True})), \
             patch.object(ro.Path, "home", return_value=TMP / "nohome"):
            d = ro.gather_ops_data()
        self.assertEqual(pg.call_args_list[-1][0][0], "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj")
        tables = {m.group(1) for s in cur.sql for m in re.finditer(r"FROM ([\w.]+)", s)}
        self.assertTrue({"shared_observations", "scheduler_runs", "fix_attempts", "snmp_metrics", "claude_actions", "claude_queue",
                         "bb_events", "telemetry.weather", "telemetry.presence", "telemetry.known_devices", "memories"} <= tables)
        self.assertEqual(d["memory_count"], 42); self.assertEqual(d["weather"], {"high_f": 91, "low_f": 60})
        self.assertEqual(d["queue_completed_count"], 3); self.assertEqual(len(d["observations"]), 1)
        self.assertEqual(d["scheduler"]["total"], 0); self.assertEqual(d["bb_events_summary"]["healed"], 0)


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_column(self):
        cover = TMP / "cover.png"; cover.write_bytes(b"img")
        rc, cl, sr, out = _run_main([ARTICLE, "Everything Is Fine, Probably"], image=str(cover))
        self.assertEqual(rc, 0)
        self.assertEqual(cl.call_count, 2)
        posts = list(ro.CONTENT_DIR.glob("*-everything-is-fine-probably.md"))
        self.assertEqual(len(posts), 1)
        text = posts[0].read_text()
        self.assertIn('cover:\n  image: "/images/operations/', text); self.assertTrue(text.endswith(ARTICLE))
        self.assertEqual(sr.call_args[0][0][0], "cwebp")
        ro.nova_journal.git_push.assert_called_once()
        self.assertIn(":gear: *Daily Ops Column posted*", ro.nova_config.post_both.call_args[0][0])
        self.assertIn("Done!", out)

    def test_error_stub_is_refused_and_alerted(self):
        rc, cl, sr, out = _run_main(["Failed to authenticate. Please run /login."])
        self.assertEqual(rc, 1); self.assertEqual(cl.call_count, 1)             # no title call, nothing written
        ro.nova_journal.git_push.assert_not_called()
        msg, kw = ro.nova_config.post_both.call_args[0][0], ro.nova_config.post_both.call_args[1]
        self.assertTrue(msg.startswith(":no_entry: *Daily Ops article skipped*")); self.assertEqual(kw, {"slack_channel": "C_CRIT"})
        self.assertIn("ABORT: generated body looks like a failure", out)

    def test_error_title_is_refused(self):
        rc, cl, sr, out = _run_main([ARTICLE, "Invalid API key"])
        self.assertEqual(rc, 1); ro.nova_journal.git_push.assert_not_called(); ro.nova_config.post_both.assert_not_called()
        self.assertIn("ABORT: title looks like a failure", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    import sys\n    sys.exit(main() or 0)', SRC)
        code = ("import sys, types\n"
                "for n in ('nova_config', 'nova_journal', 'nova_image_utils', 'nova_ops_context'):\n"
                "    m = types.ModuleType(n); m.generate_image = m.get_full_context = m.format_security_brief = m.format_infra_brief = lambda *a, **k: None\n"
                "    sys.modules[n] = m\n"
                "import psycopg2; psycopg2.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError('main ran at import'))\n"
                "import nova_rando_daily_ops as r\n"
                "print(r.STUB_WORD_FLOOR, r.OPENROUTER_MODEL)\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "120 google/gemini-2.5-flash")


if __name__ == "__main__":
    unittest.main()
