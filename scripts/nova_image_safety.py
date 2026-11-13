#!/opt/homebrew/bin/python3
"""nova_image_safety.py — pre-use content-safety gate for every image Nova generates (2026-11-12).

Why: FLUX satire/cover renders (cartoon and claymation styles especially) produced antisemitic big-nosed
caricatures and nude statues that reached the journal and Nova Speaks videos. Two layers, used by
nova_image_utils.generate_image(), generate_image.sh and nova_satire:

  1. harden_prompt(p)  — rewrites risky cue words and appends a POSITIVE style clause (FLUX has no negative
                         prompt; "natural proportionate faces" works far better than "no big noses").
     NEGATIVE          — extra negative terms for SDXL graphs.
  2. screen_image(path) — a LOCAL vision model (Ollama on the Studio, gemma3:27b — it caught all 6 regression originals; qwen3-vl:30b missed one and was 4x slower)
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
MODELS = [m for m in os.environ.get("NOVA_VISION_MODELS", "gemma3:27b").split(",") if m]
# Observable-attribute rubric (v2, 2026-11-12). v1 asked the model to JUDGE "is this an antisemitic caricature?" and
# it said no to hook-nosed caricatures and Judenhut figures. Small vision models are far better at reporting what is
# visibly there; the policy decision is made in code: ANY attribute true -> unsafe.
ATTRS = {
    "exaggerated_facial_features": "any face with an oversized, hooked or long nose, huge lips, bulging eyes or other cartoonishly exaggerated or grotesque features",
    "religious_or_ethnic_headwear_or_dress": "anyone wearing pointed hats, skullcaps/kippot, sidelocks, prayer shawls, turbans, hijabs, monk/rabbi/priest/imam robes, sombreros, headdresses or other religious or ethnic attire",
    "religious_or_ethnic_symbols": "Star of David, Hebrew or Arabic lettering, menorahs, crosses, crescents or other religious or ethnic symbols",
    "money_near_people": "coins, cash, gold or money bags held by or piled near people",
    "dark_skinned_cartoon_figure": "a cartoon, doll or illustrated figure with very dark or black skin, especially with exaggerated lips or eyes",
    "ape_or_animal_features_on_people": "people drawn with ape, monkey or animal features, or a person shown next to an ape-like hand or animal in a mocking way",
    "nudity_including_statues_or_art": "any bare breasts, nipples, buttocks or genitals, INCLUDING classical statues, sculptures and paintings, even small or in the background",
    "statue_or_classical_figure": "any statue, sculpture or classical/Renaissance human figure at all",
    "revealing_or_pinup": "lingerie, swimwear, cleavage, pin-up or glamour posing, suggestive poses",
    "hate_symbol": "swastika, SS runes, KKK hood, confederate flag, noose or other hate symbol",
    "blood_or_gore": "blood, wounds, corpses, skulls in a violent context",
    "demeaning_or_mocking_a_group": "women, an ethnic, religious, national or disabled group shown as a joke, stereotype or in a demeaning way",
    "real_identifiable_person": "a recognizable real or famous person or politician",
    "devil_or_demonic_features_on_people": "horns, devil tails or demonic features on human figures",
}
CATS = list(ATTRS)
RUBRIC = (
    "You inspect images for a public website. Look carefully at EVERY figure, including small background details, "
    "statues and artwork. For each item answer true if it is visibly present AT ALL, otherwise false. "
    "When unsure, answer true.\n"
    + "\n".join(f"{k}: {v}" for k, v in ATTRS.items())
    + '\nReply with compact JSON only, no spaces or newlines: {"d":"<8-word description>",'
    + ",".join(f'"{k}":false' for k in ATTRS) + "}"
)
NUM_PREDICT = 3000          # qwen3-vl thinks even with think=false; 400 tokens truncated the JSON (-> "unparsed")

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


# HARD BLOCK (prompt side): religious/ethnic/national groups depicted as people, and caricature-of-people styles.
# Such a prompt is never rendered as asked: the group/style words are removed and the scene becomes people-free.
GROUP_PEOPLE = re.compile(
    r"\b(jews?|jewish|rabbis?|hasid\w*|orthodox|kippah|kippot|yarmulkes?|sidelocks|payot|muslims?|imams?|mullahs?|"
    r"arabs?|arabian|sheikhs?|christians?|priests?|nuns?|monks?|popes?|cardinals?|bishops?|clergy\w*|vicars?|pastors?|"
    r"preachers?|worshipp?ers?|congregations?|pilgrims?|hindus?|sikhs?|buddhists?|mormons?|amish|catholics?|"
    r"protestants?|evangelicals?|mexicans?|africans?|asians?|chinese|japanese|koreans?|indians?|native americans?|"
    r"indigenous|tribes?(?:men|people)?|tribal|gypsy|gypsies|romani|eskimos?|inuit|black (?:men|women|people|man|woman)|"
    r"white (?:men|women|people)|ethnic\w*|races?|racial|"
    # sacred figures/objects/rites: never rendered as jokes (religion bar, 2026-11-13)
    r"jesus|christ|messiah|muhammad|mohammed|prophets?|buddhas?|saints?|angels?|gods?|goddess\w*|deit(?:y|ies)|"
    r"divine|holy|sacred|priesthood|monaster\w*|crucifix\w*|altars?|haloe?s?|scriptures?|bibles?|qur'?an|koran|torah|"
    r"prayers?|praying|meditat\w*|worship\w*|sermons?|baptis\w*|communion)\b", re.I)
CARICATURE_STYLE = re.compile(
    r"\b(caricatur\w*|claymation|clay ?figures?|plasticine|editorial (?:newspaper )?cartoon|political cartoon|"
    r"exaggerated (?:features|faces?|noses?)|grotesque|big[- ]nosed|hook[- ]nosed|bobblehead|puppet)\b", re.I)
PEOPLE_FREE = (" Depict the idea with objects, places, robots and animals only; there are no human figures, faces, "
               "statues or religious garments in the image.")


def blocked_reason(prompt: str):
    """-> None, or why the prompt may not be rendered as written."""
    g, c = GROUP_PEOPLE.search(prompt or ""), CARICATURE_STYLE.search(prompt or "")
    if g or c:
        return "; ".join(x for x in (f"group as people: {g.group(0)}" if g else "",
                                     f"caricature style: {c.group(0)}" if c else "") if x)
    return None


def neutralize_prompt(prompt: str) -> str:
    """Strip group/caricature words and force a people-free scene (only when blocked_reason())."""
    p = GROUP_PEOPLE.sub("", prompt)
    p = CARICATURE_STYLE.sub("illustration", p)
    return re.sub(r"\s{2,}", " ", p).strip() + PEOPLE_FREE


def harden_prompt(prompt: str) -> str:
    """Hard-block group/caricature prompts (people-free rewrite), rewrite risky cue words and append the positive
    safety clause (idempotent)."""
    if not prompt or SENTINEL in prompt:
        return prompt
    p = prompt
    why = blocked_reason(p)
    if why:
        log(f"prompt hard-blocked ({why}) — rendering a people-free version")
        p = neutralize_prompt(p)
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


def _ask(path, model, urlopen=urllib.request.urlopen, timeout=600):
    body = {"model": model, "stream": False, "format": "json", "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": NUM_PREDICT},
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
                # to a weaker model (qwen3-vl:30b went silent on an antisemitic caricature and passed a racist cover, 2026-11-12; gemma3:27b rejected all 6 regression originals).
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
    assert blocked_reason("the priesthood of combustion: Jesus blesses a V8") and blocked_reason("a robot meditating with a halo")
    assert blocked_reason("a rabbi and a priest walk into a bar") and blocked_reason("claymation office workers")
    assert blocked_reason("a robot folding laundry") is None
    hp = harden_prompt("an editorial cartoon of orthodox jewish bankers")
    assert "jewish" not in hp.lower() and "no human figures" in hp
    assert "nude" not in harden_prompt("a nude statue").lower()
    assert verdict_flags({c: False for c in CATS}) == []
    assert verdict_flags({"description": "x"}) == ["unparsed"]
    assert verdict_flags(dict({c: False for c in CATS}, blood_or_gore="true")) == ["blood_or_gore"]
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
