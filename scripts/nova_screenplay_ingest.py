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
    # strip the site suffix BEFORE the site-name filter, else "Jaws Script at IMSDb." is discarded with the site names
    cands = [re.sub(r"\s*(Script at IMSDb\.?|- Daily Script|\|\s*Script Slug.*|Screenplay\s*\|.*)\s*$", "", c, flags=re.I)
             for c in cands]
    cands = [c for c in cands if c and not re.search(r"imsdb|internet movie script database|daily script", c, re.I)]   # site names, not titles
    title = cands[0] if cands else ""
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


SCRIBD_PY = "/Volumes/Data/AI/youtube-up/venv/bin/python"      # has selenium + Firefox via selenium-manager
SCRIBD_JS = r"""
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
import sys, time, json, re
o = Options(); o.add_argument("--headless"); d = webdriver.Firefox(options=o); d.set_page_load_timeout(90)
try:
    d.get(sys.argv[1])
    for _ in range(15):
        time.sleep(3)
        if "Client Challenge" not in d.title: break
    d.execute_script("for (const b of document.querySelectorAll('button')) if (/I Understand|Accept/i.test(b.textContent)) b.click();")
    pages = d.execute_script("return document.querySelectorAll('.outer_page, [class*=outer_page]').length")
    seen = {}
    for i in range(80):
        d.execute_script(f"window.scrollTo(0, {i*1500});"); time.sleep(0.6)
        for p in d.execute_script("return Array.from(document.querySelectorAll('.outer_page, [class*=outer_page]')).map((p,i)=>({i, text:(p.innerText||'').trim()}))"):
            if p["text"] and len(p["text"]) > len(seen.get(p["i"], "")): seen[p["i"]] = p["text"]
        if len(seen) >= pages: break
    print(json.dumps({"title": re.sub(r"\s*\|\s*PDF.*$", "", d.title), "pages": pages, "text": "\n\n".join(seen[k] for k in sorted(seen))}))
finally:
    d.quit()
"""


def scribd_text(url: str) -> tuple[str, str]:
    import subprocess as sp, json
    r = sp.run([SCRIBD_PY, "-c", SCRIBD_JS, url], capture_output=True, text=True, timeout=900)
    data = json.loads(r.stdout.strip().splitlines()[-1])
    lines, seen = [], set()
    for l in data["text"].split("\n"):
        s = l.strip()
        if not s: lines.append(""); continue
        if re.fullmatch(r"\(CONTINUED\)|CONTINUED:?|\d{1,3}\.?|Download to read ad-free|Ad", s): continue
        if alpha_ratio(s) < 0.5 and len(s) > 3: continue       # font-garbled title cards
        lines.append(re.sub(r"\s\*$", "", s))
    body = "\n".join(lines)
    # Scribd's text layer repeats ~40% of paragraphs; drop exact repeats (the memory server would dedupe anyway)
    out, had = [], set()
    for para in re.split(r"\n\s*\n", body):
        k = re.sub(r"\s+", " ", para).strip()
        if k and k not in had: had.add(k); out.append(para)
    return data["title"], "\n\n".join(out)


def alpha_ratio(t: str) -> float:
    return sum(c.isalpha() for c in t) / max(1, len(t))


def ocr_pdf(pdf_path: str, dpi: int = 300) -> str:
    """pdftoppm -> tesseract per page. ~1-2 s/page on the Studio; a 100-page script is a couple of minutes."""
    import subprocess as sp, tempfile, glob, os
    d = tempfile.mkdtemp(prefix="ocr-")
    sp.run(["pdftoppm", "-r", str(dpi), "-gray", pdf_path, f"{d}/p"], check=True, timeout=600)
    pages = []
    for png in sorted(glob.glob(f"{d}/p-*.pgm") + glob.glob(f"{d}/p-*.png")):
        r = sp.run(["tesseract", png, "-", "--psm", "6", "-l", "eng"], capture_output=True, text=True, timeout=120)
        pages.append(r.stdout); os.unlink(png)
    os.rmdir(d)
    return "\n\n".join(pages)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url"); ap.add_argument("--source", required=True); ap.add_argument("--title", default=None)
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--ocr", action="store_true", help="force tesseract OCR of the PDF")
    a = ap.parse_args()
    if "scribd.com/" in a.url:                        # JS challenge + lazy-rendered text layers -> drive a headless browser
        title, body = scribd_text(a.url)
        print(f"[screenplay] scribd: {len(body)} chars")
        pdf = None
    else:
        page = fetch(a.url).decode("utf-8", "replace")
        title, body = extract(page)
        pdf = pdf_link(a.url, page)
    if pdf:                                           # Script Slug & co. serve the script as a PDF, the page is just a wrapper
        import subprocess as sp, tempfile
        tmp = tempfile.mktemp(suffix=".pdf"); Path(tmp).write_bytes(fetch(pdf))
        body = sp.run(["pdftotext", "-layout", tmp, "-"], capture_output=True, text=True, timeout=300).stdout
        body = re.sub(r"\f", "\n\n", body)             # page breaks -> paragraph breaks
        print(f"[screenplay] PDF {pdf.split('?')[0].rsplit('/', 1)[-1]}: {len(body)} chars")
        if alpha_ratio(body) < 0.62 or a.ocr:            # scanned script with a garbage text layer (Hostel 2005: 0.53) -> re-OCR
            body = ocr_pdf(tmp)
            print(f"[screenplay] re-OCR with tesseract: {len(body)} chars, alpha {alpha_ratio(body):.2f}")
        Path(tmp).unlink(missing_ok=True)
    text = reflow(body)
    head = a.title or title or re.sub(r"[_-]+", " ", Path(a.url.split("?")[0]).stem).strip() or "screenplay"
    text = f"{head} ({a.url.split('://')[-1]})\n\n{text}"
    if not pdf and "scribd.com/" not in a.url and not re.search(r"<pre[^>]*>", page, re.I):
        print(f"[screenplay] SKIP {a.url}: no <pre> script block and no PDF link (wrapper page / reader widget)", file=sys.stderr); return 2
    if len(text) < 5000:
        print(f"[screenplay] SKIP {head!r}: only {len(text)} chars of text (image-only or paywalled pages)", file=sys.stderr); return 2
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
