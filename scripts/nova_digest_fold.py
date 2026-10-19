#!/usr/bin/env python3
"""nova_digest_fold.py — run a thin daily generator, but fold its article into the daily digest.

Jordan (2026-10-19): Burbank and Ops had 2,500+ articles; several daily generators post the same shape
every day (the rando daily-ops log, the LA County emergency recap, the vector-audit diary, the Copenhagen
alert review, the nightly weird-memories column). Their content now becomes SECTIONS of the evening digest
(nova_journal.py digest) instead of standalone articles. Standalone stays: the morning BLUF, the
security-ops report, the Burbank airwaves dispatch, breaking emergencies and real incident posts.

How (no edit to the generators — other agents own those files): this wrapper imports the generator as a
module, swaps its publish path (nova_journal.publish_hugo / git_push / notify_slack, the module's own
publish()/notify(), image generation) for a capture, and calls its entry point. The captured title + body
land in nova_ops.digest_folds (one row per generator per day, the latest wins). The digest reads today's
rows via sections_for_digest() and appends a short excerpt + headline for each.

CLI  nova_digest_fold.py SCRIPT [--call FUNC] [-- generator args...]   (default FUNC: main)
     nova_digest_fold.py --show [--day YYYY-MM-DD]                      (what tonight's digest will fold)
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from datetime import date
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

EXCERPT_WORDS = 140
# Display names for the digest's section headers.
LABELS = {
    "nova_rando_daily_ops": "The ops desk",
    "nova_journal_emergency": "LA County emergency recap",
    "nova_vector_audit": "Inside my memory store",
    "nova_copenhagen": "Alert review: real vs. noise",
    "nova_rando_weird_memories": "Tonight's weird memories",
}
SCHEMA = """CREATE TABLE IF NOT EXISTS digest_folds (
    day date NOT NULL, generator text NOT NULL, title text NOT NULL, body text NOT NULL,
    section text, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (day, generator))"""


def log(m):
    if not os.environ.get("NOVA_TEST_QUIET"):
        print(f"[digest-fold] {m}", flush=True)


def _pg():
    import psycopg2
    import nova_dsn
    c = psycopg2.connect(nova_dsn.pg_dsn("nova_ops", "connect_timeout=10"))
    c.autocommit = True
    return c


def store(generator: str, title: str, body: str, section: str | None = None, day: str | None = None) -> bool:
    """Upsert today's fold for a generator. Returns True when stored."""
    if not (title or "").strip() or not (body or "").strip():
        return False
    c = _pg()
    try:
        with c.cursor() as cur:
            cur.execute(SCHEMA)
            cur.execute("INSERT INTO digest_folds (day, generator, title, body, section) VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (day, generator) DO UPDATE SET title=EXCLUDED.title, body=EXCLUDED.body, "
                        "section=EXCLUDED.section, created_at=now()",
                        (day or date.today().isoformat(), generator, title.strip()[:300], body, section))
        return True
    finally:
        c.close()


def excerpt(body: str, words: int = EXCERPT_WORDS) -> str:
    """The first prose paragraphs of a body, ~words long: no headers, images, rules, tables, datelines."""
    out, n = [], 0
    for para in re.split(r"\n\s*\n", body or ""):
        p = para.strip()
        if (not p or p[0] in "#!|<>" or p.startswith(("---", "*Burbank", "*Published", "```")) or "](" in p[:3]
                or re.fullmatch(r"[*_\-\s]+", p)):
            continue
        p = re.sub(r"\s+", " ", p)
        w = p.split()
        if n + len(w) > words:
            out.append(" ".join(w[: max(0, words - n)]).rstrip(",;:") + "…")
            break
        out.append(p)
        n += len(w)
        if n >= words:
            break
    return "\n\n".join(out)


def render_sections(rows: list[tuple]) -> str:
    """rows: [(generator, title, body)] -> markdown appended to the digest ("" when none)."""
    parts = []
    for gen, title, body in rows:
        ex = excerpt(body)
        if not ex:
            continue
        clean_title = re.sub(r"^[^\w\"'(]+", "", title or "").strip()
        parts.append(f"### {LABELS.get(gen, gen.replace('_', ' '))}: {clean_title}\n\n{ex}")
    if not parts:
        return ""
    return ("\n\n## Folded in from today's desks\n\n"
            "*These used to be their own daily posts. Now they live here, trimmed to what mattered.*\n\n"
            + "\n\n".join(parts) + "\n")


def sections_for_digest(day: str | None = None) -> str:
    """Today's folded sections for the digest. Fail-open: "" on any error."""
    try:
        c = _pg()
        try:
            with c.cursor() as cur:
                cur.execute(SCHEMA)
                cur.execute("SELECT generator, title, body FROM digest_folds WHERE day = %s ORDER BY created_at",
                            (day or date.today().isoformat(),))
                return render_sections(cur.fetchall())
        finally:
            c.close()
    except Exception as e:  # noqa: BLE001
        log(f"sections_for_digest failed (digest publishes without folds): {e}")
        return ""


# ─────────────────────────── the wrapper ───────────────────────────
class Capture:
    def __init__(self, generator: str):
        self.generator = generator
        self.items: list[tuple[str, str, str | None]] = []

    def publish_hugo(self, title=None, body=None, section=None, *a, **k):
        title = k.get("title", title)
        body = k.get("body", body)
        section = k.get("section", section)
        self.items.append((str(title or ""), str(body or ""), section))
        log(f"captured publish_hugo: {str(title)[:80]!r}")
        return True

    def publish(self, title=None, body=None, *a, **k):
        self.items.append((str(title or ""), str(body or ""), None))
        log(f"captured publish(): {str(title)[:80]!r}")
        return True

    @staticmethod
    def git_push(*a, **k):
        return "nothing"

    @staticmethod
    def quiet(*a, **k):
        return None


def run(script: str, call: str = "main", args: list[str] | None = None) -> int:
    """Run SCRIPT's entry point with its publish path captured; store what it would have published."""
    path = Path(script)
    if not path.is_absolute():
        path = SCRIPTS_DIR / path
    if not path.exists() or path.parent.resolve() != SCRIPTS_DIR:
        log(f"refusing {script}: not a script in {SCRIPTS_DIR}")
        return 2
    gen = path.stem
    cap = Capture(gen)
    import nova_journal as nj
    patches = {"publish_hugo": cap.publish_hugo, "git_push": cap.git_push, "notify_slack": cap.quiet,
               "generate_image": cap.quiet}
    for k, v in patches.items():
        if hasattr(nj, k):
            setattr(nj, k, v)
    try:
        import nova_image_utils
        nova_image_utils.generate_image = cap.quiet
    except Exception:  # noqa: BLE001
        pass
    sys.argv = [str(path)] + list(args or [])
    spec = importlib.util.spec_from_file_location(f"_folded_{gen}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for k, v in dict(patches, publish=cap.publish, notify=cap.quiet).items():
        if callable(getattr(mod, k, None)):
            setattr(mod, k, v)
    fn = getattr(mod, call, None)
    if not callable(fn):
        log(f"{gen} has no callable {call}()")
        return 2
    rc = 0
    try:
        r = fn()
        rc = r if isinstance(r, int) and not isinstance(r, bool) else 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    if not cap.items:
        log(f"{gen}: nothing to fold today (generator rc={rc})")
        return rc
    title, body, section = cap.items[-1]
    ok = store(gen, title, body, section)
    log(f"{gen}: {'folded' if ok else 'NOT folded (empty)'} into tonight's digest — {title[:80]!r}")
    return 0 if ok else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("script", nargs="?")
    ap.add_argument("--call", default="main")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--day")
    a = ap.parse_args(argv)
    if a.show:
        print(sections_for_digest(a.day) or "(nothing folded)")
        return 0
    if not a.script:
        ap.print_help()
        return 0
    return run(a.script, a.call, extra)


if __name__ == "__main__":
    sys.exit(main())
