#!/usr/bin/env python3
"""
nova_reanimator_ingest.py — Ingest Re-Animator trilogy scripts and facts into Nova's memory.

Re-Animator (1985), Bride of Re-Animator (1990), Beyond Re-Animator (2003)
Based on H.P. Lovecraft's "Herbert West–Reanimator" (1922).

Written by Jordan Koch.
"""

import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = "http://127.0.0.1:18790/remember"

stats = {"stored": 0, "errors": 0}


def log(msg):
    print(f"[reanimator {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def store(text, metadata):
    payload = json.dumps({
        "text": text[:2000],
        "source": "movie_script_reanimator",
        "metadata": metadata,
    }).encode()
    try:
        req = urllib.request.Request(VECTOR_URL, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        stats["stored"] += 1
    except Exception as e:
        stats["errors"] += 1
        log(f"  Store failed: {e}")


def ingest_reanimator_1985():
    """Re-Animator (1985) - Directed by Stuart Gordon, screenplay by Dennis Paoli, William Norris, Stuart Gordon."""
    film = "Re-Animator (1985)"
    meta = {"film": film, "year": "1985", "director": "Stuart Gordon", "based_on": "H.P. Lovecraft - Herbert West-Reanimator"}

    facts = [
        # Plot and scenes
        f"{film}: Opening scene in Zurich — Herbert West's professor Dr. Hans Gruber dies from an overdose of West's reagent. His eyes explode. West insists 'I gave him life.' Campus security arrives to find the gruesome scene.",
        f"{film}: Herbert West arrives at Miskatonic University Medical School in Arkham, Massachusetts, seeking to continue his reanimation research. He rents a room from fellow student Dan Cain.",
        f"{film}: West's reagent is a glowing green phosphorescent liquid stored in a syringe. He keeps it refrigerated. The exact formula is never revealed but involves a radical approach to defeating brain death.",
        f"{film}: Dan Cain discovers West has reanimated his dead cat Rufus in the basement. The cat attacks violently — the reanimated dead are aggressive and mindless. West kills it with a shovel.",
        f"{film}: Dean Halsey catches West and Dan in the morgue attempting reanimation. The corpse awakens and kills Halsey. West reanimates Halsey, who becomes a lobotomized zombie under the control of Dr. Hill.",
        f"{film}: Dr. Carl Hill is the film's secondary antagonist — a plagiarist who stole Hans Gruber's research. He attempts to steal West's reagent. West decapitates him with a shovel and reanimates both the head and body separately.",
        f"{film}: The decapitated Dr. Hill develops telekinetic control over other reanimated corpses. His severed head commands his headless body. He kidnaps Megan Halsey (Dan's girlfriend) and attempts to assault her.",
        f"{film}: The climax takes place in the Miskatonic Medical School morgue. Hill's army of reanimated corpses attacks. Dan saves Megan but she is strangled by her own reanimated father. Dan injects her with the reagent as the film ends.",
        f"{film}: Herbert West's defining character trait is his absolute scientific detachment from the horror of his work. He views death as a technical problem to be solved, not a moral boundary.",
        f"{film}: The film's practical effects were created by John Naulin, Anthony Doublin, and John Buechler. The gore is deliberately excessive and darkly comedic — Stuart Gordon called it 'splatstick.'",

        # Production facts
        f"{film}: Budget was $900,000. Shot in 18 days at Empire Studios in Rome, Italy (interiors) and locations in Los Angeles. Grossed $2 million theatrically.",
        f"{film}: Jeffrey Combs was cast as Herbert West after Stuart Gordon saw him perform in a Chicago stage production. Combs based the character on his college roommate — focused, brilliant, socially oblivious.",
        f"{film}: Originally developed as a stage play called 'Re-Animator: The Musical' for Chicago's Organic Theater Company. Stuart Gordon adapted it for film when producer Brian Yuzna approached him.",
        f"{film}: The MPAA gave the film an X rating. It was released unrated in theaters. The home video version has approximately 2 minutes of cuts for an R rating.",
        f"{film}: Richard Band composed the score, deliberately echoing Bernard Herrmann's Psycho theme. Band has said it was an intentional homage, not plagiarism.",
        f"{film}: Barbara Crampton (Megan Halsey) performed the controversial 'head' scene without a body double. She later said it was the most professionally challenging scene of her career.",
        f"{film}: The film is loosely based on H.P. Lovecraft's 1922 serial 'Herbert West–Reanimator,' published in six installments in Home Brew magazine. Lovecraft himself considered it hackwork written for money.",
        f"{film}: Stuart Gordon and Brian Yuzna went on to produce multiple Lovecraft adaptations: From Beyond (1986), Castle Freak (1995), Dagon (2001). Re-Animator launched the 'Lovecraft on screen' movement.",
        f"{film}: The glowing green reagent was created using liquid from glow sticks mixed with water. Different concentrations were used depending on the scene's lighting requirements.",
        f"{film}: David Gale (Dr. Hill) performed most of his post-decapitation scenes by sticking his head through a hole in a table. The prosthetic head was a latex cast used for wide shots.",
    ]

    for fact in facts:
        store(fact, {**meta, "type": "script_and_production"})


def ingest_bride_of_reanimator_1990():
    """Bride of Re-Animator (1990) - Directed by Brian Yuzna."""
    film = "Bride of Re-Animator (1990)"
    meta = {"film": film, "year": "1990", "director": "Brian Yuzna", "based_on": "H.P. Lovecraft - Herbert West-Reanimator"}

    facts = [
        f"{film}: Set 8 months after the Miskatonic Massacre. West and Dan have been working as medics in a civil war in Peru, harvesting fresh body parts from battlefields for reanimation experiments.",
        f"{film}: West's new goal is to create life from scratch — assembling a complete human being from parts. He calls it 'tissue matching' and considers it the next evolution of his work.",
        f"{film}: Dan is haunted by guilt over Megan's death. West exploits this by using Megan's heart as the core of their assembled 'bride' — manipulating Dan's grief to keep him working.",
        f"{film}: The bride is assembled from multiple donors: the heart of Megan Halsey, legs from a dancer, hands from a pianist, eyes from various sources. West sees her as proof that death is merely a design problem.",
        f"{film}: Lieutenant Leslie Chapham investigates the Miskatonic incident and suspects West and Dan. He is killed and reanimated — his severed fingers continue to crawl independently.",
        f"{film}: Dr. Hill's severed head returns, now with bat wings grafted to the sides of its head, allowing it to fly. He seeks revenge against West and attempts to control the bride.",
        f"{film}: The bride is brought to life in the climax. She is aware, confused, and horrified by her own existence. She rejects both Dan and West before tearing herself apart — choosing death over being their creation.",
        f"{film}: The basement of their house collapses into a crypt filled with all their failed experiments. The rejects come alive en masse — a army of mismatched body part creatures attack.",
        f"{film}: Screaming Mad George created the special effects, including the iconic finger-spider, the eyeball-spider, and various assembled creature designs. Each failed experiment is unique.",
        f"{film}: Brian Yuzna directed after Stuart Gordon declined. Yuzna pushed further into body horror territory, influenced by Cronenberg and the surrealist art of Hans Bellmer.",
        f"{film}: Budget was $2.5 million (nearly triple the original). Shot at Empire Studios in Rome. The increased budget allowed for more elaborate creature effects.",
        f"{film}: The film's subtitle references both James Whale's Bride of Frankenstein (1935) and the Lovecraft serial's emphasis on West's growing ambition across installments.",
        f"{film}: Jeffrey Combs and Bruce Abbott both returned as West and Dan. Combs has said the sequel allowed him to explore West's complete lack of ethical boundaries more deeply.",
        f"{film}: The bride is played by Kathleen Kinmont. Her creation scene parallels Elsa Lanchester's bride scene in Bride of Frankenstein — the rejection is an inversion of the original.",
        f"{film}: The film explores the theme of body autonomy — the bride's rejection is a statement about consent and creation. She was never asked to exist.",
    ]

    for fact in facts:
        store(fact, {**meta, "type": "script_and_production"})


def ingest_beyond_reanimator_2003():
    """Beyond Re-Animator (2003) - Directed by Brian Yuzna."""
    film = "Beyond Re-Animator (2003)"
    meta = {"film": film, "year": "2003", "director": "Brian Yuzna", "based_on": "H.P. Lovecraft - Herbert West-Reanimator"}

    facts = [
        f"{film}: Herbert West has been imprisoned for 13 years following the Miskatonic Massacre. He continues his research in secret using prison resources — rats, dead inmates, smuggled chemicals.",
        f"{film}: West discovers 'NPE' (Nano-Plasmic Energy) — the electrical life force that separates the living from the dead. He can now extract it from living beings and transfer it to reanimated corpses, solving the aggression problem.",
        f"{film}: Young doctor Howard Phillips (named after H.P. Lovecraft) arrives at the prison as the new medical officer. As a child, he witnessed a reanimated corpse kill his sister — West's work brought him here.",
        f"{film}: Howard becomes West's reluctant assistant, drawn in by scientific curiosity despite knowing the danger. He represents the audience surrogate — horrified but fascinated.",
        f"{film}: The warden is a corrupt, violent man who discovers West's experiments and tries to exploit them. He is eventually killed and reanimated without NPE — becoming a mindless, destructive zombie.",
        f"{film}: The climax involves a prison riot where multiple reanimated corpses break free. Chaos erupts as inmates, guards, and the undead clash. West uses the chaos to escape.",
        f"{film}: The NPE concept is visualized as a glowing energy extracted via a special syringe. When administered alongside the reagent, the reanimated retain their personality and intelligence.",
        f"{film}: The film's post-credits scene shows a rat dragging away a syringe of reagent — implying the experiments will continue even without West.",
        f"{film}: Shot entirely in Barcelona, Spain on a budget of $3 million. The prison location was a real decommissioned Spanish prison (La Modelo inspired the setting).",
        f"{film}: Jeffrey Combs returned for his third and (to date) final performance as Herbert West. He has said the character is the role he's most associated with and most proud of.",
        f"{film}: Brian Yuzna directed again but relocated production to Spain where he had established Filmax as a production base. The Spanish crew brought a different visual sensibility.",
        f"{film}: The film was released direct-to-video in the United States but received theatrical distribution in Spain and several other international markets.",
        f"{film}: The 'NPE' concept was Yuzna's attempt to evolve West's science — showing that even in prison, West's mind never stops working on the problem. 13 years of theoretical refinement.",
        f"{film}: Elsa Pataky (later known for the Fast & Furious franchise) had a supporting role. The film was early in her career before her international breakthrough.",
        f"{film}: The Re-Animator trilogy spans 18 years (1985-2003). West ages from an intense young graduate student to a hardened, patient scientist. Combs adjusted his performance for each era.",
    ]

    for fact in facts:
        store(fact, {**meta, "type": "script_and_production"})


def ingest_lovecraft_source():
    """H.P. Lovecraft's original Herbert West–Reanimator (1922)."""
    source = "Herbert West-Reanimator (H.P. Lovecraft, 1922)"
    meta = {"work": source, "author": "H.P. Lovecraft", "year": "1922", "type": "literary_source"}

    facts = [
        f"{source}: Published as a six-part serial in Home Brew magazine (February-July 1922). Lovecraft wrote it as a parody of/homage to Mary Shelley's Frankenstein, forced by the serial format.",
        f"{source}: Lovecraft considered it one of his worst works, calling it 'grewsome [sic] trash.' He wrote it only because Home Brew editor George Julian Houtain offered payment per installment.",
        f"{source}: The narrator (never named) is West's college roommate and reluctant assistant at Miskatonic University. He is complicit in increasingly horrific experiments but lacks West's conviction.",
        f"{source}: Each installment ends with a cliffhanger and begins with a recap — a structure Lovecraft loathed as formulaic. He was contractually obligated to maintain this serial format.",
        f"{source}: West's experiments progress through fresh corpses, embalmed bodies, and eventually body parts. Each attempt produces more horrific results. The dead remember and resent their reanimation.",
        f"{source}: The final installment has West's previous experiments return as a group to take revenge. They tear him apart and carry his pieces away. The narrator is the sole survivor.",
        f"{source}: The story introduces Miskatonic University and the city of Arkham — locations that became central to Lovecraft's later Cthulhu Mythos. This is their first appearance in his fiction.",
        f"{source}: Unlike the film adaptation, the literary West is described as small, blond, and spectacled — a mild-looking man whose ordinary appearance contrasts with his horrific work.",
        f"{source}: The story's racism (particularly in Part 3, which involves a Black boxing champion) reflects Lovecraft's well-documented prejudices. Modern reprints sometimes include contextual notes.",
        f"{source}: The 'reagent' in the original story is never described in detail beyond being an injectable solution. The glowing green color is entirely a film invention by Stuart Gordon.",
    ]

    for fact in facts:
        store(fact, {**meta})


def main():
    log("Starting Re-Animator trilogy ingest")

    nova_config.post_both(
        ":syringe: *Re-Animator Ingest — Starting*\nIngesting scripts and production facts for the Re-Animator trilogy (1985, 1990, 2003) + Lovecraft source material.",
        slack_channel=nova_config.SLACK_NOTIFY
    )

    ingest_reanimator_1985()
    log(f"  Re-Animator (1985): {stats['stored']} facts")

    ingest_bride_of_reanimator_1990()
    log(f"  Bride of Re-Animator (1990): {stats['stored']} facts total")

    ingest_beyond_reanimator_2003()
    log(f"  Beyond Re-Animator (2003): {stats['stored']} facts total")

    ingest_lovecraft_source()
    log(f"  Lovecraft source: {stats['stored']} facts total")

    nova_config.post_both(
        f":white_check_mark: *Re-Animator Ingest — Complete*\n• Facts stored: {stats['stored']}\n• Errors: {stats['errors']}\n• Films: Re-Animator (1985), Bride of Re-Animator (1990), Beyond Re-Animator (2003)\n• Source: H.P. Lovecraft 'Herbert West–Reanimator' (1922)",
        slack_channel=nova_config.SLACK_NOTIFY
    )
    log(f"Done! {stats['stored']} facts stored, {stats['errors']} errors.")


if __name__ == "__main__":
    main()
