#!/usr/bin/env python3
"""nova_pet_recognition.py — recognize the household's known pets from a camera crop.

Pets can't use the human face recognizer (sam_faces / dlib), so we identify them with the
qwen3-vl vision model that already runs on the camera feed, matched against a small registry
in PG (nova_ops.pet_registry). identify_pet() returns a known pet's name, or None for an
unknown animal (which is the interesting case — a stray/coyote worth alerting on).

CLI:
  nova_pet_recognition.py list
  nova_pet_recognition.py add   "Bailey" dog "small tan/brown long-haired dog, graying muzzle"
  nova_pet_recognition.py describe <image>        # auto-describe an animal crop (registry seeding)
  nova_pet_recognition.py identify <image>        # -> pet name / UNKNOWN_ANIMAL / NONE
"""
import json, base64, sys, urllib.request
from contextlib import closing
import psycopg2
from psycopg2.extras import RealDictCursor

PG_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen3-vl:4b"


def _conn():
    return psycopg2.connect(PG_DSN, cursor_factory=RealDictCursor)


def ensure_schema():
    with closing(_conn()) as c, c, c.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS pet_registry (
            name TEXT PRIMARY KEY, species TEXT, description TEXT,
            added_at TEXT DEFAULT now()::text)""")


def list_pets():
    with closing(_conn()) as c, c.cursor() as cur:
        cur.execute("SELECT name, species, description FROM pet_registry ORDER BY name")
        return cur.fetchall()


def set_pet(name, species, description):
    ensure_schema()
    with closing(_conn()) as c, c, c.cursor() as cur:
        cur.execute("""INSERT INTO pet_registry (name, species, description) VALUES (%s,%s,%s)
            ON CONFLICT (name) DO UPDATE SET species=EXCLUDED.species,
            description=EXCLUDED.description""", (name, species, description))


def _vlm(image_path, prompt, npredict=400):  # qwen3-vl reasons in <think>; budget for that + answer
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt, "images": [b64]}],  # /api/chat: image in message
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": npredict},
    }).encode()
    req = urllib.request.Request(OLLAMA, data=payload, headers={"Content-Type": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=60).read())
    c = data.get("message", {}).get("content", "") or ""
    if "<think>" in c:                      # strip qwen reasoning block
        e = c.rfind("</think>")
        c = c[e + 8:] if e > 0 else c
    return c.strip()


def describe(image_path):
    """One-sentence visual description of the animal in a crop — used to seed the registry."""
    return _vlm(image_path, "Describe ONLY the animal in this image in one sentence: species, "
                "size, coat color/pattern, and any distinctive features. No preamble.")


def identify_pet(image_path):
    """Return a known pet's name, or None if it's an unknown animal / no animal.

    Substring match on the model's answer against the registry keeps it robust to the model
    adding punctuation or a short phrase. None is the alert-worthy case (unknown animal)."""
    pets = list_pets()
    if not pets:
        return None
    roster = "\n".join(f"- {p['name']}: {p['description']}" for p in pets)
    prompt = ("Identify the household pet in this security-camera crop.\n"
              f"Known pets:\n{roster}\n\n"
              "If the animal clearly matches ONE known pet, reply with ONLY that name. "
              "If it's an animal that matches none, reply 'UNKNOWN_ANIMAL'. "
              "If there's no animal, reply 'NONE'.")
    ans = _vlm(image_path, prompt).strip().strip('".\'')
    for p in sorted(pets, key=lambda p: -len(p["name"])):   # longest first: "Maxine" must not match as "Max"
        if p["name"].lower() in ans.lower():
            return p["name"]
    return None


def _cli():
    a = sys.argv[1:]
    if not a or a[0] == "list":
        for p in list_pets():
            print(f"  {p['name']:12} [{p['species']}] — {p['description']}")
    elif a[0] == "add":
        set_pet(a[1], a[2], a[3]); print("saved", a[1])
    elif a[0] == "describe":
        print(describe(a[1]))
    elif a[0] == "identify":
        print(identify_pet(a[1]) or "UNKNOWN_ANIMAL/NONE")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
