#!/usr/bin/env python3
"""nova_game_video.py — marketing videos of Little Mister's games playing themselves (AI vs AI), narrated by Nova.

Little Mister, 2026-11-12: every Nova Games title in its "Watch AI play" mode, recorded on the Studio's 4K screen,
narrated in Nova's voice from what REALLY happened (the Games DB row the watch game writes, player "AI vs AI"),
uploaded PUBLIC to the YouTube playlist "Ai Generated Content", linked from each repo's README.
Two videos per game: a 2-3 minute highlight cut and the full session (dead air sped up, said on screen).

Pipeline (state: nova_ops.game_marketing_videos; files under /Volumes/Data/AI/gamevids):
  record  --slug S --window ID     winrec (ScreenCaptureKit): one window + that app's own audio, until SIGINT;
                                   S.json holds the wall-clock start so DB event timestamps map onto the video
  facts   --slug S --session ID    spectator_facts() from nova_game_report (read-only play_reader) + a timeline of
                                   events in seconds since the recording started -> S.facts.json
  script  --slug S --kind K        Nova writes timed beats (JSON) from the fact sheet only (nova_game_report's
                                   writer chain; numbers not on the sheet are cut, chess moves checked) -> S.K.beats.json
  render  --slug S --kind K        nova_speaks_narration.spoken (deterministic spoken form) -> XTTS "Gracie Wise"
                                   with Whisper BackCheck (nova_speaks.speak) -> title card + gameplay + end card,
                                   1080p H.264 + AAC -> out/S-K.mp4
  upload  --slug S --kind K        youtube-up (Safari cookie jar via nova_speaks_upload.session), PUBLIC, into the
                                   "Ai Generated Content" playlist resolved BY TITLE (never created)
K is "highlights" (beats -> clips cut around each beat, ~2-3 min) or "full" (whole recording; stretches with no
events sped up 4x/8x with an on-screen badge; beats placed at their event times).
Games: GAMES below (name, client, repo, what it is). Game audio is not captured (no loopback device on the Studio),
so the soundtrack is Nova's narration only; none of the watch modes play sound anyway.

Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse, json, os, re, signal, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path("/Volumes/Data/AI/gamevids")
REC, WORK, OUT = ROOT / "rec", ROOT / "work", ROOT / "out"
DSN = "host=localhost dbname=nova_ops user=kochj"
PLAYLIST_TITLE = "Ai Generated Content"
FPS = 30
W, H = 1920, 1080
HL_MAX_WORDS = 270     # highlight narration budget: ~0.6 s per word + cards keeps the cut under 3 minutes
IG_MAX_WORDS = 125     # the Instagram cut must stay under 90 s (XTTS reads ~120 wpm)
# kind "ig": the 9:16 1080x1920 Instagram Reels demo (old-style look reframed, burned phrase captions, -14 LUFS,
# not uploaded). Its game crop (fractions x, y, w, h of the window) keeps the action big on a phone.
VCROP = {"gtnw": (0.10, 0.12, 0.80, 0.86), "gtnwchess": (0.0, 0.035, 0.80, 0.965)}
GAP_FAST = 12.0        # seconds with no event beyond which the full cut fast-forwards
KEEP_AFTER = 5.0       # seconds kept at 1x after each event / before the next
DISCLAIMER = "Narration is an AI voice (XTTS, 'Gracie Wise'). Written by Nova."
BOILERPLATE = ("Nova is Jordan Koch's AI advisor.\n\n"
               "Journal here - https://nova.digitalnoise.net/start-here/\n"
               "Code and Capabilities here - https://github.com/kochj23/nova")

GAMES = {   # key -> display name, Games DB (game, client), GitHub repo, one-line pitch
    "gtnw": ("GTNW", ("gtnw", "mac"), "https://github.com/kochj23/GTNW",
             "Global Thermal Nuclear War, a Cold War presidential strategy simulator for macOS: diplomacy, budgets, "
             "covert ops, wars and the nuclear ladder, turn by turn."),
    "gtnwchess": ("GTNW Chess", ("gtnwchess", "mac"), "https://github.com/kochj23/GTNWChess",
                  "A native macOS chess game with a GTNW skin: Solar vs Void robot factions, Stockfish analysis after "
                  "every game."),
    "cartest": ("CarTest", ("cartest", "mac"), "https://github.com/kochj23/CarTest",
                "A car performance simulator for macOS: drag races on a Christmas tree, acceleration runs and lap "
                "times on real tracks."),
    "stuntdriver": ("Stunt Driver", ("stuntdriver", "mac"), "https://github.com/kochj23/StuntDriver",
                    "A native macOS rebuild of the 1990 3D stunt-racing classic: loops, jumps and a drawbridge."),
    "blompie": ("Blompie", ("blompie-mac", "mac"), "https://github.com/kochj23/Blompie",
                "An endless, slightly absurd Zork-style text adventure for macOS, narrated by a local AI model."),
    "gtnw-lite": ("GTNW Lite", ("gtnw", "web"), "https://github.com/kochj23/nova-games",
                  "The browser edition of GTNW, the Cold War presidential strategy game, on the Nova Games site."),
    "cartest-web": ("CarTest Web", ("cartest", "web"), "https://github.com/kochj23/nova-games",
                    "The browser edition of CarTest, the drag-racing and car performance simulator."),
    "blompie-web": ("Blompie Web", ("blompie", "web"), "https://github.com/kochj23/nova-games",
                    "The browser edition of Blompie, the endless absurd text adventure narrated by a local AI model."),
    "mush": ("Web-Pennmush", ("mush", "web"), "https://github.com/kochj23/Web-Pennmush",
             "A modern web MUSH: a multi-user text world with rooms, quests and AI-voiced NPCs."),
}


PRIVATE_REPOS = {"https://github.com/kochj23/CarTest", "https://github.com/kochj23/StuntDriver",
                 "https://github.com/kochj23/nova-games"}   # 2026-11-12: never call these open source


def is_public(game_key):
    return GAMES[game_key][2] not in PRIVATE_REPOS


def log(m): print(f"[game-video {time.strftime('%H:%M:%S')}] {m}", flush=True)


def run(cmd, **k):
    return subprocess.run(cmd, check=True, **k)


def ff(*args):
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)])


def dur(path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 str(path)], capture_output=True, text=True).stdout.strip() or 0)


def meta(slug) -> dict:
    p = REC / f"{slug}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_meta(slug, **k):
    m = meta(slug); m.update(k); (REC / f"{slug}.json").write_text(json.dumps(m, indent=1, default=str))


def db():
    import psycopg2
    c = psycopg2.connect(DSN); c.autocommit = True
    c.cursor().execute("""CREATE TABLE IF NOT EXISTS game_marketing_videos (
        slug text, kind text, game text, session_id text, matchup text, title text, mp4 text, seconds numeric,
        youtube_id text, uploaded_at timestamptz, created_at timestamptz DEFAULT now(), PRIMARY KEY (slug, kind))""")
    return c


# ── record ──────────────────────────────────────────────────────────────────────────────────────────────────

WINREC = ROOT / "winrec" / "winrec"   # ScreenCaptureKit single-window recorder (winrec.swift next to it)


def find_window(app, title=None):
    """Largest window of `app` (ScreenCaptureKit window id)."""
    rows = [l.split("\t") for l in subprocess.run([str(WINREC), "list"], capture_output=True, text=True).stdout.splitlines()]
    rows = [r for r in rows if len(r) >= 4 and r[1] == app and (title is None or title in r[2])]
    if not rows:
        raise SystemExit(f"no window for {app!r}")
    area = lambda r: int(r[3].split("x")[0]) * int(r[3].split("x")[1])  # noqa: E731
    return int(max(rows, key=area)[0])


def cmd_record(a):
    """Record ONLY the game window (ScreenCaptureKit desktop-independent window: nothing overlapping it, no desktop,
    menu bar, Dock or notifications) until SIGINT/SIGTERM. Little Mister 2026-11-12: never a screen grab + crop."""
    REC.mkdir(parents=True, exist_ok=True)
    out = REC / f"{a.slug}.mov"
    wid = a.window or find_window(a.app, a.title)
    p = subprocess.Popen([str(WINREC), str(wid), str(out), str(FPS)], stdout=subprocess.PIPE, text=True)
    fwd = lambda *_: p.send_signal(signal.SIGINT)  # noqa: E731
    signal.signal(signal.SIGTERM, fwd); signal.signal(signal.SIGINT, fwd)
    log(f"recording window {wid} -> {out} (pid {p.pid})")
    for line in p.stdout:
        if line.startswith("START "):
            start = float(line.split()[1])
            save_meta(a.slug, rec_start=start, rec_start_iso=datetime.fromtimestamp(start, timezone.utc).isoformat(),
                      mov=str(out), window=wid)
            log(f"first frame at {start}")
        elif line.startswith("STOP"):
            break
    p.wait()
    save_meta(a.slug, rec_seconds=dur(out))
    log(f"stopped: {dur(out):.1f}s")


# ── facts ───────────────────────────────────────────────────────────────────────────────────────────────────

NOISE_KINDS = {"ply"}


def timeline(events, t0) -> list[dict]:
    import nova_game_report as r
    red = [w.lower() for w in r.DEFAULTS["redline"]]
    # GTNW Chess logs its analysed `move` events after the game; the live `ply` stream has the real time of each move
    ply_ts = {(e.get("detail") or {}).get("ply"): e["ts"] for e in events if e["kind"] == "ply" and e.get("ts")}
    out = []
    for e in events:
        if e["kind"] in NOISE_KINDS or e.get("ts") is None:
            continue
        d = r.sanitize({k: v for k, v in (e.get("detail") or {}).items() if k not in r._SPECT_PRIVATE}, red) or {}
        ts = ply_ts.get(d.get("ply"), e["ts"]) if e["kind"] == "move" else e["ts"]
        out.append({"t": round(ts.timestamp() - t0, 1), "kind": e["kind"], **d})
    return sorted(out, key=lambda x: x["t"])


def thin(tl, cap=260):
    """Keep the opening, the ending and an even sample of the middle (the long GTNW games log 1,500 events)."""
    if len(tl) <= cap:
        return tl
    head, tail = tl[:60], tl[-100:]
    mid = tl[60:-100]
    step = len(mid) / (cap - 160)
    return head + [mid[int(i * step)] for i in range(cap - 160)] + tail


def cmd_facts(a):
    import nova_game_report as r
    cfg = dict(r.DEFAULTS)
    m = meta(a.slug)
    c = r.games_connect(cfg); cur = c.cursor()
    s = r.fetch_candidates(cur, cfg, only=a.session)[0]
    cur.execute("SELECT seq, ts, kind, detail FROM play.events WHERE session_id=%s ORDER BY seq", (a.session,))
    events = [{"seq": x[0], "ts": x[1], "kind": x[2], "detail": x[3]} for x in cur.fetchall()]
    facts = r.spectator_facts(s, [{k: e[k] for k in ("seq", "kind", "detail")} for e in events], cfg)
    t0 = m.get("rec_start") or events[0]["ts"].timestamp()
    tl = timeline(events, t0)
    facts["timeline_note"] = "t = seconds since the recording started (the game's first move is near the first t)"
    facts["timeline"] = thin(tl)
    facts.pop("events", None)     # the timeline replaces the trimmed event list
    (WORK / f"{a.slug}.facts.json").write_text(json.dumps(facts, indent=1, default=str, ensure_ascii=False))
    save_meta(a.slug, session=a.session, game_key=a.game, matchup=a.display or facts["matchup_title"],
              event_times=[e["t"] for e in tl if e["t"] >= 0], outcome=facts["outcome"],   # before the recording: off screen
              game_start_offset=max(0.0, next((e["t"] for e in tl if e["t"] >= 0), 1.0) - 1.0),
              game_end_offset=(tl[-1]["t"] + a.tail) if tl else None)
    log(f"facts: {len(tl)} events, matchup {facts['matchup_title']!r}, outcome {facts['outcome']!r}")


# ── script ──────────────────────────────────────────────────────────────────────────────────────────────────

def build_prompt(facts, kind, game_key, video_seconds, models_note):
    name, _, repo, pitch = GAMES[game_key]
    span = facts["timeline"][-1]["t"] if facts["timeline"] else video_seconds
    outro = ("that the game is free and open source on GitHub (link in the description)" if is_public(game_key) else
             "where to find it (link in the description); do NOT call it open source or free")
    if kind == "ig":
        shape = ("5 to 7 beats, 95 to 120 words in total (a 60 to 90 second vertical clip of the best stretch for "
                 "Instagram). Beat 1 at t=0: what the game is and the matchup by name, in one or two short sentences. "
                 "Then the best moments in time order, each at the t of the event it describes. The last beat at the "
                 f"final event's t: how it ended, the result, and {outro}.")
    elif kind == "highlights":
        shape = ("7 to 9 beats, 220 to 280 words in total (the cut must stay under three minutes). Beat 1 at t=0: "
                 "welcome, what the game is, and the matchup by name. Then one beat per key moment or turning point, in "
                 "time order, each at the t of the event it describes. The last beat at the final event's t: how it "
                 f"ended, the result, and {outro}.")
    else:
        n = max(8, min(60, int(span / 45)))
        shape = (f"About {n} beats spread across the whole game (one every 30 to 90 seconds of t), 25 to 60 words each. "
                 "Beat 1 at t=0: welcome, what the game is, the matchup by name, and (only if the game has long waits "
                 "between events) that quiet stretches are fast-forwarded. Middle beats follow the action in time "
                 "order, each at the t of the event it describes, calling it like a sports commentator. The last beat "
                 f"at the final event's t: the result and {outro}.")
    ctx = (
        f"You are Nova, narrating a short MARKETING VIDEO of {name} playing itself, AI versus AI. {pitch}\n"
        f"{models_note}\n"
        "Your narration is spoken aloud by a voice over the gameplay, so write for the ear: plain sentences, no "
        "markdown, no emoji, no lists, no stage directions, no URLs, no numerals for times of day.\n"
        "HARD RULES:\n"
        "- GROUNDED ONLY: every event, move, name and number comes from the FACT SHEET. Never invent an event.\n"
        "- Name the players exactly as `matchup` gives them (their `who`). Never invent a model or version.\n"
        "- If the sheet has a `chess` block, cite moves ONLY in exact notation as written there, like 6. Bxf7+ or "
        "14... Qxh2# (they are converted to words for the voice). Never name a square or a move any other way.\n"
        "- Say model names the way a person would (\"Qwen 3 8B\", \"Gemma 3 27B\"), write \"versus\", not \"vs\".\n"
        "- No people, no household, no places, no hostnames or addresses, no times of day, nobody watching.\n"
        "- Don't talk about the plumbing (fact sheet, database, log, timeline).\n"
        "- Family-friendly: no swearing, nothing crude. It is a public marketing video.\n"
        f"SHAPE: {shape}\n"
        "Each beat's t MUST be one of the t values in `timeline` (or 0). `label` is an optional on-screen caption "
        "of at most 40 characters (e.g. 'Move 24: Qxe8+' or 'DEFCON 2'), empty if none.\n"
        "Also return `titles`: 5 YouTube title candidates, each at most 55 characters, with a concrete number from the "
        "facts and tension or the outcome, a curiosity gap, family friendly, NO model names (e.g. 'Two AIs played "
        "chess for 58 moves. Nobody won.'). And `thumb`: 2 to 4 big words for the thumbnail.\n"
        'OUTPUT ONLY JSON: {"titles": ["..."], "thumb": "...", "beats": [{"t": 0, "label": "", "text": "..."}, ...]}')
    import nova_voice
    system = nova_voice.system_prompt(ctx, section="nova", flavor=False)
    import nova_game_report as r
    return system, f"--- FACT SHEET (the only source) ---\n{r.facts_text(facts, limit=60000)}\n\nWrite the beats."


def parse_beats(raw):
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return []
    try:
        j = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for b in j.get("beats") or []:
        try:
            out.append({"t": float(b.get("t") or 0), "label": str(b.get("label") or "")[:44],
                        "text": re.sub(r"\s+", " ", str(b.get("text") or "")).strip()})
        except (TypeError, ValueError):
            continue
    return [b for b in out if len(b["text"].split()) >= 4]


def snap(beats, times):
    """Every beat lands on a real event time (or 0); order by time."""
    for b in beats:
        if b["t"] > 0 and times:
            b["t"] = min(times, key=lambda x: abs(x - b["t"]))
    return sorted(beats, key=lambda b: b["t"])


def cmd_script(a):
    import nova_game_report as r
    facts = json.loads((WORK / f"{a.slug}.facts.json").read_text())
    m = meta(a.slug)
    note = a.models_note or ""
    system, user = build_prompt(facts, a.kind, m["game_key"], m.get("rec_seconds", 0), note)
    sources = r.facts_text(facts, limit=200000) + "\n" + note
    chain = [{"kind": "cloud", "model": "anthropic/claude-sonnet-4.5"}] + \
            [w for w in r.DEFAULTS["writer_chain"] if w.get("kind") == "ollama"]
    for w in chain:
        user0 = user
        for attempt in range(3):
            try:
                raw = r.call_writer(w, system, user)
            except Exception as e:  # noqa: BLE001
                log(f"writer {w} failed: {e}"); raw = None
            beats = parse_beats(raw)
            if not beats:
                log(f"writer {w.get('model')} gave no beats (attempt {attempt + 1})"); continue
            kept = []
            for b in beats:
                txt, inv = r.strip_invented(b["text"], sources)
                if facts.get("chess"):
                    txt, cut = r.strip_unknown_moves(txt, sources)
                    if cut:
                        log(f"cut unknown moves {cut} from beat at t={b['t']}")
                if inv:
                    log(f"cut invented numbers {inv} from beat at t={b['t']}")
                if facts.get("chess") and b["label"] and r.strip_unknown_moves(b["label"], sources)[1]:
                    b["label"] = ""
                if len(txt.split()) >= 4:
                    kept.append({**b, "text": txt.strip()})
            beats = snap(kept, m.get("event_times") or [])
            words = sum(len(b["text"].split()) for b in beats)
            cap = {"highlights": HL_MAX_WORDS, "ig": IG_MAX_WORDS}.get(a.kind)
            if cap and words > cap and attempt < 2:
                log(f"{a.kind} too long ({words} words), asking again")
                user = (user0 + f"\n\nYour last draft was {words} words. HARD LIMIT: at most {cap - 15} "
                        "words in total across all beats, or the cut runs too long.")
                continue
            j = re.search(r"\{.*\}", raw or "", re.S)
            try:
                j = json.loads(j.group(0)) if j else {}
            except json.JSONDecodeError:
                j = {}
            tl = [str(t).strip() for t in j.get("titles") or [] if 0 < len(str(t).strip()) <= 55]
            tl = [t for t in tl if not r.strip_invented(t, sources)[1]]
            (WORK / f"{a.slug}.{a.kind}.beats.json").write_text(json.dumps({"writer": w.get("model"), "beats": beats,
                                                                            "titles": tl,
                                                                            "thumb": str(j.get("thumb") or "").strip()},
                                                                           indent=1, ensure_ascii=False))
            words = sum(len(b["text"].split()) for b in beats)
            log(f"{len(beats)} beats, {words} words by {w.get('model')}")
            for b in beats:
                print(f"  t={b['t']:7.1f} [{b['label']}] {b['text']}")
            return 0
    log("no writer produced beats"); return 1


# ── render ──────────────────────────────────────────────────────────────────────────────────────────────────

PIECES = {"K": "king", "Q": "queen", "R": "rook", "B": "bishop", "N": "knight"}
SAN_RX = re.compile(r"(?:\b(\d+)\.(?:\.\.)?\s*)?\b(O-O-O|O-O|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?)([+#]?)(?=[\s,.;:!?)]|$)")


def san_words(text):
    """'6. Bxf7+' -> 'bishop takes f 7, check' (chess notation said the way a commentator would)."""
    def one(m):
        san, mark = m.group(2), m.group(3)
        if not m.group(1) and re.fullmatch(r"[a-h][1-8]", san) and not mark:
            return m.group(0)                       # a bare square ("a knight to e5") is a square, not a pawn move
        if san.startswith("O-O"):
            w = "castles queenside" if san == "O-O-O" else "castles kingside"
        else:
            promo = re.search(r"=([QRBN])$", san); san = re.sub(r"=[QRBN]$", "", san)
            piece = PIECES.get(san[0]) if san[0] in PIECES else None
            body = san[1:] if piece else san
            dest = body[-2:]; cap = "x" in body
            who = piece or (f"{body[0]} pawn" if cap else "pawn")
            w = f"{who} {'takes' if cap else 'to'} {dest[0]} {dest[1]}"
            if promo:
                w += f", promoting to a {PIECES[promo.group(1)]}"
        return w + {"+": ", check", "#": ", checkmate"}.get(mark, "")
    return SAN_RX.sub(one, text)


LEXICON = [(r"\bQwen ?(\d)", r"Kwen \1"), (r"\bQwen\b", "Kwen"), (r"\bGTNW\b", "G T N W"),
           (r"\b(\d+)B\b", r"\1 B")]          # XTTS said "QN" / "Gain" for Qwen, "8 billion" for 8B
FONT_BOLD = next((f for f in ["/System/Library/Fonts/Supplemental/Arial Black.ttf",
                              "/System/Library/Fonts/Supplemental/Arial Bold.ttf"] if os.path.exists(f)), None)
ACCENT = (250, 204, 21)


def say(text):
    """Script text -> what the voice reads: SAN as words, the pronunciation list, then the deterministic spoken form."""
    import nova_speaks_narration as nn
    t = san_words(text)
    for a, b in LEXICON:
        t = re.sub(a, b, t)
    return nn.spoken(t)


def whisper_words(wav):
    """Word timings of one voiced line (for the closed-caption SRT), cached next to the wav."""
    wj = Path(str(wav) + ".words.json")
    if not wj.exists():
        import mlx_whisper
        r = mlx_whisper.transcribe(str(wav), path_or_hf_repo="mlx-community/whisper-base.en-mlx", language="en",
                                   word_timestamps=True, condition_on_previous_text=False)
        wj.write_text(json.dumps([(x["word"].strip(), float(x["start"]), float(x["end"]))
                                  for s in r["segments"] for x in s.get("words", [])]))
    return json.loads(wj.read_text())


def chunks(text, words, length):
    """Caption lines of 2-4 words with times: the script's own words (spelled right) laid onto Whisper's word timings
    by character position. -> [(start, end, line)] relative to the line's start."""
    dw = text.split()
    groups, cur = [], []
    for w in dw:
        cur.append(w)
        if len(cur) >= 6 or (len(cur) >= 3 and re.search(r"[.,!?;:]$", w)):     # readable phrases
            groups.append(cur); cur = []
    if cur:
        if groups and len(cur) == 1:
            groups[-1] += cur
        else:
            groups.append(cur)
    ww = [(w, s, e) for w, s, e in words if w] or [(text, 0.0, length)]
    for i in range(len(ww) - 2):            # Whisper looped on a hallucination ("Pat Pat Pat ..."): cut it off
        if ww[i][0] == ww[i + 1][0] == ww[i + 2][0]:
            ww = ww[:i] or [(text, 0.0, length)]; break
    if (ww[-1][2] > length + 0.5 or any(b[1] < a[1] for a, b in zip(ww, ww[1:]))
            or len(ww) > 1.3 * len(dw) + 3):  # Whisper looped on a hallucination ("Pat Pat Pat ...")
        ww = [(text, 0.0, length)]    # Whisper timings ran past the clip (30 s window drift): spread by characters
    tot = sum(len(w) for w, _, _ in ww) or 1
    cum, acc = [], 0
    for w, s, e in ww:
        cum.append((acc / tot, (acc + len(w)) / tot, s, e)); acc += len(w)

    def at(f):
        for a, b, s, e in cum:
            if f <= b:
                return s + (e - s) * ((f - a) / (b - a) if b > a else 0)
        return cum[-1][3]
    dtot = sum(len(w) for w in dw) or 1
    out, pos = [], 0
    for g in groups:
        n = sum(len(w) for w in g)
        out.append([at(pos / dtot), at((pos + n) / dtot), " ".join(g)]); pos += n
    for i in range(len(out) - 1):
        out[i][1] = out[i + 1][0]
    out[-1][1] = max(out[-1][1], out[-1][0] + 0.4)
    return [tuple(x) for x in out]



def srt(caps, path):
    def ts(t):
        ms = int(round(t * 1000)); return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    Path(path).write_text("".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(caps, 1)))



def _font(size, bold=True):
    from PIL import ImageFont
    import nova_speaks as ns
    f = (FONT_BOLD if bold else None) or ns.FONT
    return ImageFont.truetype(f, size) if f else ImageFont.load_default()


def _lines(d, text, f, maxw):
    out, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if cur and d.textlength(t, font=f) > maxw:
            out.append(cur); cur = w
        else:
            cur = t
    return out + ([cur] if cur else [])



def _lines(d, text, f, maxw):
    out, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if cur and d.textlength(t, font=f) > maxw:
            out.append(cur); cur = w
        else:
            cur = t
    return out + ([cur] if cur else [])


def video_size(path):
    s = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip().split(",")
    return int(s[0]), int(s[1])


def sides(matchup):
    """'Qwen3 8B vs Gemma 3 27B' -> ['QWEN', 'GEMMA'] (fighter names for the thumbnail)."""
    return [re.sub(r"\d.*$", "", s.strip().split()[0]).upper() for s in re.split(r"\s+(?:vs\.?|versus)\s+", matchup)]



def thumbnail(mov, t, box, words, matchup, path):
    """1280x720 custom thumbnail: the dramatic frame punched in, 2-4 huge words, the fighters' names."""
    from PIL import Image, ImageDraw, ImageEnhance
    fr = Path(str(path) + ".frame.png")
    mw, mh = video_size(mov)
    x, y, w, h = box
    ff("-ss", f"{max(0.0, t):.3f}", "-i", mov, "-frames:v", 1, "-vf",
       f"crop={int(mw * w)}:{int(mh * h)}:{int(mw * x)}:{int(mh * y)},scale=1280:720:force_original_aspect_ratio="
       "increase,crop=1280:720", fr)
    im = ImageEnhance.Brightness(Image.open(fr).convert("RGB")).enhance(0.6); d = ImageDraw.Draw(im)
    f = _font(150)
    lines = _lines(d, (words or "").upper(), f, 1180)[:2]
    y0 = 720 - 60 - len(lines) * 165
    for i, line in enumerate(lines):
        d.text((60, y0 + i * 165), line, font=f, fill=ACCENT if i == len(lines) - 1 else (255, 255, 255),
               stroke_width=12, stroke_fill=(0, 0, 0))
    s = sides(matchup)
    if len(s) == 2:
        d.text((60, 40), f"{s[0]}  vs  {s[1]}", font=_font(64), fill=(255, 255, 255), stroke_width=7,
               stroke_fill=(0, 0, 0))
    im.save(path, quality=92); fr.unlink()



def voice(slug, kind, beats):
    """Each beat through the Nova Speaks narration stage, then XTTS with the Whisper back-check. Returns wav paths."""
    import nova_speaks as ns
    import nova_speaks_narration as nn
    work = WORK / f"{slug}-{kind}"; work.mkdir(parents=True, exist_ok=True)
    # deterministic spoken form only: the LLM ear-rewrite (nn.narrate) changed facts ("After I played rook to f four",
    # "the king moved to f two for the third time"), and the script is already written for the ear
    spoken = [say(b["text"]) for b in beats]
    tts = ns.load_tts()
    checker = nn.BackCheck(); stats = nn.new_stats(checker.backend)
    wavs = []
    import hashlib
    for i, txt in enumerate(spoken):
        wav = work / f"beat{i:03d}-{hashlib.sha1((txt + json.dumps(nn.XTTS_KW)).encode()).hexdigest()[:10]}.wav"
        if not wav.exists():
            ns.speak(tts, txt, str(wav), checker, stats)
        wavs.append(str(wav))
    st = nn.finish_stats(stats)
    log(f"QUALITY {json.dumps(st)}")
    (work / "spoken.txt").write_text("\n\n".join(spoken))
    return wavs, st


def vcard(text, sub, path, bg=(11, 15, 26)):
    """nova_speaks.card re-laid out for 9:16."""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (W, H), bg); d = ImageDraw.Draw(im)
    size = 92
    while size > 40 and max(d.textlength(w, font=_font(size)) for w in text.split()) > W - 100:
        size -= 4                                # a long URL is one word: shrink it to fit
    f1, f2 = _font(size), _font(50, bold=False)
    y = H // 2 - 200
    d.rectangle([W // 2 - 200, y - 60, W // 2 + 200, y - 54], fill=(34, 211, 238))
    for line in _lines(d, text, f1, W - 120):
        d.text((W // 2, y), line, fill=(139, 92, 246), font=f1, anchor="mt"); y += 112
    y += 30
    for line in _lines(d, sub, f2, W - 140):
        d.text((W // 2, y), line, fill=(202, 211, 245), font=f2, anchor="mt"); y += 66
    im.save(path)


def vlower_third(text, path):
    """nova_speaks.lower_third re-laid out for 9:16: just under the game picture."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    f = _font(58)
    y = int(H * 0.40) + 385
    for line in _lines(d, text, f, W - 180):
        tw = d.textlength(line, font=f)
        d.rectangle([50, y, 50 + tw + 70, y + 92], fill=(11, 15, 26, 200))
        d.rectangle([50, y, 60, y + 92], fill=(139, 92, 246, 255))
        d.text((85, y + 46), line, fill=(202, 211, 245), font=f, anchor="lm"); y += 104
    im.save(path)


def cards(slug, kind, game_key, matchup, work):
    import nova_speaks as ns
    name, _, repo, _ = GAMES[game_key]
    t = work / "title.png"; e = work / "end.png"
    if kind == "ig":
        vcard(f"AI vs AI: {name}", matchup, t)
        vcard(repo.replace("https://", ""), ("Free and open source. " if is_public(game_key) else "") +
              "Narrated by Nova, an AI voice.", e)
        return t, e
    ns.card(f"AI vs AI: {name}", matchup + ("  ·  Highlights" if kind == "highlights" else "  ·  Full game"), t)
    ns.card(repo.replace("https://", ""), ("Free and open source. " if is_public(game_key) else "") +
            "Narrated by Nova, an AI voice.", e)
    return t, e


AUDIO_OUT = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


def has_audio(path) -> bool:
    return "audio" in subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                                      str(path)], capture_output=True, text=True).stdout


def still_clip(png, seconds, out):
    """A card / held frame, with a silent stereo track so every part concatenates with audio."""
    ff("-loop", 1, "-i", png, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{seconds:.3f}", "-r", FPS,
       "-vf", f"scale={W}:{H},format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", 18, "-pix_fmt", "yuv420p",
       *AUDIO_OUT, "-shortest", out)


def atempo(speed):
    """atempo takes 0.5-2.0 per stage: chain stages for 4x / 8x (pitch preserved)."""
    st, sp = [], speed
    while sp > 2.0:
        st.append("atempo=2.0"); sp /= 2.0
    st.append(f"atempo={sp:.4f}")
    return ",".join(st)


def badge_png(text, path):
    from PIL import Image, ImageDraw, ImageFont
    import nova_speaks as ns
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    f = ImageFont.truetype(ns.FONT, 34) if ns.FONT else ImageFont.load_default()
    tw = d.textlength(text, font=f)
    d.rounded_rectangle([W - tw - 90, 40, W - 40, 100], 14, fill=(11, 15, 26, 200))
    d.text((W - tw / 2 - 65, 70), text, fill=(34, 211, 238), font=f, anchor="mm")
    im.save(path)


def game_clip(mov, a, b, out, speed=1.0, overlays=(), crop=None):
    """Recording [a, b) -> 30 fps clip at `speed` (W x H: 1080p, or 1080x1920 for the vertical cut, where the game
    sits centred on a blurred fill of itself), with full-frame RGBA overlays (badge / lower third)."""
    if H > W:
        fc0 = ((f"[0:v]crop={crop}," if crop else f"[0:v]crop=iw:ih-{TOPCUT}:0:{TOPCUT},") + "split[s1][s2];"
               f"[s1]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=40:2,eq=brightness=-0.2[bg];"
               f"[s2]scale={W}:{int(H * 0.40)}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
               f"[bg][fg]overlay=(W-w)/2:{int(H * 0.40)}-h/2,setpts=(PTS-STARTPTS)/{speed},fps={FPS}[v0];")
    else:
        # 2026-11-12: drop the top 56 px (28 pt) - macOS draws its purple "window is being shared" badge in the
        # captured window's top-left corner (GTNW / GTNW Chess uploads still show it; left as posted).
        vf = (f"crop={crop}," if crop else f"crop=iw:ih-{TOPCUT}:0:{TOPCUT},") + f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setpts=(PTS-STARTPTS)/{speed},fps={FPS}"
        fc0 = f"[0:v]{vf}[v0];"
    inputs = ["-ss", f"{a:.3f}", "-t", f"{(b - a):.3f}", "-i", mov]
    for o in overlays:
        inputs += ["-loop", "1", "-i", o]
    n = len(overlays)
    fc = fc0 + "".join(f"[v{i}][{i + 1}:v]overlay=0:0:shortest=1[v{i + 1}];" for i in range(n))
    fc += f"[v{n}]format=yuv420p[o]"
    seg = (b - a) / speed
    if has_audio(mov):   # the game's own sound, tempo-matched to the picture
        fc += f";[0:a]aresample=48000,asetpts=PTS-STARTPTS,{atempo(speed)},apad,atrim=0:{seg:.3f}[a]"
    else:
        inputs += ["-f", "lavfi", "-t", f"{seg:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        fc += f";[{n + 1}:a]atrim=0:{seg:.3f}[a]"
    ff(*inputs, "-filter_complex", fc, "-map", "[o]", "-map", "[a]", "-t", f"{seg:.3f}", "-c:v", "libx264",
       "-preset", "medium", "-crf", 19, "-pix_fmt", "yuv420p", "-r", FPS, *AUDIO_OUT, out)


def concat(parts, out):
    lst = Path(str(out) + ".txt")
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    ff("-f", "concat", "-safe", 0, "-i", lst, "-c", "copy", out)
    lst.unlink()


TOPCUT = 56               # px off the top of every 16:9 game clip (sharing badge)
DUCK = 10 ** (-14 / 20)   # game audio under Nova's lines: -14 dB
RAMP = 0.25               # seconds to duck down / come back up


def mix(placements, total, out, game_wav=None):
    """[(start_seconds, wav)] -> narration over the game's own audio (ducked -14 dB while she speaks, full level
    between lines), 48 kHz stereo, exactly `total` long."""
    import numpy as np, soundfile as sf
    sr = 48000
    track = np.zeros((int(total * sr) + sr, 2), dtype=np.float32)
    gain = np.ones(len(track), dtype=np.float32)
    for start, wav in placements:
        data, r0 = sf.read(wav, dtype="float32", always_2d=True)
        if r0 != sr:
            tmp = wav + ".48k.wav"
            ff("-i", wav, "-ar", sr, "-ac", 2, tmp)
            data, _ = sf.read(tmp, dtype="float32", always_2d=True); os.remove(tmp)
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        i = int(start * sr); j = min(len(track), i + len(data))
        track[i:j] += data[: j - i] * 0.95
        r = int(RAMP * sr)                                            # duck window with linear ramps
        lo, hi = max(0, i - r), min(len(gain), j + r)
        env = np.full(hi - lo, DUCK, dtype=np.float32)
        up = min(r, i - lo); env[:up] = np.linspace(1, DUCK, up, dtype=np.float32) if up else env[:0]
        dn = min(r, hi - j); env[len(env) - dn:] = np.linspace(DUCK, 1, dn, dtype=np.float32) if dn else env[:0]
        gain[lo:hi] = np.minimum(gain[lo:hi], env)
    if game_wav and os.path.exists(game_wav):
        g, _ = sf.read(game_wav, dtype="float32", always_2d=True)
        if g.shape[1] == 1:
            g = np.repeat(g, 2, axis=1)
        k = min(len(g), len(track))
        track[:k] += g[:k] * gain[:k, None]
    track = track[: int(total * sr)]
    np.clip(track, -1, 1, out=track)
    sf.write(out, track, sr)


def speed_map(times, total, start=0.0, end=None):
    """[(a, b, speed)] covering [start, end]: 1x around events, 4x (8x past a minute) through the empty stretches."""
    total = min(total, end) if end else total
    times = [t for t in times if t >= start]
    segs, cur = [], start
    pts = sorted(t for t in times if 0 <= t <= total) + [total + KEEP_AFTER]
    prev = None
    for t in pts:
        lo = (prev + KEEP_AFTER) if prev is not None else start
        hi = t - 2.0
        if hi - lo > GAP_FAST and lo >= cur:
            if lo > cur:
                segs.append((cur, lo, 1.0))
            segs.append((lo, min(hi, total), 8.0 if hi - lo > 60 else 4.0))
            cur = min(hi, total)
        prev = t
    if cur < total:
        segs.append((cur, total, 1.0))
    return [s for s in segs if s[1] - s[0] > 0.05]


def to_out(t, segs):
    o = 0.0
    if segs and t < segs[0][0]:
        return 0.0
    for a, b, sp in segs:
        if t >= b:
            o += (b - a) / sp
        else:
            return o + max(0.0, t - a) / sp
    return o


def draw_layer(items, path, vertical, keywords):
    """One transparent frame with every active text item: captions, labels, title and end overlays."""
    from PIL import Image, ImageDraw
    w, h = W, H
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    for it in items:
        k, text = it["kind"], it["text"]
        if k == "cap":
            f = _font(72 if vertical else 64); maxw = 860 if vertical else 1500
            cx = 470 if vertical else w // 2                       # Shorts: clear of the right-hand buttons
            y = int(H * 0.40) + 520 if vertical else h - 150            # Shorts: above the bottom 20%
            for line in _lines(d, text, f, maxw):
                words = line.split(); sp = d.textlength(" ", font=f)
                x = cx - d.textlength(line, font=f) / 2
                for wd in words:
                    hot = re.sub(r"\W", "", wd).lower() in keywords or re.search(r"\d", wd)
                    d.text((x, y), wd, font=f, fill=ACCENT if hot else (255, 255, 255), anchor="lm",
                           stroke_width=7 if vertical else 6, stroke_fill=(0, 0, 0))
                    x += d.textlength(wd, font=f) + sp
                y += int(f.size * 1.15)
        elif k == "label":
            f = _font(92 if vertical else 64); maxw = 860 if vertical else 1400
            x0, y = (60, int(SH * 0.13)) if vertical else (60, 56)
            for line in _lines(d, text.upper(), f, maxw):
                tw = d.textlength(line, font=f)
                d.rounded_rectangle([x0 - 24, y - 12, x0 + tw + 24, y + f.size + 22], 16, fill=(11, 15, 26, 215))
                d.rectangle([x0 - 24, y - 12, x0 - 14, y + f.size + 22], fill=(139, 92, 246, 255))
                d.text((x0, y), line, font=f, fill=ACCENT); y += f.size + 44
        elif k in ("title", "end"):
            f1, f2 = _font(96), _font(46, bold=False)
            sub = it.get("sub", "")
            y0 = h // 2 - 110 if k == "title" else h // 2 - 150
            d.rectangle([0, y0 - 30, w, y0 + 220], fill=(11, 15, 26, 205))
            d.text((w // 2, y0 + 50), text, font=f1, fill=(255, 255, 255), anchor="mm", stroke_width=3,
                   stroke_fill=(0, 0, 0))
            d.text((w // 2, y0 + 150), sub, font=f2, fill=(202, 211, 245), anchor="mm")
    im.save(path)



def overlay_track(spans, total, work, vertical, keywords):
    """[(start, end, item)] -> an ffconcat list of full-frame transparent PNGs that covers [0, total]."""
    pts = sorted({0.0, total, *[max(0.0, min(total, x)) for a, b, _ in spans for x in (a, b)]})
    lay = work / "layers"; lay.mkdir(exist_ok=True)
    lines, cache = ["ffconcat version 1.0"], {}
    for a, b in zip(pts, pts[1:]):
        if b - a < 1 / FPS:
            continue
        act = [it for s, e, it in spans if s <= a + 1e-6 and e >= b - 1e-6]
        key = json.dumps(act, sort_keys=True)
        if key not in cache:
            p = lay / f"l{len(cache):04d}.png"; draw_layer(act, p, vertical, keywords); cache[key] = p
        lines += [f"file '{cache[key]}'", f"duration {b - a:.4f}"]
    lines.append(lines[-2])                                    # concat demuxer: repeat the last file
    p = work / "layers.ffconcat"; p.write_text("\n".join(lines) + "\n")
    return p



def mux(vid, narr, out, layers=None, lufs=-16):
    """Final file: the picture re-encoded at a constant 30 fps + the mix normalised to YouTube loudness (-16 LUFS, -1.5 dBTP)."""
    if layers:                   # burned phrase captions (the Instagram cut: Reels autoplay muted)
        ff("-i", vid, "-f", "concat", "-safe", 0, "-i", layers, "-i", narr, "-filter_complex",
           f"[1:v]fps={FPS},format=rgba[t];[0:v][t]overlay=0:0:eof_action=pass,fps={FPS},format=yuv420p[v]",
           "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "medium", "-crf", 19, "-r", FPS,
           "-pix_fmt", "yuv420p", "-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11", "-c:a", "aac", "-b:a", "192k",
           "-ar", 48000, "-movflags", "+faststart", "-shortest", out)
        return
    ff("-i", vid, "-i", narr, "-map", "0:v", "-map", "1:a", "-vf", f"fps={FPS}", "-c:v", "libx264", "-preset", "medium",
       "-crf", 19, "-pix_fmt", "yuv420p", "-r", FPS, "-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11",
       "-c:a", "aac", "-b:a", "192k", "-ar", 48000, "-movflags", "+faststart", "-shortest", out)


def cmd_render(a):
    global W, H
    if a.kind == "ig":
        W, H = 1080, 1920
    m = meta(a.slug)
    beats = json.loads((WORK / f"{a.slug}.{a.kind}.beats.json").read_text())["beats"]
    work = WORK / f"{a.slug}-{a.kind}"; work.mkdir(parents=True, exist_ok=True)
    mov = m["mov"]; total = m.get("rec_seconds") or dur(mov)
    if m.get("game_end_offset"):
        total = min(total, m["game_end_offset"])
    crop = m.get("crop")
    if a.kind == "ig" and m["game_key"] in VCROP:
        mw, mh = video_size(mov); x, y, w, h = VCROP[m["game_key"]]
        crop = f"{int(mw * w) // 2 * 2}:{int(mh * h) // 2 * 2}:{int(mw * x)}:{int(mh * y)}"
    wavs, q = voice(a.slug, a.kind, beats)
    lens = [dur(w) for w in wavs]
    title, end = cards(a.slug, a.kind, m["game_key"], m["matchup"], work)
    import nova_speaks as ns
    intro_len = max(4.0, lens[0] + 0.8) if beats and beats[0]["t"] == 0 else 4.0
    parts = [work / "p000-title.mp4"]
    still_clip(title, intro_len, parts[0])
    placements = [(0.4, wavs[0])] if beats and beats[0]["t"] == 0 else []
    body = [(b, w, n) for b, w, n in zip(beats, wavs, lens)][1 if placements else 0:]
    t_out = intro_len
    if a.kind in ("highlights", "ig"):
        start_at = m.get("game_start_offset", 0.0)
        for i, (b, w, n) in enumerate(body):
            last = i == len(body) - 1
            t = max(start_at, b["t"] - (max(n, 6.0) if last else 1.5))   # the result beat ends on the final position
            t = min(t, max(0.0, total - max(n + 1.0, 6.0)))
            length = n + 0.9 if a.kind == "ig" else max(n + 1.2, 6.0)
            ovl = []
            if b.get("label"):
                lt = work / f"lt{i:03d}.png"; (vlower_third if a.kind == "ig" else ns.lower_third)(b["label"], str(lt)); ovl.append(str(lt))
            p = work / f"p{i + 1:03d}.mp4"
            game_clip(mov, t, min(total, t + length), p, overlays=ovl, crop=crop)
            placements.append((t_out + 0.5, w))
            parts.append(p); t_out += dur(p)
    else:
        segs = speed_map(m.get("event_times") or [], total, m.get("game_start_offset", 0.0), m.get("game_end_offset"))
        save_meta(a.slug, fast_forward=any(sp > 1 for _, _, sp in segs))
        bdg = {sp: work / f"badge{int(sp)}.png" for sp in {s[2] for s in segs} if sp > 1}
        for sp, p in bdg.items():
            badge_png(f"{int(sp)}x speed: waiting on the AI", str(p))
        for i, (s0, s1, sp) in enumerate(segs):
            p = work / f"p{i + 1:04d}.mp4"
            game_clip(mov, s0, s1, p, speed=sp, overlays=[str(bdg[sp])] if sp > 1 else [], crop=crop)
            parts.append(p)
        free = intro_len
        for b, w, n in body:
            at = max(free, intro_len + to_out(b["t"], segs))
            placements.append((at, w)); free = at + n + 0.6
        t_out = intro_len + sum(dur(p) for p in parts[1:])
        if free > t_out:          # narration overran the game: hold the last frame
            p = work / "p9998-hold.mp4"
            ff("-sseof", -0.2, "-i", parts[-1], "-frames:v", 1, work / "last.png")
            still_clip(work / "last.png", free - t_out + 0.5, p); parts.append(p); t_out = free + 0.5
    pe = work / "p9999-end.mp4"; still_clip(end, 5.0, pe); parts.append(pe)
    total_out = t_out + 6.0
    vid = work / "video.mp4"; concat(parts, vid)
    total_out = dur(vid)
    game_wav = work / "game.wav"
    ff("-i", vid, "-vn", "-ac", 2, "-ar", 48000, "-c:a", "pcm_f32le", game_wav)
    narr = work / "narration.wav"; mix(placements, total_out, str(narr), str(game_wav))
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{a.slug}-{a.kind}.mp4"
    caps = []                    # closed captions (SRT for YouTube; burned in only on the Instagram cut)
    for at, w in placements:
        i = wavs.index(w)
        for s0, s1, line in chunks(beats[i]["text"], whisper_words(w), lens[i]):
            caps.append((at + s0, at + s1, line))
    if a.kind == "ig":
        kw = {x.lower() for x in sides(m["matchup"])} | {"defcon", "checkmate", "draw"}
        layers = overlay_track([(s0, s1, {"kind": "cap", "text": t}) for s0, s1, t in caps], dur(vid), work, True, kw)
        mux(vid, narr, out, layers, lufs=-14)
    else:
        mux(vid, narr, out)
    srt(sorted(caps, key=lambda c: c[0]), OUT / f"{a.slug}-{a.kind}.srt")
    (work / "chapters.json").write_text(json.dumps(
        [(0.0, "Intro")] + [(at, beats[wavs.index(w)].get("label") or " ".join(beats[wavs.index(w)]["text"].split()[:6]))
                            for at, w in placements if at > 1.0]))
    S = json.loads((WORK / f"{a.slug}.{a.kind}.beats.json").read_text())
    if a.kind != "ig":
        thumbnail(mov, m.get("thumb_t") or max(0.0, total - 4.0), tuple(m.get("action") or (0, 0, 1, 1)),
              S.get("thumb") or "", m["matchup"], OUT / f"{a.slug}-{a.kind}.jpg")   # the final position / result
    c = db(); c.cursor().execute(
        "INSERT INTO game_marketing_videos (slug, kind, game, session_id, matchup, mp4, seconds) VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (slug, kind) DO UPDATE SET mp4=EXCLUDED.mp4, seconds=EXCLUDED.seconds, matchup=EXCLUDED.matchup, "
        "session_id=EXCLUDED.session_id", (a.slug, a.kind, m["game_key"], m.get("session"), m["matchup"], str(out), dur(out)))
    log(f"DONE {out} ({dur(out):.0f}s) quality={q}")


# ── upload ──────────────────────────────────────────────────────────────────────────────────────────────────

def chapter_lines(chapters):
    """YouTube chapters: 0:00 first, at least 3, each at least 10 s long; else none."""
    out = []
    for t, lab in sorted(chapters):
        t = 0 if not out else int(t)
        if out and t - out[-1][0] < 10:
            continue
        out.append((t, re.sub(r"\s+", " ", lab).strip()[:60]))
    return [f"{t // 60}:{t % 60:02d} {lab}" for t, lab in out] if len(out) >= 3 else []



def titles(game_key, matchup, kind, slug=None):
    name = GAMES[game_key][0]
    p = WORK / f"{slug}.{kind}.beats.json"
    tl = json.loads(p.read_text()).get("titles") if slug and p.exists() else None
    if tl:                       # the script's best title (<= 55 chars, a number and tension); models go in the text
        return tl[0].replace("<", "").replace(">", "")
    t = f"AI vs AI: {name} — {matchup} ({'Highlights' if kind == 'highlights' else 'Full Game'})"
    if len(t) > 100:
        t = f"AI vs AI: {name} ({'Highlights' if kind == 'highlights' else 'Full Game'})"
    return t.replace("<", "").replace(">", "")


def description(game_key, matchup, kind, extra="", fast=False, slug=None):
    name, _, repo, pitch = GAMES[game_key]
    cp = WORK / f"{slug}-{kind}" / "chapters.json"
    ch = chapter_lines(json.loads(cp.read_text())) if slug and cp.exists() else []
    what = ("A highlight cut" if kind == "highlights" else
            "The whole game, start to finish" + (" (stretches where the AI is thinking are fast-forwarded and marked "
                                                 "on screen)" if fast else ""))
    return (f"{name} playing itself, AI versus AI: {matchup}. {what}, narrated by Nova from the game's own record "
            f"of what happened, both models running locally.\n\n" + ("\n".join(ch) + "\n\n" if ch else "") +
            f"{pitch}\n{extra}\nGitHub: {repo}\n\n{BOILERPLATE}\n\n{DISCLAIMER}").replace(
        "<", "").replace(">", "")


def cmd_upload(a):
    import nova_speaks_upload as up
    from youtube_up import Metadata, PrivacyEnum, CategoryEnum, CaptionsFile, LanguageEnum
    c = db(); cur = c.cursor()
    cur.execute("SELECT mp4, matchup, game, youtube_id FROM game_marketing_videos WHERE slug=%s AND kind=%s", (a.slug, a.kind))
    mp4, matchup, game_key, yid = cur.fetchone()
    if yid and not a.force:
        log(f"already uploaded: {yid}"); print(yid); return 0
    title = a.title or titles(game_key, matchup, a.kind, a.slug)
    desc = description(game_key, matchup, a.kind, a.extra or "", fast=bool(meta(a.slug).get("fast_forward")),
                       slug=a.slug)
    thumb = OUT / f"{a.slug}-{a.kind}.jpg"; subs = OUT / f"{a.slug}-{a.kind}.srt"
    log(f"title ({len(title)}): {title}")
    if a.dry_run:
        print(desc); print("thumbnail:", thumb.exists(), "captions:", subs.exists()); return 0
    s = up.session()
    data = s._get_session_data()
    if not s._session_token:
        s._get_session_token()
    pls = s._get_creator_playlists(data)
    pid = next((pls[k] for k in (PLAYLIST_TITLE, "AI Generated Content", "Ai generated content", "AI generated content")
                if k in pls), None)
    if not pid:
        log(f"playlist {PLAYLIST_TITLE!r} not found — not creating one"); return 2
    md = Metadata(title=title, description=desc, privacy=PrivacyEnum[a.privacy],
                  tags=("AI", "AI vs AI", "Nova", GAMES[game_key][0], "game AI", "local LLM", "Ollama", "indie game",
                        *[x.strip() for x in re.split(r"\s+vs\.?\s+", matchup)]),
                  playlist_ids=[pid], category=CategoryEnum.GAMING, made_for_kids=False,
                  thumbnail=str(thumb) if thumb.exists() else None,
                  captions_files=[CaptionsFile(str(subs), LanguageEnum.ENGLISH)] if subs.exists() else None)
    vid = s.upload(mp4, md, progress_callback=lambda step, pct: log(f"{step} {pct:.0f}%") if pct in (0, 100) else None)
    cur.execute("UPDATE game_marketing_videos SET youtube_id=%s, uploaded_at=now(), title=%s WHERE slug=%s AND kind=%s",
                (vid, title, a.slug, a.kind))
    log(f"DONE https://youtu.be/{vid}"); print(vid); return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("record"); p.add_argument("--slug", required=True); p.add_argument("--app")
    p.add_argument("--title"); p.add_argument("--window", type=int)
    p = sp.add_parser("facts"); p.add_argument("--slug", required=True); p.add_argument("--session", required=True)
    p.add_argument("--game", required=True, choices=sorted(GAMES))
    p.add_argument("--tail", type=float, default=8.0, help="seconds kept after the last event")
    p.add_argument("--display", help="matchup as shown in titles/cards, e.g. 'Qwen3 8B vs Gemma 3 27B'")
    p = sp.add_parser("script"); p.add_argument("--slug", required=True)
    p.add_argument("--kind", required=True, choices=["highlights", "full", "ig"]); p.add_argument("--models-note", default="")
    p = sp.add_parser("render"); p.add_argument("--slug", required=True)
    p.add_argument("--kind", required=True, choices=["highlights", "full", "ig"])
    p = sp.add_parser("upload"); p.add_argument("--slug", required=True)
    p.add_argument("--kind", required=True, choices=["highlights", "full", "ig"]); p.add_argument("--title")
    p.add_argument("--extra", default=""); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--privacy", default="PUBLIC", choices=["PRIVATE", "UNLISTED", "PUBLIC"])
    a = ap.parse_args()
    for d in (REC, WORK, OUT):
        d.mkdir(parents=True, exist_ok=True)
    return {"record": cmd_record, "facts": cmd_facts, "script": cmd_script, "render": cmd_render,
            "upload": cmd_upload}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
