#!/usr/bin/env python3
"""Tests for nova_pet_recognition.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import base64
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
SCRIPT = SCRIPTS / "nova_pet_recognition.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="pet-test-"))
IMG = TMP / "crop.jpg"
IMG.write_bytes(b"\xff\xd8not-really-a-jpeg\xff\xd9")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr = _load("pet_recognition_under_test", SCRIPT)
PETS = [{"name": "Bailey", "species": "dog", "description": "small tan long-haired dog"},
        {"name": "Mochi", "species": "cat", "description": "grey tabby"}]


class _Cur:
    def __init__(self, rows):
        self.rows, self.sql, self.params = rows, [], []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cur):
        self.cur, self.closed = cur, False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


class _Resp:
    def __init__(self, content):
        self._d = json.dumps({"message": {"content": content}}).encode()

    def read(self):
        return self._d


def _vlm(answer):
    return patch.object(pr.urllib.request, "urlopen", MagicMock(return_value=_Resp(answer)))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_local_only_model(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertTrue(pr.OLLAMA.startswith("http://127.0.0.1:11434/"))
        self.assertNotIn("password", pr.PG_DSN)

    def test_sql_is_parameterized_and_writes_stay_in_pet_registry(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"pet_registry"})
        cur = _Cur([])
        with patch.object(pr.psycopg2, "connect", return_value=_Conn(cur)):
            pr.set_pet("Rex'; DROP TABLE pet_registry; --", "dog", "d")
        ins = [(s, p) for s, p in zip(cur.sql, cur.params) if "INSERT" in s][0]
        self.assertNotIn("DROP", ins[0]); self.assertEqual(ins[1][0], "Rex'; DROP TABLE pet_registry; --")


class TestPerformance(unittest.TestCase):
    def test_roster_prompt_and_match_scale_to_10k_pets(self):
        pets = [{"name": f"Pet{i}", "species": "dog", "description": f"desc {i}"} for i in range(10_000)]
        seen = {}

        def fake_vlm(path, prompt, npredict=400):
            seen["prompt"] = prompt
            return "Pet9999."
        t0 = time.perf_counter()
        with patch.object(pr, "list_pets", return_value=pets), patch.object(pr, "_vlm", fake_vlm):
            self.assertEqual(pr.identify_pet(str(IMG)), "Pet9999")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(seen["prompt"].count("\n- Pet"), 10_000)


class TestRetry(unittest.TestCase):
    def test_vision_backend_failure_escapes_identify_and_describe(self):
        # RETRY GAP: _vlm/urlopen — one 60 s attempt; the exception propagates so the caller (camera gate) decides
        with patch.object(pr, "list_pets", return_value=PETS), \
             patch.object(pr.urllib.request, "urlopen", side_effect=OSError("ollama down")):
            with self.assertRaises(OSError):
                pr.identify_pet(str(IMG))
            with self.assertRaises(OSError):
                pr.describe(str(IMG))

    def test_registry_read_failure_escapes_before_any_vision_call(self):
        # RETRY GAP: list_pets()/psycopg2.connect — one attempt, no fallback roster
        uo = MagicMock()
        with patch.object(pr.psycopg2, "connect", side_effect=OSError("pg down")), \
             patch.object(pr.urllib.request, "urlopen", uo):
            with self.assertRaises(OSError):
                pr.identify_pet(str(IMG))
        uo.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_think_block_is_stripped(self):
        with _vlm("<think>it looks fluffy</think>\nBailey"):
            self.assertEqual(pr._vlm(str(IMG), "p"), "Bailey")
        with _vlm("<think>never closed Bailey"):
            self.assertEqual(pr._vlm(str(IMG), "p"), "<think>never closed Bailey")
        with _vlm(""):
            self.assertEqual(pr._vlm(str(IMG), "p"), "")
        with patch.object(pr.urllib.request, "urlopen", MagicMock(return_value=types.SimpleNamespace(read=lambda: b"{}"))):
            self.assertEqual(pr._vlm(str(IMG), "p"), "")

    def test_identify_matches_loosely_and_returns_none_for_strangers(self):
        with patch.object(pr, "list_pets", return_value=PETS):
            with _vlm('"bailey."'):
                self.assertEqual(pr.identify_pet(str(IMG)), "Bailey")
            with _vlm("That is MOCHI the cat"):
                self.assertEqual(pr.identify_pet(str(IMG)), "Mochi")
            with _vlm("UNKNOWN_ANIMAL"):
                self.assertIsNone(pr.identify_pet(str(IMG)))
            with _vlm("NONE"):
                self.assertIsNone(pr.identify_pet(str(IMG)))

    def test_empty_registry_short_circuits_without_a_vision_call(self):
        uo = MagicMock()
        with patch.object(pr, "list_pets", return_value=[]), patch.object(pr.urllib.request, "urlopen", uo):
            self.assertIsNone(pr.identify_pet(str(IMG)))
        uo.assert_not_called()


class TestIntegration(unittest.TestCase):
    def test_vlm_payload_shape_matches_ollama_chat_with_inline_image(self):
        uo = MagicMock(return_value=_Resp("ok"))
        with patch.object(pr.urllib.request, "urlopen", uo):
            pr.describe(str(IMG))
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, pr.OLLAMA)
        body = json.loads(req.data)
        self.assertEqual(body["model"], pr.MODEL); self.assertFalse(body["stream"])
        self.assertEqual(body["messages"][0]["images"], [base64.b64encode(IMG.read_bytes()).decode()])
        self.assertEqual(body["options"], {"temperature": 0.1, "num_predict": 400})
        self.assertIn("Describe ONLY the animal", body["messages"][0]["content"])

    def test_identify_prompt_carries_the_registry_roster(self):
        uo = MagicMock(return_value=_Resp("NONE"))
        with patch.object(pr, "list_pets", return_value=PETS), patch.object(pr.urllib.request, "urlopen", uo):
            pr.identify_pet(str(IMG))
        prompt = json.loads(uo.call_args[0][0].data)["messages"][0]["content"]
        self.assertIn("- Bailey: small tan long-haired dog", prompt); self.assertIn("- Mochi: grey tabby", prompt)
        self.assertIn("reply 'UNKNOWN_ANIMAL'", prompt)

    def test_set_pet_ensures_schema_then_upserts_on_a_dict_cursor_connection(self):
        cur = _Cur([])
        with patch.object(pr.psycopg2, "connect", return_value=_Conn(cur)) as pg:
            pr.set_pet("Bailey", "dog", "tan")
        self.assertEqual(pg.call_args[1]["cursor_factory"], pr.RealDictCursor)
        self.assertIn("CREATE TABLE IF NOT EXISTS pet_registry", cur.sql[0])
        self.assertIn("ON CONFLICT (name) DO UPDATE", cur.sql[1]); self.assertEqual(cur.params[1], ("Bailey", "dog", "tan"))


class TestFunctional(unittest.TestCase):
    def _cli(self, *argv, rows=PETS, answer="Bailey"):
        cur = _Cur(rows)
        out = io.StringIO()
        with patch.object(pr.psycopg2, "connect", return_value=_Conn(cur)), _vlm(answer), \
             patch.object(sys, "argv", ["nova_pet_recognition.py", *argv]), redirect_stdout(out):
            pr._cli()
        return out.getvalue(), cur

    def test_golden_identify_and_list(self):
        out, _ = self._cli("identify", str(IMG))
        self.assertEqual(out.strip(), "Bailey")
        out, cur = self._cli("list")
        self.assertIn("Bailey       [dog] — small tan long-haired dog", out)
        self.assertIn("ORDER BY name", cur.sql[0])

    def test_add_persists_and_unknown_animal_is_reported(self):
        out, cur = self._cli("add", "Rex", "dog", "big black lab")
        self.assertEqual(out.strip(), "saved Rex")
        self.assertEqual(cur.params[-1], ("Rex", "dog", "big black lab"))
        out, _ = self._cli("identify", str(IMG), answer="UNKNOWN_ANIMAL")
        self.assertEqual(out.strip(), "UNKNOWN_ANIMAL/NONE")

    def test_error_path_unknown_command_prints_usage(self):
        out, cur = self._cli("frobnicate")
        self.assertIn("nova_pet_recognition.py identify <image>", out)
        self.assertEqual(cur.sql, [])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_the_cli(self):
        self.assertIn('if __name__ == "__main__":\n    _cli()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_pet_recognition"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
