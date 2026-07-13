#!/opt/homebrew/bin/python3
"""nova_weekly_ops_wrap.py — one-shot DETAILED "everything we did this week" recap.

Jordan asked (2026-07-12) for a thorough week-in-review: read this week's /operations
articles AND pull every concrete thing done from the ops DB, then narrate it all in
Nova's ops voice with a cover image, publish to /operations, and drop the link in Slack.

Reuses nova_weekly_ops_report's gather()/fmt()/publish() (comprehensive infra brief +
cover + git push + Slack notify) and ADDS: the week's published articles + a detailed
claude_actions ledger + shipped git commits — the "every single thing" Jordan wants.
"""
import glob
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import nova_weekly_ops_report as wk

HUGO_OPS = Path.home() / "nova-journal" / "content" / "operations"


def articles_this_week() -> str:
    """This week's /operations posts: title + first two real paragraphs each."""
    prefixes = [(date.today() - timedelta(days=i)).isoformat() for i in range(7)]
    out = []
    for f in sorted(glob.glob(str(HUGO_OPS / "*.md"))):
        base = Path(f).name
        if not any(base.startswith(p) for p in prefixes):
            continue
        title, paras = base, []
        for ln in Path(f).read_text(errors="ignore").splitlines():
            s = ln.strip()
            if s.startswith("title:"):
                title = s.split(":", 1)[1].strip().strip('"')
            elif s and not s.startswith(("---", "#", "!", "*", "date:", "draft:", "categories:",
                                         "tags:", "description:", "cover:", "image:", "alt:", "relative:")):
                paras.append(s)
                if len(paras) >= 2:
                    break
        out.append(f'- {base[:10]} — "{title}": {" ".join(paras)[:280]}')
    return "\n".join(out) or "(no operations articles found this week)"


def action_ledger() -> str:
    """The actual things done this week — claude_actions descriptions, newest first."""
    rows = wk.q(wk.DB,
                "SELECT to_char(ts,'MM-DD') || ' | ' || coalesce(action_type,'?') || ' | ' || "
                "coalesce(target,'') || ' | ' || left(coalesce(description,''),130) || "
                "' -> ' || coalesce(outcome,'') "
                "FROM claude_actions WHERE ts > now() - interval '7 days' "
                "AND description IS NOT NULL AND length(description) > 8 "
                "ORDER BY ts DESC LIMIT 70")
    lines = [wk._sanitize(r[0]) for r in rows]
    return "\n".join(lines) or "(no logged actions)"


def git_commits() -> str:
    out = []
    for label, repo in [("journal (nova.digitalnoise.net)", Path.home() / "nova-journal"),
                        ("nova platform (.openclaw)", Path.home() / ".openclaw")]:
        try:
            r = subprocess.run(["git", "-C", str(repo), "log", "--since=7.days", "--pretty=%s"],
                               capture_output=True, text=True, timeout=20)
            subs = [s for s in r.stdout.splitlines() if s.strip()][:40]
            out.append(f"{label}: {len(subs)} commits\n" + "\n".join(f"  · {s[:110]}" for s in subs))
        except Exception:
            pass
    return "\n\n".join(out) or "(no commits)"


SYSTEM = wk.SYSTEM.replace(
    "STRUCTURE (~700-1000 words, loose; funny section headers welcome):",
    "This is a SPECIAL, LONGER week-in-review — Little Mister asked for the DETAILED version: "
    "EVERYTHING we actually did this week, not just the vibe. Be thorough and specific — name the "
    "real work (the migrations, the fixes, the articles you published, the scans, the code you "
    "shipped). Weave in the /operations articles you published this week as the week's running "
    "narrative — reference them by theme. Strictly SFW; no sexual content, ever.\n\n"
    "STRUCTURE (~1200-1800 words, generous; funny section headers welcome):"
)


def main():
    wk.log("=== Weekly OPS WRAP (detailed) starting ===")
    brief = wk.fmt(wk.gather())
    mega = (
        brief
        + "\n\nARTICLES I PUBLISHED THIS WEEK (/operations) — the week's narrative, reference these:\n"
        + articles_this_week()
        + "\n\nDETAILED ACTION LEDGER (claude_actions, last 7d — the actual things done):\n"
        + action_ledger()
        + "\n\nSHIPPED CODE (git commits this week):\n"
        + git_commits()
        + "\n\nWrite the DETAILED week-in-review now — everything we did."
    )
    wk.log(f"Mega-brief assembled ({len(mega)} chars)")
    body = wk.call_llm(SYSTEM, mega, max_tokens=6500).strip()
    if not body or len(body) < 400:
        wk.log("Generation failed/short — aborting"); return
    wk.log(f"Generated wrap ({len(body)} chars)")
    title = wk.generate_title(body)
    wk.log(f"Title: {title}")
    url = wk.publish(title, body)   # cover image + git push + Slack notify with the link
    wk.log(f"Done: {url}")
    print(url)


if __name__ == "__main__":
    main()
