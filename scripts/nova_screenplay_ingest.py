#!/usr/bin/env python3
"""nova_screenplay_ingest.py — ingest a screenplay page (IMSDb, Daily Script, Monologue Archive, any <pre>-formatted
script) into Nova's memory as readable prose chunks.

Why not nova_ingest.py url: these sites 500/465 on non-browser user agents, and raw screenplay formatting
(indented cues, short lines) fails the ingest gate's alpha-ratio floor — Misery stored 10 chunks of 232k chars
until the text was reflowed (2026-10-05). This fetches with a browser UA, pulls the <pre> block, collapses
whitespace, joins each character cue to its line ("ANNIE: ..."), then hands the text to nova_ingest.py file mode.

  nova_screenplay_ingest.py https://imsdb.com/scripts/Jaws.html --source blockbuster_films
  nova_screenplay_ingest.py <url> --source horror --title "PSYCHO — revised screenplay by Joseph Stefano"
"""
import argparse, html, re, subprocess, sys, urllib.request
from pathlib import Path

STAGING = Path("/Volumes/Data/nova-ingest-staging")
UA = "Mozilla/5.0 (Macintosh) AppleWebKit/605 Safari/605"
CUE = re.compile(r"[A-Z0-9 .'()\-]{2,40}")


def reflow(raw: str) -> str:
    blocks = [re.sub(r"\s+", " ", b).strip() for b in re.split(r"\n\s*\n", raw)]
    out, cue = [], None
    for b in blocks:
        if not b: continue
        if CUE.fullmatch(b): cue = b; continue
        out.append(f"{cue}: {b}" if cue else b); cue = None
    return "\n\n".join(out)


def extract(page: str) -> tuple[str, str]:
    cands = re.findall(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', page, re.I) \
          + re.findall(r"<title[^>]*>(.*?)</title>", page, re.S | re.I) + re.findall(r"<h1[^>]*>(.*?)</h1>", page, re.S | re.I)
    cands = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip() for c in cands]
    cands = [c for c in cands if c and not re.search(r"imsdb|internet movie script database|daily script", c, re.I)]   # site names, not titles
    title = cands[0] if cands else ""
    title = re.sub(r"\s*(Script at IMSDb\.?|- Daily Script|\|\s*Script Slug.*|Screenplay\s*\|.*)\s*$", "", title, flags=re.I)
    page = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page, flags=re.S | re.I)
    pre = re.search(r"<pre[^>]*>(.*?)</pre>", page, re.S | re.I)
    body = html.unescape(re.sub(r"<[^>]+>", "\n", pre.group(1) if pre else page))
    body = re.split(r"Classical Monologues for Men|Copyright ©", body)[0]          # Monologue Archive footer
    return title, body


def fetch(url: str) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=120).read()


def pdf_link(url: str, page: str):
    """The URL itself when it is a PDF, else the first .pdf asset the page links to, else None."""
    if re.search(r"\.pdf(\?|$)", url, re.I): return url
    m = re.search(r"""https?://[^"'\s<>]+\.pdf(?:\?[^"'\s<>]*)?""", page, re.I)
    return m.group(0) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url"); ap.add_argument("--source", required=True); ap.add_argument("--title", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    page = fetch(a.url).decode("utf-8", "replace")
    title, body = extract(page)
    pdf = pdf_link(a.url, page)
    if pdf:                                           # Script Slug & co. serve the script as a PDF, the page is just a wrapper
        import subprocess as sp, tempfile
        tmp = tempfile.mktemp(suffix=".pdf"); Path(tmp).write_bytes(fetch(pdf))
        body = sp.run(["pdftotext", "-layout", tmp, "-"], capture_output=True, text=True, timeout=300).stdout
        body = re.sub(r"\f", "\n\n", body)             # page breaks -> paragraph breaks
        Path(tmp).unlink(missing_ok=True)
        print(f"[screenplay] PDF {pdf.split('?')[0].rsplit('/', 1)[-1]}: {len(body)} chars")
    text = reflow(body)
    head = a.title or title or re.sub(r"[_-]+", " ", Path(a.url.split("?")[0]).stem).strip() or "screenplay"
    text = f"{head} ({a.url.split('://')[-1]})\n\n{text}"
    alpha = sum(c.isalpha() for c in text) / max(1, len(text))
    STAGING.mkdir(parents=True, exist_ok=True)
    out = STAGING / (re.sub(r"[^\w]+", "_", head.lower()).strip("_")[:80] + ".txt")
    out.write_text(text)
    print(f"[screenplay] {head!r}: {len(text)} chars, alpha {alpha:.2f}, file {out}")
    if alpha < 0.6: print("[screenplay] WARNING: low alpha ratio — chunks may still be discarded", file=sys.stderr)
    if a.dry_run: return 0
    r = subprocess.run([sys.executable, str(Path(__file__).with_name("nova_ingest.py")), "file", str(out), "--source", a.source, "--yes"],
                       capture_output=True, text=True, timeout=1800)
    print((r.stdout or r.stderr).strip().splitlines()[-1][:160]); return r.returncode


if __name__ == "__main__":
    sys.exit(main())
