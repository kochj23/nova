#!/usr/bin/env python3
"""
nova_bestseller_ingest.py — Ingest Wikipedia content for all books that sold 100M+ copies.

Fetches each book's Wikipedia page, follows links to related pages (author, adaptations,
sequels, etc.), classifies content by genre, and ingests into Nova's vector memory.

Written by Jordan Koch.
"""

import json
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

MEMORY_URL = "http://127.0.0.1:18790/remember?async=1"
STATUS_INTERVAL = 300
CHUNK_SIZE = 1500
DELAY_BETWEEN_PAGES = 5.0

BOOKS_100M = [
    "A Tale of Two Cities", "The Little Prince", "The Alchemist (novel)",
    "Harry Potter and the Philosopher's Stone", "And Then There Were None",
    "Dream of the Red Chamber", "The Hobbit", "Harry Potter",
    "The Lord of the Rings", "Le Petit Prince",
    "Don Quixote", "The Da Vinci Code", "The Catcher in the Rye",
    "The Adventures of Pinocchio", "Anne of Green Gables",
    "One Hundred Years of Solitude", "Heidi (novel)",
    "The Lion, the Witch and the Wardrobe", "The Chronicles of Narnia",
    "Diary of a Wimpy Kid", "Twilight (novel series)",
    "Fifty Shades of Grey", "The Hunger Games", "Chicken Soup for the Soul",
    "Perry Mason", "Choose Your Own Adventure", "Sweet Valley High",
    "Noddy (character)", "Jack Reacher", "Nancy Drew",
    "Robert Langdon", "Geronimo Stilton", "Percy Jackson & the Olympians",
    "The Baby-sitters Club", "Clifford the Big Red Dog",
    "Peter Rabbit", "Musashi (novel)", "Mr. Men",
    "Millennium (novel series)", "James Bond (literary character)",
    "Scouting for Boys", "Guinness World Records",
    "The McGuffey Readers", "Berenstain Bears", "Frank Merriwell",
    # 50-100 million copies
    "She: A History of Adventure", "The Da Vinci Code",
    "Harry Potter and the Chamber of Secrets", "The Catcher in the Rye",
    "Harry Potter and the Prisoner of Azkaban", "Harry Potter and the Goblet of Fire",
    "Harry Potter and the Order of the Phoenix", "Harry Potter and the Half-Blood Prince",
    "Harry Potter and the Deathly Hallows", "Sophie's World",
    "The Bridges of Madison County", "One Hundred Years of Solitude",
    "Lolita", "Heidi (novel)", "The Common Sense Book of Baby and Child Care",
    "Anne of Green Gables", "Black Beauty", "The Name of the Rose",
    "The Eagle Has Landed (novel)", "Watership Down",
    "Charlotte's Web", "The Ginger Man", "The Purpose Driven Life",
    "The Tale of Peter Rabbit", "Jonathan Livingston Seagull",
    "The Very Hungry Caterpillar", "A Message to Garcia",
    "To Kill a Mockingbird", "Flowers in the Attic",
    "Cosmos (Carl Sagan book)", "Angels & Demons",
    "Kane and Abel (novel)", "Fear of Flying (novel)",
    "How the Steel Was Tempered", "Things Fall Apart",
    "Animal Farm", "Wolf Totem (novel)", "Gone with the Wind",
    "The Kite Runner", "The Diary of a Young Girl",
    "War and Peace", "The Thorn Birds", "Think and Grow Rich",
    "The Revolt of Mamie Stover", "The Girl with the Dragon Tattoo",
    "The Lost Symbol", "The Help (novel)", "Perfume (novel)",
    "Who Moved My Cheese?", "The Lovely Bones",
    "The Pillars of the Earth", "Life After Life (Moody book)",
    "The Shadow of the Wind", "Inferno (Dan Brown novel)",
    "A Brief History of Time", "The Godfather (novel)",
    "Catching Fire", "Mockingjay", "Good to Great",
    "Rich Dad Poor Dad", "Where the Wild Things Are",
    "The Joy of Cooking", "What to Expect When You're Expecting",
    "The Outsiders (novel)", "The Exorcist (novel)",
    "Dune (novel)", "The Road Less Traveled",
    "Interpreter of Maladies", "Shogun (novel)",
]

# Genre classification by keywords
VECTOR_CATEGORIES = {
    "literature_classic": [
        "classic", "19th century", "dickens", "hugo", "tolstoy",
        "austen", "twain", "dostoevsky", "literary fiction",
        "cervantes", "realism", "naturalism", "victorian",
    ],
    "literature_fantasy": [
        "fantasy", "tolkien", "hobbit", "ring", "magic",
        "wizard", "narnia", "lewis", "dragon", "quest",
        "middle-earth", "sorcerer", "enchant",
    ],
    "literature_mystery": [
        "mystery", "detective", "crime", "murder", "thriller",
        "agatha christie", "whodunit", "suspense", "clue",
        "perry mason", "dan brown", "code", "investigation",
    ],
    "literature_scifi": [
        "science fiction", "dystopia", "utopia", "future",
        "robot", "space", "alien", "technology", "hunger games",
        "divergent", "orwell", "brave new world",
    ],
    "literature_children": [
        "children", "young adult", "ya", "juvenile", "picture book",
        "diary of a wimpy kid", "berenstain", "clifford",
        "peter rabbit", "mr. men", "noddy", "geronimo stilton",
        "baby-sitters", "sweet valley",
    ],
    "literature_romance": [
        "romance", "love", "passion", "twilight", "fifty shades",
        "relationship", "erotic", "desire",
    ],
    "literature_adventure": [
        "adventure", "quest", "journey", "exploration",
        "treasure", "pirate", "james bond", "jack reacher",
        "action", "spy", "espionage", "dirk pitt",
    ],
    "literature_philosophy": [
        "philosophy", "wisdom", "spiritual", "enlightenment",
        "alchemist", "coelho", "self-help", "chicken soup",
        "motivation", "inspiration",
    ],
    "literature_historical": [
        "historical", "war", "revolution", "empire", "dynasty",
        "medieval", "ancient", "civil war", "world war",
        "tale of two cities", "musashi",
    ],
    "literature_nonfiction": [
        "nonfiction", "reference", "encyclopedia", "dictionary",
        "guide", "manual", "scouting", "guinness", "mcguffey",
        "education", "textbook",
    ],
    "literature_general": [],  # fallback
}

# ── State ─────────────────────────────────────────────────────────────────────

shutdown = False
stats = {
    "books_processed": 0,
    "pages_processed": 0,
    "chunks_ingested": 0,
    "current_book": "",
    "current_page": "",
    "current_vector": "",
    "errors": 0,
    "by_vector": {},
    "by_book": {},
}
last_status_time = 0


def signal_handler(sig, frame):
    global shutdown
    shutdown = True
    log("Shutdown requested...")


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[books-ingest {ts}] {msg}", flush=True)


def notify(text):
    try:
        nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception as e:
        log(f"Slack notify failed: {e}")


# ── Classification ────────────────────────────────────────────────────────────

def classify_content(title, text):
    combined = (title + " " + text[:2000]).lower()
    scores = {}
    for vector, keywords in VECTOR_CATEGORIES.items():
        if not keywords:
            continue
        score = sum(1 for kw in keywords if kw in combined)
        if score > 0:
            scores[vector] = score
    if not scores:
        return "literature_general"
    return max(scores, key=scores.get)


# ── Wikipedia Fetching ────────────────────────────────────────────────────────

def fetch_wiki_page(title):
    api_url = (
        f"https://en.wikipedia.org/w/api.php?action=query"
        f"&titles={urllib.parse.quote(title)}"
        f"&prop=extracts|links&explaintext=1&pllimit=max&format=json"
    )
    req = urllib.request.Request(api_url, headers={"User-Agent": "Nova/1.0 (local research bot; kochj23@github)"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 10 * (attempt + 1)
                log(f"  Rate limited (429), waiting {wait}s...")
                time.sleep(wait)
                continue
            return None, [], str(e)
        except Exception as e:
            return None, [], str(e)
    else:
        return None, [], "rate limited after 5 retries"

    pages = data.get("query", {}).get("pages", {})
    if not pages:
        return None, [], "no pages"

    page = list(pages.values())[0]
    if "missing" in page:
        return None, [], "page missing"

    text = page.get("extract", "")
    page_title = page.get("title", title)

    links = []
    for link in page.get("links", []):
        link_title = link.get("title", "")
        if link.get("ns", 0) == 0 and ":" not in link_title:
            links.append(link_title)

    return (page_title, text), links, None


def chunk_text(text, chunk_size=CHUNK_SIZE):
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    for para in paragraphs:
        para = para.strip()
        if not para or len(para) < 30:
            continue
        if len(current) + len(para) > chunk_size:
            if current:
                chunks.append(current.strip())
            current = para
        else:
            current += "\n\n" + para if current else para
    if current.strip():
        chunks.append(current.strip())
    return chunks


def ingest_chunk(text, title, vector, book_name):
    payload = json.dumps({
        "text": text,
        "metadata": {
            "source": vector,
            "title": title,
            "book": book_name,
            "type": "wikipedia_book",
            "ingested_at": datetime.now().isoformat(),
            "privacy": "public",
        },
    }).encode()
    req = urllib.request.Request(
        MEMORY_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True
    except Exception:
        return False


# ── Status Reporting ──────────────────────────────────────────────────────────

def post_status():
    top_vectors = sorted(stats["by_vector"].items(), key=lambda x: x[1], reverse=True)[:8]
    vector_lines = "\n".join(f"  • {v}: {c} chunks" for v, c in top_vectors)
    recent_books = sorted(stats["by_book"].items(), key=lambda x: x[1], reverse=True)[:5]
    book_lines = "\n".join(f"  • {b}: {c} chunks" for b, c in recent_books)

    msg = (
        f":books: *Bestseller Book Ingest* — {stats['books_processed']}/{len(BOOKS_100M)} books\n"
        f":page_facing_up: Pages: {stats['pages_processed']} | Chunks: {stats['chunks_ingested']}\n"
        f":x: Errors: {stats['errors']}\n"
        f":book: Current book: {stats['current_book']}\n"
        f":mag: Current page: {stats['current_page']}\n"
        f":label: Vector: {stats['current_vector']}\n\n"
        f"*By genre:*\n{vector_lines}\n\n"
        f"*Top books:*\n{book_lines}"
    )
    notify(msg)


# ── Main ──────────────────────────────────────────────────────────────────────

def process_book(book_title):
    """Fetch a book's Wikipedia page + follow related links."""
    global last_status_time

    stats["current_book"] = book_title
    log(f"--- Processing: {book_title} ---")

    # Fetch main book page
    result, links, error = fetch_wiki_page(book_title)
    if error or not result:
        stats["errors"] += 1
        log(f"  Error fetching {book_title}: {error}")
        return

    page_title, text = result
    if len(text) < 100:
        return

    vector = classify_content(page_title, text)
    stats["current_page"] = page_title
    stats["current_vector"] = vector

    # Chunk and ingest main page
    chunks = chunk_text(text)
    for chunk in chunks:
        if ingest_chunk(chunk, page_title, vector, book_title):
            stats["chunks_ingested"] += 1
            stats["by_vector"][vector] = stats["by_vector"].get(vector, 0) + 1
            stats["by_book"][book_title] = stats["by_book"].get(book_title, 0) + 1

    stats["pages_processed"] += 1
    log(f"  {page_title} → {vector} ({len(chunks)} chunks)")

    # Follow up to 20 relevant links per book
    relevant_links = [l for l in links if any(kw in l.lower() for kw in
        [book_title.split()[0].lower(), "author", "novel", "film", "adaptation",
         "sequel", "series", "character", "plot", "reception", "award"])][:20]

    # If not many relevant, take first 15
    if len(relevant_links) < 10:
        relevant_links = links[:15]

    visited = {page_title}
    for link_title in relevant_links:
        if shutdown:
            break
        if link_title in visited:
            continue
        visited.add(link_title)

        time.sleep(DELAY_BETWEEN_PAGES)

        result, _, error = fetch_wiki_page(link_title)
        if error or not result:
            stats["errors"] += 1
            continue

        sub_title, sub_text = result
        if len(sub_text) < 100:
            continue

        sub_vector = classify_content(sub_title, sub_text)
        stats["current_page"] = sub_title
        stats["current_vector"] = sub_vector

        sub_chunks = chunk_text(sub_text)
        for chunk in sub_chunks:
            if ingest_chunk(chunk, sub_title, sub_vector, book_title):
                stats["chunks_ingested"] += 1
                stats["by_vector"][sub_vector] = stats["by_vector"].get(sub_vector, 0) + 1
                stats["by_book"][book_title] = stats["by_book"].get(book_title, 0) + 1

        stats["pages_processed"] += 1

        # Status update check
        if time.time() - last_status_time >= STATUS_INTERVAL:
            post_status()
            last_status_time = time.time()

    stats["books_processed"] += 1


def main():
    global last_status_time
    last_status_time = time.time()

    log(f"Starting Bestseller Book Ingest — {len(BOOKS_100M)} books (100M+ copies sold)")
    notify(
        f":books: *Bestseller Book Ingest Starting*\n"
        f"• Books: {len(BOOKS_100M)} titles (all sold 100M+ copies)\n"
        f"• Strategy: Main page + up to 20 related links per book\n"
        f"• Vectors: genre-based (fantasy, mystery, children, classic, etc.)\n"
        f"• Updates every 5 min"
    )

    for book in BOOKS_100M:
        if shutdown:
            break
        process_book(book)
        time.sleep(DELAY_BETWEEN_PAGES)

    # Final report
    post_status()
    top_vectors = sorted(stats["by_vector"].items(), key=lambda x: x[1], reverse=True)
    vector_summary = "\n".join(f"  • {v}: {c}" for v, c in top_vectors)

    notify(
        f":checkered_flag: *Bestseller Book Ingest Complete!*\n"
        f"• Books processed: {stats['books_processed']}/{len(BOOKS_100M)}\n"
        f"• Pages crawled: {stats['pages_processed']}\n"
        f"• Chunks ingested: {stats['chunks_ingested']}\n"
        f"• Errors: {stats['errors']}\n\n"
        f"*Final genre breakdown:*\n{vector_summary}"
    )
    log(f"Done. {stats['chunks_ingested']} chunks from {stats['pages_processed']} pages.")


if __name__ == "__main__":
    main()
