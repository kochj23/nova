#!/usr/bin/env python3
"""nova_ukraine_causes.py — weekly PRIVATE Ukraine cause watch.

Jordan asked Nova to "keep on the lookout for worthy causes" supporting Ukraine. Once a week this:

  1. SCANS the last N days (default 7) of ingested memories (news, geopolitics, military history,
     intelligence, and the military / Ukraine YouTube channels such as Task & Purpose, The Military Show,
     Ukraine News NowUA and RFU News) for fundraisers, appeals and organisations supporting Ukraine.
  2. VETS each candidate against a curated allowlist in PG (nova_ops.ukraine_cause_allowlist; every
     row's name, official URL and US 501(c)(3) status was web-verified before it was added).
     Anything NOT on the allowlist is shown only with "UNVERIFIED — check before giving", plus any visible
     red flags (no registration, crypto-only, urgency pressure, impersonation of a known org).
  3. REPORTS up to 5 items to Jordan's private Slack DM. If nothing NEW and credible (allowlisted) turned
     up, nothing is posted at all.

PRIVACY: this is private. It posts only to Jordan's DM (Slack only, no Discord mirror), never writes to
memory, never touches the journal / Hugo / publishing paths, and never mentions Jordan's own donations.
It never donates and never handles payment details; it only points at official sites.

Usage:
  nova_ukraine_causes.py              # scan + DM Jordan (scheduled Sundays 10:00)
  nova_ukraine_causes.py --dry-run    # print the post instead of sending; records nothing
  nova_ukraine_causes.py --days 14    # widen the scan window
  nova_ukraine_causes.py --selftest   # offline self-check (no PG, no Slack)

Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_config  # noqa: E402  (shared Slack + PG helpers)

# ── Where the post may go. PRIVATE only. ─────────────────────────────────────
POST_CHANNEL = nova_config.JORDAN_DM          # Jordan's DM with Nova
INFO_CHANNEL = "C0BC4SNUTQR"                  # #nova-info (private ops channel), fallback if the DM fails
PRIVATE_CHANNELS = frozenset({POST_CHANNEL, INFO_CHANNEL})

MAX_ITEMS = 5
MAX_UNVERIFIED = 2
COOLDOWN_DAYS = 28            # don't repeat the same org within four weeks
SCAN_ROW_LIMIT = 3000
WINDOW = 260                  # chars of context either side of a cause keyword

SCAN_SOURCES = ("news", "daily_news", "geopolitics", "military_history", "intelligence",
                "youtube_subscriptions", "television", "documentary")
SCAN_CHANNELS = ("Task & Purpose", "The Military Show", "Ukraine News NowUA",
                 "RFU News — Strategic Geopolitics", "Combat Veteran News", "LazerPig",
                 "The Domino Effect", "NV (New Voice of Ukraine)", "Ukrainska Pravda (English)",
                 "Kyiv Independent", "Defence Blog")
EXCLUDED_SOURCES = frozenset({"nova_articles"})   # Nova's own published output is not a source

CAUSE_SQL_RX = (r"(donat|fundrais|charit|crowdfund|united ?24|foundation|demining|volunteer|"
                r"gofundme|appeal|relief|humanitarian)")
CAUSE_RX = re.compile(r"\b(donat\w*|fundrais\w*|charit\w*|crowdfund\w*|united\s?24|gofundme|"
                      r"appeal\w*|humanitarian aid|demining|volunteer\w*|support (?:the|our|ukraine'?s?) "
                      r"(?:troops|defenders|soldiers|army|military|medics))\b", re.I)

# Unverified-org extraction: proper-noun names ending in a charity-ish word, or a donation domain.
ORG_NAME_RX = re.compile(
    r"\b((?:[A-Z][\w'’&\-]+\s+){1,4}(?:Foundation|Fund|Charity|Initiative|Relief|Aid|Fundraiser|Trust)"
    r"(?:\s+(?:Ops|UA|Ukraine|International|Network|Project|Inc))?)\b")
DOMAIN_RX = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:org|com|ua|net|fund|help|gov\.ua))\b(/[\w/\-.?=&%]*)?", re.I)
ORG_STOPLIST = re.compile(
    r"(monetary fund|wealth fund|reserve fund|pension fund|hedge fund|investment fund|state fund|"
    r"national fund|peace fund|bond fund|index fund|federal|european union|the fund|this fund|"
    r"rapid aid|military aid|foreign aid|lethal aid|us aid|u\.s\. aid|american aid|aid package)", re.I)
DOMAIN_STOPLIST = frozenset({
    "youtube.com", "youtu.be", "nv.ua", "english.nv.ua", "pravda.com.ua", "www.pravda.com.ua",
    "kyivindependent.com", "defence-blog.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "tiktok.com", "patreon.com", "reddit.com", "t.me", "google.com", "nytimes.com", "bbc.com", "cnn.com",
    "reuters.com", "apnews.com", "nova.digitalnoise.net", "digitalnoise.net", "bit.ly"})

RED_FLAG_RX = {
    "crypto-only": re.compile(r"\b(bitcoin|btc|ethereum|eth|usdt|tether|crypto(?:currency)?|wallet address|"
                              r"send (?:coins|tokens))\b", re.I),
    "urgency pressure": re.compile(r"\b(urgent(?:ly)?|act now|last chance|only \d+ (?:hours?|days?) left|"
                                   r"immediately|before it'?s too late|right now or)\b", re.I),
}
REGISTRATION_RX = re.compile(r"(501\s*\(?c\)?\s*\(?3\)?|registered charity|charity (?:number|no\.?)|\bEIN\b|"
                             r"registered (?:non-?profit|foundation)|ЄДРПОУ|EDRPOU)", re.I)

# Anything that looks like the public journal / publishing path is forbidden for this script.
FORBIDDEN_TARGET_RX = re.compile(r"(journal|hugo|publish|nova\.digitalnoise\.net|blog)", re.I)


def log(msg: str) -> None:
    print(f"[ukraine_causes] {msg}", file=sys.stderr)


# ── Data access ───────────────────────────────────────────────────────────────
def load_allowlist(conn) -> list[dict]:
    """Active allowlist rows from nova_ops.ukraine_cause_allowlist."""
    cols = ("slug", "name", "aliases", "official_url", "domains", "focus", "funds", "why",
            "us_501c3", "ein", "tax_note", "verified_on")
    with conn.cursor() as cur:
        cur.execute("SELECT slug, name, aliases, official_url, domains, focus, funds, why, us_501c3, ein, "
                    "tax_note, verified_on FROM ukraine_cause_allowlist WHERE active ORDER BY slug")
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def recently_reported(conn, days: int = COOLDOWN_DAYS) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT cause_key FROM ukraine_cause_reports "
                    "WHERE reported_at > now() - make_interval(days => %s)", (days,))
        return {r[0] for r in cur.fetchall()}


def record_reported(conn, items: list[dict]) -> None:
    with conn.cursor() as cur:
        for it in items:
            cur.execute("INSERT INTO ukraine_cause_reports (cause_key, name, verified, seen_in) "
                        "VALUES (%s, %s, %s, %s)",
                        (it["key"], it["name"], it["verified"], it["seen"][0]["label"] if it["seen"] else None))
    conn.commit()


def fetch_memories(conn, days: int = 7) -> list[dict]:
    """Recent Ukraine-related memories that mention a cause keyword, from the news/military sources."""
    sql = ("SELECT id, source, text, coalesce(metadata->>'channel', metadata->>'show', metadata->>'feed', ''), "
           "coalesce(metadata->>'title', ''), coalesce(metadata->>'url', '') "
           "FROM memories WHERE created_at > now() - make_interval(days => %s) "
           "AND (source = ANY(%s) OR coalesce(metadata->>'channel', metadata->>'show', metadata->>'feed') = ANY(%s)) "
           "AND NOT (source = ANY(%s)) "
           "AND text ~* 'ukrain' AND text ~* %s "
           "ORDER BY created_at DESC LIMIT %s")
    with conn.cursor() as cur:
        cur.execute(sql, (days, list(SCAN_SOURCES), list(SCAN_CHANNELS), list(EXCLUDED_SOURCES),
                          CAUSE_SQL_RX, SCAN_ROW_LIMIT))
        rows = cur.fetchall()
    out = []
    for mid, source, text, channel, title, url in rows:
        if nova_config.is_private_source(source):
            continue
        out.append({"id": mid, "source": source, "text": text or "", "channel": channel or "",
                    "title": title or "", "url": url or ""})
    return out


# ── Pure logic ────────────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _alias_patterns(org: dict) -> list[re.Pattern]:
    names = [org["name"], *(org.get("aliases") or [])]
    pats = []
    for n in names:
        n = (n or "").strip()
        if len(n) < 4:
            continue
        pats.append(re.compile(r"(?<![\w])" + re.escape(n).replace(r"\ ", r"\s+") + r"(?![\w])", re.I))
    return pats


def match_allowlist(text: str, allowlist: list[dict]) -> list[str]:
    """Slugs of allowlisted orgs named in text."""
    return [o["slug"] for o in allowlist if any(p.search(text or "") for p in _alias_patterns(o))]


def is_exact_allowlisted(name: str, allowlist: list[dict]) -> bool:
    """True when name IS an allowlisted org's name/alias (optionally prefixed "the")."""
    n = _norm(name).removeprefix("the ")
    return any(n == _norm(a) for o in allowlist for a in [o["name"], *(o.get("aliases") or [])])


def _domain_root(d: str) -> str:
    d = d.lower().removeprefix("www.")
    return d


def official_domain(domain: str, org: dict) -> bool:
    d = _domain_root(domain)
    return any(d == x.lower() or d.endswith("." + x.lower()) for x in (org.get("domains") or []))


def cause_windows(text: str) -> list[str]:
    """Context windows around every cause keyword."""
    wins, last_end = [], -1
    for m in CAUSE_RX.finditer(text or ""):
        a, b = max(0, m.start() - WINDOW), min(len(text), m.end() + WINDOW)
        if a <= last_end and wins:
            wins[-1] = wins[-1] + text[last_end:b]
        else:
            wins.append(text[a:b])
        last_end = b
    return wins


def red_flags(window: str, name: str, allowlist: list[dict], domain: str | None = None) -> list[str]:
    """Visible red flags for an UNVERIFIED candidate."""
    flags = []
    if RED_FLAG_RX["crypto-only"].search(window) and not DOMAIN_RX.search(window.replace(name, "")):
        flags.append("crypto-only (no website/registration seen)")
    elif RED_FLAG_RX["crypto-only"].search(window):
        flags.append("asks for crypto")
    if RED_FLAG_RX["urgency pressure"].search(window):
        flags.append("urgency pressure")
    if not REGISTRATION_RX.search(window):
        flags.append("no registration info seen")
    imp = impersonates(name, allowlist, domain)
    if imp:
        flags.append(f"possible impersonation of {imp}")
    return flags


_GENERIC_WORDS = frozenset({"the", "for", "of", "and", "ukraine", "ukrainian", "foundation", "fund", "charity",
                            "charitable", "inc", "society", "international", "relief", "aid", "battalion",
                            "medical", "peace"})


def impersonates(name: str, allowlist: list[dict], domain: str | None = None) -> str | None:
    """Name of the allowlisted org this candidate looks like but isn't (near-miss name, or a known org's
    name on a domain that is not its official one)."""
    n = _norm(name)
    for o in allowlist:
        for alias in [o["name"], *(o.get("aliases") or [])]:
            a = _norm(alias)
            if not a or len(a) < 4:
                continue
            if n == a:
                if domain and not official_domain(domain, o):
                    return o["name"]
                continue
            ratio = difflib.SequenceMatcher(None, n, a).ratio()
            if ratio >= 0.82 or (len(a) >= 6 and a in n and n != a):
                return o["name"]
            # Borrows every distinctive word of a known org's name ("Razom Ukraine Fund" vs "Razom for Ukraine").
            distinct = {t for t in a.split() if t not in _GENERIC_WORDS}
            if distinct and sum(map(len, distinct)) >= 5 and distinct <= set(n.split()):
                return o["name"]
        if domain:
            stem = _norm(domain.split(".")[0]).replace(" ", "")
            for alias in [o["name"], *(o.get("aliases") or [])]:
                a = _norm(alias).replace(" ", "")
                if len(a) >= 6 and (a in stem or stem in a) and not official_domain(domain, o):
                    return o["name"]
    return None


def _seen_label(mem: dict) -> dict:
    who = mem["channel"] or mem["source"]
    title = (mem["title"] or "").strip()
    label = f"{who} — \"{title[:110]}\"" if title else who
    return {"label": label, "url": mem["url"]}


def _snippet(window: str, needle_rx: re.Pattern | None = None, width: int = 180) -> str:
    w = re.sub(r"\s+", " ", window).strip()
    if needle_rx:
        m = needle_rx.search(w)
        if m:
            a = max(0, m.start() - width // 2)
            w = w[a:a + width]
            return ("…" if a else "") + w.strip() + "…"
    return w[:width].strip() + ("…" if len(w) > width else "")


def find_candidates(memories: list[dict], allowlist: list[dict]) -> tuple[dict, dict]:
    """Return (verified, unverified) dicts keyed by cause key."""
    by_slug = {o["slug"]: o for o in allowlist}
    verified: dict[str, dict] = {}
    unverified: dict[str, dict] = {}
    for mem in memories:
        text = mem["text"]
        title = mem["title"]
        # Verified: an allowlisted org named in a cause context (or in the headline itself).
        for slug in match_allowlist(title + "\n" + text, allowlist):
            org = by_slug[slug]
            pats = _alias_patterns(org)
            in_title = any(p.search(title) for p in pats)
            ctx = [w for w in cause_windows(text) if any(p.search(w) for p in pats)]
            if not (in_title or ctx):
                continue
            it = verified.setdefault(slug, {"key": slug, "name": org["name"], "verified": True, "org": org,
                                           "mentions": 0, "headline": 0, "seen": [], "snippet": "",
                                           "flags": []})
            it["mentions"] += 1
            it["headline"] += int(in_title)
            lab = _seen_label(mem)
            if lab not in it["seen"]:
                it["seen"].append(lab)
            if in_title and not it["snippet"]:
                it["snippet"] = title.strip()
            elif ctx and not it["snippet"]:
                it["snippet"] = _snippet(ctx[0], pats[0] if pats else None)
            # Impersonation check: a known org's name next to a non-official donation domain.
            for w in ctx:
                for dm in DOMAIN_RX.finditer(w):
                    d = _domain_root(dm.group(1))
                    if d not in DOMAIN_STOPLIST and not official_domain(d, org) and \
                            _norm(org["name"]).split()[0] in d:
                        flag = f"saw a non-official domain ({d}) using this name — use the official link only"
                        if flag not in it["flags"]:
                            it["flags"].append(flag)
        # Unverified: named foundations/funds and donation domains in cause context, not on the allowlist.
        for w in cause_windows(text):
            names = [(m.group(1).strip(), None) for m in ORG_NAME_RX.finditer(w)]
            names += [(None, _domain_root(m.group(1))) for m in DOMAIN_RX.finditer(w)]
            for name, domain in names:
                if domain and (domain in DOMAIN_STOPLIST or any(official_domain(domain, o) for o in allowlist)):
                    continue
                label = name or domain
                if not label or ORG_STOPLIST.search(label):
                    continue
                if name and (is_exact_allowlisted(name, allowlist) or len(name.split()) < 2):
                    continue   # exact allowlisted names are handled above; near-misses stay (impersonation)
                if name and not re.search(r"ukrain|kyiv|defend|drone|army|brigade|volunteer|medic|"
                                          r"humanitarian|demin|veteran|relief", w, re.I):
                    continue
                key = "u:" + _norm(label)
                it = unverified.setdefault(key, {"key": key, "name": label, "verified": False, "org": None,
                                                "mentions": 0, "headline": 0, "seen": [], "flags": [],
                                                "snippet": _snippet(w, re.compile(re.escape(label), re.I))})
                it["mentions"] += 1
                lab = _seen_label(mem)
                if lab not in it["seen"]:
                    it["seen"].append(lab)
                for f in red_flags(w, label, allowlist, domain):
                    if f not in it["flags"]:
                        it["flags"].append(f)
    return verified, unverified


def select_items(verified: dict, unverified: dict, already: set[str],
                 max_items: int = MAX_ITEMS, max_unverified: int = MAX_UNVERIFIED) -> list[dict]:
    """Pick up to max_items NEW items, vetted first. Returns [] unless at least one NEW vetted item exists
    (unverified finds alone never trigger a post)."""
    v = sorted((i for k, i in verified.items() if k not in already),
               key=lambda i: (-i["headline"], -i["mentions"], i["name"]))
    if not v:
        return []
    # Unverified: need corroboration (seen in 2+ places) so a single garbled transcript can't surface one.
    u = sorted((i for k, i in unverified.items() if k not in already and len(i["seen"]) >= 2),
               key=lambda i: (-len(i["seen"]), -i["mentions"], i["name"]))
    picked = v[:max_items]
    room = max_items - len(picked)
    return picked + u[:min(room, max_unverified)]


def tax_line(org: dict | None) -> str:
    if not org:
        return "US tax receipt: unknown — verify registration (IRS TEOS) before giving."
    if org.get("us_501c3") is True:
        ein = f" (EIN {org['ein']})" if org.get("ein") else ""
        return f"US tax receipt: likely — US 501(c)(3){ein}. {org.get('tax_note') or ''}".strip()
    if org.get("us_501c3") is False:
        return f"US tax receipt: unlikely — not a US 501(c)(3). {org.get('tax_note') or ''}".strip()
    return f"US tax receipt: unconfirmed. {org.get('tax_note') or ''}".strip()


def format_post(items: list[dict], days: int = 7) -> str:
    """The private weekly DM. Never mentions Jordan's own giving."""
    if not items:
        return ""
    lines = [f"*Ukraine cause watch* — what turned up in the last {days} days (private, just for you). "
             "Nothing is ever donated automatically; links go to the official sites only."]
    for n, it in enumerate(items, 1):
        lines.append("")
        if it["verified"]:
            org = it["org"]
            lines.append(f"*{n}. {org['name']}* — {org['focus']} · _vetted (on the allowlist)_")
            lines.append(f"• Funds: {org['funds']}")
            lines.append(f"• Why it's worthy: {org['why']}")
            lines.append(f"• Official link: {org['official_url']}")
        else:
            lines.append(f"*{n}. {it['name']}* — *UNVERIFIED — check before giving*")
            lines.append("• Not on the vetted allowlist; Nova has not confirmed who runs it or where money goes.")
        if it.get("snippet"):
            lines.append(f"• This week: {it['snippet']}")
        seen = next((s for s in it["seen"] if s.get("url")), it["seen"][0])   # prefer a linkable source
        more = f" (+{len(it['seen']) - 1} more)" if len(it["seen"]) > 1 else ""
        src = f"{seen['label']} {seen['url']}".strip() if seen.get("url") else seen["label"]
        lines.append(f"• Seen in: {src}{more}")
        lines.append(f"• {tax_line(it['org'])}")
        if it["flags"]:
            lines.append(f"• Red flags: {'; '.join(it['flags'])}")
    return "\n".join(lines)


def send_private(text: str, channel: str = POST_CHANNEL) -> bool:
    """Slack only (no Discord mirror), private channels only. Refuses anything else."""
    if channel not in PRIVATE_CHANNELS or FORBIDDEN_TARGET_RX.search(channel):
        raise ValueError(f"refusing non-private target {channel!r}")
    ok = nova_config.post_both(text, slack_channel=channel, discord_channel="")
    if not ok and channel != INFO_CHANNEL:
        log("DM post failed; falling back to #nova-info")
        ok = nova_config.post_both(text, slack_channel=INFO_CHANNEL, discord_channel="")
    return bool(ok)


# ── Orchestration ─────────────────────────────────────────────────────────────
def run(days: int = 7, dry_run: bool = False, connect=None) -> int:
    connect = connect or nova_config.pg_connect
    try:
        ops = connect("nova_ops")
        allowlist = load_allowlist(ops)
        already = recently_reported(ops)
    except Exception as e:  # noqa: BLE001
        log(f"allowlist unavailable ({e}); posting nothing")
        return 1
    if not allowlist:
        log("allowlist is empty; posting nothing (vetting requires it)")
        return 1
    try:
        mem = connect("nova_memories")
        try:
            memories = fetch_memories(mem, days)
        finally:
            mem.close()
    except Exception as e:  # noqa: BLE001
        log(f"memory scan failed ({e}); posting nothing")
        ops.close()
        return 1
    verified, unverified = find_candidates(memories, allowlist)
    items = select_items(verified, unverified, already)
    log(f"scanned {len(memories)} memories: {len(verified)} vetted / {len(unverified)} unverified candidates; "
        f"{len(already)} on cooldown; {len(items)} selected")
    text = format_post(items, days)
    if not text:
        log("nothing new and credible this week — no post")
        ops.close()
        return 0
    if dry_run:
        print(text)
        ops.close()
        return 0
    ok = send_private(text)
    if ok:
        record_reported(ops, items)
    ops.close()
    return 0 if ok else 1


def demo() -> None:
    """Offline self-check."""
    allow = [{"slug": "razom", "name": "Razom for Ukraine", "aliases": ["Razom"], "official_url":
              "https://razomforukraine.org", "domains": ["razomforukraine.org"], "focus": "humanitarian",
              "funds": "x", "why": "y", "us_501c3": True, "ein": "00-0000000", "tax_note": "", "verified_on": None}]
    mems = [{"id": "1", "source": "news", "channel": "Ch", "title": "t", "url": "",
             "text": "Ukraine needs help. Please donate to Razom for Ukraine today."},
            {"id": "2", "source": "news", "channel": "Ch2", "title": "t2", "url": "",
             "text": "Ukraine drone appeal: send bitcoin urgently to Razom Ukraine Fund, act now."}]
    v, u = find_candidates(mems, allow)
    assert "razom" in v, v
    assert any("Razom Ukraine Fund" in i["name"] for i in u.values()), u
    flags = next(iter(u.values()))["flags"]
    assert any("impersonation" in f for f in flags) and "urgency pressure" in flags, flags
    assert select_items({}, u, set()) == []
    assert "Razom for Ukraine" in format_post(select_items(v, u, set()))
    print("selftest ok")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Weekly PRIVATE Ukraine cause watch (DMs Jordan; never public).")
    ap.add_argument("--dry-run", action="store_true", help="print the post instead of sending; record nothing")
    ap.add_argument("--days", type=int, default=7, help="scan window in days (default 7)")
    ap.add_argument("--selftest", action="store_true", help="offline self-check")
    a = ap.parse_args(argv)
    if a.selftest:
        demo()
        return 0
    return run(days=max(1, min(a.days, 60)), dry_run=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
