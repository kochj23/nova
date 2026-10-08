#!/usr/bin/env python3
"""nova_card_rack.py — THE MINISTER'S CARD-RACK: look for leaks where nobody hides things, in plain view.

From Poe's "The Purloined Letter": the Prefect's men search the Minister D-'s rooms for weeks,
probing chair legs and measuring book bindings, and find nothing. Dupin calls on the Minister in
green spectacles and sees the letter at once, dirty and crumpled in a cheap filigree card-rack
hanging from the mantelpiece. It had been turned inside out, re-addressed and re-sealed so it no
longer looked like itself, and it was hidden by not being hidden at all. Dupin explains it with a
map puzzle: the beginner picks the tiny place-names, the expert picks the word printed in large
letters right across the map, because the eye passes over what is too obvious. In Stoker's
"Dracula" the hunters decide to keep Mina out of their plans, because the Count's link to her
runs both ways. He reads their plan through her anyway, leaves them waiting at Varna and lands at
Galatz. In Anne Rice's "The Vampire Lestat", Lestat goes public as a rock singer and author, and
his concert wakes Akasha. Something put in public view cannot be taken back.

Nova's version reads only what an outsider can already read: the Hugo journal and the Nova Speaks
narration scripts that go to PUBLIC YouTube. It reuses the detectors that already guard publishing
(the MAC, e-mail and home-path scrubbers in nova_journal, the household-name redline in
nova_operations_security, the credential shapes in nova_relay) and adds entropy, coordinates,
private IPs and a defensive-posture phrase list ("the house is empty", "camera is offline",
"remediation plan"). Each NEW finding is filed to claude_queue with the file, line, kind and a
fingerprint (sha256 prefix). The matched text is never printed, logged or stored. Nothing is
redacted or deleted: a person decides. A fingerprint added to the allowlist is never filed again.

Minimal first version (spec "Minimal first version"): regex + entropy over the last 30 days of
Hugo content and Nova Speaks transcripts, plus posture keywords, to claude_queue. Not yet: Slack,
public memories, herd mail, cloud-LLM prompts, the quarterly local-model Dupin inference pass,
"publish after resolved" delays, a pre-publish hook.

CLI:    --run [--dry-run]   --show   --selftest
Table:  card_rack_findings (path, kind, fingerprint UNIQUE; first_seen/last_seen; queue_id)
Config: service_config service='nova_card_rack' key='allowlist' (JSON list of fingerprints)
Queue:  claude_queue session 'card-rack', one item per run listing the new findings
Schedule: nightly 01:00.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_journal as NJ  # noqa: E402  (_MAC_RE, _SCRUB_PATTERNS, _SAFE_EMAILS, _HOME_PATH_RE, HUGO_ROOT)
import nova_operations_security as OS  # noqa: E402  (_HOUSEHOLD_RE: Amy/Dylan hard redline)
import nova_relay as RL  # noqa: E402  (SECRET_PATTERNS: token / key / JWT / private-key shapes)
import nova_watch_common as W  # noqa: E402

SERVICE = "nova_card_rack"
QUEUE_SESSION = "card-rack"
DAYS = 30
SPEAKS_DIR = Path(os.environ.get("NOVA_SPEAKS_OUT", "/Volumes/nas/nova-fs/videos/review"))  # = nova_speaks.OUT_DIR
SURFACES = (("hugo", NJ.HUGO_ROOT / "content", "*.md"), ("speaks", SPEAKS_DIR, "NovaSpeaks-*.txt"))
ENTROPY_MIN = 4.3       # bits/char; pure hex tops out at 4.0, so sha digests and Peaslee's roots never trip it
INFO_KINDS = {"private_ip"}   # counted, never filed: internal IPs are allowed in ops articles (Jordan's call)

_EMAIL_RE = NJ._SCRUB_PATTERNS[-1]
_PRIVATE_IP = re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b")
_COORDS = re.compile(r"-?\b\d{1,2}\.\d{4,}\s*°?\s*[NS]?\s*,\s*-?\d{1,3}\.\d{4,}\b")
_TOKEN = re.compile(r"[A-Za-z0-9_=-]{24,}")   # no "/" or "+": paths are not keys
_URL = re.compile(r"\S+://\S+|\S+\.(?:webp|png|jpe?g|gif|mp4|md|html)\b")
POSTURE = re.compile(
    r"(?i)\b(?:the house is empty|(?:nobody|no one)(?:'s| is) (?:home|at the house) (?:until|this|tonight|for)|"
    r"(?:jordan|little mister|we)(?: is| are|'s|'re) (?:away|out of town|on vacation)|"
    r"(?:camera|sensor|alarm|lock|doorbell)s? (?:is|are|went|has been|have been) (?:offline|down|disabled|dead|dark)|"
    r"(?:the shine|organ|guard|watch|detector) (?:is|was|has been) (?:disabled|off|paused)|"
    r"quarantin\w* (?:in progress|underway)|remediation plan)\b")
# ponytail: a fixed phrase list; door/"nobody's home" metaphors ("front door open with a sign") are left out.


def log(m: str) -> None:
    print(f"[card-rack {datetime.now():%H:%M:%S}] {m}", flush=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS card_rack_findings (
  id bigserial PRIMARY KEY,
  first_seen timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now(),
  surface text NOT NULL, path text NOT NULL, line int, kind text NOT NULL,
  fingerprint text NOT NULL, queue_id int,
  UNIQUE (path, kind, fingerprint));
"""


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {e}")
        return []


# ── detectors (pure) ────────────────────────────────────────────────────────

def entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values()) if n else 0.0


def _high_entropy(line: str):
    for m in _TOKEN.finditer(_URL.sub(" ", line)):
        t = m.group(0)
        if (re.search(r"\d", t) and re.search(r"[a-z]", t) and re.search(r"[A-Z]", t)
                and entropy(t) >= ENTROPY_MIN):
            yield t


def _credentials(line: str):
    """nova_relay's shapes, but not mid-word ("desk-satellite-..." is no sk- key) and not digit-free
    prose ("the open secret: ...") unless it is a PEM private-key block."""
    for p in RL.SECRET_PATTERNS:
        for m in p.finditer(line):
            t = m.group(0)
            if (m.start() == 0 or not line[m.start() - 1].isalnum()) and (re.search(r"\d", t) or "PRIVATE KEY" in t):
                yield t


def _household(line: str):
    """The ops-security Amy/Dylan redline on whole names only ("amygdala" and "Bob Dylan" are not the household)."""
    for m in OS._HOUSEHOLD_RE.finditer(line):
        end = m.start() + len(m.group(0).rstrip("-_ \t"))
        if not line[end:end + 1].isalpha() and not line[:m.start()].rstrip().lower().endswith("bob"):
            yield m.group(1)


DETECTORS = (
    ("credential", _credentials),
    ("mac", lambda ln: (m.group(0) for m in NJ._MAC_RE.finditer(ln))),
    ("email", lambda ln: (m.group(0) for m in _EMAIL_RE.finditer(ln) if m.group(0) not in NJ._SAFE_EMAILS)),
    ("home_path", lambda ln: (m.group(0) for m in NJ._HOME_PATH_RE.finditer(ln))),
    ("household", _household),
    ("coordinates", lambda ln: (m.group(0) for m in _COORDS.finditer(ln))),
    ("private_ip", lambda ln: (m.group(0) for m in _PRIVATE_IP.finditer(ln))),
    ("high_entropy", _high_entropy),
    ("posture", lambda ln: (m.group(0) for m in POSTURE.finditer(ln))),
)


def fingerprint(match: str) -> str:
    return hashlib.sha256(match.strip().lower().encode()).hexdigest()[:16]


def scan_text(text: str) -> list:
    """[(line, kind, fingerprint)] — the matched text itself never leaves this function."""
    out, seen = [], set()
    for i, ln in enumerate(text.splitlines(), 1):
        cred = False
        for kind, det in DETECTORS:
            if kind == "high_entropy" and cred:   # the token already counted as a credential
                continue
            for hit in det(ln):
                cred = cred or kind == "credential"
                key = (kind, fingerprint(hit))
                if key not in seen:          # one finding per distinct value per file
                    seen.add(key)
                    out.append((i, kind, key[1]))
    return out


def display(p: Path) -> str:
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)


def corpus(surfaces=SURFACES, days: int = DAYS, now: float | None = None) -> list:
    """[(surface, path)] modified in the last `days` days. A missing dir (NAS unmounted) is skipped."""
    # ponytail: file mtime is the age; a fresh clone makes every article "new". Front-matter date would be exact.
    cut = (now or time.time()) - days * 86400
    out = []
    for name, d, pat in surfaces:
        if not d.is_dir():
            log(f"{name}: {d} not readable, skipped")
            continue
        out += [(name, p) for p in sorted(d.rglob(pat)) if p.stat().st_mtime >= cut]
    return out


def scan(files: list, allow=frozenset()) -> list:
    out = []
    for surface, p in files:
        try:
            text = p.read_text(errors="replace")
        except OSError as e:
            log(f"unreadable {display(p)}: {e}")
            continue
        out += [{"surface": surface, "path": display(p), "line": ln, "kind": k, "fp": fp}
                for ln, k, fp in scan_text(text) if fp not in allow]
    return out


def summarize(findings: list) -> dict:
    return dict(Counter(f["kind"] for f in findings).most_common())


def queue_text(new: list) -> tuple:
    """(priority, description, context) for one claude_queue item. No matched text, ever."""
    filed = [f for f in new if f["kind"] not in INFO_KINDS]
    pri = 2 if any(f["kind"] in ("credential", "household", "home_path") for f in filed) else 4
    counts = ", ".join(f"{k} {n}" for k, n in summarize(filed).items())
    desc = f"Card-Rack: {len(filed)} new item(s) in public posts ({counts})"
    ctx = ("Nova's public surfaces (Hugo journal, Nova Speaks scripts) hold these. Review each; propose a "
           "redaction or allowlist the fingerprint (service_config nova_card_rack/allowlist). Never auto-delete. "
           "Posture items: publish after resolved, not suppressed.\n"
           + "\n".join(f"- {f['kind']:<12} {f['path']}:{f['line']}  fp={f['fp']}" for f in filed[:80])
           + (f"\n... and {len(filed) - 80} more (card_rack_findings)" if len(filed) > 80 else ""))
    return pri, desc, ctx


# ── PG ──────────────────────────────────────────────────────────────────────

def allowlist(cur) -> frozenset:
    rows = _q(cur, "SELECT value FROM service_config WHERE service=%s AND key='allowlist'", (SERVICE,))
    v = rows[0][0] if rows else None
    return frozenset(v if isinstance(v, list) else [])


def known(cur) -> set:
    exists = _q(cur, "SELECT to_regclass('card_rack_findings')")
    if not exists or exists[0][0] is None:
        return set()
    return {(p, k, fp) for p, k, fp in _q(cur, "SELECT path, kind, fingerprint FROM card_rack_findings")}


def write(cur, findings: list, new: list) -> int | None:
    ensure_schema(cur)
    for f in findings:
        cur.execute("INSERT INTO card_rack_findings (surface, path, line, kind, fingerprint) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (path, kind, fingerprint) DO UPDATE SET last_seen=now(), line=EXCLUDED.line",
                    (f["surface"], f["path"], f["line"], f["kind"], f["fp"]))
    if not any(f["kind"] not in INFO_KINDS for f in new):
        return None
    pri, desc, ctx = queue_text(new)
    cur.execute("INSERT INTO claude_sessions (session_id, status) VALUES (%s,'active') ON CONFLICT (session_id) DO NOTHING",
                (QUEUE_SESSION,))
    cur.execute("INSERT INTO claude_queue (session_id, status, priority, description, context) "
                "VALUES (%s,'queued',%s,%s,%s) RETURNING id", (QUEUE_SESSION, pri, desc, ctx))
    qid = cur.fetchone()[0]
    for f in new:
        cur.execute("UPDATE card_rack_findings SET queue_id=%s WHERE path=%s AND kind=%s AND fingerprint=%s",
                    (qid, f["path"], f["kind"], f["fp"]))
    return qid


def run(dry: bool = False) -> dict:
    files = corpus()
    conn = W.connect()
    try:
        cur = conn.cursor()
        findings = scan(files, allowlist(cur))
        seen = known(cur)
        new = [f for f in findings if (f["path"], f["kind"], f["fp"]) not in seen]
        log(f"{'DRY RUN ' if dry else ''}{len(files)} files "
            f"({dict(Counter(s for s, _ in files))}); findings {summarize(findings)}; new {summarize(new)}")
        if not dry:
            qid = write(cur, findings, new)
            log(f"wrote {len(findings)} rows; " + (f"claude_queue #{qid}" if qid else "nothing new to file"))
        return {"files": len(files), "findings": summarize(findings), "new": summarize(new)}
    finally:
        conn.close()


def show() -> int:
    conn = W.connect()
    try:
        rows = _q(conn.cursor(), "SELECT kind, count(*), count(*) FILTER (WHERE last_seen > now() - interval '2 days'), "
                                 "max(first_seen) FROM card_rack_findings GROUP BY kind ORDER BY 2 DESC")
        for k, n, live, last in rows:
            print(f"{k:<12} {n:>5} total  {live:>5} still present  newest {last:%Y-%m-%d}")
        if not rows:
            print("no findings recorded")
        return 0
    finally:
        conn.close()


def selftest() -> int:
    mac = ":".join(["a4"] * 6)
    tok = "xoxb-" + "1234567890-abcdefghijKLMN"
    text = (f"device {mac} on the lan\nkey {tok}\nthe house is empty until Sunday\n"
            f"sha {'ab12' * 16}\nblob Zq9" + "Xk3LmP7vRt2Wy8NbQ4sJh6Gd" + "\nnothing here")
    kinds = {(ln, k) for ln, k, _ in scan_text(text)}
    assert (1, "mac") in kinds and (2, "credential") in kinds and (3, "posture") in kinds, kinds
    assert (5, "high_entropy") in kinds, kinds
    assert not any(ln in (4, 6) for ln, _ in kinds), kinds          # hex digest and plain prose stay quiet
    assert all(mac not in str(x) and tok not in str(x) for x in scan_text(text))
    assert scan_text("nova@digitalnoise.net wrote") == []           # Nova's own address is allowed
    assert entropy("") == 0.0 and entropy("aaaa") == 0.0
    pri, desc, ctx = queue_text([{"kind": "credential", "path": "p", "line": 2, "fp": "f"},
                                 {"kind": "private_ip", "path": "p", "line": 3, "fp": "g"}])
    assert pri == 2 and "credential 1" in desc and "private_ip" not in ctx, (desc, ctx)
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="scan public surfaces, record findings, file new ones")
    ap.add_argument("--dry-run", action="store_true", help="with --run: scan and print counts, write nothing")
    ap.add_argument("--show", action="store_true", help="recorded findings by kind")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.run:
        run(dry=a.dry_run)
        return 0
    if a.show:
        return show()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
