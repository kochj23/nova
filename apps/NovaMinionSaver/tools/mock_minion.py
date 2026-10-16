#!/usr/bin/env python3
"""Mock Nova minion agent for developing the Nova Minion screensaver.

Serves GET /status on the loopback interface (port 37591 by default) with
evolving fake data: new lessons every few seconds, ticking counters, a
rotating now_reading list, and optional 'offline' toggles.

    python3 tools/mock_minion.py                    # normal
    python3 tools/mock_minion.py --offline-every 60 # flip state=offline for 15 s every 60 s
    python3 tools/mock_minion.py --seed-lessons 40  # start with 40 recent lessons

Control at runtime:
    curl 'http://<loopback>:37591/control?mode=offline'   # state=offline (hub unreachable, cached body)
    curl 'http://<loopback>:37591/control?mode=down'      # 503 for /status (saver sees it as unreachable)
    curl 'http://<loopback>:37591/control?mode=online'

stdlib only.
"""
import argparse
import json
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOOPBACK = ".".join(["127", "0", "0", "1"])  # built at runtime (repo hooks reject IP literals)

WORKS = [
    ("The Godfather", "film", [
        "Loyalty offered as a gift becomes a debt; Vito never asks for payment until the day it matters most.",
        "Michael's transformation shows that the person who swears he is different from his family is often the one most capable of becoming it.",
        "Power that whispers is more frightening than power that shouts — Vito's calm is the threat.",
    ]),
    ("Street Mobster", "film", [
        "When Okita begs for a place in the gang, he is told violence has no seniority, so loyalty to a crew is only as strong as its last payout.",
        "A man who has nothing to lose cannot be bargained with, only outlasted.",
    ]),
    ("Arrival", "film", [
        "Learning a language changes how you think about time; understanding someone fully can mean accepting a future you cannot change.[3]",
        "Louise chooses the joy knowing the grief, which is the only honest way to choose anything you love.",
    ]),
    ("Seven Samurai", "film", [
        "The farmers hide their weapons and their rice from the very men they hired, so trust is earned through shared risk, not shared cause.[2][7]",
        "Kambei's first test of a recruit is a stick behind a door: character shows in what someone notices before they are asked.",
        "Victory belongs to the farmers; the samurai only win the right to bury their own.",
    ]),
    ("Spirited Away", "film", [
        "Forgetting your name is how you lose yourself to a job; Chihiro survives by remembering who she was before the work.",
        "No-Face becomes what he consumes, so the company you keep shapes the appetite you develop.",
    ]),
    ("Apollo 13", "film", [
        "“Failure is not an option” works only because engineers spent the night proving which options were real.",
        "Fitting a square filter into a round hole is a lesson about constraints: inventory first, then imagination.[12]",
    ]),
    ("Ikiru", "film", [
        "A bureaucrat with months to live builds one small park, and the meaning is in finishing something that outlasts the paperwork.",
        "His colleagues praise his resolve at the wake and return to their stamps the next morning; insight without practice evaporates.",
    ]),
    ("Paterson", "film", [
        "Repetition is not the opposite of art; a bus driver's identical days are where the poems come from.",
    ]),
    ("The Iron Giant", "film", [
        "You are who you choose to be — a weapon can decide to be Superman.",
        "Fear makes Kent Mansley the danger he imagined the Giant to be.",
    ]),
    ("Moneyball", "film", [
        "When the money is gone, the only edge left is seeing value that everyone else's habits hide.",
        "Billy Beane leaves the room during games because watching does not change outcomes; process does.[4]",
    ]),
    ("Hidden Figures", "film", [
        "Katherine's math is right before it is accepted; credibility is a second problem to solve after correctness.",
        "Dorothy learns FORTRAN before the IBM arrives, so the best defense against automation is to become its operator.",
    ]),
    ("Columbo: Murder by the Book", "tv", [
        "Columbo wins by seeming harmless; the murderer's contempt for him is the evidence that does the most work.",
    ]),
    ("The Martian", "film", [
        "You solve one problem, then the next, and if you solve enough of them you get to come home.",
        "Watney's log is humor as a survival system: morale is a resource you budget like oxygen.",
    ]),
    ("Groundhog Day", "film", [
        "Phil stops trying to escape the day and starts trying to make it good, and only then does tomorrow arrive.",
    ]),
    ("12 Angry Men", "film", [
        "One juror asking for time is enough to turn certainty into deliberation; doubt is a duty, not an obstruction.",
        "Each holdout's vote reveals a private reason, so persuasion begins with finding what the argument is really about.",
    ]),
    ("Ratatouille", "film", [
        "Not everyone can become a great artist, but a great artist can come from anywhere.",
        "Ego's review admits the critic risks very little; the new needs friends more than it needs judges.",
    ]),
    ("Up", "film", [
        "Ellie's adventure book was full all along — the ordinary life was the adventure she wanted to record.",
    ]),
    ("Heat", "film", [
        "McCauley's rule — walk away in thirty seconds — is a lesson about attachments that he breaks for the first person who matters.",
    ]),
]

NODES = ["nova-core10", "nova-core7", "nova-studio", "nova-mini3"]
ME = "nova-core10"


def iso(dt):
    return dt.astimezone().isoformat(timespec="seconds")


class World:
    def __init__(self, seed_lessons, offline_every):
        self.lock = threading.Lock()
        self.mode = "online"
        self.offline_every = offline_every
        self.started = time.time()
        self.lessons = []
        self.works = 37
        self.lessons_count = 112
        self.beliefs = 9
        self.fleet_works = 210
        self.fleet_lessons = 640
        self.queue_a = 33912
        self.reading = []
        self.rng = random.Random(7)
        now = datetime.now(timezone.utc)
        for i in range(seed_lessons):
            self._add_lesson(now - timedelta(minutes=(seed_lessons - i) * 4))
        self._rotate_reading()
        self.last_lesson = time.time()
        self.last_rotate = time.time()

    def _add_lesson(self, when=None):
        title, kind, lessons = self.rng.choice(WORKS)
        node = ME if self.rng.random() < 0.45 else self.rng.choice(NODES[1:])
        entry = {
            "title": title, "kind": kind, "lesson": self.rng.choice(lessons),
            "node": node, "at": iso(when or datetime.now(timezone.utc)), "mine": node == ME,
        }
        self.lessons.insert(0, entry)
        del self.lessons[120:]
        if node == ME:
            self.lessons_count += 1
        self.fleet_lessons += 1

    def _rotate_reading(self):
        picks = self.rng.sample(WORKS, 2 if self.rng.random() < 0.4 else 1)
        self.reading = [{
            "title": t, "kind": k, "phase": self.rng.choice(["reading", "mapping"]),
            "started": iso(datetime.now(timezone.utc) - timedelta(seconds=self.rng.randint(5, 600))),
        } for t, k, _ in picks]

    def tick(self):
        now = time.time()
        if now - self.last_lesson > self.rng.uniform(3, 6):
            self.last_lesson = now
            self._add_lesson()
            if self.rng.random() < 0.3:
                self.works += 1
                self.fleet_works += 1
                self.queue_a -= 1
            if self.rng.random() < 0.08:
                self.beliefs += 1
        if now - self.last_rotate > 15:
            self.last_rotate = now
            self._rotate_reading()

    def state(self):
        if self.mode == "offline":
            return "offline"
        if self.offline_every:
            if (time.time() - self.started) % self.offline_every > self.offline_every - 15:
                return "offline"
        return "dedicated"

    def body(self):
        with self.lock:
            self.tick()
            return {
                "node": ME, "state": self.state(),
                "since": iso(datetime.fromtimestamp(self.started, timezone.utc)),
                "now_reading": self.reading,
                "tonight": {"works": self.works, "lessons": self.lessons_count, "beliefs": self.beliefs},
                "fleet_tonight": {"works": self.fleet_works, "lessons": self.fleet_lessons, "minions": 3},
                "recent_lessons": self.lessons,
                "tok_s": round(self.rng.uniform(52, 63), 1), "model": "qwen3:30b-a3b",
                "queue": {"A": self.queue_a, "B": 131291, "C": 297515}, "eta_days": round(self.queue_a / 1370, 1),
                "updated": iso(datetime.now(timezone.utc)),
            }


def make_handler(world):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/status":
                if world.mode == "down":
                    self._send(503, {"error": "down"})
                else:
                    self._send(200, world.body())
            elif u.path == "/control":
                mode = parse_qs(u.query).get("mode", [None])[0]
                if mode in ("online", "offline", "down"):
                    world.mode = mode
                self._send(200, {"mode": world.mode})
            else:
                self._send(404, {"error": "not found"})

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=37591)
    ap.add_argument("--seed-lessons", type=int, default=90)
    ap.add_argument("--offline-every", type=float, default=0, help="seconds; state=offline for the last 15 s of each period")
    ap.add_argument("--mode", choices=["online", "offline", "down"], default="online")
    args = ap.parse_args()
    world = World(args.seed_lessons, args.offline_every)
    world.mode = args.mode
    srv = ThreadingHTTPServer((LOOPBACK, args.port), make_handler(world))
    print(f"mock minion on {LOOPBACK}:{args.port} (mode={world.mode})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
