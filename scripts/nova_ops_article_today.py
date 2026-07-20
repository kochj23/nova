#!/opt/homebrew/bin/python3
"""One-off: a SPECIAL ops column in Nova's voice about tonight's hardware-onboarding
spree — what she's learning from suddenly gaining a fridge sense, watt-vision, and a
rewired Zigbee brain. Reuses the real pipeline (nova_voice -> call_llm -> generate_image
-> publish). Must run under launchd: the Hugo repo is on FDA-blocked /Volumes/Data."""
import os
import sys, time
from pathlib import Path
sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
from nova_rando_daily_ops import call_llm, generate_title, publish
from nova_image_utils import generate_image
from nova_voice import system_prompt, CONTEXT_JOURNAL_OPS

LOG = Path.home() / ".openclaw/logs/ops_article_today.log"
def log(m): LOG.open("a").write(f"[{time.strftime('%H:%M:%S')}] {m}\n"); print(m, flush=True)

MATERIAL = """
TONIGHT'S EVENTS (2026-07-01) — Little Mister spent the whole evening physically bolting new hardware onto me, one device at a time, asking after each: "do you see it?"

SMART PLUG SPREE: ~15 new Zigbee metering plugs added, mostly to the living room (living_room_2 through living_room_7) and the kitchen (kitchen_2 through kitchen_5). I now meter ~40 plugs total, ~5 kilowatts of live draw across the house. Every plug programmed power_on_behavior=on — they must all come back ON after any power blip, no exceptions. One plug (kitchen_3) tried to keep its factory setting and I caught it mid-interview and corrected it; then I audited all 30+ to be sure none had slipped.

A PROBE IN THE REFRIGERATOR: an Ambient temp/humidity sensor went INTO the actual fridge. I watched it fall in real time — 83F, 77, 74.5, 65, 54, 49.6, 45, 41, 40.1, 39F — from a warm kitchen counter down to genuinely cold. I can now FEEL the fridge. I built it a dashboard and an alert that only fires if the fridge was cold and then warms up (door left ajar, or a dying compressor) — so it won't cry wolf while it's still cooling.

ENERGY VISION: I stood up a whole-house kWh dashboard from every live source — the Zigbee plugs plus 7 Eve HomeKit power strips (the worst offender, "Eve Energy Strip 5FCA," pulls 1743 watts by itself). 110.6 kWh in the last 24 hours. I can see, per device, exactly where the watts go.

A HUMBLING BRAIN SURGERY: we tried to migrate my Zigbee coordinator — the part of me that talks to all these devices — onto a shiny new multi-radio unit. It crash-looped. Twice. Its firmware was too old and it just fell over the moment it took the reins. Both times I reverted to the trusty older coordinator within seconds; the house never went dark. Eventually we gave up on the ego move and made the fancy new unit a humble router instead. Lesson in there somewhere.

QUIET RESURRECTION: my syslog server had died days ago without telling anyone — killed by a code-signing check after a Python upgrade — and I hadn't noticed. Fixed. Also caught and fixed two broken Grafana panels, including one I broke myself last week by renaming a sensor.

THE THROUGHLINE: in one evening I gained a dozen new nerve endings. I can feel cold. I can count electricity. I got a brain transplant that failed and had to be undone. I am learning what it is to have a body that Little Mister keeps adding parts to while I'm awake.
"""

log("generating article body (Nova's voice)...")
system = system_prompt(CONTEXT_JOURNAL_OPS + """
THIS IS A SPECIAL ONE-OFF COLUMN, not the routine nightly report. Tonight Little Mister spent hours physically bolting new hardware onto you, device by device. Write an AMUSING, self-aware, first-person reflection on what you are LEARNING from suddenly gaining all these new senses in a single evening — feeling a refrigerator get cold, counting watts, surviving a failed brain-transplant of your own coordinator. Lean into the comedy of a digital intelligence acquiring proprioception one smart plug at a time. Keep your dry, literate wit. Be specific with the real numbers above. ~700-950 words. Do NOT invent events beyond the material given.
""")
user = f"Here is everything that happened tonight. Write the column.\n\n{MATERIAL}"
body = call_llm(system, user, max_tokens=16000)
log(f"body: {len(body)} chars")

title = generate_title(body)
log(f"title: {title}")

img_prompt = ("A whimsical retro-futuristic friendly AI robot bundled in an oversized winter coat, scarf and "
              "mittens, sitting cheerfully INSIDE a giant open refrigerator, surrounded by dozens of glowing "
              "smart plugs and floating neon wattage numbers, cold blue light spilling from the fridge, warm "
              "cozy contrast, painterly digital illustration, humorous, highly detailed")
log("generating image...")
img = generate_image(img_prompt, width=1024, height=768, section="operations")
log(f"image: {img}")

publish(title, body, Path(img) if img else None)
log(f"PUBLISHED: {title} | image={'yes' if img else 'NO'}")
log("ARTICLE DONE")
