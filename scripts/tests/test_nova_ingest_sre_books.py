"""Pure-function checks for nova_ingest_sre_books (no network)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nova_ingest_sre_books as s  # noqa: E402

TOC = 'x href="/sre-book/introduction/" href="/sre-book/part-II-principles/" href="/sre-book/introduction/" ' \
      'href="/sre-book/bibliography/" href="/sre-book/embracing-risk/"'


def test_chapter_links_order_dedup_skip():
    assert s.chapter_links(TOC, s.BOOKS[0][3], s.BOOKS[0][2]) == [
        "https://sre.google/sre-book/introduction/", "https://sre.google/sre-book/embracing-risk/"]


def test_chapter_title_and_body_slice():
    page = ('<h2 class="chapter-title"> Chapter 3 -\n Embracing Risk </h2><nav>menu</nav>'
            '<div id="maia-main"><p>Body&nbsp;text here.</p></div><div class="footer">foot</div>')
    title, body = s.chapter(page, '<div id="maia-main"', '<div class="footer">')
    assert title == "Chapter 3 - Embracing Risk"
    assert "Body text here." in body and "foot" not in body and "menu" not in body


def test_bsrs_h1_title():
    assert s.chapter("<title>Chapter 5: BSRS</title><body><h1>Design for Least Privilege</h1></body>",
                     "<body", "</body>")[0] == "Design for Least Privilege"
