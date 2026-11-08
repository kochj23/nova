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

    def test_heartbeat_fast_enough_for_a_minute_handoff(self):
        self.assertLessEqual(AGENT.BEAT_S, 10)

    def test_screensaver_session_rules(self):
        st = {}
        self.assertFalse(AGENT.saver_session(st, False, 10_000))   # display asleep, no saver: still Jordan's
        self.assertTrue(AGENT.saver_session(st, True, 300))         # saver up: Nova's
        self.assertTrue(AGENT.saver_session(st, False, 900))        # saver engine gone under a sleeping display
        self.assertFalse(AGENT.saver_session(st, False, 2))         # a key press: Jordan's again
        self.assertFalse(AGENT.saver_session(st, False, 900))       # and it stays his until the saver returns
        self.assertFalse(AGENT.saver_session(st, True, 3))          # a lingering saver process never beats input

    def test_macos26_idle_saver_is_seen_via_the_lock_screen(self):
        # 2026-11-07: macOS 26+ runs the idle saver as legacyScreenSaver, never ScreenSaverEngine — office-2 lent
        # nothing for a day and a half. The saver locks the screen, and the lock is what 1.9 watches.
        locked = '"IOConsoleUsers" = ({"kCGSSessionOnConsoleKey"=Yes,"CGSSessionScreenIsLocked"=Yes})'
        unlocked = '"IOConsoleUsers" = ({"kCGSSessionOnConsoleKey"=Yes})'
        self.assertTrue(AGENT.screensaver_up("", locked))
        self.assertTrue(AGENT.screensaver_up("17595\n", unlocked))
        self.assertFalse(AGENT.screensaver_up("", unlocked))
        self.assertFalse(AGENT.screen_locked(""))

    def test_idle_decision_uses_the_session(self):
        self.assertIn("saver_session(_state, screensaver_up(), idle_s)",
                      (SCRIPTS / "minion/nova_minion_agent.py").read_text())

    def test_versions_bumped(self):
        self.assertEqual(AGENT.VERSION, "1.9")
        self.assertEqual(PROBE.VERSION, "1.3")


if __name__ == "__main__":
    unittest.main()
