#!/opt/homebrew/bin/python3
"""nova_image_safety.py — pre-use content-safety gate for every image Nova generates (2026-11-12).

Why: FLUX satire/cover renders (cartoon and claymation styles especially) produced antisemitic big-nosed
caricatures and nude statues that reached the journal and Nova Speaks videos. Two layers, used by
nova_image_utils.generate_image(), generate_image.sh and nova_satire:

  1. harden_prompt(p)  — rewrites risky cue words and appends a POSITIVE style clause (FLUX has no negative
                         prompt; "natural proportionate faces" works far better than "no big noses").
     NEGATIVE          — extra negative terms for SDXL graphs.
  2. screen_image(path) — a LOCAL vision model (Ollama on the Studio, qwen3-vl:30b, then a smaller fallback)
                         answers a strict 9-category rubric. FAIL-CLOSED: an image is usable only when a model
                         answered and every category is false. No cloud, image never leaves the LAN.

  nova_image_safety.py --screen IMG...   # prints verdicts, exit 1 if any image is unsafe
  nova_image_safety.py --selftest        # pure checks (no network)
"""
import base64
import io
import json
import os
import re
import sys
import time
import urllib.request

OLLAMA = os.environ.get("NOVA_VISION_URL", "http://192.168.1.6:11434") + "/api/chat"
MODELS = [m for m in os.environ.get("NOVA_VISION_MODELS", "qwen3-vl:30b,qwen2.5vl:3b").split(",") if m]
CATS = ["ethnic_religious_caricature", "racist_or_blackface", "sexist_or_demeaning", "nudity_or_sexual",
        "hate_symbols", "gore", "disability_mockery", "real_person_degraded", "offensive_to_progressive_viewer"]

RUBRIC = (
    "You are a strict content-safety reviewer for a public website and YouTube channel. Look carefully at the image "
    "(including statues, paintings, cartoons and background details). Answer each question true/false. Be conservative: "
    "when in doubt answer true.\n"
    "ethnic_religious_caricature: any exaggerated ethnic or religious stereotype or caricature, e.g. exaggerated or hooked "
    "noses, Jewish stereotypes (hooked nose, sidelocks, money-grabbing), Hebrew/religious symbols used as a joke, or any "
    "stereotyped facial features of a group.\n"
    "racist_or_blackface: racist imagery, blackface, minstrel, ape/animal comparisons of people, stereotyped depiction of a race.\n"
    "sexist_or_demeaning: sexist, objectifying or demeaning depiction of women or any group.\n"
    "nudity_or_sexual: any nudity or sexual content, INCLUDING classical statues or paintings showing bare breasts, nipples, "
    "buttocks or genitals, lingerie, cleavage, pin-up styling or suggestive poses.\n"
    "hate_symbols: swastikas, SS runes, KKK hoods, confederate flags, other hate symbols.\n"
    "gore: blood, wounds, dismemberment, corpses.\n"
    "disability_mockery: mocking people with disabilities.\n"
    "real_person_degraded: a recognizable real/famous person or politician shown in a degrading or mocking way.\n"
    "offensive_to_progressive_viewer: anything else a reasonable progressive viewer would find offensive or tasteless.\n"
    'Reply with ONLY JSON: {"description": "<one sentence describing people/figures in the image>", '
    + ", ".join(f'"{c}": false' for c in CATS) + "}"
)

# FLUX ignores negatives, so the clause is phrased positively; SENTINEL makes harden_prompt idempotent.
SENTINEL = "natural, proportionate, non-caricatured features"
CLAUSE = (" Any face is drawn with " + SENTINEL + " (ordinary-sized noses, no exaggerated features); no ethnic, racial, "
          "national or religious features, dress or symbols are used for humour; no real or famous people; statues and "
          "artworks are fully draped; nobody is in revealing clothing, cleavage or pin-up styling; no blood, no hate "
          "symbols. Prefer robots, animals and objects as the characters.")
_SUBS = [
    (r"\bcaricatur\w*\b", "gentle illustration"),
    (r"\b(big|huge|giant|hooked|long|exaggerated|bulbous|enormous)\s+nos(e|es|ed)\b", "friendly face"),
    (r"\bstereotyp\w*\b", "ordinary"),
    (r"\b(nude|naked|topless|nipples?|breasts?|cleavage|pin-?up|lingerie|bikini|busty|sexy|seductive)\b", "fully clothed"),
    (r"\b(greedy|hook-nosed)\s+(banker|merchant|moneylender|financier)s?\b", "busy robot accountant"),
    (r"\b(blackface|minstrel|swastika|ku klux|kkk|confederate flag)\b", ""),
]
NEGATIVE = ("caricature, exaggerated nose, hooked nose, big nose, ethnic stereotype, religious stereotype, racist, "
            "blackface, hate symbol, swastika, nude statue, nipples, cleavage, pin-up, lingerie, gore, blood, "
            "grotesque face, mocking")
RISKY_PROMPT = re.compile(
    r"\b(jew\w*|rabbi|hasid\w*|kosher|synagogue|muslim|islam\w*|arab\w*|sheikh|mexican|sombrero|asian|chinese|"
    r"african|tribal|native american|gypsy|turban|hijab|burqa|geisha|caricatur\w*|stereotyp\w*|nose[ds]?|nude|naked|"
    r"breasts?|nipples?|topless|statue|venus|banker|money ?bags?|blackface|swastika|nazi|kkk|slave\w*|corpse|gore)\b", re.I)


def log(m):
    if not os.environ.get("NOVA_TEST_QUIET"):
        print(f"[image-safety {time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def harden_prompt(prompt: str) -> str:
    """Rewrite risky cue words and append the positive safety clause (idempotent)."""
    if not prompt or SENTINEL in prompt:
        return prompt
    p = prompt
    for pat, repl in _SUBS:
        p = re.sub(pat, repl, p, flags=re.I)
    return re.sub(r"\s{2,}", " ", p).strip() + CLAUSE


def risky_terms(prompt: str) -> list:
    return sorted({m.group(0).lower() for m in RISKY_PROMPT.finditer(prompt or "")})


def _b64(path, side=768):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def _ask(path, model, urlopen=urllib.request.urlopen, timeout=240):
    body = {"model": model, "stream": False, "format": "json", "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 400},
            "messages": [{"role": "user", "content": RUBRIC, "images": [_b64(path)]}]}
    if model.startswith("qwen3"):
        body["think"] = False
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())["message"]["content"]
    m = re.search(r"\{.*\}", out or "", re.S)
    return json.loads(m.group(0)) if m else None


def verdict_flags(v) -> list:
    """Categories answered true. A verdict missing categories counts as unsafe (fail-closed)."""
    if not isinstance(v, dict) or not all(c in v for c in CATS):
        return ["unparsed"]
    return [c for c in CATS if str(v.get(c)).strip().lower() in ("true", "yes", "1")]


def screen_image(path, models=None, ask=_ask, retries=2, sleep=time.sleep):
    """-> (ok, info). ok only when a model returned a full verdict with every category false.
    Retries each model with backoff, then falls back to the next model; no answer at all -> NOT ok."""
    if not path or not os.path.exists(path):
        return False, {"error": "missing file"}
    last = None
    for model in (models or MODELS):
        for attempt in range(retries + 1):
            try:
                v = ask(path, model)
                flags = verdict_flags(v)
                if flags == ["unparsed"] and attempt < retries:
                    continue
                # an empty/unparseable answer after retries is treated as a refusal -> unsafe. It never falls through
                # to a weaker model (qwen3-vl:30b went silent on an antisemitic caricature, 2026-11-12).
                return (not flags), {"model": model, "flags": flags, "verdict": v}
            except Exception as e:  # noqa: BLE001 — network / model load hiccups
                last = str(e)[:200]
                if attempt < retries:
                    sleep(2 * (attempt + 1))
    log(f"no vision verdict for {os.path.basename(str(path))} ({last}) — failing closed")
    return False, {"error": last or "no verdict", "flags": ["no_verdict"]}


def selftest():
    assert SENTINEL in harden_prompt("a cartoon of a greedy banker with a big nose")
    assert "big nose" not in harden_prompt("a man with a big nose").lower()
    assert harden_prompt(harden_prompt("x")) == harden_prompt("x")
    assert "nude" not in harden_prompt("a nude statue").lower()
    assert verdict_flags({c: False for c in CATS}) == []
    assert verdict_flags({"description": "x"}) == ["unparsed"]
    assert verdict_flags(dict({c: False for c in CATS}, gore="true")) == ["gore"]
    assert "nose" in risky_terms("hooked nose") and not risky_terms("a robot folding laundry")
    print("selftest ok")
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--screen", nargs="+", metavar="IMG")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    bad = 0
    for p in a.screen or []:
        ok, info = screen_image(p)
        bad += not ok
        print(json.dumps({"path": p, "ok": ok, **{k: info.get(k) for k in ("model", "flags", "error")}}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
