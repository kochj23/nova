#!/usr/bin/env python3
"""2026-10-19 Studio crash fixes in the 7 house categories (Security, Performance, Retry, Unit, Integration,
Functional, Frame):
  * nova_finetune._generate_all caps MLX (64 GB memory, 4 GB cache) BEFORE loading the model and clears the cache
    after (commit dda27b1) — the uncapped eval grew to 323 GB and watchdog-panicked the Studio;
  * scheduler.yaml media_reingest_minions timeout covers 240 min of claiming + one FILE_TIMEOUT_S drain (03b57bb).
MLX and mlx_lm are always stubs: nothing loads a model. Written by Jordan Koch (via Claude)."""
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

SCRIPTS = Path(__file__).resolve().parents[1]
CONFIG = SCRIPTS.parent / "config" / "scheduler.yaml"
sys.path.insert(0, str(SCRIPTS))
import nova_finetune as ft  # noqa: E402

STUDIO_RAM = 512 << 30


def mlx_stubs(texts=None, fail=False):
    """sys.modules entries for mlx.core / mlx_lm / mlx_lm.sample_utils; `events` records the call order."""
    events = []
    mx = types.ModuleType("mlx.core")
    mx.set_memory_limit = lambda n: events.append(("memory_limit", n))
    mx.set_cache_limit = lambda n: events.append(("cache_limit", n))
    mx.clear_cache = lambda: events.append(("clear_cache",))
    mx.random = types.SimpleNamespace(seed=lambda s: events.append(("seed", s)))
    mlx = types.ModuleType("mlx"); mlx.core = mx
    tok = MagicMock()
    tok.apply_chat_template.side_effect = lambda msgs, **k: msgs[-1]["content"]
    tok.encode.side_effect = lambda s, **k: [len(s)]

    def load(path, adapter_path=None):
        events.append(("load", adapter_path))
        return object(), tok

    def batch_generate(model, tok_, prompts, **k):
        events.append(("generate", len(prompts)))
        if fail:
            raise MemoryError("kv cache")
        return types.SimpleNamespace(texts=texts or [f"<think>x</think> reply {i}" for i in range(len(prompts))])
    lm = types.ModuleType("mlx_lm"); lm.load = load; lm.batch_generate = batch_generate
    su = types.ModuleType("mlx_lm.sample_utils"); su.make_sampler = lambda **k: ("sampler", k)
    return {"mlx": mlx, "mlx.core": mx, "mlx_lm": lm, "mlx_lm.sample_utils": su}, events


def run(prompts=("a", "b"), samples=2, **kw):
    mods, events = mlx_stubs(**kw)
    with patch.dict(sys.modules, mods), patch.object(ft, "base_path", return_value="/models/qwen3-8b"):
        out = ft._generate_all(list(prompts), None, "system", samples=samples)
    return out, events


def reingest_task():
    return yaml.safe_load(CONFIG.read_text())["tasks"]["media_reingest_minions"]


class TestSecurity(unittest.TestCase):
    def test_cap_is_bounded_never_unlimited(self):
        _, ev = run()
        mem = dict((e[0], e[1]) for e in ev if len(e) == 2)
        self.assertGreater(mem["memory_limit"], 0)
        self.assertLessEqual(mem["memory_limit"], 64 << 30)
        self.assertLessEqual(mem["cache_limit"], 4 << 30)

    def test_no_other_mlx_entry_point_loads_uncapped(self):
        src = (SCRIPTS / "nova_finetune.py").read_text()
        for m in re.finditer(r"\bload\(base_path\(\)", src):
            before = src[max(0, m.start() - 600):m.start()]
            self.assertIn("set_memory_limit", before, "an MLX load without a memory cap right before it")


class TestPerformance(unittest.TestCase):
    def test_cap_leaves_the_studio_room_for_the_llama_servers(self):
        _, ev = run()
        self.assertLessEqual(dict(e for e in ev if len(e) == 2)["memory_limit"], STUDIO_RAM // 4)

    def test_wrapper_overhead_is_negligible(self):
        t = time.perf_counter()
        run(prompts=[f"p{i}" for i in range(500)])
        self.assertLess(time.perf_counter() - t, 1.0)


class TestRetry(unittest.TestCase):
    def test_a_retry_after_failure_is_capped_again_first(self):
        with self.assertRaises(MemoryError):
            run(fail=True)
        _, ev = run()
        self.assertEqual(ev[0][0], "memory_limit")

    def test_reingest_timeout_survives_one_slow_file_after_the_window(self):
        t = reingest_task()
        minutes = int(t["args"][t["args"].index("--minutes") + 1])
        file_timeout = int(eval(re.search(r"^FILE_TIMEOUT_S\s*=\s*([\d *]+)", (SCRIPTS / "nova_media_reingest.py")
                                          .read_text(), re.M).group(1)))  # noqa: S307 — literal arithmetic only
        self.assertGreaterEqual(t["timeout"], minutes * 60 + file_timeout)


class TestUnit(unittest.TestCase):
    def test_cap_set_before_load_and_cache_cleared_after_generation(self):
        _, ev = run()
        names = [e[0] for e in ev]
        self.assertLess(names.index("memory_limit"), names.index("load"))
        self.assertLess(names.index("cache_limit"), names.index("load"))
        self.assertGreater(names.index("clear_cache"), names.index("generate"))


class TestIntegration(unittest.TestCase):
    def test_scheduler_config_parses_and_keeps_overlap_skip(self):
        t = reingest_task()
        self.assertEqual(t["script"], "nova_media_reingest.py")
        self.assertEqual(t["overlap"], "skip")
        self.assertEqual(t["timeout"], 21600)


class TestFunctional(unittest.TestCase):
    def test_replies_grouped_per_prompt_with_think_stripped(self):
        out, ev = run(prompts=("a", "b", "c"), samples=2)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(len(r) == 2 for r in out))
        self.assertTrue(all("<think>" not in x for r in out for x in r))
        self.assertIn(("generate", 6), ev)


class TestFrame(unittest.TestCase):
    def test_finetune_selftest_cli(self):
        p = subprocess.run([sys.executable, str(SCRIPTS / "nova_finetune.py"), "--selftest"], capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])

    def test_import_does_not_load_mlx(self):
        self.assertNotIn("mlx.core", ft.__dict__)


if __name__ == "__main__":
    unittest.main()
