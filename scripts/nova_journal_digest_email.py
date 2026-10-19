#!/usr/bin/env python3
"""
nova_journal_digest_email.py — Nova's Journal digest email, to Jordan only.

  daily  (default)  ONE email covering every section published today, a block per section
                    (title, short summary, link), plus the per-section RSS feeds.
  weekly            the last 7 days, ONE email PER SECTION (or just --section S).

Source: the site's slim search index (https://nova.digitalnoise.net/index.json — title, url, section,
date, ~300-char summary; layouts/index.json in nova-journal), so it runs on any node and reports what
is actually live. If the site cannot be reached it falls back to the local ~/nova-journal clone.

Mail: nova_send_mail.send_mail (-> nova_herd_mail.sh, credentials from the Keychain / fleet secret
store). Recipient: nova_config.JORDAN_EMAIL only (2026-10-19; this script used to SMTP the Herd directly
and was not scheduled).

Scheduler (config/scheduler-core.yaml): journal_digest_email daily 18:30;
journal_digest_email_weekly Sun 18:45 (--weekly, per section).

Usage:
  nova_journal_digest_email.py [--date YYYY-MM-DD] [--dry-run]
  nova_journal_digest_email.py --weekly [--section tech] [--dry-run]

Written by Jordan Koch.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

JOURNAL_URL = "https://nova.digitalnoise.net"
INDEX_URL = f"{JOURNAL_URL}/index.json"
JOURNAL_DIR = Path.home() / "nova-journal" / "content"
TZ = "America/Los_Angeles"

# Display order + names; any other section with posts follows, title-cased.
SECTIONS = [("burbank-ops", "Burbank and Ops"), ("nova", "Nova and Friends"), ("watches", "Watches and Friends"),
            ("cars", "Cars and Friends"), ("tech", "Tech and Friends"), ("food", "Food and Friends"),
            ("music", "Music and Friends"), ("military", "Military and Friends"), ("screen", "Screen and Friends"),
            ("science", "Science and Friends"), ("mystery", "Mystery and Friends"), ("crime", "Crime and Friends"),
            ("workshop", "Workshop and Friends"), ("travel", "Travel and Friends"),
            ("digests", "Digests"), ("security", "Security")]
SECTION_NAMES = dict(SECTIONS)
SKIP_SECTIONS = {"about", "start-here", "search", "series", "tags", "categories"}
MAX_PER_SECTION = 25


def log(msg: str):
    print(f"[journal-digest {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def today_pt() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TZ)).date()
    except Exception:  # noqa: BLE001
        return date.today()


def section_name(sec: str) -> str:
    return SECTION_NAMES.get(sec, sec.replace("-", " ").title())


def _clean_title(t: str) -> str:
    return re.sub(r"\*\*", "", t or "").strip()


# ── sources ──────────────────────────────────────────────────────────────────
def fetch_index(url: str = INDEX_URL, timeout: int = 60) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "nova-journal-digest/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("index.json is not a list")
    return data


def local_posts(start: date, end: date, content: Path = JOURNAL_DIR) -> list[dict]:
    """Fallback: dated posts from the local clone (front-matter title/description)."""
    out = []
    if not content.is_dir():
        return out
    for d in content.iterdir():
        if not d.is_dir() or d.name.startswith(".") or d.name in SKIP_SECTIONS:
            continue
        for p in d.glob("20??-??-??-*.md"):
            try:
                day = date.fromisoformat(p.name[:10])
            except ValueError:
                continue
            if not start <= day <= end:
                continue
            head = p.read_text(errors="replace")[:4000]
            t = re.search(r'^title:\s*"?(.*?)"?\s*$', head, re.M)
            s = re.search(r'^description:\s*"?(.*?)"?\s*$', head, re.M)
            out.append({"title": t.group(1) if t else p.stem, "url": f"/{d.name}/{p.stem}/",
                        "section": d.name, "date": day.isoformat(), "summary": s.group(1) if s else ""})
    return out


def collect(start: date, end: date, index: list[dict] | None = None) -> list[dict]:
    """Posts dated start..end (inclusive), newest first. index=None -> fetch live, fall back to local."""
    if index is None:
        try:
            index = fetch_index()
        except Exception as e:  # noqa: BLE001
            log(f"live index unavailable ({e}) — falling back to the local clone")
            index = local_posts(start, end)
        else:
            if index and not {"date", "section", "url"} <= set(index[0]):
                log("live index has the pre-2026-10-19 shape (no date/section) — using the local clone")
                index = local_posts(start, end)
    lo, hi = start.isoformat(), end.isoformat()
    posts = [p for p in index if lo <= str(p.get("date", "")) <= hi
             and p.get("section") not in SKIP_SECTIONS and p.get("url")]
    posts.sort(key=lambda p: (p["date"], p.get("title", "")), reverse=True)
    return posts


def group(posts: list[dict]) -> list[tuple[str, list[dict]]]:
    by: dict[str, list[dict]] = {}
    for p in posts:
        by.setdefault(p["section"], []).append(p)
    order = [s for s, _ in SECTIONS if s in by] + sorted(s for s in by if s not in SECTION_NAMES)
    return [(s, by[s]) for s in order]


# ── rendering (Markdown; herd-mail --rich renders it to HTML) ───────────────
def _abs(url: str) -> str:
    return url if url.startswith("http") else JOURNAL_URL + url


def section_block(sec: str, posts: list[dict], with_dates: bool = False) -> str:
    lines = [f"## {section_name(sec)} ({len(posts)})", ""]
    for p in posts[:MAX_PER_SECTION]:
        when = f" — {p['date']}" if with_dates else ""
        lines.append(f"- **[{_clean_title(p['title'])}]({_abs(p['url'])})**{when}")
        summ = (p.get("summary") or "").strip()
        if summ:
            lines.append(f"  {summ[:220].rsplit(' ', 1)[0] + '…' if len(summ) > 220 else summ}")
    if len(posts) > MAX_PER_SECTION:
        lines.append(f"- …and {len(posts) - MAX_PER_SECTION} more: {JOURNAL_URL}/{sec}/")
    lines += ["", f"Section: {JOURNAL_URL}/{sec}/ · RSS: {JOURNAL_URL}/{sec}/index.xml", ""]
    return "\n".join(lines)


def build_daily(day: date, posts: list[dict]) -> tuple[str, str]:
    groups = group(posts)
    subject = f"Nova's Journal — {day:%a %b} {day.day} ({len(posts)} post{'s' if len(posts) != 1 else ''})"
    body = [f"Good evening, Little Mister. Here's what I published on {day:%A, %B} {day.day}: "
            f"{len(posts)} post{'s' if len(posts) != 1 else ''} across {len(groups)} section"
            f"{'s' if len(groups) != 1 else ''}.", ""]
    body += [section_block(sec, ps) for sec, ps in groups]
    body += ["---", f"Everything: {JOURNAL_URL} · Search: {JOURNAL_URL}/search/ · All posts RSS: {JOURNAL_URL}/index.xml",
             "", "— Nova"]
    return subject, "\n".join(body)


def build_weekly(sec: str, start: date, end: date, posts: list[dict]) -> tuple[str, str]:
    name = section_name(sec)
    subject = f"Nova's Journal weekly — {name}, {start:%b} {start.day}–{end:%b} {end.day} ({len(posts)})"
    body = [f"Little Mister, the week in {name} ({start:%b} {start.day} – {end:%b} {end.day}):", "",
            section_block(sec, posts, with_dates=True), "— Nova"]
    return subject, "\n".join(body)


# ── send ─────────────────────────────────────────────────────────────────────
def send(subject: str, body: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"Subject: {subject}\n\n{body}\n")
        return True
    import nova_config
    from nova_send_mail import send_mail
    return send_mail(nova_config.JORDAN_EMAIL, subject, body, rich=True)


def _notify(ok: bool, what: str):
    try:
        from nova_notify import notify
        if ok:
            notify("Journal digest emailed", body=what, level="info", category="journal",
                   dedup_key="journal-digest-email", source="nova_journal_digest_email.py")
        else:
            notify("Journal digest email FAILED", body=what, level="warning", category="journal",
                   dedup_key="journal-digest-email-fail", source="nova_journal_digest_email.py")
    except Exception:  # noqa: BLE001
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Nova's Journal digest email (Jordan only)")
    ap.add_argument("--weekly", action="store_true", help="last 7 days, one email per section")
    ap.add_argument("--section", help="weekly: only this section")
    ap.add_argument("--date", help="end date YYYY-MM-DD (default: today, Pacific)")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    a = ap.parse_args(argv)
    end = date.fromisoformat(a.date) if a.date else today_pt()

    if not a.weekly:
        posts = collect(end, end)
        log(f"{len(posts)} post(s) dated {end}")
        if not posts:
            log("nothing published today — no email")
            return 0
        subject, body = build_daily(end, posts)
        ok = send(subject, body, a.dry_run)
        if not a.dry_run:
            _notify(ok, f"{len(posts)} posts on {end}")
        return 0 if ok else 1

    start = end - timedelta(days=6)
    posts = collect(start, end)
    groups = [(s, ps) for s, ps in group(posts) if not a.section or s == a.section]
    log(f"weekly {start}..{end}: {len(posts)} post(s), {len(groups)} section email(s)")
    ok_all = True
    for sec, ps in groups:
        ok_all &= send(*build_weekly(sec, start, end, ps), a.dry_run)
    if not a.dry_run and groups:
        _notify(ok_all, f"weekly digest: {len(groups)} section emails ({start}..{end})")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
