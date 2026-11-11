#!/usr/bin/env python3
"""Tests for nova_rosetta.py — the 7 house categories (Security, Performance, Retry, Unit, Integration, Functional,
Frame). Every HTTP call is a mocked urlopen; PG is mocked. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import sys
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_rosetta.py"
SRC = SCRIPT.read_text()

spec = importlib.util.spec_from_file_location("nova_rosetta", SCRIPT)
R = importlib.util.module_from_spec(spec)
sys.modules["nova_rosetta"] = R      # nova_lb imports it by name
spec.loader.exec_module(R)
import nova_lb as LB  # noqa: E402


def _resp(obj):
    r = MagicMock()
    r.read.return_value = json.dumps(obj).encode()
    r.__enter__.return_value = r
    return r


def _ollama(text, n=10, thinking=None):
    msg = {"content": text}
    if thinking:
        msg["thinking"] = thinking
    return _resp({"message": msg, "eval_count": n, "eval_duration": 5e8, "prompt_eval_count": 7, "done_reason": "stop"})


def _openai(text, n=6, timings=None):
    d = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
         "usage": {"completion_tokens": n, "prompt_tokens": 3}}
    if timings:
        d["timings"] = timings
    return _resp(d)


CAND = {
    "ollama": {"name": "nova-core7", "ip": "192.168.1.150", "protocol": "ollama", "port": 11434, "accelerator": "rocm",
               "runtime_model": "qwen3:8b"},
    "mlx": {"name": "tv-movies-mini", "ip": "192.168.1.7", "protocol": "mlx", "port": 5050, "accelerator": "mlx",
            "runtime_model": "/Users/Shared/mlx-models/Qwen3-8B-4bit"},
    "llamacpp": {"name": "mac-studio", "ip": "192.168.1.6", "protocol": "llamacpp", "port": 11435,
                 "accelerator": "metal", "runtime_model": "/x/blobs/sha256-1194192cf2a187eb02722e"},
}
SCHEMA = {"type": "OBJECT", "properties": {"ok": {"type": "BOOLEAN"}, "n": {"type": "INTEGER"}}, "required": ["ok"]}


class _Base(unittest.TestCase):
    def setUp(self):
        boom = MagicMock(side_effect=AssertionError("unmocked outbound"))
        for p in (patch.object(R.urllib.request, "urlopen", boom), patch.object(R, "record_perf")):
            p.start()
            self.addCleanup(p.stop)

    def body(self, uo, i=0):
        return json.loads(uo.call_args_list[i].args[0].data)


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        for pat in ("password=", "api_key", "Bearer ", "sk-"):
            self.assertNotIn(pat, SRC)

    def test_never_sends_num_ctx(self):
        # a num_ctx different from the server default spawns a new runner = a reload (agent_docs nova-inference)
        for rt in ("ollama", "mlx", "llamacpp"):
            _, body = R.build_request(rt, "http://h", "m", [{"role": "user", "content": "x"}], temperature=0.2,
                                      max_tokens=50, schema={"type": "object"}, think=False, family="qwen3")
            self.assertNotIn("num_ctx", json.dumps(body))

    def test_perf_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")

    def test_lan_only_targets(self):
        with patch.object(R.urllib.request, "urlopen", return_value=_ollama("hi")) as uo:
            R.chat("qwen3-8b", [{"role": "user", "content": "x"}], candidates=[CAND["ollama"]])
        self.assertTrue(uo.call_args.args[0].full_url.startswith("http://192.168.1."))


class TestPerformance(_Base):
    def test_10k_resolutions_fast(self):
        served = ["nomic-embed-text:latest", "qwen3:14b", "llama3.2:3b", "qwen3:8b"]
        t = time.perf_counter()
        for _ in range(10_000):
            R.resolve("qwen3-8b", "ollama", served)
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_ranking_1k_rows_fast(self):
        rows = [{"name": f"n{i}", "ip": "192.168.1.9", "status": "up", "p50_ms": float(i), "active_connections": 0,
                 "protocols": {"ollama": 11434}, "accel": {"ollama": "rocm"}, "resident": {"ollama": ["qwen3:8b"]}}
                for i in range(1000)]
        t = time.perf_counter()
        self.assertEqual(LB.rank_candidates(rows, "qwen3-8b")[0]["name"], "n0")
        self.assertLess(time.perf_counter() - t, 1.0)


class TestRetry(_Base):
    def test_fails_over_to_next_candidate(self):
        uo = MagicMock(side_effect=[urllib.error.URLError("refused"), _openai("from mlx")])
        with patch.object(R.urllib.request, "urlopen", uo):
            r = R.chat("qwen3-8b", [{"role": "user", "content": "x"}], candidates=[CAND["ollama"], CAND["mlx"]])
        self.assertTrue(r["success"])
        self.assertEqual((r["node"], r["runtime"], r["text"]), ("tv-movies-mini", "mlx", "from mlx"))
        self.assertEqual([a.get("ok", False) for a in r["attempts"]], [False, True])
        self.assertEqual(R.record_perf.call_args_list[0].kwargs, {"failed": True})     # the dead node sinks

    def test_mlx_schema_retry_feeds_back_the_error(self):
        uo = MagicMock(side_effect=[_openai("sure! no json here"), _openai('{"ok": true}')])
        with patch.object(R.urllib.request, "urlopen", uo):
            r = R.chat("qwen3-8b", [{"role": "user", "content": "x"}], schema=SCHEMA, candidates=[CAND["mlx"]])
        self.assertEqual(r["json"], {"ok": True})
        second = self.body(uo, 1)["messages"]
        self.assertEqual(second[-2]["role"], "assistant")
        self.assertIn("did not match the schema", second[-1]["content"])

    def test_all_fail_reports_every_attempt_and_respects_max(self):
        uo = MagicMock(side_effect=urllib.error.URLError("down"))
        with patch.object(R.urllib.request, "urlopen", uo):
            r = R.chat("qwen3-8b", [{"role": "user", "content": "x"}],
                       candidates=[CAND["ollama"], CAND["mlx"], CAND["ollama"], CAND["mlx"]], max_attempts=2)
        self.assertFalse(r["success"])
        self.assertEqual(uo.call_count, 2)
        self.assertIn("nova-core7/ollama", r["error"])

    def test_no_candidates(self):
        r = R.chat("qwen3-8b", [], candidates=[])
        self.assertIn("resident", r["error"])


class TestUnit(_Base):
    def test_canonical_from_any_runtime_name(self):
        self.assertEqual(R.canonical("qwen3:30b-a3b"), "qwen3-30b-a3b")
        self.assertEqual(R.canonical("mlx-community/Qwen3-8B-4bit"), "qwen3-8b")
        self.assertEqual(R.canonical("/Volumes/Data/mlx-models/qwen2.5-32b-4bit"), "qwen2.5-32b")
        self.assertEqual(R.canonical("/var/models/blobs/sha256-1194192cf2a187eb02722edcc3f7"),
                         "qwen3-coder-30b")
        self.assertEqual(R.canonical("nova"), "nova")
        self.assertIsNone(R.canonical("gpt-4o"))

    def test_omni_is_not_the_30b(self):
        self.assertIsNone(R.resolve("qwen3-30b-a3b", "mlx", ["mlx-community/Qwen3-Omni-30B-A3B-Instruct-4bit"]))

    def test_ollama_tags_match_exactly(self):
        self.assertIsNone(R.resolve("qwen3-8b", "ollama", ["qwen3:8b-q8_0", "qwen3:14b"]))
        self.assertEqual(R.resolve("nova", "ollama", ["nova:latest"]), "nova:latest")

    def test_split_think(self):
        self.assertEqual(R.split_think("<think>hmm\n</think>\n\nHello"), ("Hello", "hmm"))
        self.assertEqual(R.split_think("<think>cut off by max_tok"), ("", "cut off by max_tok"))
        self.assertEqual(R.split_think("plain"), ("plain", ""))

    def test_schema_normalization_and_validation(self):
        s = R.normalize_schema(SCHEMA)
        self.assertEqual((s["type"], s["properties"]["ok"]["type"]), ("object", "boolean"))
        self.assertIsNone(R.validate({"ok": True, "n": 2}, s))
        self.assertIn("missing", R.validate({"n": 2}, s))
        self.assertIn("expected integer", R.validate({"ok": True, "n": True}, s))
        self.assertIn("not in", R.validate("x", {"type": "string", "enum": ["a"]}))

    def test_extract_json(self):
        self.assertEqual(R.extract_json('Here:\n```json\n{"a": [1, 2]}\n```'), {"a": [1, 2]})
        self.assertEqual(R.extract_json("x {bad} then [1]"), [1])
        with self.assertRaises(ValueError):
            R.extract_json("nothing")

    def test_request_translation_per_runtime(self):
        msgs = [{"role": "user", "content": "x", "images": ["AAAA"]}]
        url, b = R.build_request("ollama", "http://h:11434", "qwen3:8b", msgs, temperature=0.1, max_tokens=5,
                                 schema={"type": "object"}, think=False)
        self.assertEqual(url, "http://h:11434/api/chat")
        self.assertEqual((b["format"], b["options"]), ({"type": "object"}, {"temperature": 0.1, "num_predict": 5}))
        self.assertNotIn("think", b)     # think=False leaks untagged reasoning on Ollama 0.40 — never sent
        self.assertEqual(b["messages"][0]["images"], ["AAAA"])
        url, b = R.build_request("llamacpp", "http://h:11435", "g", msgs, schema={"type": "object"})
        self.assertEqual(url, "http://h:11435/v1/chat/completions")
        self.assertEqual(b["response_format"]["json_schema"]["schema"], {"type": "object"})
        self.assertEqual(b["messages"][0]["content"][1]["image_url"]["url"], "data:image/png;base64,AAAA")
        _, b = R.build_request("mlx", "http://h", "m", [{"role": "user", "content": "x"}], schema={"type": "object"},
                               think=False, family="qwen3")
        self.assertNotIn("response_format", b)
        self.assertEqual(b["messages"][0]["content"], "x /no_think")

    def test_response_normalization(self):
        o = R.parse_response("ollama", {"message": {"content": "<think>a</think>b", "thinking": "t"},
                                        "eval_count": 20, "eval_duration": 1e9}, 3.0)
        self.assertEqual((o["text"], o["thinking"], o["tok_s"]), ("b", "t\na", 20.0))
        o = R.parse_response("llamacpp", {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}],
                                          "usage": {"completion_tokens": 9}, "timings": {"predicted_per_second": 81.25}}, 1)
        self.assertEqual((o["tok_s"], o["finish"], o["tokens_out"]), (81.2, "length", 9))
        o = R.parse_response("mlx", {"choices": [{"message": {"content": "x"}}], "usage": {"completion_tokens": 10}}, 2.0)
        self.assertEqual(o["tok_s"], 5.0)


class TestIntegration(_Base):
    def test_llamacpp_structured_output_is_constrained_not_retried(self):
        with patch.object(R.urllib.request, "urlopen", return_value=_openai('{"ok": false}')) as uo:
            r = R.chat("qwen3-coder-30b", [{"role": "user", "content": "x"}], schema=SCHEMA,
                       candidates=[CAND["llamacpp"]])
        self.assertEqual((r["json"], uo.call_count), ({"ok": False}, 1))
        self.assertEqual(self.body(uo)["response_format"]["json_schema"]["schema"]["type"], "object")

    def test_chat_asks_nova_lb_when_no_candidates_given(self):
        with patch.object(LB, "candidates_shared", return_value=[CAND["ollama"]]) as cs, \
                patch.object(R.urllib.request, "urlopen", return_value=_ollama("<think>x</think>hey")):
            r = R.chat("qwen3:8b", [{"role": "user", "content": "x"}], schema=None)
        self.assertEqual(cs.call_args.args[0], "qwen3-8b")
        self.assertEqual((r["text"], r["accelerator"], r["tok_s"]), ("hey", "rocm", 20.0))

    def test_success_records_measured_speed(self):
        with patch.object(R.urllib.request, "urlopen", return_value=_ollama("hi")):
            R.chat("qwen3-8b", [], candidates=[CAND["ollama"]])
        R.record_perf.assert_called_once_with("nova-core7", "ollama", "qwen3-8b", 20.0)

    def test_record_perf_upserts_ewma_and_swallows_errors(self):
        patch.stopall()
        cur = MagicMock(); conn = MagicMock(); conn.cursor.return_value = cur
        with patch("psycopg2.connect", return_value=conn):
            R.record_perf("n", "mlx", "qwen3-8b", 42.0)
            self.assertIn("ON CONFLICT", cur.execute.call_args.args[0])
            self.assertEqual(cur.execute.call_args.args[1], ("n", "mlx", "qwen3-8b", 42.0, False))
            R.record_perf("n", "mlx", "qwen3-8b", 0, failed=True)
            self.assertEqual(cur.execute.call_args.args[1], ("n", "mlx", "qwen3-8b", 1.0, True))
        with patch("psycopg2.connect", side_effect=OSError("pg down")):
            R.record_perf("n", "mlx", "qwen3-8b", 1.0)    # no raise
        self.setUp()


class TestFunctional(_Base):
    def test_unknown_model(self):
        self.assertFalse(R.chat("gpt-4o", [])["success"])

    def test_cli_resolve(self):
        with patch.object(sys, "argv", ["x", "--resolve", "qwen3:30b-a3b"]), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(R.main(), 0)
        self.assertEqual(out.getvalue().strip(), "qwen3-30b-a3b")

    def test_same_question_three_runtimes_one_answer_shape(self):
        replies = {"ollama": _ollama("<think>r</think>A"), "mlx": _openai("<think>r</think>A"),
                   "llamacpp": _openai("A", timings={"predicted_per_second": 50})}
        keys = None
        for rt, reply in replies.items():
            with patch.object(R.urllib.request, "urlopen", return_value=reply):
                r = R.chat("qwen3-coder-30b" if rt == "llamacpp" else "qwen3-8b", [{"role": "user", "content": "q"}],
                           candidates=[CAND[rt]])
            self.assertEqual(r["text"], "A", rt)
            keys = keys or set(r)
            self.assertEqual(set(r), keys, rt)


class TestJoin(_Base):
    """Idle machines joining: runtime discovery, accelerator, PG record, LAN-only."""
    def test_probe_runtimes_identifies_each_server(self):
        def uo(url, timeout):
            if url in ("http://10.0.0.5:11434/api/version", "http://10.0.0.5:8080/v1/models",
                       "http://10.0.0.5:8080/props", "http://10.0.0.5:8000/v1/models"):
                return _resp({})
            raise urllib.error.URLError("closed")
        with patch.object(R.urllib.request, "urlopen", side_effect=uo):
            self.assertEqual(R.probe_runtimes("10.0.0.5"), {"ollama": 11434, "llamacpp": 8080, "openai": 8000})

    def test_join_without_ssh_infers_gpu_from_vram_and_records_row(self):
        cur = MagicMock(); conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(R, "probe_runtimes", return_value={"ollama": 11434}), \
                patch.object(R, "_remote_detect", return_value=None), \
                patch.object(R, "_get_json", return_value={"models": [{"name": "qwen3:8b", "size_vram": 5e9}]}), \
                patch("psycopg2.connect", return_value=conn):
            node = R.join("192.168.1.42")
        self.assertEqual((node["name"], node["accel"], node["gpu"]), ("node-192-168-1-42", {"ollama": "gpu"}, True))
        ins = cur.execute.call_args.args
        self.assertIn("INSERT INTO rosetta_nodes", ins[0])
        self.assertEqual(ins[1][0:2], ("node-192-168-1-42", "192.168.1.42"))

    def test_join_uses_remote_detect_and_override(self):
        cur = MagicMock(); conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(R, "probe_runtimes", return_value={"ollama": 11434, "mlx": 5050}), \
                patch.object(R, "_remote_detect", return_value={"accel": "metal", "hostname": "air"}), \
                patch("psycopg2.connect", return_value=conn):
            self.assertEqual(R.join("192.168.1.43")["accel"], {"ollama": "metal", "mlx": "mlx"})
            self.assertEqual(R.join("192.168.1.43", accel="rocm", name="lap")["accel"]["ollama"], "rocm")

    def test_join_refuses_public_ips_unknown_accels_and_dead_hosts(self):
        with self.assertRaises(ValueError):
            R.join("8.8.8.8")
        with patch.object(R, "probe_runtimes", return_value={}):
            with self.assertRaisesRegex(ValueError, "no inference runtime"):
                R.join("192.168.1.44")
        with patch.object(R, "probe_runtimes", return_value={"ollama": 11434}), \
                patch.object(R, "_remote_detect", return_value=None):
            with self.assertRaisesRegex(ValueError, "unknown accelerator"):
                R.join("192.168.1.44", accel="tpu")

    def test_loans_and_minion_names(self):
        self.assertEqual([R.parse_duration(d) for d in ("30m", "8h", "2d", "1h30m")], [1800, 28800, 172800, 5400])
        for bad in ("", "soon", "8x", "-1h"):
            with self.assertRaises(ValueError):
                R.parse_duration(bad)
        cur = MagicMock(); conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(R, "_minion_ip", return_value=("192.168.1.99", "office-2")), \
                patch.object(R, "probe_runtimes", return_value={"ollama": 11434}), \
                patch.object(R, "_remote_detect", return_value={"accel": "metal"}), \
                patch("psycopg2.connect", return_value=conn):
            node = R.join("office-2", seconds=28800)
        self.assertEqual((node["name"], node["ip"]), ("office-2", "192.168.1.99"))
        self.assertGreater(node["expires_at"], time.time() + 28000)
        self.assertEqual(cur.execute.call_args.args[1][-2:], (28800, 28800))
        with patch.object(R, "_minion_ip", return_value=None):
            with self.assertRaisesRegex(ValueError, "neither an IP"):
                R.join("nobody")

    def test_detect_local_reports_an_accelerator(self):
        info = R.detect_local()
        self.assertIn(info["accel"], R.ACCEL_PRIOR_TPS)
        self.assertIn("os", info)

    def test_join_script_is_safe_and_lan_only(self):
        sh = (SCRIPTS.parent / "deploy" / "nova-node-join.sh").read_text()
        self.assertIn("OLLAMA_KEEP_ALIVE", sh)
        self.assertIn("set -euo pipefail", sh)
        self.assertNotRegex(sh, r"(?i)(password|token|secret)=")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)

    def test_catalog_shape(self):
        for cid, m in R.CATALOG.items():
            self.assertTrue(m.artifacts, cid)
            self.assertTrue(set(m.artifacts) <= set(R.API), cid)


if __name__ == "__main__":
    unittest.main()
