#!/usr/bin/env python3
"""nova_channel_pools.py — YouTube subscription pools that feed generators OUTSIDE the *and Friends*
roundups (Jordan 2026-10-19).

  * NOVA_NEWS_CHANNELS + NOVA_BUSINESS_CHANNELS -> Nova and Friends opinion writing: the nova_journal
    "opinion" profile (candidate topics + grounding) and nova_politics_column (brief items).
  * LA_LOCAL_CHANNELS -> Burbank and Ops: nova_local_burbank.py's daily Burbank news.

Names are exact subscription names (nova_ops.service_config yt_subs_audio/subscriptions), matched on the
same normalised key as nova_friends.norm. A channel belongs to exactly one subject or pool (the overlap test
in tests/test_nova_friends_7cat.py covers these pools too).

INFOWARS RULE (Jordan 2026-10-19): InfoWars is a claims-to-fact-check source ONLY. Its videos are never a
candidate topic and never grounding for facts; when it is handed to a writer at all, every line carries
UNVERIFIED_LABEL, and label_claims() makes sure any mention of it in a draft is labelled as an
unverified claim.

Stdlib only (the cursor is passed in), so nova_journal / nova_politics_column / nova_local_burbank can
import it without new dependencies.
"""
import re

NOVA_NEWS_CHANNELS = (
    "CNN", "NBC News", "MS NOW", "Associated Press", "VICE", "VICE TV", "The Bulwark", "The Lincoln Project",
    "The Damage Report", "Pod Save America", "Pod Save the World", "Lovett or Leave It", "What A Day",
    "Offline with Jon Favreau", "More Perfect Union", "Intelligence Squared", "WIRED Investigations", "InfoWars",
)
NOVA_BUSINESS_CHANNELS = (
    "The Diary Of A CEO", "The Diary Of A CEO Clips", "Patrick Boyle", "Slidebean", "StephentheInvestor",
    "The Iced Coffee Hour", "Brand Radar", "CREATORVC LIVE", "Arvin Haddad", "What's Going on With Shipping?",
)
LA_LOCAL_CHANNELS = (
    "ABC7", "KTLA 5", "NBCLA", "CBS LA", "FOX 11 Los Angeles", "KQED", "California Weather Watch",
    "Los Angeles Police Department", "ConcreteJungle LosAngeles", "LIVING IN LOS ANGELES AND THE SOUTH BAY",
)
POOLS = {"news": NOVA_NEWS_CHANNELS, "business": NOVA_BUSINESS_CHANNELS, "la_local": LA_LOCAL_CHANNELS}

# Claims-to-fact-check only: never a topic, never grounding, always labelled.
CLAIMS_ONLY_CHANNELS = ("InfoWars",)
UNVERIFIED_LABEL = "UNVERIFIED CLAIM (InfoWars — a claims-to-fact-check source, not a factual source)"
CLAIMS_RULE = ("Lines marked UNVERIFIED CLAIM come from InfoWars, a claims-to-fact-check source. They are NOT "
               "facts: never state them as true and never use them as evidence. If you mention one, call it an "
               "unverified claim from InfoWars and check it against the other material.")


def norm(name):
    """Channel-name key — identical to nova_friends.norm (curly apostrophes, case, whitespace)."""
    return re.sub(r"\s+", " ", (name or "").replace("’", "'")).strip().lower()


_CLAIMS_KEYS = frozenset(norm(c) for c in CLAIMS_ONLY_CHANNELS)


def is_claims_only(channel):
    return norm(channel) in _CLAIMS_KEYS


# Same key as nova_friends.NORM_SQL (kept in step by a test).
NORM_SQL = r"lower(btrim(regexp_replace(replace(coalesce(metadata->>'channel', metadata->>'show'), E'’', ''''), '\s+', ' ', 'g')))"
_PRIVATE_RX = re.compile(r"openclaw|\b(?:\d{1,3}\.){3}\d{1,3}\b|/Users/|/Volumes/", re.I)


def fetch_pool(cur, channels, days=2, per_video=900, max_videos=30):
    """Recent captures (yt_subs_audio audio + downloaded /videos/youtube/ episodes) of these channels,
    grouped per video, newest first -> [{channel, title, url, date, ts, text, claims_only}]. Private rows and
    rows tripping the public-journal markers are dropped."""
    keys = sorted({norm(c) for c in channels})
    cur.execute(f"""/* channel_pool */ SELECT text, created_at, metadata FROM memories
                    WHERE created_at > now() - make_interval(days => %s)
                      AND coalesce(metadata->>'privacy', 'public') NOT IN ('private', 'local-only')
                      AND (metadata->>'pipeline' = 'yt_subs_audio'
                           OR metadata->>'source_file' LIKE '%%/videos/youtube/%%')
                      AND {NORM_SQL} = ANY(%s)
                    ORDER BY created_at DESC,
                             CASE WHEN metadata->>'chunk' ~ '^[0-9]+$' THEN (metadata->>'chunk')::int END NULLS FIRST""",
                (int(days), keys))
    out, idx = [], {}
    for text, created, md in cur.fetchall():
        md = md if isinstance(md, dict) else {}
        if _PRIVATE_RX.search(text or "") or _PRIVATE_RX.search(md.get("title") or ""):
            continue
        chan = md.get("channel") or md.get("show") or ""
        vid = md.get("video_id")
        url = md.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None)
        key = url or f"{chan}|{md.get('title') or md.get('source_file') or ''}"
        if key not in idx:
            if len(out) >= max_videos:
                continue
            idx[key] = len(out)
            out.append({"channel": chan, "title": (md.get("title") or "").strip(), "url": url,
                        "date": str(created)[:10], "ts": str(created), "text": "", "claims_only": is_claims_only(chan)})
        item = out[idx[key]]
        if len(item["text"]) < per_video:
            item["text"] = (item["text"] + " " + (text or "")).strip()[:per_video]
    return out


def facts(items):
    """Items usable as topics / factual grounding — never a claims-only channel."""
    return [it for it in items if not it.get("claims_only") and not is_claims_only(it.get("channel"))]


def claims(items):
    return [it for it in items if it.get("claims_only") or is_claims_only(it.get("channel"))]


def topic_candidates(items):
    """Video titles from fact-usable channels (InfoWars never becomes a topic)."""
    seen, out = set(), []
    for it in facts(items):
        t = (it.get("title") or "").strip()
        if len(t) >= 12 and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


_STOP = {"about", "after", "their", "there", "these", "those", "which", "would", "could", "should", "every",
         "today", "video", "episode", "breaking", "update", "news", "what", "with", "from", "this", "that",
         "have", "will", "they", "into", "just", "your", "over", "when", "says", "said"}


def _words(t):
    return {w for w in re.findall(r"[a-z][a-z'-]{3,}", (t or "").lower())} - _STOP


def related(items, topic, min_shared=2):
    """Items whose title shares >= min_shared real words with the topic (or IS the topic)."""
    tw = _words(topic)
    return [it for it in items
            if (it.get("title") or "").strip().lower() == (topic or "").strip().lower()
            or len(_words(it.get("title")) & tw) >= min_shared]


def as_line(it, chars=600):
    """One grounding line. A claims-only item always carries UNVERIFIED_LABEL first."""
    body = re.sub(r"\s+", " ", it.get("text") or "")[:chars]
    head = f"[{it.get('channel')} — {it.get('title') or 'video'}]"
    if it.get("claims_only") or is_claims_only(it.get("channel")):
        return f"{UNVERIFIED_LABEL}: {head} {body}"
    return f"{head} {body}"


_INFOWARS_RX = re.compile(r"\binfo\s?wars\b", re.I)
_UNVERIFIED_RX = re.compile(r"unverified|unsubstantiated|unproven|fact-check|not verified", re.I)


def label_claims(text, window=160):
    """Every mention of InfoWars in a draft must be labelled as an unverified claim: an unlabelled mention
    (no 'unverified'-type word within `window` chars) gets ' (an unverified claim source)' appended."""
    if not text or not _INFOWARS_RX.search(text):
        return text
    out, last = [], 0
    for m in _INFOWARS_RX.finditer(text):
        ctx = text[max(0, m.start() - window):m.end() + window]
        out.append(text[last:m.end()])
        if not _UNVERIFIED_RX.search(ctx):
            out.append(" (an unverified claim source)")
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def claims_labelled(text, window=160):
    """True when every InfoWars mention has an unverified-claim label nearby."""
    return all(_UNVERIFIED_RX.search(text[max(0, m.start() - window):m.end() + window])
               for m in _INFOWARS_RX.finditer(text or ""))
