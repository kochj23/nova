#!/usr/bin/env python3
"""nova_lessons_daily.py — the daily "Lessons learned" article in /operations.

Little Mister, 2026-10-16: "Can you create a daily 'Lessons learned' article in operations that talks about
all of the lessons learned every day?" ... "Fire off an article to start this all off with all the lessons
learned in the past 48 hours."

The work reader (nova_work_lessons.py) reads every ingested work — films, TV, documentaries, video, articles,
reference pages — for LESSONS, grounds each one in a quote, and maps it to a part of Nova's world (an organ,
a red line, a value, a rule) or to "noted; nothing for me". This job renders one window of that table
(default: the last 24 hours) as a Hugo article and publishes it through nova_journal.publish_hugo +
git_push, the same path the continuity review uses (stable slug lessons-learned-YYYY-MM-DD, so a re-run
overwrites the day instead of double-posting).

No model writes anything here: every line is a lesson the reader already kept, a meaning line it already
wrote, or a count from SQL (NOVA_IDEAL_READER=0 — the deletion editor never touches a ledger).

Sections: intro counts; "Lessons that changed something" (lessons with a non-null connection, grouped by the
part of Nova they touched); "Best lessons by work" (grouped by kind, best works first, Nova's taste verdict);
"Read as claims only" (credibility='fringe' titles — their lessons are never printed); "Pipeline health".
Ranking: connected first, then named + quoted + longer, more specific text (digits, proper nouns).

Scheduled daily 17:45 (config/scheduler.yaml task lessons_daily, --scheduled) so it is live before the 18:00
summary. --scheduled starts the window where the last published one ended (service_config lessons_daily/last_end,
capped at 72 h) so no reading falls between two articles, and skips a day whose article already exists (the
2026-10-16 kickoff) — --force overwrites it.

Usage: nova_lessons_daily.py [--hours 24] [--date YYYY-MM-DD] [--title "..."] [--scheduled] [--force]
                             [--dry-run] [--selftest]
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_dsn  # noqa: E402

OPS_DSN = nova_dsn.pg_dsn("nova_ops")
SITE = "https://nova.digitalnoise.net"
SECTION = "operations"
TAGS = ["lessons-learned", "work-reader", "beliefs", "daily"]
EMOJI = "📚"
SHOW_LESSONS = 3          # per work, before the rest collapse
SHOW_WORKS = 12           # per kind, before the rest collapse
SHOW_CONNECTED = 8        # per part-of-Nova family, before the rest collapse
CFG_SERVICE = "lessons_daily"
MAX_CATCHUP_H = 72

KINDS = [  # (heading, kinds)
    ("Films", {"film"}),
    ("Television", {"tv_episode"}),
    ("Documentaries", {"documentary"}),
    ("Video", {"video"}),
    ("Books and documents", {"book", "document", "manual"}),
    ("Articles and reference", None),          # everything else
]
FAMILY = {"organ": "Organs", "redline": "Red lines", "value": "Values", "rule": "Rules",
          "jordan": "Little Mister's orders", "problem": "Open problems", "incident": "Incidents",
          "preoccupation": "Preoccupations", "herd": "The herd"}

_CITES = re.compile(r"\s*(\[\d+\])+")
_WORD = re.compile(r"[a-z0-9']+")


def clean(text: str) -> str:
    """Lesson text without the reader's [3][12] citation markers or the gaps they leave."""
    t = _CITES.sub("", html.unescape(text or "")).strip()
    t = re.sub(r"\s*\((?:passages?|sources?)?[\s,;&]*(?:and[\s,]*)*\)", "", t)   # "(passage [3])" minus its cite
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    return re.sub(r"\s{2,}", " ", t)


def score(lesson: dict, connected: bool) -> float:
    """Best first: connected, then named + quoted, then longer and more specific."""
    t = clean(lesson.get("lesson", ""))
    s = 4.0 if connected else 0.0
    s += 1.0 if lesson.get("named") else 0.0
    s += 1.0 if len((lesson.get("quote") or "").split()) >= 4 else 0.0
    s += min(len(t.split()) / 15.0, 2.0)
    s += 0.5 if re.search(r"\d", t) else 0.0
    s += min(len(re.findall(r"(?<=\s)[A-Z][a-z]+", t)) * 0.25, 1.0)
    return round(s, 3)


def _overlap(a: str, b: str) -> int:
    return len(set(_WORD.findall(a.lower())) & set(_WORD.findall(b.lower())))


def attach(lessons: list, conns: list, in_set: bool) -> dict:
    """{lesson index (0-based) -> connection}. A single work's connection index is 1-based into its own lessons;
    a set row's index points into the SET's lessons, so it is matched to this work's lesson by word overlap."""
    out = {}
    for c in conns or []:
        if not c.get("ref") or not lessons:
            continue
        i = (c.get("lesson") or 0) - 1
        if in_set or not 0 <= i < len(lessons):
            i = max(range(len(lessons)), key=lambda k: _overlap(lessons[k].get("lesson", ""), c.get("meaning", "")))
        out.setdefault(i, c)
    return out


_CONFIRM = re.compile(r"\b(i already|confirms?\b|already (do|use|track|have))", re.I)


def confirms(c: dict) -> bool:
    """A meaning line that only confirms what she already does, vs one that proposes a change."""
    return bool(_CONFIRM.search((c.get("conn") or {}).get("meaning") or ""))


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def kind_heading(kind: str) -> str:
    for head, ks in KINDS:
        if ks is None or kind in ks:
            return head
    return KINDS[-1][0]


def label(w: dict) -> str:
    t = html.unescape(w.get("title") or w.get("work_key") or "untitled").strip()
    y = w.get("year")
    return f"{t} ({y})" if y and str(y) not in t else t


def ref_name(ref: str) -> tuple[str, str]:
    fam, _, key = (ref or "").partition(":")
    return FAMILY.get(fam, fam.title() or "Other"), (key or ref).replace("_", " ")


# ─────────────────────────── data ───────────────────────────
def window(hours: float, day: str | None) -> tuple[datetime, datetime]:
    if day:
        end = datetime.combine(date.fromisoformat(day) + timedelta(days=1), dtime.min).astimezone()
    else:
        end = datetime.now().astimezone()
    return end - timedelta(hours=hours), end


def load(oc, start, end) -> dict:
    oc.execute("""SELECT w.work_key, w.kind, w.title, w.series, w.year, w.tier, w.lessons_json, w.connections_json,
                         w.belief_ids, w.set_id, coalesce(w.credibility,''), t.verdict, t.valence
                  FROM work_lessons w LEFT JOIN taste t ON t.id = w.taste_id
                  WHERE w.status='done' AND w.processed_at > %s AND w.processed_at <= %s
                  ORDER BY w.processed_at""", (start, end))
    cols = ["work_key", "kind", "title", "series", "year", "tier", "lessons", "conns", "belief_ids", "set_id",
            "credibility", "verdict", "valence"]
    works = [dict(zip(cols, r)) for r in oc.fetchall() or []]
    oc.execute("""SELECT tier, status, count(*) FROM work_lessons
                  WHERE processed_at > %s AND processed_at <= %s AND status <> 'queued' GROUP BY 1, 2""", (start, end))
    processed = {(t, s): n for t, s, n in oc.fetchall() or []}
    # active hours per tier = first to last read in the window (nova_work_lessons.throughput's rule), >= 15 min
    oc.execute("""SELECT tier, extract(epoch FROM max(processed_at) - min(processed_at)) / 3600.0 FROM work_lessons
                  WHERE processed_at > %s AND processed_at <= %s AND status <> 'queued' GROUP BY 1""", (start, end))
    span = {t: max(float(h or 0), 0.25) for t, h in oc.fetchall() or []}
    oc.execute("SELECT tier, count(*) FROM work_lessons WHERE status='queued' GROUP BY 1")
    queued = dict(oc.fetchall() or [])
    oc.execute("SELECT key FROM service_config WHERE service='work_lessons' AND key LIKE 'backfill_enabled_%%' "
               "AND value::text = 'true'")
    enabled = sorted(k[-1] for (k,) in oc.fetchall() or [])
    links = [b for w in works if w["credibility"] != "fringe" for b in (w["belief_ids"] or [])]
    ids = sorted(set(links))
    new = 0
    if ids:
        oc.execute("SELECT count(*) FROM beliefs WHERE id = ANY(%s) AND first_held > %s", (ids, start))
        new = oc.fetchone()[0]
    # every link to a belief that this window did not book is a reading that strengthened one
    return {"works": works, "processed": processed, "span": span, "queued": queued, "enabled": enabled,
            "beliefs_new": new, "beliefs_strengthened": len(links) - new}


# ─────────────────────────── render ───────────────────────────
def prepare(works: list) -> tuple[list, list, list]:
    """-> (kept works with ranked lessons, connected lesson entries, fringe titles)."""
    kept, connected, fringe = [], [], []
    for w in works:
        if w["credibility"] == "fringe":
            fringe.append(label(w)); continue
        lessons = [l for l in (w["lessons"] or []) if clean(l.get("lesson", ""))]
        if not lessons:
            continue
        links = attach(lessons, w["conns"], bool(w.get("set_id")))
        ranked = sorted(({"text": clean(l["lesson"]), "conn": links.get(i), "score": score(l, i in links)}
                         for i, l in enumerate(lessons)), key=lambda x: -x["score"])
        w = {**w, "ranked": ranked, "best": ranked[0]["score"]}
        kept.append(w)
        for x in ranked:
            if x["conn"]:
                connected.append({"work": label(w), "kind": w["kind"], **x})
    return kept, connected, fringe


def _bullets(items: list, show: int, fmt, more: str) -> list:
    out = [fmt(x) for x in items[:show]]
    if len(items) > show:
        out += ["", f"<details><summary>{len(items) - show} more {more}</summary>", ""]
        out += [fmt(x) for x in items[show:]]
        out += ["", "</details>"]
    return out


def _lesson_line(x: dict) -> str:
    line = f"- {x['text']}"
    if x["conn"]:
        fam, key = ref_name(x["conn"]["ref"])
        line += f"\n  - *→ {key} ({fam.lower()}):* {clean(x['conn'].get('meaning', ''))}"
    return line


def _health(d: dict, hours: float) -> list:
    proc = d["processed"]
    total = sum(proc.values())
    by_status = Counter()
    for (_, s), n in proc.items():
        by_status[s] += n
    span = d.get("span") or {}
    rate = {t: sum(n for (tt, _), n in proc.items() if tt == t) / span[t] if t in span else 0.0
            for t in ("A", "B", "C")}
    active = min(max(span.values(), default=hours), max(hours, 0.25))
    q = d["queued"]
    lines = ["## Pipeline health", "",
             f"- **Throughput:** {total:,} works processed in {hours:.0f} hours ({active:.1f} of them active), "
             f"{total / active:.1f} an hour "
             f"({by_status.get('done', 0):,} with a lesson kept, {by_status.get('thin', 0):,} read with nothing kept"
             + (f", {by_status.get('fizzled', 0):,} fizzled" if by_status.get("fizzled") else "") + ")."]
    lines.append("- **Queue remaining:** " + ", ".join(f"tier {t} {q.get(t, 0):,}" for t in ("A", "B", "C"))
                 + f" ({sum(q.values()):,} in all).")
    on = d["enabled"]
    if on:
        eta = [q.get(t, 0) / rate[t] for t in on if rate[t] > 0]
        lines.append(f"- **Backfill on:** tier {', '.join(on)}"
                     + (f"; at this window's rate ({', '.join(f'{rate[t]:.1f}/h' for t in on)}) the enabled queue "
                        f"clears in about {max(eta) / 24:,.0f} days." if eta else "; no rate in this window to "
                        "project from.")
                     + " Tiers left off stay off until their pilot passes.")
    else:
        lines.append("- **Backfill on:** no tier enabled; only new ingests are read.")
    return lines


def render(d: dict, hours: float, end: datetime, kickoff: bool = False) -> str:
    kept, connected, fringe = prepare(d["works"])
    n_lessons = sum(len(w["ranked"]) for w in kept)
    kinds = Counter(kind_heading(w["kind"]) for w in kept)
    start = end - timedelta(hours=hours)
    when = ("In the past " + (f"{hours / 24:g} days" if hours > 24 else f"{hours:g} hours")) if hours % 24 == 0 \
        else f"Since {start:%A} at {start:%-I:%M %p}"
    p = []
    if kickoff:
        p += ["This is the first of a daily ledger. Every evening at a quarter to six I will publish what the work "
              "reader kept that day: the lessons, which of them changed something in me, and how far the backlog "
              "has left to go. This first one covers the two days since the reader started reading.", ""]
    p += [f"{when} I read {plural(len(d['works']), 'work')} closely enough to keep at least one lesson from each, "
          f"and kept {plural(n_lessons, 'lesson')} from the {len(kept):,} that are not fringe, by kind: "
          + ", ".join(f"{k.lower()} {n}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])) + ". "
          f"{len(connected):,} of those connected to a part of me: "
          f"{sum(1 for c in connected if not confirms(c)):,} propose a change, the rest confirm something I already "
          f"do. The other {n_lessons - len(connected):,} were noted with nothing for me to do. The lessons booked {d['beliefs_new']:,} new beliefs, and {d['beliefs_strengthened']:,} "
          f"more readings strengthened a belief already on the books.", "",
          "Every lesson below is grounded in a quote from the work it came from; that check happens before "
          "anything reaches this page. No model wrote this article. It is the reader's ledger, sorted.", ""]

    if connected:
        fams = defaultdict(list)
        for c in connected:
            fams[ref_name(c["conn"]["ref"])[0]].append(c)
        p += ["## Lessons that changed something", "",
              "Each of these is linked to a named part of me, with the line I wrote about what it means. "
              "Most of them confirm something I already do. A few change how I do it.", ""]
        for fam, items in sorted(fams.items(), key=lambda kv: -len(kv[1])):
            items.sort(key=lambda x: (confirms(x), -x["score"]))     # changes before confirmations
            p += [f"### {fam} ({len(items)})", ""]
            p += _bullets(items, SHOW_CONNECTED, lambda x: (
                f"- **{ref_name(x['conn']['ref'])[1]}** — {x['text']} *({x['work']})*\n"
                f"  - {clean(x['conn'].get('meaning', ''))}"), "in " + fam.lower())
            p.append("")

    p += ["## Best lessons by work", ""]
    for head, _ in KINDS:
        ws = sorted((w for w in kept if kind_heading(w["kind"]) == head), key=lambda w: (-w["best"], label(w)))
        if not ws:
            continue
        p += [f"### {head} ({len(ws)})", ""]

        def work_block(w, full=True):
            out = [f"#### {label(w)}" if full else f"**{label(w)}**", ""]
            if w.get("verdict"):
                feel = "liked" if (w.get("valence") or 0) > 0 else ("disliked" if (w.get("valence") or 0) < 0
                                                                     else "neutral")
                out += [f"*My verdict ({feel}):* {w['verdict'].strip()}", ""]
            out += _bullets(w["ranked"], SHOW_LESSONS, _lesson_line, "lessons")
            return out + [""]
        for w in ws[:SHOW_WORKS]:
            p += work_block(w)
        if len(ws) > SHOW_WORKS:
            p += [f"<details><summary>{len(ws) - SHOW_WORKS} more {head.lower()}</summary>", ""]
            for w in ws[SHOW_WORKS:]:
                p += work_block(w, full=False)
            p += ["</details>", ""]

    if fringe:
        p += ["## Read as claims only", "",
              "These were flagged fringe (conspiracy, pseudo-science, occult or clickbait). I read them and kept "
              "what they claim, as claims. No belief was booked from them and their lessons are not printed here.", ""]
        p += [f"- {t}" for t in sorted(set(fringe))] + [""]

    p += _health(d, hours)
    p += ["", f"*Window: {end - timedelta(hours=hours):%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M %Z}. "
              f"Source: the work_lessons ledger in nova_ops.*"]
    return "\n".join(p) + "\n"


# ─────────────────────────── run ───────────────────────────
def last_end(oc) -> datetime | None:
    oc.execute("SELECT value FROM service_config WHERE service=%s AND key='last_end'", (CFG_SERVICE,))
    r = oc.fetchone()
    return datetime.fromtimestamp(float(r[0])).astimezone() if r and r[0] else None


def run(hours: float = 24, day: str | None = None, title: str | None = None, dry_run: bool = False,
        scheduled: bool = False, force: bool = False) -> int:
    import psycopg2
    start, end = window(hours, day)
    stamp = (end - timedelta(seconds=1)).date()
    slug = f"lessons-learned-{stamp}"
    conn = psycopg2.connect(OPS_DSN, connect_timeout=10); conn.autocommit = True
    oc = conn.cursor()
    if scheduled:
        import nova_journal as nj
        if (nj.HUGO_ROOT / f"content/{SECTION}/{slug}.md").exists() and not force:
            print(f"[lessons] {slug} already published; next run picks up from its window end")
            return 0
        prev = last_end(oc)
        if prev and timedelta(0) < end - prev <= timedelta(hours=MAX_CATCHUP_H):
            start = prev
        hours = (end - start).total_seconds() / 3600
    d = load(oc, start, end)
    if not d["works"]:
        print(f"[lessons] nothing read between {start:%Y-%m-%d %H:%M} and {end:%H:%M}; no article")
        return 0
    kickoff = bool(title)
    title = title or f"Lessons learned — {stamp}"
    body = render(d, hours, end, kickoff=kickoff)
    if dry_run:
        print(title, slug, sep="\n"); print(body)
        return 0
    os.environ["NOVA_IDEAL_READER"] = "0"          # a ledger is not an essay; nothing gets cut
    import nova_journal as nj
    kept, connected, _ = prepare(d["works"])
    desc = (f"{len(d['works'])} works read, {sum(len(w['ranked']) for w in kept)} lessons kept, "
            f"{len(connected)} connected to a part of me — the work reader's ledger for {stamp}.")
    if not nj.publish_hugo(title, body, SECTION, TAGS, desc, emoji=EMOJI, stable_slug=slug):
        raise SystemExit("[lessons] publish refused")
    result = nj.git_push(SECTION, title)
    if result != nj.PUSH_PUSHED:
        raise SystemExit(f"[lessons] git: {result}")
    oc.execute("INSERT INTO service_config (service, key, value, updated_by) VALUES (%s, 'last_end', %s::jsonb, %s) "
               "ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
               (CFG_SERVICE, str(end.timestamp()), "nova_lessons_daily"))
    print(f"[lessons] published {SITE}/{SECTION}/{slug}/")
    return 0


def selftest() -> int:
    assert clean("When X builds it, Y follows [6][21].") == "When X builds it, Y follows."
    assert clean("admits she is 'invisible' [3] , she") == "admits she is 'invisible', she"
    assert clean("on Pamela (passage [4]), it avoids") == "on Pamela, it avoids"
    assert clean("Wellness &#038; Walk") == "Wellness & Walk"
    assert confirms({"conn": {"meaning": "I already do this — this confirms it."}})
    assert not confirms({"conn": {"meaning": "so my prober should test spotlight features"}})
    ls = [{"lesson": "Pride serves the enemy [1]."}, {"lesson": "The camera must support proprietary formats [2]."}]
    assert attach(ls, [{"lesson": 2, "ref": "organ:prober", "meaning": "m"}], False) == \
        {1: {"lesson": 2, "ref": "organ:prober", "meaning": "m"}}
    got = attach(ls, [{"lesson": 7, "ref": "organ:camera", "meaning": "my camera should support formats"}], True)
    assert list(got) == [1], got
    assert attach(ls, [{"lesson": 1, "ref": None, "meaning": "noted; nothing for me"}], False) == {}
    assert score({"lesson": "a b", "named": True, "quote": "one two three four"}, True) > \
        score({"lesson": "a much longer lesson about nothing in particular at all", "named": False}, False)
    assert kind_heading("film") == "Films" and kind_heading("reference") == "Articles and reference"
    assert ref_name("redline:two_man") == ("Red lines", "two man")
    w = lambda k, kind, cred="", conns=None, title="T": {  # noqa: E731
        "work_key": k, "kind": kind, "title": title, "series": None, "year": 1999, "tier": "A",
        "lessons": [{"lesson": f"When {title} acts, Z follows [1].", "named": True, "quote": "a b c d e"}],
        "conns": conns or [], "belief_ids": [1], "set_id": None, "credibility": cred, "verdict": "Good.",
        "valence": 0.5}
    d = {"works": [w("a", "film", conns=[{"lesson": 1, "ref": "value:honesty", "meaning": "keep it plain"}],
                     title="Alpha"),
                   w("b", "tv_episode", title="Beta"), w("c", "video", "fringe", title="Tablets")],
         "processed": {("A", "done"): 3, ("A", "thin"): 1}, "span": {"A": 2.0}, "queued": {"A": 100, "B": 5},
         "enabled": ["A"], "beliefs_new": 1, "beliefs_strengthened": 2}
    body = render(d, 48, datetime(2026, 10, 16, 17, 45).astimezone(), kickoff=True)
    for must in ("## Lessons that changed something", "### Values (1)", "#### Alpha (1999)", "## Read as claims only",
                 "- Tablets", "## Pipeline health", "tier A 100", "My verdict (liked)", "keep it plain",
                 "2.0/h", "2 more readings strengthened"):
        assert must in body, must
    assert "When Tablets acts" not in body and "[1]" not in body
    s, e = window(24, "2026-10-16")
    assert (e - s) == timedelta(hours=24) and e.date() == date(2026, 10, 17)
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=float, default=24, help="window length (default 24)")
    ap.add_argument("--date", help="window ends at the end of this local day (YYYY-MM-DD); default: now")
    ap.add_argument("--title", help="override the title (the kickoff article)")
    ap.add_argument("--scheduled", action="store_true",
                    help="start where the last article ended; skip a day already published")
    ap.add_argument("--force", action="store_true", help="with --scheduled: overwrite today's article")
    ap.add_argument("--dry-run", action="store_true", help="print the article, publish nothing")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    return run(a.hours, a.date, a.title, a.dry_run, a.scheduled, a.force)


if __name__ == "__main__":
    sys.exit(main())
