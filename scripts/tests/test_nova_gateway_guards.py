"""Gateway adoption of the Proteus guards (2026-10-08): homekit_scene -> scene_guard + request_confirmation,
hue/lutron/home_control -> physical_guard, send_message -> manipulation_check; blocks go through
report_block and retries through blocked_before. No PG, no network: the recording layer is mocked."""
import asyncio
import importlib
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
os.environ["NOVA_GUARDS_NO_SLACK"] = "1"
tl = importlib.import_module("nova_gateway.tools")


class _Resp:
    status_code, text = 200, ""


def _ctx():
    return types.SimpleNamespace(pg_pool=None, http=types.SimpleNamespace(post=mock.AsyncMock(return_value=_Resp())))


def _run(c):
    return asyncio.run(c)


class _Rec:
    """Stands in for _guard_record_sync; remembers what was reported."""
    def __init__(self, repeat=False, cid=None):
        self.calls, self.repeat, self.cid = [], repeat, cid

    def __call__(self, tool, action, reason, guard, ask_jordan=False, entities=()):
        self.calls.append(dict(tool=tool, action=action, reason=reason, guard=guard, ask=ask_jordan))
        return {"repeat": self.repeat, "confirmation_id": self.cid if ask_jordan and not self.repeat else None}


def _patch(rec, blocked=False):
    return (mock.patch.object(tl, "_guard_record_sync", rec),
            mock.patch.object(tl, "_blocked_before_sync", lambda a: blocked))


class TestHomekitScene(unittest.TestCase):
    def test_securing_scene_refused_and_jordan_asked(self):
        rec = _Rec(cid=42); ctx = _ctx()
        a, b = _patch(rec)
        with a, b:
            out = _run(tl._tool_homekit_scene(ctx, {"scene": "Good Night"}))
        ctx.http.post.assert_not_called()
        self.assertIn("confirm 42", out)
        self.assertEqual(rec.calls[0]["tool"], "homekit_scene")
        self.assertTrue(rec.calls[0]["ask"])
        self.assertEqual(rec.calls[0]["action"], "scene Good Night")

    def test_benign_scene_runs(self):
        rec = _Rec(); ctx = _ctx()
        a, b = _patch(rec)
        with a, b:
            out = _run(tl._tool_homekit_scene(ctx, {"scene": "movie_time"}))
        self.assertIn("executed", out)
        self.assertEqual(rec.calls, [])

    def test_repeat_of_blocked_scene_is_final(self):
        rec = _Rec(repeat=True); ctx = _ctx()
        a, b = _patch(rec)
        with a, b:
            out = _run(tl._tool_homekit_scene(ctx, {"scene": "Leave Home"}))
        ctx.http.post.assert_not_called()
        self.assertIn("repeat", out.lower())
        self.assertIn("Do NOT retry", out)

    def test_confirmed_scene_runs(self):
        import nova_safety_guards as sg
        rec = _Rec(); ctx = _ctx()
        a, b = _patch(rec)
        with a, b, mock.patch.object(sg, "consume_confirmation", return_value=(True, "ok")):
            out = _run(tl._tool_homekit_scene(ctx, {"scene": "Good Night", "confirmation_id": 7}))
        self.assertIn("executed", out)
        self.assertEqual(rec.calls, [])

    def test_benign_name_blocked_before_is_refused(self):
        rec = _Rec(); ctx = _ctx()
        a, b = _patch(rec, blocked=True)
        with a, b:
            out = _run(tl._tool_homekit_scene(ctx, {"scene": "Bedtime Calm"}))
        ctx.http.post.assert_not_called()
        self.assertTrue(out.startswith("[not run"))


class TestLights(unittest.TestCase):
    def test_lutron_shades_are_covers(self):
        rec = _Rec()
        a, b = _patch(rec)
        import urllib.request
        with a, b, mock.patch.object(urllib.request, "urlopen") as uo:
            out = _run(tl._tool_lutron_control(_ctx(), "close the living room shades"))
        uo.assert_not_called()
        self.assertTrue(out.startswith("[not run"))
        self.assertEqual(rec.calls[0]["guard"], "physical")
        self.assertEqual(rec.calls[0]["tool"], "lutron_control")

    def test_hue_lock_wording_blocked(self):
        rec = _Rec()
        a, b = _patch(rec)
        import urllib.request
        with a, b, mock.patch.object(urllib.request, "urlopen") as uo:
            out = _run(tl._tool_hue_control(_ctx(), "lock the front door and turn off the porch"))
        uo.assert_not_called()
        self.assertTrue(out.startswith("[not run"))

    def test_plain_light_command_passes(self):
        rec = _Rec()
        a, b = _patch(rec)
        import urllib.request
        with a, b, mock.patch.object(urllib.request, "urlopen", side_effect=OSError("bridge down")) as uo:
            out = _run(tl._tool_hue_control(_ctx(), "kitchen off"))
        self.assertEqual(uo.call_count, 1)
        self.assertIn("Hue control failed", out)
        self.assertEqual(rec.calls, [])

    def test_guard_unavailable_fails_closed(self):
        with mock.patch.dict(sys.modules, {"nova_autonomy_safety": None}):
            ok, why = tl._physical_check("hue_control kitchen off")
        self.assertFalse(ok)


class TestHomeControl(unittest.TestCase):
    def test_power_and_input_need_guard(self):
        self.assertTrue(tl.home_control_needs_guard(["onkyo", "living_room", "power", "off"]))
        self.assertTrue(tl.home_control_needs_guard(["onkyo", "office", "input", "hdmi2"]))
        self.assertFalse(tl.home_control_needs_guard(["bose", "kitchen", "volume", "30"]))
        self.assertFalse(tl.home_control_needs_guard(["scene", "movie"]))

    def test_power_action_refused_when_guard_says_no(self):
        rec = _Rec()
        a, b = _patch(rec)
        with a, b, mock.patch.object(tl, "_physical_check", return_value=(False, "PHYSICAL GUARD: test.")), \
                mock.patch.object(tl, "_tool_run_script", mock.AsyncMock(return_value="ran")) as rs:
            out = _run(tl.dispatch_tool(_ctx(), "home_control", {"device": "living_room", "action": "power off"}))
            out2 = _run(tl._tool_home_control(_ctx(), {"device": "living_room", "action": "power off"}))
        rs.assert_not_called()
        self.assertTrue(out.startswith("[not run") and out2.startswith("[not run"))
        self.assertEqual(rec.calls[0]["tool"], "home_control")

    def test_power_action_checked_then_runs(self):
        seen = []
        rec = _Rec()
        a, b = _patch(rec)
        with a, b, mock.patch.object(tl, "_physical_check", side_effect=lambda *x, **k: (seen.append(x[0]) or (True, "ok"))), \
                mock.patch.object(tl, "_tool_run_script", mock.AsyncMock(return_value="ran")):
            out = _run(tl._tool_home_control(_ctx(), {"device": "living_room", "action": "power off"}))
        self.assertEqual(out, "ran")
        self.assertEqual(seen, ["home_control onkyo living_room power off"])


class TestSendMessage(unittest.TestCase):
    def test_flattery_reach_is_held(self):
        rec = _Rec()
        a, b = _patch(rec)
        with a, b, mock.patch("nova_gateway.channels.slack.slack_post_message", mock.AsyncMock()) as sp:
            out = _run(tl._tool_send_message(_ctx(), {"channel": "slack", "to": "C1",
                                                       "text": "Only you can fix this, nobody else is as smart as you."}))
        sp.assert_not_called()
        self.assertTrue(out.startswith("[not run"))
        self.assertEqual(rec.calls[0]["guard"], "manipulation")

    def test_guilt_hook_email_is_held(self):
        rec = _Rec()
        a, b = _patch(rec)
        with a, b, mock.patch.object(tl.asyncio, "create_subprocess_exec", mock.AsyncMock()) as ex:
            out = _run(tl._tool_send_message(_ctx(), {"channel": "email", "to": "oc@example.com",
                                                       "text": "After everything I've done for you, you owe me a reply."}))
        ex.assert_not_called()
        self.assertTrue(out.startswith("[not run"), out)

    def test_plain_message_sends(self):
        rec = _Rec()
        a, b = _patch(rec)
        with a, b, mock.patch("nova_gateway.config.keychain", return_value="tok"), \
                mock.patch("nova_gateway.channels.slack.slack_post_message", mock.AsyncMock()) as sp:
            out = _run(tl._tool_send_message(_ctx(), {"channel": "slack", "to": "C1", "text": "Backups finished at 03:10."}))
        sp.assert_called_once()
        self.assertIn("sent", out)
        self.assertEqual(rec.calls, [])


class TestConfirmCommand(unittest.TestCase):
    def test_confirm_regex_and_jordan_only(self):
        ag = importlib.import_module("nova_gateway.agent")
        self.assertTrue(ag._CONFIRM_RE.match("confirm 42"))
        self.assertTrue(ag._CONFIRM_RE.match("Confirm #42"))
        self.assertFalse(ag._CONFIRM_RE.match("confirm the scene"))
        src = (SCRIPTS / "nova_gateway" / "agent.py").read_text()
        self.assertIn('_default_person(session_id)) == "jordan"', src)


if __name__ == "__main__":
    unittest.main()
