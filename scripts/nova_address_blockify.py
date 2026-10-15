#!/usr/bin/env python3
"""nova_address_blockify.py — public text never carries a house number; it says "the 1200 block of".

Found 2026-10-15 by Scanner's Catch (nova_scanners_catch.py): 121 journal posts quoted street addresses
lifted from police/fire dispatch ("Armed Assault with Deadly Weapon at <number> South Alvarado Street").
Those are bystanders' homes. Newsrooms publish the block, not the door; so does Nova now.

blockify(text) rewrites "<3-5 digit number> <street name> <suffix>" to "the <hundreds> block of <street> <suffix>".
nova_journal.publish_hugo calls it on every post; `--rewrite-published` applies it once to the existing
journal (a git-tracked repo, so every change is reversible and reviewable).

Usage: nova_address_blockify.py --rewrite-published [--dry-run]   |   --selftest
Written by Jordan Koch (via Claude).
"""
import argparse
import re
import sys
from pathlib import Path

_SUFFIX = (r"St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Rd|Road|Way|Pl|Place|Ln|Lane|Ct|Court|"
           r"Ter|Terrace|Pkwy|Parkway|Hwy|Highway|Cir|Circle")
ADDRESS = re.compile(r"\b(\d{3,5})\s+((?:[NSEW]\.?\s+|North\s+|South\s+|East\s+|West\s+)?"
                     r"(?:[A-Z][A-Za-z'\-]+\s+){1,3}(?:" + _SUFFIX + r")\b\.?)")


def _block(m: re.Match) -> str:
    n = int(m.group(1))
    return f"the {n // 100 * 100} block of {m.group(2)}"


def blockify(text: str) -> str:
    """House numbers -> block. Idempotent ("the 1200 block of X St" has no house number left). Pure."""
    if not text:
        return text
    out = ADDRESS.sub(_block, text)
    return re.sub(r"\b(at|on|near|in)\s+the the\b", r"\1 the", out)


def rewrite_published(root: Path, dry_run=False) -> tuple[int, int]:
    files = changed = 0
    for f in root.rglob("*.md"):
        t = f.read_text(errors="ignore")
        n = len(ADDRESS.findall(t))
        if not n:
            continue
        files += 1
        changed += n
        if not dry_run:
            f.write_text(blockify(t))
    return files, changed


def selftest() -> int:
    assert blockify("Assault at 1234 South Alvarado Street now") == "Assault at the 1200 block of South Alvarado Street now"
    assert blockify("fire at 450 W Elm Ave.") == "fire at the 400 block of W Elm Ave."
    assert blockify("the 1200 block of Main St") == "the 1200 block of Main St"
    assert blockify("In 2026 we had 300 calls") == "In 2026 we had 300 calls"
    assert blockify("") == ""
    print("selftest ok")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rewrite-published", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.rewrite_published:
        sys.path.insert(0, str(Path(__file__).parent))
        import nova_journal as nj
        files, n = rewrite_published(nj.HUGO_ROOT / "content", a.dry_run)
        print(f"[blockify] {'would rewrite' if a.dry_run else 'rewrote'} {n} addresses in {files} posts")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
