#!/opt/homebrew/opt/python@3.14/bin/python3.14
"""nova_recipe_ingest.py — widen Nova's recipe memories beyond Meat Church, for the three daily plates in
Food and Friends (Jordan 2026-11-02: one recipe each for the pitmaster, the pescatarian and the vegetarian).

The recipe library was 295 Meat Church pages — perfect for the smoker, useless for a vegetarian pizza night or a
fully-cooked fish dinner, which is why October's Recipe Recommendation rotated the same four seafood dishes.

For each site: read its sitemap(s), keep recipe URLs whose slug matches that site's keywords (fish / egg dishes,
pizza / Italian / burrito bowls / appetizer platters), fetch the page, and keep it ONLY if it carries a schema.org
Recipe (JSON-LD) — the structured name, yield, times, ingredients and steps become the memory text (no page chrome,
no comments). Stored like the Meat Church ingest: source 'recipes', metadata type 'recipe', url, title, source_site,
chunk — so nova_friends.fetch_recipes picks them up unchanged. Already-stored URLs (PG) are skipped.

Usage: nova_recipe_ingest.py [--per-site N] [--site host] [--dry-run]
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
VECTOR_URL = "http://memory-server.digitalnoise.net:18790/remember"
CHUNK = 1500

FISH = r"cod|halibut|salmon|trout|tilapia|sea-bass|seabass|snapper|mahi|swordfish|haddock|branzino|arctic-char|fish|sole|flounder|pollock|catfish"
EGG = r"frittata|quiche|shakshuka|omelet|omelette|strata|egg-bake|breakfast-casserole|deviled-eggs|egg-muffins|baked-eggs|egg-bites"
KID = (r"pizza|pasta|lasagna|ziti|gnocchi|risotto|ravioli|marinara|eggplant-parm|parmesan|alfredo|pesto|minestrone|"
       r"burrito|sofritas|quesadilla|enchilada|taco|nachos|guacamole|pico|cilantro-lime-rice|black-beans|"
       r"crostini|bruschetta|flatbread|focaccia|calzone|stromboli|board|platter|appetizer|dip|tart|croquette|arancini|"
       r"caprese|mac-and-cheese|grilled-cheese|stuffed-shells|tortellini")
NOT_VEG = r"chicken|beef|pork|sausage|bacon|prosciutto|pepperoni|meat|turkey|shrimp|steak|lamb|ham|anchov|salami|chorizo|clam|crab|lobster|scallop|mussel|oyster|squid|calamari|tuna"
SHELLFISH = r"shrimp|crab|lobster|scallop|mussel|oyster|clam|squid|calamari|crawfish|prawn|ceviche|sushi|poke|tartare|crudo|gravlax|lox|smoked-salmon"

SITES = {
    "www.loveandlemons.com": {"name": "Love and Lemons", "sitemaps": ["https://www.loveandlemons.com/sitemap.xml"],
                              "keep": rf"(?:{KID}|{EGG})", "drop": NOT_VEG},
    "www.seriouseats.com": {"name": "Serious Eats", "sitemaps": [f"https://www.seriouseats.com/sitemap_{i}.xml" for i in (1, 2, 3, 4)],
                            "keep": rf"(?:{FISH}|{EGG}|{KID})", "drop": SHELLFISH},
    "www.simplyrecipes.com": {"name": "Simply Recipes", "sitemaps": [f"https://www.simplyrecipes.com/sitemap_{i}.xml" for i in (1, 2, 3)],
                              "keep": rf"(?:{FISH}|{EGG}|{KID})", "drop": SHELLFISH},
    "www.eatingwell.com": {"name": "EatingWell", "sitemaps": [f"https://www.eatingwell.com/sitemap_{i}.xml" for i in (1, 2, 3)],
                           "keep": rf"(?:{FISH}|{EGG}|steak|tri-tip|beef|brisket|burger|chili)", "drop": SHELLFISH},
}
NOT_RECIPE = r"recipes-|-recipes|-ideas|^best-|ways-to|how-to-|guide|tips|review|gallery|roundup|trick|-vs-|what-is|^\d+-"


def log(m):
    print(f"[recipe-ingest {time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def get(url: str, timeout=30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xml", "Accept-Language": "en-US"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def sitemap_urls(sm: str) -> list[str]:
    try:
        x = get(sm)
    except Exception as e:  # noqa: BLE001
        log(f"sitemap {sm}: {type(e).__name__}"); return []
    locs = [html.unescape(u) for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", x)]
    if "<sitemapindex" in x:                     # an index: recurse one level into post sitemaps
        out = []
        for s in locs:
            if re.search(r"post|recipe|sitemap_\d", s):
                out += sitemap_urls(s)
        return out
    return locs


def candidates(host: str, urls: list[str]) -> list[str]:
    """Recipe-looking URLs that match the site's keep words and none of its drop words. Pure."""
    cfg = SITES[host]
    out = []
    for u in urls:
        slug = u.rstrip("/").rsplit("/", 1)[-1].lower()
        if re.search(NOT_RECIPE, slug):          # roundups, guides, listicles — not one recipe
            continue
        if re.search(cfg["keep"], slug) and not re.search(cfg["drop"], slug):
            out.append(u)
    return list(dict.fromkeys(out))


def _steps(ins) -> list[str]:
    out = []
    for s in ins or []:
        if isinstance(s, str):
            out.append(s)
        elif isinstance(s, dict) and s.get("@type") == "HowToSection":
            out += _steps(s.get("itemListElement"))
        elif isinstance(s, dict):
            out.append(s.get("text") or s.get("name") or "")
    return [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x))).strip() for x in out if x]


def _iso(d) -> str:
    m = re.match(r"P(?:T)?(?:(\d+)H)?(?:(\d+)M)?", d or "")
    if not m or not any(m.groups()):
        return ""
    h, mi = int(m.group(1) or 0), int(m.group(2) or 0)
    return (f"{h} hr " if h else "") + (f"{mi} min" if mi else "")


def parse_recipe(page: str) -> dict | None:
    """schema.org Recipe from a page's JSON-LD -> {name, text}. Pure."""
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S):
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        items = d if isinstance(d, list) else d.get("@graph", [d])
        flat = []
        for it in items:                     # Yoast nests lists inside @graph lists
            flat += it if isinstance(it, list) else [it]
        for it in flat:
            if not isinstance(it, dict):
                continue
            t = it.get("@type")
            if t == "Recipe" or (isinstance(t, list) and "Recipe" in t):
                ing = [re.sub(r"\s+", " ", html.unescape(x)).strip() for x in it.get("recipeIngredient") or []]
                steps = _steps(it.get("recipeInstructions"))
                if len(ing) < 3 or not steps:
                    return None
                name = html.unescape(it.get("name") or "").strip()
                y = it.get("recipeYield")
                y = y[0] if isinstance(y, list) and y else y
                times = " · ".join(f"{k} {v}" for k, v in (("Prep", _iso(it.get("prepTime"))), ("Cook", _iso(it.get("cookTime"))),
                                                             ("Total", _iso(it.get("totalTime")))) if v)
                head = " · ".join(x for x in (f"Serves/yield: {y}" if y else "", times) if x)
                desc = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", it.get("description") or ""))).strip()
                text = (f"{name}\n{head}\n{desc}\n\nIngredients:\n" + "\n".join(f"- {x}" for x in ing) +
                        "\n\nMethod:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
                return {"name": name, "text": text.strip()}
    return None


def chunks(text: str) -> list[str]:
    out, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > CHUNK and cur:
            out.append(cur); cur = ""
        cur += para + "\n"
    return out + ([cur] if cur.strip() else [])


def remember(text: str, meta: dict) -> bool:
    req = urllib.request.Request(VECTOR_URL + "?async=1", data=json.dumps({"text": text, "source": "recipes", "metadata": meta}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=30); return True
    except Exception as e:  # noqa: BLE001
        log(f"remember failed: {e}"); return False


def stored_urls() -> set[str]:
    from nova_watches_and_friends import MEM_DSN, _connect
    c = _connect(MEM_DSN)
    try:
        cur = c.cursor()
        cur.execute("SELECT DISTINCT metadata->>'url' FROM memories WHERE source='recipes' AND metadata->>'type'='recipe'")
        return {r[0] for r in cur.fetchall() if r[0]}
    finally:
        c.close()


def main(argv) -> int:
    per = int(argv[argv.index("--per-site") + 1]) if "--per-site" in argv else 60
    only = argv[argv.index("--site") + 1] if "--site" in argv else None
    dry = "--dry-run" in argv
    have = set() if dry else stored_urls()
    total = 0
    for host, cfg in SITES.items():
        if only and only != host:
            continue
        urls = []
        for sm in cfg["sitemaps"]:
            urls += sitemap_urls(sm)
        cands = [u for u in candidates(host, urls) if u not in have]
        log(f"{host}: {len(urls)} urls, {len(cands)} new candidates")
        n = 0
        for u in cands:
            if n >= per:
                break
            try:
                r = parse_recipe(get(u))
            except Exception as e:  # noqa: BLE001
                log(f"  {u}: {type(e).__name__}"); time.sleep(1); continue
            if not r:
                continue
            if dry:
                log(f"  would store: {r['name']}"); n += 1; continue
            parts = chunks(f"{cfg['name']} Recipe: {r['text']}")
            ok = all(remember(p, {"url": u, "title": r["name"], "source_site": host.removeprefix("www."), "type": "recipe",
                                  "chunk": i, "total_chunks": len(parts)}) for i, p in enumerate(parts, 1))
            if ok:
                n += 1; total += 1
            time.sleep(0.7)                  # polite
        log(f"{host}: stored {n}")
    log(f"done: {total} recipes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
