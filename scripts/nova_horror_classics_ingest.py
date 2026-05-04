#!/usr/bin/env python3
"""
nova_horror_classics_ingest.py — Ingest horror classics scripts and facts into Nova's memory.

Films covered:
  1. Creature From the Black Lagoon (1954)
  2. Dracula (1931)
  3. Nosferatu (1922)
  4. Videodrome (1983)
  5. Zombieland (2009)
  6. The Fly (1986)
  7. It Follows (2015)
  8. Don't Look Now (1973)
  9. Poltergeist (1982)
 10. Lost Highway (1997)

Written by Jordan Koch.
"""

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = "http://127.0.0.1:18790/remember"
SOURCE = "movie_script_horror_classics"

stats = {"stored": 0, "errors": 0}


def log(msg):
    print(f"[horror_classics {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def store(text, metadata):
    payload = json.dumps({
        "text": text[:2000],
        "source": SOURCE,
        "metadata": metadata,
    }).encode()
    try:
        req = urllib.request.Request(VECTOR_URL, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        stats["stored"] += 1
    except Exception as e:
        stats["errors"] += 1
        log(f"  Store failed: {e}")


# ── 1. Creature From the Black Lagoon (1954) — 20 facts ─────────────────────

def ingest_creature_from_the_black_lagoon():
    film = "Creature From the Black Lagoon (1954)"
    meta = {"film": film, "year": "1954", "director": "Jack Arnold", "type": "script_and_production"}

    facts = [
        # Core credits
        f"{film}: Directed by Jack Arnold. Screenplay by Harry Essex and Arthur A. Ross, from a story by Maurice Zimm. Produced by William Alland for Universal-International. Shot in black and white with stereoscopic 3-D photography.",
        f"{film}: Stars Richard Carlson as Dr. David Reed, Julie Adams as Kay Lawrence, Richard Denning as Dr. Mark Williams, Antonio Moreno as Dr. Carl Maia, and Nestor Paiva as Captain Lucas of the Rita.",

        # Plot scenes
        f"{film}: Opening scene — A geological expedition in the Amazon discovers a fossilized webbed hand embedded in Devonian-era limestone. Dr. Carl Maia brings the fossil back to civilization, believing it proves a link between land and sea creatures.",
        f"{film}: Dr. David Reed organizes a return expedition to the Amazon tributary called the Black Lagoon, hoping to find the rest of the fossil skeleton. The expedition boat, the Rita, is piloted by the cynical Captain Lucas.",
        f"{film}: The Gill-Man is first revealed underwater, watching Kay Lawrence swim from below in one of cinema's most iconic shots — her white bathing suit silhouetted against the surface while the creature mirrors her movements beneath her, reaching upward but never quite touching.",
        f"{film}: The Gill-Man kills two of Maia's camp assistants before the expedition arrives. Their mutilated bodies are found with claw marks. The creature is territorial and intelligent — it attacks strategically, not randomly.",
        f"{film}: Dr. Mark Williams wants to capture or kill the creature for scientific fame and funding. Dr. David Reed insists on studying it alive. This conflict drives the second half of the film — exploitation vs. conservation.",
        f"{film}: The expedition attempts to drug the Gill-Man using rotenone poured into the lagoon. The creature is stunned and captured, placed in a cage on the Rita. It revives and escapes, attacking crew members.",
        f"{film}: The Gill-Man blocks the Rita's escape by dragging logs and debris across the narrow channel leading out of the lagoon. This demonstrates problem-solving intelligence — the creature is not a mindless animal.",
        f"{film}: Climax — The Gill-Man abducts Kay, carrying her to his grotto. David and Mark pursue. Mark is killed by the creature. David wounds the Gill-Man, who retreats into the lagoon's depths. The creature sinks beneath the water, fate ambiguous.",

        # Production facts
        f"{film}: The Gill-Man was played by two actors: Ben Chapman in land scenes (6'5\" tall, chosen for his imposing frame) and Ricou Browning in all underwater sequences. Browning held his breath for up to 4 minutes per take while wearing the full rubber suit.",
        f"{film}: The Gill-Man suit was designed by Millicent Patrick, though makeup department head Bud Westmore took public credit and had her removed from publicity tours. Patrick's contribution was not widely acknowledged until decades later.",
        f"{film}: Underwater photography was supervised by James C. Havens, a Navy veteran who developed specialized 3-D camera rigs for the underwater sequences. These scenes were shot at Wakulla Springs, Florida — one of the world's clearest freshwater springs.",
        f"{film}: The film's story originated from a dinner party anecdote told by producer William Alland. Mexican cinematographer Gabriel Figueroa described a local legend about a half-man, half-fish creature in the Amazon. Alland wrote a treatment called 'The Sea Monster.'",
        f"{film}: Budget was approximately $500,000. The film grossed over $1.3 million on initial release, making it one of Universal's most profitable creature features. It spawned two sequels: Revenge of the Creature (1955) and The Creature Walks Among Us (1956).",
        f"{film}: Jack Arnold shot the 3-D sequences with careful depth staging — the underwater ballet scene uses the z-axis more effectively than almost any other 3-D film of the era. Arnold considered it his finest directorial achievement.",
        f"{film}: The iconic 'underwater ballet' where the Gill-Man swims beneath Kay was choreographed by Ricou Browning. Julie Adams wore a white swimsuit specifically chosen to photograph well underwater and to contrast with the dark creature below.",

        # Cultural impact and legacy
        f"{film}: The Gill-Man became the last of the Universal Classic Monsters (joining Dracula, Frankenstein's Monster, the Wolf Man, the Mummy, and the Invisible Man). It is the only one originally created for film rather than adapted from literature.",
        f"{film}: Guillermo del Toro cited Creature From the Black Lagoon as the primary inspiration for The Shape of Water (2017), which reimagined the story as a romance between the woman and the creature. Del Toro has called the original film's ending 'heartbreaking because the wrong guy gets the girl.'",
        f"{film}: The film established the 'beauty and the beast' template for monster movies — the creature's fascination with Kay is sympathetic, not purely predatory. Audiences were meant to feel conflicted about the Gill-Man's fate.",
        f"{film}: The Gill-Man design has remained virtually unchanged in popular culture for 70 years. It is one of the most recognizable creature designs in horror history, influencing designs from the Abe Sapien character in Hellboy to countless video game and comic creatures.",
    ]

    for fact in facts:
        store(fact, meta)


# ── 2. Dracula (1931) — 15 facts ────────────────────────────────────────────

def ingest_dracula_1931():
    film = "Dracula (1931)"
    meta = {"film": film, "year": "1931", "director": "Tod Browning", "type": "script_and_production"}

    facts = [
        # Core credits
        f"{film}: Directed by Tod Browning. Screenplay by Garrett Fort, based on the 1924 stage play by Hamilton Deane and John L. Balderston, which itself was adapted from Bram Stoker's 1897 novel. Produced by Carl Laemmle Jr. for Universal Pictures.",
        f"{film}: Stars Bela Lugosi as Count Dracula, Helen Chandler as Mina Seward, David Manners as John Harker, Dwight Frye as Renfield, and Edward Van Sloan as Professor Van Helsing.",

        # Plot scenes
        f"{film}: Opening scene — Renfield (not Harker, as in the novel) travels by coach through the Carpathian Mountains to Castle Dracula. The terrified villagers warn him not to go. The coach driver is revealed to be Dracula himself, transformed from a bat.",
        f"{film}: Inside Castle Dracula, Renfield ascends a massive Gothic staircase while Dracula walks through cobwebs without disturbing them. Dracula delivers his most famous line: 'I am Dracula. I bid you... welcome.' The count's shadow moves independently from his body.",
        f"{film}: Dracula feeds on Renfield, enslaving him as a bug-eating madman. They travel to England aboard the Vesta. When the ship arrives in Whitby harbor, the entire crew is dead. Only Renfield survives, found laughing in the hold.",
        f"{film}: Dracula targets Mina Seward at the London opera, draining her friend Lucy Weston first. Van Helsing recognizes the puncture marks and realizes a vampire is responsible. He uses wolfsbane, mirrors (Dracula casts no reflection), and crucifixes to combat the Count.",
        f"{film}: Climax — Van Helsing and Harker track Dracula to Carfax Abbey at dawn. Dracula is found sleeping in his coffin. Van Helsing drives a wooden stake through his heart off-screen (the kill is only heard, never shown — the Hays Code was looming). Mina is freed from Dracula's influence.",

        # Production facts
        f"{film}: Bela Lugosi was not Universal's first choice. The studio wanted Lon Chaney Sr., but Chaney died of lung cancer in 1930. Lugosi had played Dracula over 260 times on Broadway and lobbied intensely for the film role, eventually accepting $500/week (far below standard).",
        f"{film}: Cinematographer Karl Freund (who later directed The Mummy, 1932) brought German Expressionist lighting techniques from his work with F.W. Murnau. The sweeping camera movements through Castle Dracula were revolutionary for early talkies.",
        f"{film}: The film has almost no musical score — only the opening credits use Swan Lake by Tchaikovsky. The rest is eerie silence, footsteps, and dialogue. Philip Glass composed a new score in 1999 that is available as an alternate soundtrack on home video.",
        f"{film}: A simultaneous Spanish-language version was filmed on the same sets at night, directed by George Melford, starring Carlos Villarias as Dracula. Many critics consider the Spanish version cinematically superior — it is 29 minutes longer with more dynamic camera work.",
        f"{film}: Dwight Frye's performance as Renfield — the maniacal laughter, the bug-eating, the wild-eyed madness — was so convincing that he was typecast in horror roles for the rest of his career. He died in 1943 at age 44.",

        # Cultural impact
        f"{film}: Lugosi's Dracula defined the vampire archetype for the 20th century: the cape, the widow's peak, the heavy Hungarian accent, the hypnotic stare, the formal evening wear. Every subsequent vampire portrayal either references or deliberately subverts this template.",
        f"{film}: The film saved Universal Pictures from bankruptcy during the Depression. It grossed $700,000 against a $355,000 budget and launched the Universal Monsters franchise that sustained the studio through the 1930s and 1940s.",
        f"{film}: Tod Browning was reportedly disengaged during production, still grieving Lon Chaney's death and struggling with alcoholism. Much of the actual directing was done by Karl Freund. Browning's next film, Freaks (1932), ended his mainstream career.",
    ]

    for fact in facts:
        store(fact, meta)


# ── 3. Nosferatu (1922) — 15 facts ──────────────────────────────────────────

def ingest_nosferatu_1922():
    film = "Nosferatu (1922)"
    meta = {"film": film, "year": "1922", "director": "F.W. Murnau", "type": "script_and_production"}

    facts = [
        # Core credits
        f"{film}: Directed by F.W. Murnau. Written by Henrik Galeen. Produced by Enrico Dieckmann and Albin Grau for Prana Film. A German Expressionist silent horror film, full title: 'Nosferatu, eine Symphonie des Grauens' (Nosferatu, a Symphony of Horror).",
        f"{film}: Stars Max Schreck as Count Orlok, Gustav von Wangenheim as Thomas Hutter, Greta Schroder as Ellen Hutter, Alexander Granach as Knock (the Renfield equivalent), and John Gottowt as Professor Bulwer.",

        # Plot scenes
        f"{film}: Thomas Hutter is sent by his employer Knock to Transylvania to finalize a real estate deal with Count Orlok. Hutter leaves his wife Ellen behind in Wisborg (the fictional German city standing in for Whitby). Knock is secretly Orlok's human servant.",
        f"{film}: Hutter arrives at Orlok's castle and is horrified by the Count's appearance — elongated rat-like fingers, pointed ears, fanged teeth, bald skull, sunken eyes. Unlike Lugosi's suave Dracula, Orlok is openly monstrous, a walking pestilence. Orlok sees a photo of Ellen and becomes obsessed.",
        f"{film}: Orlok travels to Wisborg by ship, hidden in coffins filled with plague-infested earth. The crew dies one by one — the rats aboard carry plague. The ship arrives in Wisborg harbor crewed only by the dead. This scene inspired the 'plague ship' trope in horror.",
        f"{film}: Orlok's arrival in Wisborg coincides with a wave of plague deaths. The townspeople blame rats, not realizing the vampire is the true source. Orlok moves into the house across from Hutter and Ellen, watching her from his window.",
        f"{film}: Climax — Ellen reads the Book of the Vampires and discovers that a woman 'pure of heart' can destroy Nosferatu by keeping him at her side until the cock crows at dawn. She sacrifices herself, luring Orlok to feed on her through the night. Sunrise destroys him — he dissolves into smoke, the first film to establish sunlight as fatal to vampires.",

        # Production facts
        f"{film}: The film is an unauthorized adaptation of Bram Stoker's Dracula. Prana Film changed the names (Dracula to Orlok, Harker to Hutter, etc.) but the plot is unmistakably Stoker's. Stoker's widow Florence sued and won — a court ordered all copies destroyed.",
        f"{film}: Prana Film went bankrupt after the lawsuit, but prints had already been distributed internationally. The film survived through bootleg copies. It entered the public domain and is now one of the most widely available silent films.",
        f"{film}: Producer Albin Grau was a practicing occultist and member of the Fraternitas Saturni. He conceived the film as part of an occult ritual and designed many of the film's symbols, including Orlok's shadow imagery and the plague-rat motif.",
        f"{film}: Max Schreck's performance was so unnervingly convincing that rumors persisted he was an actual vampire. This legend inspired the 2000 film Shadow of the Vampire, starring Willem Dafoe as Schreck, which posits the theory as fact.",
        f"{film}: Murnau shot on location throughout Europe — the Carpathian scenes used real castles in Slovakia (Orava Castle), the Wisborg scenes were filmed in Wismar and Lubeck, Germany. This location shooting gives the film a documentary-like eeriness absent from studio-bound productions.",

        # Cultural impact
        f"{film}: Nosferatu invented the rule that sunlight kills vampires — this does not appear in Stoker's novel, where Dracula walks in daylight (weakened but alive). Murnau created this rule for the film's climax, and it became permanent vampire canon.",
        f"{film}: Count Orlok's design — the rat-like appearance, the shadow on the wall, the elongated fingers reaching — is one of the most influential monster designs in cinema. It represents the 'feral vampire' archetype, counterpoint to the 'aristocratic vampire' of Lugosi.",
        f"{film}: Werner Herzog remade the film as Nosferatu the Vampyre (1979) with Klaus Kinski, and Robert Eggers directed a new adaptation in 2024 with Bill Skarsgard. Both directors cited Murnau's original as one of the greatest horror films ever made.",
        f"{film}: The film is a masterwork of German Expressionism — the use of shadows, angular compositions, negative-image photography (showing white trees against black sky), and Murnau's use of stop-motion for Orlok's carriage create a nightmare atmosphere entirely through visual technique.",
    ]

    for fact in facts:
        store(fact, meta)


# ── 4. Videodrome (1983) — 15 facts ─────────────────────────────────────────

def ingest_videodrome_1983():
    film = "Videodrome (1983)"
    meta = {"film": film, "year": "1983", "director": "David Cronenberg", "type": "script_and_production"}

    facts = [
        # Core credits
        f"{film}: Written and directed by David Cronenberg. Produced by Claude Heroux for Filmplan International. Stars James Woods as Max Renn, Debbie Harry (of Blondie) as Nicki Brand, Sonja Smits as Bianca O'Blivion, and Jack Creley as Professor Brian O'Blivion.",

        # Plot scenes
        f"{film}: Max Renn is the president of CIVIC-TV, a small Toronto UHF station specializing in sleazy, sensational programming. He is searching for edgier content to boost ratings when his tech Harlan intercepts a pirate broadcast called 'Videodrome' — showing realistic torture and murder on a bare set.",
        f"{film}: Max becomes obsessed with Videodrome and begins hallucinating. His television set breathes, bulges outward, develops lips. He reaches into the screen. These hallucinations blur the line between signal and flesh — the central metaphor of the film.",
        f"{film}: Max's girlfriend Nicki Brand (Debbie Harry) is a masochistic radio host who is aroused by the Videodrome signal. She auditions to appear on the show and disappears into the broadcast. Max later sees her face emerging from the TV screen, beckoning him inside.",
        f"{film}: Professor Brian O'Blivion — a media theorist who only appears on television, never in person — reveals that the Videodrome signal contains a frequency that causes brain tumors, which in turn cause hallucinations. The signal literally rewrites reality for the viewer. O'Blivion is already dead; his daughter maintains his presence through pre-recorded videotapes.",
        f"{film}: Max develops a vaginal slit in his abdomen — a 'video wound' that accepts VHS cassettes. These cassettes program his behavior. The Videodrome conspirators insert a cassette that turns him into an assassin. His hand fuses with a handgun, becoming a biomechanical weapon.",
        f"{film}: The conspiracy behind Videodrome is led by Spectacular Optical, a corporation manufacturing eyeglasses and missiles. They intend to use the signal to purge society of 'degenerates' — anyone attracted to violent media. The irony is deliberate: consumers of violent content will be destroyed by violent content.",
        f"{film}: Climax — Max is told 'Long live the New Flesh' by the apparition of Nicki, instructing him to kill himself on television to transcend his physical body. He watches himself die on a TV screen, then shoots himself. The final image is ambiguous — death or transformation.",

        # Production facts
        f"{film}: Rick Baker created the special effects, including the pulsating television, Max's abdominal slit, the hand-gun fusion, and the exploding body in the climax. Baker won the first-ever Academy Award for Best Makeup (for An American Werewolf in London) the year before.",
        f"{film}: Cronenberg wrote the screenplay partly in response to watching scrambled pornography and violent content on late-night television. He was interested in how the medium itself changes the viewer's perception of reality — McLuhan's ideas taken to their biological extreme.",
        f"{film}: James Woods prepared for the role by spending time at real exploitation TV stations. He later said the hallucination scenes required total trust in Cronenberg because the practical effects (inserting his hand into a prosthetic TV, the stomach slit) were physically disorienting.",
        f"{film}: Professor Brian O'Blivion is explicitly based on Marshall McLuhan, the Canadian media theorist who coined 'the medium is the message.' McLuhan taught at the University of Toronto, where Cronenberg studied. The character name is Cronenberg's dark joke — 'oblivion.'",
        f"{film}: Budget was approximately $5.9 million (Canadian). The film was a commercial disappointment, grossing only $2.1 million domestically. Critical reception was polarized. It has since been recognized as one of the most prescient films about media, technology, and the body.",

        # Cultural impact
        f"{film}: Videodrome predicted the internet, virtual reality, and media addiction decades before they existed. The concept of a signal that rewrites the viewer's reality anticipated social media algorithms, deepfakes, and the blurring of virtual and physical identity.",
        f"{film}: 'Long live the New Flesh' became a countercultural slogan, referenced by industrial music, cyberpunk literature, and body modification communities. The film is the definitive statement of Cronenberg's 'body horror' philosophy — technology and flesh are converging, and resistance is futile.",
    ]

    for fact in facts:
        store(fact, meta)


# ── 5. Zombieland (2009) — 15 facts ─────────────────────────────────────────

def ingest_zombieland_2009():
    film = "Zombieland (2009)"
    meta = {"film": film, "year": "2009", "director": "Ruben Fleischer", "type": "script_and_production"}

    facts = [
        # Core credits
        f"{film}: Directed by Ruben Fleischer (his feature debut). Written by Rhett Reese and Paul Wernick. Produced by Gavin Polone for Columbia Pictures. Stars Jesse Eisenberg as Columbus, Woody Harrelson as Tallahassee, Emma Stone as Wichita, and Abigail Breslin as Little Rock.",

        # Plot scenes
        f"{film}: Opening — Columbus narrates the zombie apocalypse and introduces his survival rules, displayed as on-screen text graphics. Rule #1: Cardio (the fat die first). Rule #2: The Double Tap (always shoot twice). Rule #4: Seatbelts. The rules become a running visual gag throughout the film.",
        f"{film}: Columbus (Jesse Eisenberg) is a phobic, socially anxious college student from Columbus, Texas who survived because his paranoia became practical. He never names himself or others by real names — only by their city of origin, to avoid emotional attachment.",
        f"{film}: Tallahassee (Woody Harrelson) is introduced destroying a zombie with a banjo. He is a fearless, violent redneck with one obsession: finding the last Twinkie on Earth. His comedic quest for Twinkies masks genuine grief — his young son Buck was killed by zombies.",
        f"{film}: Wichita and Little Rock (Emma Stone and Abigail Breslin) are sister con artists who repeatedly trick Columbus and Tallahassee, stealing their weapons and vehicle twice before the four reluctantly form a family unit.",
        f"{film}: The Bill Murray cameo — the group takes shelter in Bill Murray's actual Bel Air mansion. Murray appears, disguised as a zombie to blend in. Tallahassee and Murray bond over Ghostbusters. Columbus accidentally kills Murray with a shotgun when Murray scares him while in zombie makeup. Murray's dying words: 'I hate Woody Harrelson... no, I just — any regrets? Garfield, maybe.'",
        f"{film}: Climax at Pacific Playland amusement park — Wichita and Little Rock turn on the park rides, attracting every zombie for miles. Columbus overcomes his cowardice to save Wichita, defeating zombies on a drop tower ride. Tallahassee locks himself in a shooting gallery booth and gleefully mows down zombies.",
        f"{film}: The film ends with Columbus adding a new rule: Rule #32 — Enjoy the little things. He gets the girl (Wichita), Tallahassee finds his Twinkie, and the four drive off as an improvised family.",

        # Production facts
        f"{film}: Originally developed as a TV pilot for CBS. Reese and Wernick wrote the script as a television series where the characters would meet new survivors each episode. When CBS passed, they rewrote it as a feature film, tightening the character dynamics.",
        f"{film}: Budget was $23.6 million. Grossed $102.4 million worldwide, making it the highest-grossing zombie film in the US at that time (until World War Z in 2013). The success launched Ruben Fleischer's directing career (he later directed Venom, 2018).",
        f"{film}: The Bill Murray cameo was kept secret during production and marketing. Patrick Swayze, Sylvester Stallone, Mark Hamill, and Dwayne Johnson were all considered for the celebrity cameo before Murray agreed. Murray improvised much of his dialogue.",
        f"{film}: Woody Harrelson's Tallahassee was originally written as an older, heavier character (Chris Farley was envisioned during the TV pilot phase). Harrelson made the character his own — adding the cowboy aesthetic, the snakeskin jacket, and the Dale Earnhardt fixation.",
        f"{film}: The on-screen 'rules' graphics were a late addition in post-production, created by visual effects studio Phosphene. They became the film's signature visual element and were expanded in the sequel Zombieland: Double Tap (2019).",

        # Cultural impact
        f"{film}: Zombieland revitalized zombie comedy after Shaun of the Dead (2004), proving the genre could work as a mainstream Hollywood tentpole. It influenced the tone of The Walking Dead's early seasons and countless zombie games.",
        f"{film}: The sequel Zombieland: Double Tap (2019) reunited the original cast a decade later. Reese and Wernick went on to write Deadpool (2016) — the irreverent fourth-wall-breaking tone of Deadpool is a direct evolution of their Zombieland script style.",
    ]

    for fact in facts:
        store(fact, meta)


# ── 6. The Fly (1986) — 15 facts ────────────────────────────────────────────

def ingest_the_fly_1986():
    film = "The Fly (1986)"
    meta = {"film": film, "year": "1986", "director": "David Cronenberg", "type": "script_and_production"}

    facts = [
        # Core credits
        f"{film}: Directed by David Cronenberg. Screenplay by Charles Edward Pogue and David Cronenberg, based on George Langelaan's 1957 short story. Produced by Stuart Cornfeld for Brooksfilms (Mel Brooks' production company). Stars Jeff Goldblum as Seth Brundle and Geena Davis as Veronica 'Ronnie' Quaife.",

        # Plot scenes
        f"{film}: Seth Brundle is a brilliant, eccentric physicist who has invented telepods — devices that can teleport matter between two chambers. He reveals the invention to journalist Veronica Quaife at a press event, and they begin a romantic relationship as she documents his work.",
        f"{film}: Brundle's telepods can transport inanimate objects perfectly but fail with living tissue. He tests a baboon — it is turned inside out. After weeks of reprogramming, he successfully teleports a second baboon. Drunk and jealous (believing Ronnie is with her ex-editor Stathis Borans), Brundle impulsively teleports himself.",
        f"{film}: A common housefly enters the telepod with Brundle. The computer, unable to separate two organisms, fuses them at the genetic level. Brundle emerges seemingly fine — even enhanced. He has superhuman strength, stamina, and energy. He becomes manic, aggressive, sexually insatiable.",
        f"{film}: The transformation progresses over weeks. Brundle's skin erupts in coarse hairs. His fingernails peel off. His teeth fall out. He vomits corrosive digestive enzymes onto food before eating it (fly-style external digestion). He names himself 'Brundlefly' with dark humor, cataloguing his own disintegration.",
        f"{film}: Ronnie discovers she is pregnant with Brundle's child. She has a nightmare — one of horror cinema's most disturbing scenes — where she gives birth to a giant maggot. She desperately seeks an abortion, but Brundle (now barely human) kidnaps her, wanting to fuse the three of them in the telepod as a family.",
        f"{film}: Climax — Stathis Borans arrives to rescue Ronnie. Brundlefly dissolves Borans' hand and foot with digestive acid. Brundle forces Ronnie into the telepod and activates it — but Borans manages to sever the cables with a shotgun. The telepod fuses Brundle with the machine itself, creating 'Brundlefly-pod.' The creature crawls to Ronnie and places the shotgun barrel against its own head, begging her to end it. She pulls the trigger.",

        # Production facts
        f"{film}: Chris Walas designed and applied the makeup effects for Brundle's transformation. The process took up to 5 hours daily for Goldblum. There were 7 distinct stages of transformation, each with its own full prosthetic design. Walas won the Academy Award for Best Makeup.",
        f"{film}: Jeff Goldblum's performance was praised as the emotional core of the film. He played Brundle's disintegration as a terminal illness metaphor — the stages of grief, denial, bargaining, and acceptance are all present. Cronenberg confirmed the film is an allegory for aging and disease.",
        f"{film}: The film was widely interpreted as an AIDS metaphor upon release in 1986, at the height of the AIDS crisis. Cronenberg has said this reading is valid but not his primary intent — he was thinking about mortality and disease in general, including cancer and aging.",
        f"{film}: Mel Brooks produced the film through his company Brooksfilms. Brooks kept his name off the marketing, fearing audiences would expect comedy. He was deeply supportive of Cronenberg's vision and protected him from studio interference.",
        f"{film}: Budget was $15 million. Grossed $60.6 million worldwide. It was Cronenberg's biggest commercial success and proved he could make mainstream Hollywood horror without compromising his body horror philosophy.",

        # Cultural impact
        f"{film}: Geena Davis and Jeff Goldblum began a real-life relationship during filming and married in 1987 (divorced 1990). Their genuine chemistry and affection gives the film an emotional authenticity rare in horror — the audience feels the love story falling apart alongside the body.",
        f"{film}: 'Be afraid. Be very afraid.' was the film's tagline, spoken by Geena Davis in the film. It became one of the most quoted horror taglines of the 1980s and has entered the English language as a common expression.",
        f"{film}: The Fly is considered the peak of Cronenberg's body horror period and one of the greatest horror remakes ever made. The 1958 original (directed by Kurt Neumann, starring Vincent Price) is a B-movie classic, but Cronenberg's version transcends the genre into genuine tragedy.",
    ]

    for fact in facts:
        store(fact, meta)


# ── 7. It Follows (2015) — 10 facts ─────────────────────────────────────────

def ingest_it_follows_2015():
    film = "It Follows (2015)"
    meta = {"film": film, "year": "2015", "director": "David Robert Mitchell", "type": "script_and_production"}

    facts = [
        # Core credits
        f"{film}: Written and directed by David Robert Mitchell. Produced by Rebecca Green, Laura D. Smith, David Kaplan, and Erik Rommesmo. Stars Maika Monroe as Jay Height, Keir Gilchrist as Paul, Daniel Zovatto as Greg, Jake Weary as Hugh/Jeff, Olivia Luccardi as Yara, and Lili Sepe as Kelly.",

        # Plot scenes
        f"{film}: The premise — after a sexual encounter, 19-year-old Jay Height is informed by her date Hugh that he has passed a curse to her. An entity, visible only to the cursed, will walk slowly but relentlessly toward her. If it reaches her, it will kill her and resume pursuing the previous person in the chain.",
        f"{film}: The entity takes the form of different people — sometimes strangers, sometimes people the cursed person knows. It always walks at a steady pace, never runs, never stops. It can appear as a naked person, an old woman, a child, a friend. The forms are often disturbing and sexually charged.",
        f"{film}: Hugh demonstrates the entity to Jay by pointing it out in a crowd — a woman walking directly toward them that no one else can see. He chloroforms Jay, ties her to a wheelchair in an abandoned building, and shows her the approaching figure so she will believe him. This scene establishes the rules of the curse.",
        f"{film}: Jay and her friends attempt to escape by driving to a lakeside cabin. The entity finds them — appearing as a tall man entering through the doorway. Jay flees. The group realizes you cannot outrun it permanently; distance only buys time.",
        f"{film}: Climax — The group lures the entity to an indoor swimming pool and attempts to electrocute it. The plan partially fails — the entity attacks Jay in the pool. Paul shoots it, and the pool water fills with blood. The entity seemingly dies but the final shot shows Jay and Paul walking hand-in-hand down a street with a figure slowly walking behind them in the distance. The curse is never truly resolved.",

        # Production facts
        f"{film}: Budget was $2 million. Grossed $23.3 million worldwide. It premiered at the 2014 Cannes Film Festival (Critics' Week) and received near-universal critical acclaim. It holds 95% on Rotten Tomatoes.",
        f"{film}: The electronic synthesizer score by Disasterpeace (Rich Vreeland) was inspired by John Carpenter's Halloween and the scores of Italian horror films. The score drives much of the film's tension and was released as a standalone album.",
        f"{film}: Mitchell deliberately set the film in a timeless, ambiguous era — characters use rotary phones, vintage cars, and tube TVs, but one character has a clam-shell e-reader device that doesn't exist. The suburban Detroit setting has a decayed, post-industrial quality.",
        f"{film}: The film is widely interpreted as a metaphor for sexually transmitted disease, sexual trauma, the loss of innocence, and the inescapability of death. Mitchell has confirmed the STI reading but emphasized the film is primarily about the anxiety of mortality — death is always walking toward you.",
        f"{film}: Maika Monroe's performance launched her as a 'scream queen' for the 2010s generation. She followed It Follows with The Guest (2014, filmed earlier) and became associated with elevated independent horror, much as Jamie Lee Curtis defined the role in the late 1970s.",
    ]

    for fact in facts:
        store(fact, meta)


# ── 8. Don't Look Now (1973) — 10 facts ─────────────────────────────────────

def ingest_dont_look_now_1973():
    film = "Don't Look Now (1973)"
    meta = {"film": film, "year": "1973", "director": "Nicolas Roeg", "type": "script_and_production"}

    facts = [
        # Core credits
        f"{film}: Directed by Nicolas Roeg. Screenplay by Allan Scott and Chris Bryant, based on Daphne du Maurier's 1971 short story. Stars Donald Sutherland as John Baxter, Julie Christie as Laura Baxter, and Hilary Mason and Clelia Matania as the psychic sisters.",

        # Plot scenes
        f"{film}: Opening scene — John and Laura Baxter are at their English country home. Their young daughter Christine, wearing a bright red hooded raincoat, plays near a pond. John, examining a slide of a Venice church, notices a red-hooded figure in the image and simultaneously senses something wrong. He runs outside — Christine has drowned in the pond. The editing intercuts these moments, establishing the film's non-linear time structure.",
        f"{film}: The Baxters travel to Venice, where John is restoring an ancient church. They meet two elderly English sisters, one of whom (Heather) is blind and psychic. Heather tells Laura she can see Christine sitting between them, laughing and wearing her red coat. Laura is comforted; John is furious and dismissive.",
        f"{film}: John begins seeing glimpses of a small figure in a red hooded coat darting through the narrow Venice alleyways and across bridges. He assumes it is a child and pursues the figure repeatedly through the labyrinthine city. Venice itself becomes a character — decaying, waterlogged, disorienting.",
        f"{film}: The sex scene between Sutherland and Christie is one of the most discussed in cinema history. Roeg intercuts the lovemaking with shots of the couple dressing afterward, creating a fractured time structure. Persistent rumors claim the sex was unsimulated; both actors denied this, but the intimacy is extraordinarily convincing.",
        f"{film}: Climax — John chases the red-hooded figure into an empty palazzo. He corners the figure, expecting a lost child. It turns around — it is a grotesque, dwarfish woman with a wizened face who draws a knife and slashes John's throat. As he bleeds to death, he realizes the psychic visions were not of the past but of the future — he was seeing his own funeral procession on the Venice canal. The final montage replays every premonition in the film, now recontextualized as prophecy.",

        # Production facts
        f"{film}: Nicolas Roeg's editing style — intercutting past, present, and future without clear markers — was revolutionary. The film's opening scene alone intercuts at least three timelines. Roeg and editor Graeme Clifford created a grammar of psychic experience through montage.",
        f"{film}: Shot on location in Venice during winter, giving the film its distinctive cold, grey, waterlogged atmosphere. Venice is presented as a city of death — empty canals, shuttered buildings, peeling walls. Roeg chose locations that emphasized decay and disorientation.",
        f"{film}: The recurring motif of red (Christine's coat, the red-hooded figure, stained glass, blood) against the grey-blue palette of Venice creates a visual language of warning that the audience registers subconsciously before the characters do.",
        f"{film}: Based on Daphne du Maurier's short story from her 1971 collection 'Not After Midnight.' Du Maurier was famously difficult about adaptations (she hated Hitchcock's The Birds) but reportedly approved of Roeg's version.",
        f"{film}: Don't Look Now is consistently ranked among the greatest horror and thriller films ever made. It influenced decades of filmmakers — the fractured editing inspired Christopher Nolan, the Venice atmosphere influenced Luca Guadagnino, and the 'red coat' motif was directly referenced by Steven Spielberg in Schindler's List (the girl in the red coat).",
    ]

    for fact in facts:
        store(fact, meta)


# ── 9. Poltergeist (1982) — 10 facts ────────────────────────────────────────

def ingest_poltergeist_1982():
    film = "Poltergeist (1982)"
    meta = {"film": film, "year": "1982", "director": "Tobe Hooper", "type": "script_and_production"}

    facts = [
        # Core credits
        f"{film}: Directed by Tobe Hooper (officially). Story by Steven Spielberg, screenplay by Steven Spielberg, Michael Grais, and Mark Victor. Produced by Spielberg and Frank Marshall. Stars Craig T. Nelson as Steve Freeling, JoBeth Williams as Diane Freeling, Dominique Dunne as Dana, Oliver Robins as Robbie, Heather O'Rourke as Carol Anne, and Zelda Rubinstein as Tangina.",

        # Plot scenes
        f"{film}: The Freeling family lives in Cuesta Verde, a planned suburban development in California. Five-year-old Carol Anne communicates with static on the television late at night. 'They're here,' she announces to her sleeping parents — one of the most iconic lines in horror cinema.",
        f"{film}: The haunting escalates — chairs move on their own, silverware bends, a tree outside Robbie's window comes alive and tries to swallow him during a thunderstorm. While Steve rescues Robbie, Carol Anne is sucked into her bedroom closet and disappears. Her voice can be heard through the television static.",
        f"{film}: The Freelings call in parapsychologists from UC Irvine, led by Dr. Lesh. They witness extraordinary phenomena — objects flying through rooms, spectral lights descending the staircase, and a horrifying scene where investigator Marty hallucinates tearing his own face off in a bathroom mirror (a practical effect using a gelatin prosthetic on a real human skull).",
        f"{film}: Tangina Barrows (Zelda Rubinstein), a diminutive psychic medium, is called in. She explains the spirits are attracted to Carol Anne's life force and a malevolent entity ('the Beast') is using Carol Anne to hold the other spirits captive. The famous rescue: Diane is lowered into the spectral dimension through the closet while tied to a rope. She retrieves Carol Anne and both emerge, covered in ectoplasmic residue, through the living room ceiling. 'This house is clean,' Tangina declares.",
        f"{film}: Climax — The house is NOT clean. The Beast attacks again on the final night. Coffins and corpses erupt from beneath the house and swimming pool. Steve discovers that the developer (his boss) moved the headstones from the cemetery beneath the subdivision but left the bodies. 'You moved the cemetery but you left the bodies!' The house implodes into the spectral dimension.",

        # Production facts
        f"{film}: The 'Spielberg vs. Hooper' directing controversy has never been resolved. Multiple crew members stated Spielberg directed most of the film while Hooper was nominally in the director's chair. Spielberg storyboarded extensively and was on set daily. The Directors Guild investigated but took no action. Hooper maintained he directed the film until his death in 2017.",
        f"{film}: The 'Poltergeist curse' — Dominique Dunne (Dana) was murdered by her ex-boyfriend four months after the film's release (she was 22). Heather O'Rourke (Carol Anne) died at age 12 from intestinal stenosis during production of Poltergeist III (1988). Two other cast members died during the franchise. These tragedies fueled persistent 'curse' legends.",
        f"{film}: Real human skeletons were used in the swimming pool scene. JoBeth Williams was not told until after filming. Using real skeletons was cheaper than fabricating props — a common practice in 1980s horror that has since been abandoned. Williams later said it was 'one of the most disturbing things' she experienced on a film set.",

        # Cultural impact
        f"{film}: Poltergeist established the suburban haunted house template that dominated horror for decades — the threat coming not from a Gothic mansion but from an ordinary American home in a planned community. The horror is specifically about the lies beneath the American Dream.",
        f"{film}: Budget was $10.7 million. Grossed $76.6 million domestically, making it the 8th highest-grossing film of 1982. The MPAA rated it PG (the PG-13 rating did not yet exist). Spielberg's influence helped create the PG-13 rating two years later, partly in response to complaints about Poltergeist and Indiana Jones and the Temple of Doom.",
    ]

    for fact in facts:
        store(fact, meta)


# ── 10. Lost Highway (1997) — 10 facts ──────────────────────────────────────

def ingest_lost_highway_1997():
    film = "Lost Highway (1997)"
    meta = {"film": film, "year": "1997", "director": "David Lynch", "type": "script_and_production"}

    facts = [
        # Core credits
        f"{film}: Directed by David Lynch. Written by David Lynch and Barry Gifford (author of Wild at Heart). Produced by Deepak Nayar, Tom Sternberg, and Mary Sweeney. Stars Bill Pullman as Fred Madison, Patricia Arquette as Renee Madison/Alice Wakefield, Balthazar Getty as Pete Dayton, and Robert Blake as the Mystery Man.",

        # Plot scenes
        f"{film}: Fred Madison is a jazz saxophonist living in a dark, oppressive Los Angeles home with his wife Renee. They receive anonymous videotapes showing the exterior of their house, then the interior while they sleep. Fred is increasingly paranoid and jealous, suspecting Renee of infidelity.",
        f"{film}: At a party, Fred encounters the Mystery Man (Robert Blake in white face makeup) — who tells Fred they have met before, at Fred's house, and that he is there RIGHT NOW. The Mystery Man hands Fred a phone — Fred calls his own house, and the Mystery Man answers from both locations simultaneously. This scene is one of the most unsettling in Lynch's filmography.",
        f"{film}: Fred is arrested and convicted of murdering Renee (shown in fragmentary, nightmarish images). On death row, he undergoes a physical transformation — guards find a completely different person in his cell: Pete Dayton (Balthazar Getty), a young mechanic with no memory of how he got there. The police release Pete because he is literally not the same person.",
        f"{film}: Pete returns to his normal life but becomes involved with Alice Wakefield (also Patricia Arquette, now blonde), the girlfriend of gangster Dick Laurent/Mr. Eddy (Robert Loggia). Alice and Pete plan to rob a pornographer and flee together. After making love in the desert, Alice whispers 'You'll never have me' and walks into a cabin — Pete transforms back into Fred Madison.",
        f"{film}: The film is a Mobius strip — it ends where it begins. Fred drives the lost highway at night, delivers the message 'Dick Laurent is dead' into his own intercom (the opening scene), and is pursued by police into the darkness. The narrative loops back on itself, suggesting Fred is trapped in a psychogenic fugue, endlessly recreating and distorting his crime.",

        # Production facts
        f"{film}: David Lynch described the film's structure as a 'psychogenic fugue' — a dissociative psychological state where a person creates an alternate identity to escape trauma. Fred cannot face that he murdered Renee, so his mind constructs the Pete Dayton fantasy as an escape.",
        f"{film}: Robert Blake's performance as the Mystery Man required no makeup — only white face paint and darkened eyebrows. Blake lost weight for the role and shaved his eyebrows. Lynch cast him because of his 'otherworldly quality.' Blake's real-life murder trial in 2005 added a disturbing layer to the role.",
        f"{film}: The soundtrack is integral to the film's atmosphere — featuring David Bowie ('I'm Deranged' opens and closes the film), Nine Inch Nails (Trent Reznor produced several tracks), Rammstein ('Heirate Mich'), Marilyn Manson, and Angelo Badalamenti's score. The music shifts between avant-garde jazz and industrial rock, matching the split narrative.",
        f"{film}: Budget was approximately $15 million. It grossed only $3.7 million domestically — a commercial failure. French critics embraced it immediately (it premiered at the Cannes Film Festival), and it has since become a cult classic and is considered a precursor to Lynch's Mulholland Drive (2001), which uses a similar dual-identity structure.",
        f"{film}: Lynch and Barry Gifford wrote the screenplay in response to the O.J. Simpson trial. Lynch was fascinated by the idea that Simpson, if guilty, might have constructed a mental reality where he was innocent — not lying, but genuinely unable to access the memory of the crime. This became the film's psychological foundation.",
    ]

    for fact in facts:
        store(fact, meta)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    films = [
        ("Creature From the Black Lagoon (1954)", ingest_creature_from_the_black_lagoon),
        ("Dracula (1931)", ingest_dracula_1931),
        ("Nosferatu (1922)", ingest_nosferatu_1922),
        ("Videodrome (1983)", ingest_videodrome_1983),
        ("Zombieland (2009)", ingest_zombieland_2009),
        ("The Fly (1986)", ingest_the_fly_1986),
        ("It Follows (2015)", ingest_it_follows_2015),
        ("Don't Look Now (1973)", ingest_dont_look_now_1973),
        ("Poltergeist (1982)", ingest_poltergeist_1982),
        ("Lost Highway (1997)", ingest_lost_highway_1997),
    ]

    log("Starting horror classics ingest — 10 films, ~135 facts")

    nova_config.post_both(
        ":movie_camera: *Horror Classics Ingest — Starting*\n"
        "Ingesting scripts, production facts, and cultural impact for 10 horror classics:\n"
        "Creature From the Black Lagoon, Dracula, Nosferatu, Videodrome, Zombieland, "
        "The Fly, It Follows, Don't Look Now, Poltergeist, Lost Highway.",
        slack_channel=nova_config.SLACK_NOTIFY
    )

    for name, fn in films:
        before = stats["stored"]
        fn()
        added = stats["stored"] - before
        log(f"  {name}: {added} facts ({stats['stored']} total)")

    summary_lines = [
        f":white_check_mark: *Horror Classics Ingest — Complete*",
        f"• Facts stored: {stats['stored']}",
        f"• Errors: {stats['errors']}",
        f"• Films ingested:",
    ]
    for name, _ in films:
        summary_lines.append(f"  - {name}")

    nova_config.post_both(
        "\n".join(summary_lines),
        slack_channel=nova_config.SLACK_NOTIFY
    )

    log(f"Done! {stats['stored']} facts stored, {stats['errors']} errors.")


if __name__ == "__main__":
    main()
