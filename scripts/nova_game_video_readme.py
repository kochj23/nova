#!/usr/bin/env python3
"""nova_game_video_readme.py — put a "Watch it play itself" section near the top of a repo README, with linked
YouTube thumbnails for the AI vs AI videos in nova_ops.game_marketing_videos (nova_game_video.py).

  nova_game_video_readme.py --readme /path/README.md --after-line-regex '^\\| .*At a glance' --slugs gtnwchess
Idempotent: the section sits between <!-- watch-it-play:start --> and <!-- watch-it-play:end --> markers and is
replaced on re-runs. Default position: after the first blank line that follows the title/badge block.
Written by Jordan Koch (via Claude).
"""
import argparse, re, sys
from pathlib import Path
import psycopg2

START, END = "<!-- watch-it-play:start -->", "<!-- watch-it-play:end -->"


def section(slugs):
    c = psycopg2.connect("host=localhost dbname=nova_ops user=kochj"); cur = c.cursor()
    lines = [START, "## Watch it play itself", "",
             "AI versus AI, narrated by Nova (an AI voice). Two local models play each other; the narration is "
             "written from the game's own record of what happened.", ""]
    for slug in slugs:
        cur.execute("SELECT kind, title, youtube_id FROM game_marketing_videos WHERE slug=%s AND youtube_id IS NOT NULL "
                    "ORDER BY kind DESC", (slug,))       # highlights, then full
        rows = cur.fetchall()
        if not rows:
            sys.exit(f"no uploaded videos for {slug}")
        cells = [f'<a href="https://www.youtube.com/watch?v={vid}"><img src="https://img.youtube.com/vi/{vid}/hqdefault.jpg" '
                 f'width="360" alt="{t.replace(chr(34), "")}"></a>' for _, t, vid in rows]
        kn = {"highlights": "Highlights", "full": "Full game"}
        caps = [f"**{kn.get(k, k)}:** [{t}](https://www.youtube.com/watch?v={vid})" for k, t, vid in rows]
        lines += ["| " + " | ".join(caps) + " |", "|" + "---|" * len(rows), "| " + " | ".join(cells) + " |", ""]
    return "\n".join(lines + [END, ""])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--readme", required=True); ap.add_argument("--slugs", nargs="+", required=True)
    ap.add_argument("--before-regex", help="insert before the first line matching this (e.g. '^## ')")
    a = ap.parse_args()
    p = Path(a.readme); txt = p.read_text()
    sec = section(a.slugs)
    if START in txt:
        txt = re.sub(re.escape(START) + r".*?" + re.escape(END) + r"\n?", sec, txt, flags=re.S)
    else:
        lines = txt.splitlines(keepends=True)
        idx = None
        if a.before_regex:
            idx = next((i for i, l in enumerate(lines) if i > 0 and re.search(a.before_regex, l)), None)
        if idx is None:   # first '## ' heading
            idx = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
        lines.insert(idx, sec + "\n")
        txt = "".join(lines)
    p.write_text(txt)
    print(f"updated {p}")


if __name__ == "__main__":
    main()
