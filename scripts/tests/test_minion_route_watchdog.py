"""Route watchdog (2026-11-06): the minion agent and the LLM probe exit for a fresh process after ten minutes of
nothing but EHOSTUNREACH, the macOS failure that left nova-core10 silent for five days until a restart."""
import errno
import importlib.util
import unittest
import urllib.error
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AGENT = load("nova_minion_agent", "minion/nova_minion_agent.py")
PROBE = load("nova_llm_probe", "cluster/nova_llm_probe.py")
NO_ROUTE = urllib.error.URLError(OSError(errno.EHOSTUNREACH, "No route to host"))
TIMEOUT = urllib.error.URLError(OSError(errno.ETIMEDOUT, "Operation timed out"))


class TestRouteWatchdog(unittest.TestCase):
    def test_only_no_route_counts(self):
        for m in (AGENT, PROBE):
            self.assertTrue(m.no_route(NO_ROUTE))
            self.assertFalse(m.no_route(TIMEOUT))
            self.assertFalse(m.no_route(urllib.error.HTTPError("u", 503, "x", {}, None)))

    def test_exits_only_after_ten_minutes_of_no_route(self):
        for m in (AGENT, PROBE):
            w = {}
            self.assertFalse(m.route_watch(w, NO_ROUTE, 0))
            self.assertFalse(m.route_watch(w, NO_ROUTE, m.ROUTE_RESET_S - 1))
            self.assertTrue(m.route_watch(w, NO_ROUTE, m.ROUTE_RESET_S))

    def test_a_success_or_another_error_resets_the_clock(self):
        for m in (AGENT, PROBE):
            w = {}
            m.route_watch(w, NO_ROUTE, 0)
            m.route_watch(w, None, 300)
            self.assertFalse(m.route_watch(w, NO_ROUTE, 700))
            m.route_watch(w, TIMEOUT, 800)
            self.assertFalse(m.route_watch(w, NO_ROUTE, 1300))

    def test_window_is_ten_minutes(self):
        self.assertEqual(AGENT.ROUTE_RESET_S, 600)
        self.assertEqual(PROBE.ROUTE_RESET_S, 600)

    def test_versions_bumped(self):
        self.assertEqual(AGENT.VERSION, "1.6")
        self.assertEqual(PROBE.VERSION, "1.3")


if __name__ == "__main__":
    unittest.main()
