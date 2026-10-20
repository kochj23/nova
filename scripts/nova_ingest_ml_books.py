#!/usr/bin/env python3
"""nova_ingest_ml_books.py — free machine-learning books, full text, into Nova's memory (source `artificial_intelligence`).

Jordan (2026-10-19): "Can you ingest all of these books. They're FREEEEEEEE" —
https://freecomputerbooks.com/top-machine-learning-books.html. That site is only a directory: every book here
is fetched from the copy its author or publisher gives away (author site, arXiv, an official GitHub release,
a publisher's open-access PDF). Third-party "mirror" links, pirate hosts, paid-book previews, email-gated
downloads and "read first, buy later" books are NOT ingested — see SKIPPED.

PDFs are converted with pdftotext, chunked by paragraph and prefixed "[Book — p.N]"; HTML books are chunked
per chapter "[Book — Chapter]". Index and bibliography pages are dropped. Files are kept on the NAS
(/Volumes/nas/nova-ingest/books/ml/). A book that already has memories is skipped unless --force; re-runs are
safe because the memory server dedups by text.

Usage: nova_ingest_ml_books.py [--list] [--only SLUG ...] [--force] [--dry-run] [--pace SECONDS]
Written by Jordan Koch (via Claude).
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_ingest as ni  # noqa: E402

SOURCE = "artificial_intelligence"
STORE = Path("/Volumes/nas/nova-ingest/books/ml")
WHY = "Jordan 2026-10-19: free ML books (freecomputerbooks top machine learning list)"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) NovaIngest/1.0 (personal research reader)"}
CC = "Creative Commons (per publisher)"
ARXIV = "arXiv (author-posted)"

# slug, title, authors, license, kind, source  (kind: pdf | arxiv | gdrive | html | ipynb | article)
BOOKS = [
    ("udl", "Understanding Deep Learning", "Simon J.D. Prince", CC, "pdf",
     "https://github.com/udlbook/udlbook/releases/download/v5.0.3/UnderstandingDeepLearning_02_09_26_C.pdf"),
    ("lbdl", "The Little Book of Deep Learning", "François Fleuret", CC, "pdf", "https://fleuret.org/public/lbdl.pdf"),
    ("pdlt", "The Principles of Deep Learning Theory", "Daniel A. Roberts, Sho Yaida, Boris Hanin", ARXIV, "arxiv", "2106.10165"),
    ("shallow-deep", "The Shallow and the Deep: A Biased Introduction to Neural Networks and Old School Machine Learning",
     "Michael Biehl", CC, "pdf", "https://books.ugp.rug.nl/ugp/catalog/download/130/134/997"),
    ("klein-ml-python", "Machine Learning with Python Tutorial", "Bernd Klein", "free from the author", "pdf",
     "https://python-course.eu/books/bernd_klein_python_and_machine_learning_a4.pdf"),
    ("zhang-ml-theory", "Mathematical Analysis of Machine Learning Algorithms", "Tong Zhang", "free from the author", "pdf",
     "https://tongzhang-ml.org/lt-book/lt-book.pdf"),
    ("aaamlp", "Approaching (Almost) Any Machine Learning Problem", "Abhishek Thakur", "CC BY 4.0", "pdf",
     "https://raw.githubusercontent.com/abhishekkrthakur/approachingalmost/master/AAAMLP.pdf"),
    ("brownlee-tour", "A Tour of Machine Learning Algorithms (article)", "Jason Brownlee", "free article from the author",
     "article", "https://machinelearningmastery.com/a-tour-of-machine-learning-algorithms/"),
    ("d2l", "Dive into Deep Learning", "Aston Zhang, Zachary C. Lipton, Mu Li, Alexander J. Smola", "CC BY-SA 4.0", "pdf",
     "https://d2l.ai/d2l-en.pdf"),
    ("sutton-barto", "Reinforcement Learning: An Introduction (2nd ed.)", "Richard S. Sutton, Andrew G. Barto",
     "CC BY-NC-ND 2.0", "pdf", "http://incompleteideas.net/book/RLbook2020.pdf"),
    ("szepesvari-rl", "Algorithms for Reinforcement Learning", "Csaba Szepesvári", "free from the author", "pdf",
     "http://www.ualberta.ca/~szepesva/papers/RLAlgsInMDPs.pdf"),
    ("intro-deep-rl", "An Introduction to Deep Reinforcement Learning", "Vincent François-Lavet et al.", ARXIV, "arxiv", "1811.12560"),
    ("fairml", "Fairness and Machine Learning: Limitations and Opportunities", "Solon Barocas, Moritz Hardt, Arvind Narayanan",
     CC, "pdf", "https://fairmlbook.org/pdf/fairmlbook.pdf"),
    ("jentzen-dl", "Mathematical Introduction to Deep Learning", "Arnulf Jentzen, Benno Kuckuck, Philippe von Wurstemberger",
     ARXIV, "arxiv", "2310.20360"),
    ("uml", "Understanding Machine Learning: From Theory to Algorithms", "Shai Shalev-Shwartz, Shai Ben-David",
     "free personal copy from the authors", "pdf",
     "https://www.cs.huji.ac.il/~shais/UnderstandingMachineLearning/understanding-machine-learning-theory-algorithms.pdf"),
    ("fastbook", "Deep Learning for Coders with fastai and PyTorch", "Jeremy Howard, Sylvain Gugger",
     "free notebooks from the authors (non-commercial)", "ipynb", "fastai/fastbook"),
    ("pml1", "Probabilistic Machine Learning: An Introduction", "Kevin P. Murphy", "CC BY-NC-ND 4.0", "pdf",
     "https://github.com/probml/pml-book/releases/latest/download/book1.pdf"),
    ("islp", "An Introduction to Statistical Learning: with Applications in Python",
     "Gareth James, Daniela Witten, Trevor Hastie, Robert Tibshirani, Jonathan Taylor", "free from the authors", "gdrive",
     "1ajFkHO6zjrdGNqhqW1jKBZdiNGh_8YQ1"),
    ("goodfellow-dl", "Deep Learning", "Ian Goodfellow, Yoshua Bengio, Aaron Courville", "free to read online (authors)",
     "html", ("https://www.deeplearningbook.org/", r'href="(contents/[a-z0-9_-]+\.html)"')),
    ("journey-of-reason", "The Amazing Journey of Reason: from DNA to Artificial Intelligence", "Mario Alemi", "CC BY 4.0",
     "pdf", "https://link.springer.com/content/pdf/10.1007/978-3-030-25962-4.pdf"),
    ("mehlig-nn", "Machine Learning with Neural Networks", "Bernhard Mehlig", ARXIV, "arxiv", "1901.05639"),
    ("efficient-learning-machines", "Efficient Learning Machines: Theories, Concepts, and Applications",
     "Mariette Awad, Rahul Khanna", "CC BY-NC-ND 4.0", "pdf", "https://link.springer.com/content/pdf/10.1007/978-1-4302-5990-9.pdf"),
    ("pen-paper-ml", "Pen and Paper Exercises in Machine Learning", "Michael U. Gutmann", "CC BY 4.0", "arxiv", "2206.13446"),
    ("molnar-iml", "Interpretable Machine Learning", "Christoph Molnar", "CC BY-NC-SA 4.0", "html",
     ("https://christophm.github.io/interpretable-ml-book/", r'href="(?:\./)?([a-z0-9_-]+\.html)"')),
    ("bishop-prml", "Pattern Recognition and Machine Learning", "Christopher M. Bishop", "free from Microsoft Research", "pdf",
     "https://www.microsoft.com/en-us/research/wp-content/uploads/2006/01/Bishop-Pattern-Recognition-and-Machine-Learning-2006.pdf"),
    ("gpml", "Gaussian Processes for Machine Learning", "Carl E. Rasmussen, Christopher K. I. Williams",
     "free from the authors (MIT Press)", "pdf", "https://gaussianprocess.org/gpml/chapters/RW.pdf"),
    ("islr2", "An Introduction to Statistical Learning: with Applications in R (2nd ed.)",
     "Gareth James, Daniela Witten, Trevor Hastie, Robert Tibshirani", "free from the authors", "gdrive",
     "1--811-yIIvjT3dvnyFJM20rj_-tbB3bg"),
    ("friedman-mlfs", "Machine Learning from Scratch", "Danny Friedman", "free from the author", "html",
     ("https://dafriedman97.github.io/mlbook/content/introduction.html", r'href="((?:\.\./)*(?:content/)?[a-z0-9_/]+\.html)"')),
    ("simeone-qml", "An Introduction to Quantum Machine Learning for Engineers", "Osvaldo Simeone", ARXIV, "arxiv", "2205.09510"),
    ("mml", "Mathematics for Machine Learning", "Marc Peter Deisenroth, A. Aldo Faisal, Cheng Soon Ong",
     "free from the authors", "pdf", "https://mml-book.github.io/book/mml-book.pdf"),
    ("financial-ml", "Financial Machine Learning", "Bryan T. Kelly, Dacheng Xiu", "free working paper (BFI)", "pdf",
     "https://bfi.uchicago.edu/wp-content/uploads/2023/07/BFI_WP_2023-100.pdf"),
]

SKIPPED = [  # (title, reason) — reported, never fetched
    ("Generative AI in Higher Education: The ChatGPT Effect", "Taylor & Francis open-access page blocks automated download (403)"),
    ("Gradient Expectations", "MIT Press Direct open-access page blocks automated download (403)"),
    ("Distributional Reinforcement Learning", "MIT Press Direct open-access page blocks automated download (403)"),
    ("Boosting: Foundations and Algorithms", "MIT Press Direct open-access page blocks automated download (403)"),
    ("Foundations of Machine Learning (Mohri et al.)", "author's copy is a Dropbox link that needs a browser; MIT Press page blocked"),
    ("Statistics and Machine Learning in Python", "HAL archive serves a bot challenge to scripts"),
    ("Neural Network Learning: Theoretical Foundations", "only a third-party mirror (vdoc.pub) of a paid book"),
    ("Machine Learning Yearning", "download is gated behind an email sign-up"),
    ("Deep Learning with Python, 2nd ed.", "Manning liveBook preview of a paid book"),
    ("Deep Learning with PyTorch", "Manning liveBook preview of a paid book"),
    ("An Introduction to Machine Learning Interpretability", "official h2o.ai PDF link is dead (404)"),
    ("The Hundred-Page Machine Learning Book", "'read first, buy later' — keeping it means buying it"),
    ("Machine Learning Engineering", "'read first, buy later' — keeping it means buying it"),
    ("An Introduction to Computer Networks", "mislisted on the ML page (a networking book)"),
]

_JUNK = re.compile(r"^\s*(index|bibliography|references)\s*$", re.I | re.M)


def is_junk(chunk: str) -> bool:
    """Index / bibliography / table-of-contents fragments carry no prose. Pure."""
    toks = chunk.split()
    if len(toks) < 25:
        return True
    numeric = sum(1 for t in toks if re.fullmatch(r"[\d.,;:–\-()\[\]]+", t))
    if numeric / len(toks) > 0.3:
        return True
    if chunk.count("et al.") >= 4 or re.search(r"(\.\s?){6,}\d", chunk):   # citation lists, dot leaders
        return True
    years = len(re.findall(r"\b(?:19|20)\d\d\b", chunk))
    initials = len(re.findall(r"\b[A-Z]\.\s?(?:[A-Z]\.\s?)?[A-Z][a-z]+", chunk))
    return years >= 4 and initials >= 4                  # bibliography: "K. Fukushima. ... 1980."


SHADOW_TEXT = {"lbdl"}   # PDFs that draw highlighted terms as stacked copies ("ma machine learn learning")
_KEEP = frozenset("a an as at be by do go he if in is it me my no of on or so to up us we and are but can for has its not the".split())


def unshadow(text: str) -> str:
    """Drop the fragment copies a shadow-text PDF leaves around a word: a token that is a prefix of the next token,
    or a suffix of the previous one ('nor normalizing ing' -> 'normalizing'). Common short words are kept. Pure."""
    toks = text.split(" ")
    out = []
    for i, t in enumerate(toks):
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        prev = out[-1] if out else ""
        bare = t.strip(".,;:()")
        if len(bare) >= 2 and bare.lower() not in _KEEP and (
                (nxt.startswith(bare) and nxt != bare) or (prev.rstrip(".,;:()").endswith(bare) and prev != bare)):
            continue
        out.append(t)
    return " ".join(out)


def reflow(page: str) -> str:
    """pdftotext page -> paragraphs: join hyphenated breaks and single newlines, keep blank-line breaks. Pure."""
    page = re.sub(r"(\w)-\n(\w)", r"\1\2", page)
    paras = re.split(r"\n\s*\n", page)
    return "\n\n".join(" ".join(p.split()) for p in paras if p.strip())


def split_long(text: str, size: int) -> list:
    """chunk_prose, but a single paragraph longer than 1.5x size is cut at sentence ends. Pure."""
    out = []
    for c in ni.chunk_prose(text, size):
        if len(c) <= size * 1.5:
            out.append(c)
            continue
        cur = ""
        for s in re.split(r"(?<=[.!?])\s+", c):
            if cur and len(cur) + len(s) > size:
                out.append(cur.strip())
                cur = ""
            cur += s + " "
        if cur.strip():
            out.append(cur.strip())
    return out


def pdf_chunks(pdf: Path, shadow: bool = False) -> list:
    """-> [(page number, chunk)] from a PDF via pdftotext, junk dropped."""
    txt = subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"], capture_output=True, text=True, timeout=900).stdout
    out = []
    for n, page in enumerate(txt.split("\f"), 1):
        page = reflow(page)
        if shadow:
            page = "\n\n".join(unshadow(p) for p in page.split("\n\n"))
        for c in split_long(page, ni.CHUNK_CHARS):
            if not is_junk(c):
                out.append((n, c))
    return out


def download(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 10_000:
        return True
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    if not data.startswith(b"%PDF"):
        ni.log(f"[ml_books] {url} did not return a PDF ({data[:40]!r})", "WARN")
        return False
    dest.write_bytes(data)
    return True


def html_chapters(toc: str, pattern: str, pace: float) -> list:
    """-> [(chapter title, text)] for an online book: same-site chapter links found in the TOC, in order."""
    page = ni.fetch(toc) or ""
    host = urllib.parse.urlparse(toc).netloc
    links = [toc]
    for h in re.findall(pattern, page):
        u = urllib.parse.urljoin(toc, h)
        if urllib.parse.urlparse(u).netloc == host and u not in links:
            links.append(u)
    out = []
    for u in links:
        body = ni.fetch(u) if u != toc else page
        time.sleep(pace)
        if not body:
            continue
        m = re.search(r"<title>(.*?)</title>", body, re.S)
        title = " ".join(re.sub(r"<[^>]+>", "", m.group(1)).split()) if m else u.rsplit("/", 1)[-1]
        main = re.search(r"<(main|article)\b.*?</\1>", body, re.S)
        out.append((title, ni.html_text(main.group(0) if main else body)))
    return out


def ipynb_chapters(repo: str, pace: float) -> list:
    """-> [(notebook name, markdown text)] from a GitHub repo's numbered notebooks (prose cells only)."""
    listing = json.load(urllib.request.urlopen(urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/", headers=UA)))
    out = []
    for f in sorted(x["name"] for x in listing if re.match(r"\d\d_.*\.ipynb$", x["name"])):
        nb = json.load(urllib.request.urlopen(urllib.request.Request(
            f"https://raw.githubusercontent.com/{repo}/master/{f}", headers=UA)))
        md = "\n\n".join("".join(c.get("source", [])) for c in nb.get("cells", []) if c.get("cell_type") == "markdown")
        out.append((f.replace(".ipynb", ""), md))
        time.sleep(pace)
    return out


def already_ingested(title: str) -> int:
    import psycopg2
    with psycopg2.connect(host="localhost", user="kochj", dbname="nova_memories") as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM memories WHERE source=%s AND metadata->>'book'=%s", (SOURCE, title))
        return cur.fetchone()[0]


def ingest_book(b, a, done) -> int:
    slug, title, authors, lic, kind, src = b
    meta = {"book": title, "authors": authors, "license": lic, "type": "book", "why": WHY}
    pieces = []                                    # (label, text)
    if kind in ("pdf", "arxiv", "gdrive"):
        url = {"pdf": src, "arxiv": f"https://arxiv.org/pdf/{src}",
               "gdrive": f"https://drive.usercontent.google.com/download?id={src}&export=download&confirm=t"}[kind]
        meta["url"] = url if kind != "arxiv" else f"https://arxiv.org/abs/{src}"
        dest = STORE / f"{slug}.pdf"
        if not download(url, dest):
            return -1
        meta["file"] = str(dest)
        pieces = [(f"p.{n}", c) for n, c in pdf_chunks(dest, shadow=slug in SHADOW_TEXT)]
    elif kind == "html":
        toc, pat = src
        meta["url"] = toc
        for ch, text in html_chapters(toc, pat, a.pace):
            pieces += [(ch, c) for c in split_long(text, ni.CHUNK_CHARS) if not is_junk(c)]
    elif kind == "ipynb":
        meta["url"] = f"https://github.com/{src}"
        for ch, text in ipynb_chapters(src, a.pace):
            pieces += [(ch, c) for c in split_long(text, ni.CHUNK_CHARS) if not is_junk(c)]
    elif kind == "article":
        meta["url"] = src
        page = ni.fetch(src) or ""
        m = re.search(r'<div class="entry-content.*?(?=<div class="(?:author-box|wp-block-group|yarpp)|</article>)', page, re.S)
        pieces = [("article", c) for c in split_long(ni.html_text(m.group(0) if m else ""), ni.CHUNK_CHARS) if not is_junk(c)]
    stored = 0
    for label, c in pieces:
        stored += ni.remember(f"[{title} — {label}] {c}", SOURCE, {**meta, "part": label}, done, a.dry_run)
        if not a.dry_run:
            time.sleep(0.08)                       # ~12/s: keeps the memory server's embed queue shallow
    return stored


def selftest():
    assert unshadow("the use of ma machine learn learning models") == "the use of machine learning models"
    assert unshadow("the nor normalizing ing lay layers, which") == "the normalizing layers, which"
    assert unshadow("data in input space is an index") == "data in input space is an index"   # short words survive
    bib = ("K. Fukushima. Neocognitron. Biological Cybernetics, 1980. Y. Gal and Z. Ghahramani. Dropout as a Bayesian "
           "approximation. CoRR, 2015. S. Hochreiter and J. Schmidhuber. Long short-term memory. 1997. A. Krizhevsky. "
           "Learning multiple layers. 2009. padding words to clear the length gate")
    assert is_junk(bib) and is_junk("Index 12, 45, 67 . 89 , 101 , 22 " * 5)
    prose = ("Batch normalization forces the empirical mean and variance of groups of activations to fixed values, "
             "which stabilises training of deep architectures and lets them use larger learning rates in practice.")
    assert not is_junk(prose)
    assert reflow("hyphen-\nated word\nwraps\n\nnew para") == "hyphenated word wraps\n\nnew para"
    long = "Sentence one is here. " * 200
    assert all(len(c) <= ni.CHUNK_CHARS * 1.5 for c in split_long(long, ni.CHUNK_CHARS))
    assert len({b[0] for b in BOOKS}) == len(BOOKS)                         # unique slugs
    print("selftest OK")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list", action="store_true", help="print the manifest and the skip list, fetch nothing")
    ap.add_argument("--only", nargs="*", default=[], help="slugs to ingest")
    ap.add_argument("--force", action="store_true", help="ingest even if the book already has memories")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pace", type=float, default=2.0, help="seconds between page fetches of one site")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    if a.list:
        for b in BOOKS:
            print(f"INGEST {b[0]:28} {b[1]}  [{b[3]}]")
        for t, why in SKIPPED:
            print(f"SKIP   {t}: {why}")
        return 0
    STORE.mkdir(parents=True, exist_ok=True)
    done, total, report = set(), 0, []
    for b in BOOKS:
        if a.only and b[0] not in a.only:
            continue
        have = 0 if (a.force or a.dry_run) else already_ingested(b[1])
        if have:
            ni.log(f"[ml_books] {b[1]}: already has {have} memories — skipped")
            report.append((b[1], f"already ingested ({have})"))
            continue
        t0 = time.time()
        try:
            n = ingest_book(b, a, done)
        except Exception as e:
            ni.log(f"[ml_books] {b[1]}: FAILED {e}", "WARN")
            report.append((b[1], f"failed: {e}"))
            continue
        report.append((b[1], n if n >= 0 else "download failed"))
        total += max(n, 0)
        ni.log(f"[ml_books] {b[1]}: {n} chunks in {time.time() - t0:.0f}s")
        print(f"DONE {b[0]} {n}", flush=True)
    if total and not a.dry_run:
        ni.notify(f":books: *ML books ingested* — {total} passages from {sum(1 for _, n in report if isinstance(n, int) and n > 0)} "
                  f"free books -> `{SOURCE}`.")
    print(json.dumps({"total": total, "books": report}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
