#!/usr/bin/env python3
"""nova_series.py — series (running stories) + the *and Friends* top_block for the public journal.

Two front-matter fields, both written conservatively and never overwritten once present:

  series: ["<name>"]        Hugo taxonomy `series` (hugo.yaml). The article page renders a
                            "Previously in this series" box (layouts/_partials/extend_post_content.html)
                            and /series/<name>/ lists every instalment.
  top_block:                The featured block at the top of a Watches/Cars/Tech/Food and Friends issue
    kind: "The Big Story"   ("## The Big Story" / "## Buying Recommendation" / "## Recipe Recommendation",
    title: "..."            written into the body by nova_watches_and_friends.py / nova_friends.py).
    summary: "..."          The homepage "Today" view (layouts/_partials/nova_today.html) reads it, and
                            falls back to parsing the body itself when it is absent.

Series are assigned ONLY for obvious running stories — a rule matches on section + generator tags or a
fixed title pattern (SERIES_RULES). Anything that matches no rule is left alone; an article that already
has `series:` is never touched (a hand-set series wins).

Usage:
  nova_series.py --backfill [--root DIR] [--write]   # every article; dry-run unless --write
  nova_series.py --sweep [--commit]                  # scheduled: write + commit + push (journal push lock)
  nova_series.py --stats [--root DIR]                # series counts as the rules would assign them
  nova_series.py --check "<title>" --section S [--tags a,b]

The sweep only edits files that are committed and clean in git (never a half-written article another
writer is still working on), skips a run while the clone is behind origin and cannot fast-forward,
commits those explicit paths, then pull --rebase (never --autostash) + push under nova_journal's
fleet-wide journal push lock; a commit it cannot push is left for journal_stranded_push.

Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

HUGO_ROOT = Path.home() / "nova-journal"
SKIP_DIRS = {"about", "start-here", "search", "series", "tags", "categories"}

# ── series rules ─────────────────────────────────────────────────────────────
# (series name, predicate(meta)) — first match wins. meta: section, title (cleaned, lower), tags (set).
_WEEK_RE = re.compile(r"^this week in ([a-z][a-z &'-]{1,30}?)\s*(?:[:—–|-]|$)")
_WEEK_NAMES = {"tech today": "Tech", "rando": "Operations"}   # renamed recaps join their section's run


def _weekly(m) -> str | None:
    if "weekly-summary" not in m["tags"]:
        return None
    hit = _WEEK_RE.match(m["title"])
    if not hit:
        return None
    subj = hit.group(1).strip()
    return "This Week in " + _WEEK_NAMES.get(subj, subj.title().replace("And", "and"))


SERIES_RULES = [
    ("Daily Security Briefing",
     lambda m: m["section"] == "burbank-ops" and "daily-briefing" in m["tags"]),
    ("Watches and Friends", lambda m: m["section"] == "watches" and m["title"].startswith("watches and friends")),
    ("Cars and Friends", lambda m: m["section"] == "cars" and m["title"].startswith("cars and friends")),
    ("Tech and Friends", lambda m: m["section"] == "tech" and m["title"].startswith("tech and friends")),
    ("Food and Friends", lambda m: m["section"] == "food" and m["title"].startswith("food and friends")),
    ("The Fishbowl",
     lambda m: m["section"] == "watches" and {"fishbowl", "opinion"} <= m["tags"]),
    ("LA County Emergency Recap",
     lambda m: m["section"] == "burbank-ops" and "emergency recap" in m["title"] and "emergency" in m["tags"]),
    ("What Flew Over Burbank",
     lambda m: m["section"] == "burbank-ops" and m["title"].startswith("what flew over burbank")),
    ("Week in Intelligence",
     lambda m: m["section"] == "burbank-ops" and m["title"].startswith("week in intelligence")),
    ("Game Reports", lambda m: m["section"] == "opinions" and "games" in m["tags"]),
    (_weekly, None),   # "This Week in <X>" recaps (nova_journal_weekly_summary.py, tag weekly-summary)
]

# ── top_block ────────────────────────────────────────────────────────────────
TOP_BLOCK_KINDS = {"watches": "The Big Story", "cars": "The Big Story",
                   "tech": "Buying Recommendation", "food": "Recipe Recommendation"}
# Public-journal scrub for anything we lift into front matter (it surfaces on the homepage).
_SCRUB = [(re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[redacted]"),
          (re.compile(r"(?i)\b(?:nova-core\d*|office-m4[\w-]*|pg-primary)\b"), "the cluster"),
          (re.compile(r"(?i)openclaw"), "Nova"),
          (re.compile(r"(?:/Users|/Volumes|/home)/\S+"), "")]


def clean_title(title: str) -> str:
    """Lower-case title without leading emoji / markdown emphasis / quotes."""
    t = (title or "").strip().strip('"\'')
    t = re.sub(r"^[^\w]+", "", t)          # leading emoji, '**', '🛡️ ' …
    t = t.replace("**", "").replace("’", "'")
    return t.strip().lower()


def assign_series(section: str, title: str, tags) -> str | None:
    """The series name for an article, or None (no obvious running story)."""
    m = {"section": (section or "").strip().lower(), "title": clean_title(title),
         "tags": {str(t).strip().lower() for t in (tags or [])}}
    for name, pred in SERIES_RULES:
        if pred is None:
            hit = name(m)
            if hit:
                return hit
        elif pred(m):
            return name
    return None


def md_plain(md: str) -> str:
    """Markdown -> one line of plain text (links keep their text), scrubbed for the public site."""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md or "")
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"^\s{0,3}(?:#+|[-*+]|\d+\.)\s+", "", s, flags=re.M)
    s = re.sub(r"[*_`>]+", "", s)
    for rx, rep in _SCRUB:
        s = rx.sub(rep, s)
    return re.sub(r"\s+", " ", s).strip()


def _cut(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


def extract_top_block(section: str, body: str) -> dict | None:
    """{kind, title, summary} from an issue's "## <Heading>" featured block, or None."""
    kind = TOP_BLOCK_KINDS.get((section or "").lower())
    if not kind or not body:
        return None
    hit = re.search(r"(?:^|\n)##[ \t]+" + re.escape(kind) + r"[ \t]*\n(.*?)(?=\n##[ \t]|\Z)", body, re.S)
    if not hit or not hit.group(1).strip():
        return None
    block = hit.group(1).strip()
    lead = re.match(r"\*\*([^*]{3,120})\*\*", block)          # a bold lead-in names the story
    first = re.split(r"(?<=[.!?])\s", md_plain(block), maxsplit=1)[0]
    title = md_plain(lead.group(1)).rstrip(" —-:") if lead else first
    summary = _cut(md_plain(block), 300)
    if not summary:
        return None
    return {"kind": kind, "title": _cut(title, 120), "summary": summary}


# ── front matter editing ─────────────────────────────────────────────────────
_FM_RE = re.compile(r"\A---\n(.*?\n)---\n", re.S)


def split_front_matter(text: str):
    """(front_matter_text, body) or (None, text) when there is no YAML front matter."""
    m = _FM_RE.match(text or "")
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def _fm_value(fm: str, key: str) -> str | None:
    m = re.search(rf"^{key}:[ \t]*(.*)$", fm, re.M)
    return m.group(1).strip() if m else None


def _fm_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else [str(v)]
    except (ValueError, TypeError):
        return [x.strip().strip('"\'') for x in raw.strip("[]").split(",") if x.strip()]


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)     # a JSON string is a valid YAML double-quoted scalar


def enrich_text(text: str, section: str) -> tuple[str, list[str]]:
    """Return (new_text, changes). Adds `series` and `top_block` when they apply and are absent."""
    fm, body = split_front_matter(text)
    if fm is None:
        return text, []
    changes, add = [], ""
    if not re.search(r"^series:", fm, re.M):
        title = _fm_value(fm, "title") or ""
        try:
            title = json.loads(title) if title.startswith('"') else title
        except ValueError:
            title = title.strip('"')
        name = assign_series(section, title, _fm_list(_fm_value(fm, "tags")))
        if name:
            add += f"series: [{_q(name)}]\n"
            changes.append(f"series={name}")
    if not re.search(r"^top_block:", fm, re.M):
        tb = extract_top_block(section, body)
        if tb:
            add += ("top_block:\n" + "".join(f"  {k}: {_q(tb[k])}\n" for k in ("kind", "title", "summary")))
            changes.append(f"top_block={tb['kind']}")
    if not add:
        return text, []
    return f"---\n{fm}{add}---\n{body}", changes


def content_files(root: Path):
    """(section, path) for every article .md under content/<section>/ (no _index, no special dirs)."""
    for d in sorted((root / "content").iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name in SKIP_DIRS:
            continue
        for p in sorted(d.glob("*.md")):
            if not p.name.startswith("_"):
                yield d.name, p


def _git(root: Path, *args, timeout=180):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout)


def dirty_paths(root: Path) -> set[str]:
    """Repo-relative paths that are modified/untracked — never touched by the sweep."""
    r = _git(root, "status", "--porcelain", "--", "content")
    return {ln[3:].strip().strip('"') for ln in r.stdout.splitlines() if len(ln) > 3}


def run(root: Path, write: bool, only_clean: bool = False):
    """Apply enrich_text to every article. Returns (changed_paths, Counter of series, top_block count)."""
    skip = dirty_paths(root) if only_clean else set()
    changed, series, blocks = [], Counter(), 0
    for section, p in content_files(root):
        rel = str(p.relative_to(root))
        if rel in skip:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new, changes = enrich_text(text, section)
        if not changes:
            continue
        for c in changes:
            if c.startswith("series="):
                series[c[7:]] += 1
            else:
                blocks += 1
        changed.append(rel)
        if write:
            p.write_text(new, encoding="utf-8")
    return changed, series, blocks


def _push_lock():
    try:
        from nova_journal import _acquire_push_lock, _release_push_lock
        return _acquire_push_lock, _release_push_lock
    except Exception:  # noqa: BLE001 — best-effort, like nova_journal itself
        return (lambda *a, **k: None), (lambda c: None)


def commit_and_push(root: Path, paths: list[str], message: str) -> str:
    """Commit exactly `paths`, then pull --rebase + push under the journal push lock."""
    if not paths:
        return "nothing"
    acquire, release = _push_lock()
    conn = acquire()
    try:
        for i in range(0, len(paths), 200):
            r = _git(root, "add", "--", *paths[i:i + 200])
            if r.returncode:
                return f"add failed: {r.stderr[:200]}"
        r = _git(root, "commit", "-q", "-m", message, "--", *paths)
        if r.returncode:
            return f"commit failed: {(r.stderr or r.stdout)[:200]}"
        # No --autostash: other writers' work-in-progress in this shared clone is never stashed. If the
        # rebase cannot run (dirty tree) or the push is rejected, the commit stays local and
        # journal_stranded_push (every 10 min) rebases + pushes it.
        r = _git(root, "pull", "--rebase", "-q", "origin", "main")
        if r.returncode:
            _git(root, "rebase", "--abort")
        r = _git(root, "push", "-q", "origin", "HEAD:main")
        return "pushed" if r.returncode == 0 else "committed (journal_stranded_push will push it)"
    finally:
        release(conn)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(HUGO_ROOT))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--backfill", action="store_true")
    g.add_argument("--sweep", action="store_true")
    g.add_argument("--stats", action="store_true")
    g.add_argument("--check", metavar="TITLE")
    ap.add_argument("--section", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--no-push", action="store_true", help="--sweep: write + commit, do not push")
    a = ap.parse_args(argv)
    root = Path(a.root).expanduser()

    if a.check is not None:
        print(assign_series(a.section, a.check, [t for t in a.tags.split(",") if t]) or "(no series)")
        return 0
    if a.stats or a.backfill:
        changed, series, blocks = run(root, write=a.backfill and a.write)
        for name, n in series.most_common():
            print(f"{n:5d}  {name}")
        print(f"{len(changed)} file(s) {'updated' if a.backfill and a.write else 'would change'}; "
              f"{sum(series.values())} series, {blocks} top_block")
        return 0
    # --sweep: the scheduled pass over the live clone. Never work on a clone that is behind origin
    # (the backfill / another sweep may already have written these files upstream).
    _git(root, "fetch", "-q", "origin")
    behind = _git(root, "rev-list", "--count", "HEAD..origin/main").stdout.strip()
    if behind not in ("", "0"):
        r = _git(root, "pull", "--rebase", "-q", "origin", "main")
        if r.returncode:
            _git(root, "rebase", "--abort")
            print(f"[series] clone is {behind} behind origin and cannot fast-forward now — skipping this run")
            return 0
    changed, series, blocks = run(root, write=True, only_clean=True)
    print(f"[series] {len(changed)} file(s): {dict(series)} top_block={blocks}")
    if changed:
        if a.no_push:
            r = _git(root, "commit", "-q", "-m", "series: assign running-story series / top_block",
                     "--", *changed)
            print("[series] committed" if r.returncode == 0 else f"[series] commit failed: {r.stderr[:200]}")
        else:
            print("[series]", commit_and_push(root, changed,
                                              f"series: {len(changed)} article(s) — running-story series / top_block"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
