#!/usr/bin/env python3
"""Batch 2: Horror/sci-fi film facts for remaining films."""
import json, sys, time, urllib.request
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
import nova_config

MEMORY_URL = "http://192.168.1.6:18790/remember"
count = 0
failed = 0
start_time = time.time()

def log(msg):
    print(f"[horror-b2 {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)

def remember(text):
    global count, failed
    payload = json.dumps({"text": text[:2000], "source": "local_knowledge", "metadata": {"type": "horror_films", "owner_favorite": True}}).encode()
    req = urllib.request.Request(MEMORY_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10):
            count += 1
            return True
    except:
        failed += 1
        return False

def ingest_film(name, facts):
    log(f"Ingesting: {name} ({len(facts)} facts)")
    for f in facts:
        remember(f)
    sample = facts[0][:100] if facts else ""
    slack_post(f":movie_camera: *{name}*\n  {len(facts)} facts ingested (total: {count:,})\n  _{sample}..._")
    time.sleep(1)

slack_post(":movie_camera: *Horror Films Batch 2 Started*\n  29 films × ~20 facts each\n  _Updates per film_")

# Generate concise but factual entries for each film
# Using 20 high-quality facts per film (more practical than 50 for this many films)

ingest_film("Halloween II (1981)", [
    "Halloween II (1981) picks up exactly where the first film ends — the same night, October 31, 1978. It was the first slasher sequel to continue the same night's events.",
    "Rick Rosenthal directed Halloween II, though John Carpenter reshot several scenes to add more gore after feeling the initial cut was too tame.",
    "The hospital setting (Haddonfield Memorial Hospital) gave the film a claustrophobic atmosphere different from the suburban streets of the original.",
    "Halloween II reveals that Laurie Strode is Michael Myers' sister — a plot twist Carpenter admitted he wrote while drunk and later regretted.",
    "Jamie Lee Curtis returned as Laurie Strode, spending most of the film sedated in a hospital bed. She earned significantly more than her $8,000 first-film salary.",
    "The body count in Halloween II is significantly higher than the original (10 vs 5), reflecting the escalation audiences expected from early-80s slashers.",
    "Dick Warlock played Michael Myers in Halloween II, replacing Nick Castle. Warlock's portrayal was more robotic and deliberate.",
    "The hot tub kill scene (nurse scalded to death) was one of the most graphic deaths in the franchise up to that point.",
    "Halloween II's ending — Loomis igniting the operating room, apparently killing both himself and Michael — was intended to end the franchise definitively.",
    "The film grossed $25.5 million against a $2.5 million budget, proving the franchise had strong commercial legs.",
    "Carpenter and Debra Hill wrote the screenplay but were less involved in daily production, leading to a tonal shift from the original's restraint.",
    "The score reuses Carpenter's original theme but adds new synthesizer compositions that feel more aggressive and urgent.",
    "Halloween II was the last film in the series to directly involve Carpenter and Hill until the 2018 reboot.",
    "The hospital's empty corridors and institutional lighting created an effectively creepy setting that many later horror films (including Halloween 2018) would reference.",
    "Donald Pleasence returned as Dr. Loomis with expanded screen time, delivering increasingly frantic warnings that nobody believes.",
    "The 'sister revelation' became the franchise's most important and controversial plot point, defining every sequel until the 2018 reboot retconned it.",
    "Lance Guest (Jimmy) was added as a potential love interest for Laurie, giving her someone to play off while incapacitated.",
    "The film was shot in Pasadena, California (same locations as the original) with the hospital scenes at a decommissioned school.",
    "Halloween II established that Michael Myers could survive seemingly fatal injuries — being shot six times, burned alive — setting up his supernatural nature in later sequels.",
    "The film's working title was simply 'Halloween II' but marketing emphasized it was 'the night HE came home... again.'",
])

ingest_film("Halloween 4: The Return of Michael Myers (1988)", [
    "Halloween 4 introduced Jamie Lloyd (Danielle Harris), Michael's 7-year-old niece and the daughter of Laurie Strode (said to have died in a car accident).",
    "Danielle Harris was 10 years old during filming and gave a performance critics praised as remarkable for a child actress in a horror film.",
    "The film brought back Donald Pleasence as Dr. Loomis, now scarred from the Halloween II explosion but still obsessed with stopping Michael.",
    "Michael Myers had been in a coma for 10 years and awakens when he overhears that he has a living relative — his niece Jamie.",
    "Halloween 4 was rushed into production in 11 months to capitalize on the franchise's value, with Moustapha Akkad producing.",
    "The film's ending — young Jamie stabbing her stepmother while wearing a clown costume, mirroring young Michael in 1963 — was a genuinely shocking twist.",
    "George P. Wilbur played Michael Myers in Halloween 4, giving the Shape a bulkier, more imposing physical presence.",
    "The film grossed $17.8 million on a $5 million budget, confirming audience appetite for Michael Myers after the Michael-free Halloween III.",
    "Dwight H. Little directed, bringing a more action-oriented style than the original's slow-burn suspense.",
    "The iconic scene of Michael impaling a man on a shotgun and lifting him off the ground demonstrated the character's superhuman strength.",
    "Halloween 4 introduced the vigilante mob subplot — Haddonfield residents forming armed posses to hunt Michael, accidentally killing an innocent man.",
    "The mask in Halloween 4 is widely considered one of the worst in the franchise — a blond-haired, wide-faced mask that looks nothing like the original.",
    "The 'rooftop chase' scene where Jamie escapes the school was filmed at a real school in Salt Lake City, Utah (the entire film was shot there).",
    "Rachel Carruthers (Ellie Cornell) was introduced as Jamie's protective stepsister and became one of the franchise's most popular 'final girl' characters.",
    "Halloween 4 re-established the franchise formula: Michael returns, kills teens, Loomis chases him, final girl survives — a template the series would follow for decades.",
    "The original script had a much darker ending where Jamie killed her entire family, but it was toned down to just the stepmother stabbing.",
    "The film's success greenlit an immediate sequel (Halloween 5) which was rushed into production for the following year.",
    "Halloween 4 is often ranked as the best sequel in the original continuity (after Halloween II) by franchise fans.",
    "The Haddonfield police force is depicted as tragically ineffective, with the sheriff's daughter among Michael's victims.",
    "Alan B. McElroy wrote the screenplay in 11 days during the 1988 Writers Guild strike, as he was not yet a guild member.",
])

ingest_film("Halloween 5: The Revenge of Michael Myers (1989)", [
    "Halloween 5 was rushed into production immediately after 4's success, with only a year between releases — resulting in a less polished product.",
    "The film introduced the mysterious 'Man in Black' who appears throughout, revealed much later (in Halloween 6) to be part of the Thorn cult controlling Michael.",
    "Danielle Harris returned as Jamie Lloyd, now mute and psychically linked to Michael. Her performance remained strong despite weaker material.",
    "Donald Pleasence became increasingly unhinged as Dr. Loomis in this entry, at one point beating Jamie for information about Michael's whereabouts.",
    "The film controversially killed off Rachel (Ellie Cornell) early, angering fans who connected with her character in Halloween 4.",
    "Dominique Othenin-Girard directed, bringing a more European sensibility but clashing with the American crew and producers.",
    "The 'laundry chute' scene where Jamie hides from Michael is considered one of the franchise's most effective suspense sequences.",
    "Halloween 5 underperformed at the box office ($11.6M vs Halloween 4's $17.8M), signaling franchise fatigue.",
    "The Man in Black's boots and silver-tipped cowboy accessories were designed to create mystery, but the subplot had no planned resolution during filming.",
    "Michael's mask improved from Halloween 4's version but still didn't match the original — a persistent complaint throughout the franchise.",
    "The ending — Man in Black attacking the police station and freeing Michael — was a cliffhanger that wouldn't be resolved for 6 years (until Halloween 6 in 1995).",
    "Don Shanks played Michael Myers, giving the character a slightly more agile, predatory movement style than previous portrayals.",
    "The 'gothic children' subplot (two bumbling comedy-relief cops) was widely hated by fans and is considered one of the franchise's worst creative decisions.",
    "Jamie's psychic connection to Michael (she has seizures when he kills) was inspired by similar telepathic bonds in Stephen King novels.",
    "The farmhouse sequence where Michael murders an entire household is one of the most violent extended sequences in the franchise.",
    "Producer Moustapha Akkad reportedly interfered significantly with the final cut, adding and removing scenes against the director's wishes.",
    "Halloween 5 is generally considered one of the weakest entries in the franchise, though the Jamie/Michael dynamic remains compelling.",
    "The film was shot primarily in Salt Lake City again, with the Myers house being a different building than in previous films.",
    "Wendy Kaplan plays Tina, Jamie's older friend whose death scene is one of the film's emotional high points.",
    "The film's poor reception led to a 6-year gap before the next sequel — the longest between entries until the 2018 reboot's 40-year gap from the original.",
])

ingest_film("Halloween H20: 20 Years Later (1998)", [
    "Halloween H20 brought Jamie Lee Curtis back as Laurie Strode, now living under the alias Keri Tate as headmistress of a private school in Northern California.",
    "The film ignores Halloween 4, 5, and 6 — treating only 1 and 2 as canon. This was the first major 'retcon reboot' in slasher franchise history.",
    "Kevin Williamson (Scream) provided the story treatment, giving the film a self-aware, post-Scream sensibility that modernized the franchise.",
    "Steve Miner directed, having previously directed Friday the 13th Parts 2 and 3 — making him one of few directors to helm entries in both rival franchises.",
    "The film's central theme is trauma — Laurie has become an alcoholic, overprotective mother who hasn't escaped the Halloween night 20 years ago.",
    "Josh Hartnett plays John, Laurie's teenage son, in his film debut. The casting brought young audiences to the franchise.",
    "LL Cool J provides comic relief as a security guard writing romance novels — a role that was controversial with franchise purists.",
    "H20 grossed $55 million on a $17 million budget, proving the franchise could still be commercially viable with the right creative approach.",
    "The climax — Laurie grabbing an axe and hunting Michael instead of running — was a deliberate inversion of the victim/killer dynamic.",
    "Curtis insisted that the ending show Laurie definitively killing Michael (beheading him) to give her character closure.",
    "The mask in H20 went through multiple versions during production, with CGI used in some shots to fix a mask the producers didn't like.",
    "Janet Leigh (Curtis's real mother and star of Psycho) has a cameo, with her character driving the same car from Psycho.",
    "The score mixes Carpenter's original theme with a new orchestral score by John Ottman, bridging classic and modern horror sensibilities.",
    "Michelle Williams and Jodi Lyn O'Keefe play students at the school — the cast reflected late-90s teen horror casting trends (Dawson's Creek era).",
    "H20's success directly led to Halloween: Resurrection (2002), which controversially undid the beheading by claiming Laurie killed a paramedic, not Michael.",
    "The film was released during the post-Scream horror boom, alongside I Know What You Did Last Summer, Urban Legend, and The Faculty.",
    "Chris Durand played Michael Myers, giving a more stalking, methodical performance in keeping with the original film's Shape.",
    "The private school setting (filmed at locations around the LA area) gave the film an upscale atmosphere unlike Haddonfield's middle-class suburbs.",
    "H20 was originally rated NC-17 and had to be cut to achieve an R rating — the MPAA objected to the intensity of the kills.",
    "The 20th anniversary marketing was brilliant — 'This summer, terror won't be taking a vacation' — and positioned it as an event horror film.",
])

ingest_film("Friday the 13th Part 2 (1981)", [
    "Friday the 13th Part 2 marks Jason Voorhees' first appearance as the killer — in the original, his mother Pamela was the murderer.",
    "Jason wears a burlap sack over his head in Part 2, not the iconic hockey mask (which doesn't appear until Part III).",
    "Steve Miner directed Part 2 (and Part III), taking over from Sean Cunningham who directed the original.",
    "The film opens with the murder of Alice (Adrienne King), the original's final girl — killed in her apartment by Jason in the first 10 minutes.",
    "Ginny Field (Amy Steel) is considered one of the franchise's best final girls — intelligent, resourceful, and a child psychology major who uses her knowledge against Jason.",
    "Ginny's strategy of donning Pamela Voorhees' sweater to confuse Jason (triggering his mother fixation) established the 'use his psychology against him' trope.",
    "Part 2 establishes Jason's wilderness shack containing his mother's decapitated head on a shrine — revealing his motivation is grief and rage over losing his mother.",
    "The film was budgeted at $1.25 million and grossed $21.7 million — proving the franchise could continue profitably without the original's creator fully involved.",
    "Jason in Part 2 is portrayed as a human (albeit large and deformed) living in the woods — not yet the supernatural undead killer of later entries.",
    "Warrington Gillette was credited as Jason but Steve Daskewisz performed most of the masked scenes. The role wasn't yet the showcase it would become.",
    "The MPAA forced significant cuts to the kill scenes, particularly a machete-to-the-face murder that was trimmed to just the aftermath.",
    "Part 2 takes place 5 years after the original, at a counselor training center near the now-closed Camp Crystal Lake.",
    "The 'wheelchair kill' (Mark rolling backwards down stairs with a machete in his face) is one of the franchise's most memorable deaths.",
    "Crazy Ralph (Walt Gorney), the 'doomsayer' character from the original, returns briefly before being killed — his 'you're all doomed!' warning going unheeded again.",
    "The film's ending is ambiguous — Ginny survives but Paul (her boyfriend) disappears, and the final shot implies Jason may have taken him.",
    "Part 2 established the franchise's formula: group of young counselors, isolated location, killed one by one, final girl survives.",
    "The legend of Jason — a boy who drowned at Camp Crystal Lake in 1957 — is told as a campfire story early in the film, establishing franchise mythology.",
    "Amy Steel has said she felt Friday the 13th Part 2 was treated as 'just a job' at the time but appreciates its cult following decades later.",
    "The burlap sack mask was designed to evoke the 'Unknown' killer archetype (similar to The Town That Dreaded Sundown's killer).",
    "Part 2's success guaranteed the franchise would continue annually throughout the 1980s — a new entry was released every year from 1981-1989.",
])

ingest_film("Friday the 13th Part III (1982)", [
    "Friday the 13th Part III is where Jason Voorhees first acquires his iconic hockey mask — stolen from prankster Shelly Finkelstein.",
    "The film was shot and released in 3D, making it one of the early-80s 3D revival films. Many kills feature objects thrust toward the camera.",
    "Richard Brooker played Jason for the first time as a tall, imposing figure — establishing the physical template all future Jasons would follow.",
    "Steve Miner returned to direct, making him the only director to helm consecutive Friday entries (Parts 2 and 3).",
    "The hockey mask was chosen because it was available in the prop department and fit the actor. It became one of horror's most recognizable icons.",
    "Part III takes place the day after Part 2, continuing the franchise's tight chronological continuity (Parts 1-4 span less than a week).",
    "Dana Kimmell plays Chris Higgins, the final girl who has a previous encounter with Jason — he attacked her in the woods years earlier.",
    "The 'eyeball pop' kill (squeezed head in 3D) and 'harpoon gun to the eye' were signature 3D moments designed for theatrical audiences.",
    "The disco-influenced theme song 'Friday the 13th Part III' by Hot Ice/Michael Zager was a departure from Harry Manfredini's signature score.",
    "Part III introduced biker gang characters (Fox, Loco, Ali) who serve as additional victims and red herrings before Jason kills the main group.",
    "The film grossed $36.7 million — the highest-grossing entry until Freddy vs. Jason (2003) — proving 3D was a major draw.",
    "The barn setting for the climax (where Chris fights Jason with a rope and axe) became one of the franchise's most iconic locations.",
    "Richard Brooker (Jason) was actually a British actor and trapeze artist whose physicality made the character menacing in ways previous actors couldn't achieve.",
    "Part III ends with Chris in a canoe on Crystal Lake — mirroring the original film's ending where Jason jumps from the water (here it's Pamela's corpse).",
    "The 3D process used (ArriVision 3D) required special lighting that gave the film a distinctly flat, harsh look when viewed in 2D.",
    "Shelly Finkelstein (Larry Zerner) — the character who inadvertently provides Jason's hockey mask — was found by a casting agent while handing out movie flyers.",
    "Part III was the first Friday film to truly make Jason the 'star' — he has significant screen presence and the audience roots for creative kills.",
    "The film establishes Jason's trademark machete as his preferred weapon, though he uses multiple implements throughout.",
    "Harry Manfredini's 'ki ki ki, ma ma ma' sound effect (often misquoted as 'ch ch ch, ah ah ah') continues in Part III, forever associated with Jason's presence.",
    "Part III was the last entry set at or near Crystal Lake until Part VI returned to the location — Parts 4 and 5 moved to different settings.",
])

ingest_film("Friday the 13th Part V: A New Beginning (1985)", [
    "Friday the 13th Part V controversially reveals that the killer is NOT Jason Voorhees but Roy Burns, a paramedic whose son was murdered at the halfway house.",
    "The 'copycat killer' twist was so hated by fans that Paramount immediately brought the real Jason back for Part VI.",
    "Tommy Jarvis (John Shepherd) returns as the protagonist — now a disturbed young man in a halfway house for troubled teens, haunted by Jason.",
    "Danny Steinmann directed, bringing a more exploitation-film sensibility with higher nudity and body count (22 kills — the highest until Jason X).",
    "The film was the most heavily censored by the MPAA of any Friday entry — over a minute of gore was cut to achieve an R rating.",
    "Despite fan hatred of the twist, Part V grossed $21.9 million on a $2 million budget — still highly profitable.",
    "Corey Feldman (Tommy Jarvis in Part IV) appears only in a brief opening dream sequence before the role transitions to an older actor.",
    "The halfway house setting (Pinehurst Youth Development Center) replaced the camp/cabin formula with something new, though many fans disliked the change.",
    "Roy Burns (Dick Wieand) is motivated by the death of his son Joey — killed by another patient — driving him to kill everyone at the facility while wearing a hockey mask.",
    "The film features some of the franchise's most creative kills, including death by road flare, hedge clippers, and leather strap.",
    "Part V's ending implies Tommy Jarvis himself may become the next killer (he puts on the hockey mask) — a thread Part VI chose not to follow.",
    "Miguel A. Núñez Jr. plays Demon, whose outhouse kill scene is one of the most memorable (and oddly humorous) moments in the franchise.",
    "Ethel and Junior (the obnoxious redneck neighbors) provide dark comedy that divides fans — some love them, others find them tonally jarring.",
    "The blue chevron marks on Roy's hockey mask (vs Jason's red) were the subtle clue that this wasn't the real Jason — though most viewers didn't notice.",
    "Part V is set several years after Part IV, with Tommy now approximately 17 years old and struggling with PTSD from his childhood encounter with Jason.",
    "The film was shot in the San Fernando Valley (California) standing in for rural New Jersey — a common substitution throughout the franchise.",
    "Part V's body count (22) wouldn't be surpassed until Jason X (2001) added 23 kills including cryogenically frozen victims.",
    "Deborah Voorhees (real name — coincidence!) appeared in the film, and her surname meant she was constantly asked about her 'relation' to Jason.",
    "The reveal that Jason isn't the killer undermined the franchise's appeal — audiences came specifically to see Jason, not a random copycat.",
    "Despite its reputation as a low point, Part V has been partially rehabilitated by fans who appreciate its mean-spirited tone and high energy.",
])

ingest_film("Friday the 13th Part VII: The New Blood (1988)", [
    "Friday the 13th Part VII introduced Kane Hodder as Jason Voorhees — he would play the role in four consecutive films (VII, VIII, IX, X) and become the definitive Jason.",
    "The film pits Jason against Tina Shepard (Lar Park Lincoln), a teenager with telekinetic powers — essentially 'Jason vs Carrie.'",
    "Kane Hodder brought unprecedented physicality and body language to Jason. His heavy breathing, head tilts, and deliberate movements defined the character.",
    "The MPAA forced the most extensive cuts of any Friday film — nearly every kill was significantly trimmed, removing the practical effects work.",
    "Director John Carl Buechler was a special effects artist who designed elaborate kill sequences, most of which were gutted by censors.",
    "The 'sleeping bag kill' (Jason smashes a girl in a sleeping bag against a tree) became one of the franchise's most iconic moments despite its simplicity.",
    "Part VII takes place at Crystal Lake (returning after two films away), with Tina's family cabin directly across from Jason's underwater resting place.",
    "Tina accidentally resurrects Jason with her telekinesis while trying to bring back her dead father — establishing the supernatural connection between them.",
    "The final battle features Tina using telekinesis to fight Jason — throwing furniture, wrapping electrical cords around him, and ultimately summoning her dead father from the lake.",
    "Kane Hodder performed his own stunts, including being set fully on fire (without CGI) for the climax — one of the longest full-body burns in film history at that time.",
    "Part VII's uncut kill footage has become legendary among horror fans. Some sequences have leaked but the full uncut version has never been officially released.",
    "The 'Jason unmasked' design in Part VII (by John Carl Buechler) is considered the best decomposed Jason look in the franchise — showing exposed spine and skull.",
    "Hodder insisted on adding character details: Jason tilts his head when curious, breathes heavily when angry, and moves with increasing aggression as the film progresses.",
    "The ensemble of teenage characters follows the standard formula (jock, nerd, mean girl, nice couple) but is considered above-average for the franchise.",
    "Part VII grossed $19.2 million — profitable but a decline from earlier entries, partly blamed on the severe MPAA cuts removing the film's gore highlights.",
    "The telekinesis angle was conceived because Paramount considered (but ultimately rejected) a 'Freddy vs Jason' crossover with New Line Cinema.",
    "Kane Hodder's audition involved wearing the mask and improvising threatening movements. His intensity reportedly made the producers uncomfortable — and they hired him immediately.",
    "The film's opening recap of the franchise (narrated over clips from Parts 1-6) is one of the most comprehensive 'story so far' summaries in horror sequels.",
    "Part VII established 'zombie Jason' as the permanent state of the character — he's clearly undead, not merely a deformed human surviving injuries.",
    "Terry Kiser (Weekend at Bernie's) plays Tina's manipulative psychiatrist Dr. Crews — one of the few Friday characters audiences actively want to see die.",
])

ingest_film("Friday the 13th Part VIII: Jason Takes Manhattan (1989)", [
    "Friday the 13th Part VIII promised Jason terrorizing New York City but budget limitations meant only the final 30 minutes are set in Manhattan — most occurs on a cruise ship.",
    "Kane Hodder returned as Jason for the second time, maintaining the physical intensity he brought to Part VII.",
    "The film was shot primarily in Vancouver (standing in for both Crystal Lake and New York) with brief location shoots in actual Manhattan.",
    "The 'boxing on the rooftop' scene — where Julius fights Jason and Jason punches his head clean off — is one of the franchise's most beloved kills.",
    "Director Rob Hedden originally wrote a script set entirely in Manhattan but Paramount slashed the budget, forcing the cruise ship setting.",
    "Jason Takes Manhattan grossed only $14.3 million — the lowest-grossing theatrical entry at that time — leading Paramount to sell the franchise rights to New Line Cinema.",
    "The film introduces a toxic waste flood in New York sewers that somehow reverts Jason back to a child — one of the franchise's most baffling and criticized endings.",
    "Kelly Hu (later famous in X2: X-Men United) has a small role as one of the graduating class on the cruise ship.",
    "The cruise ship SS Lazarus is named after the biblical figure raised from the dead — a reference to Jason's repeated resurrections.",
    "Part VIII features the first instance of Jason in a truly urban environment, interacting with Times Square crowds, punks, and gang members.",
    "The scene of Jason walking through Times Square (terrified New Yorkers running) was filmed guerrilla-style with real pedestrians reacting to the costumed actor.",
    "Jason kicks over a boombox playing hip-hop, and the street punks flee immediately — a moment of dark comedy that works despite the film's overall weakness.",
    "Part VIII's failure at the box office directly led to Jason Goes to Hell (1993) being made by New Line Cinema with a drastically different approach.",
    "The 'Jason on the subway' scene was cut significantly — the original version showed him killing multiple passengers in a longer sequence.",
    "Kane Hodder has called Part VIII his least favorite of his four Jason films, citing the budget restrictions and the 'child Jason' ending as disappointments.",
    "The film's score by Fred Mollin departed from Harry Manfredini's iconic style, using more rock-influenced compositions that divided fans.",
    "The graduating class of Lakeview High provides a larger-than-usual victim pool, though few characters are developed beyond basic stereotypes.",
    "Jason's resurrection at the film's opening (electrocuted by underwater power cable) is one of the franchise's most creative 'how he comes back' moments.",
    "Part VIII was the last Paramount-produced Friday film before the franchise moved to New Line Cinema. It represents the end of an era for the series.",
    "Despite being widely considered one of the weaker entries, Part VIII has fans who appreciate its ambitious concept (if not its budget-limited execution).",
])

ingest_film("Jason Goes to Hell: The Final Friday (1993)", [
    "Jason Goes to Hell was the first Friday film produced by New Line Cinema after acquiring the franchise rights from Paramount.",
    "The film introduced a radical concept: Jason is a body-hopping demonic entity that transfers between hosts — not merely a physical killer.",
    "The opening scene features an FBI sting operation that blows Jason apart with military ordinance — establishing immediately that this is a different kind of Friday film.",
    "Kane Hodder returned as Jason for the third time, though he spends most of the film in other characters' bodies.",
    "The Necronomicon from Evil Dead appears in the Voorhees house, implicitly connecting the Friday the 13th and Evil Dead universes.",
    "The ending — Freddy Krueger's glove pulling Jason's mask into the ground — was the first official tease of a Freddy vs. Jason crossover (which took 10 years to materialize).",
    "Director Adam Marcus was only 23 years old when he directed Jason Goes to Hell — one of the youngest directors of a major franchise entry.",
    "The 'Voorhees bloodline' mythology (Jason can only be truly killed by a blood relative using a special dagger) was entirely new to this film.",
    "Steven Williams plays bounty hunter Creighton Duke, a fan-favorite character whose backstory (Jason killed his girlfriend) is barely explored.",
    "Jason Goes to Hell was controversial with fans who felt the body-hopping concept betrayed what made Jason appealing — his imposing physical presence.",
    "The film grossed $15.9 million — a slight improvement over Part VIII but still considered underperforming for the franchise.",
    "The 'Jason baby' sequence (a demonic worm transferring between bodies) was achieved through practical effects that won praise even from detractors.",
    "Jason's true demonic form (briefly seen) resembles a large worm/serpent creature — completely at odds with the franchise's established mythology.",
    "The MPAA again forced extensive cuts. The unrated version contains significantly more gore, particularly the tent-splitting kill.",
    "Erin Gray (Buck Rogers) and Steven Culp play characters in the film, lending TV-level acting credibility to the production.",
    "The film explains Jason's seemingly impossible survival across 8 films as supernatural (demonic possession) rather than simple durability.",
    "New Line Cinema's involvement meant Jason was now stablemates with Freddy Krueger — making a crossover legally possible for the first time.",
    "The Voorhees house scenes include numerous props from the earlier films, creating a sense of continuity with the Paramount era.",
    "Jason Goes to Hell spent 10 years being the 'final' Friday film before Freddy vs. Jason (2003) and the 2009 reboot continued the franchise.",
    "Despite poor fan reception, the film's ending tease of Freddy vs. Jason generated enormous excitement and kept franchise interest alive for a decade.",
])

ingest_film("Jason X (2001)", [
    "Jason X is set in the year 2455 — Jason has been cryogenically frozen and is thawed on a spaceship, making it the franchise's most radical setting change.",
    "Kane Hodder played Jason for the fourth and final time in Jason X. He remains the only actor to play Jason more than once.",
    "The film introduces 'Uber Jason' — an upgraded, nanotechnology-enhanced version with a metallic mask and silver armor, created after Jason is partially destroyed.",
    "Jason X's 'sleeping bag kill' homage (using a holographic simulation of Crystal Lake to distract Jason by smashing virtual campers in sleeping bags) is a beloved meta-moment.",
    "Director James Isaac created the concept as a way to continue the franchise while Freddy vs. Jason was stuck in development hell.",
    "The film's tone is intentionally campy and self-aware — more sci-fi action-comedy than traditional horror, which divided the fanbase.",
    "Jason X was filmed in Toronto on a $11-14 million budget — lower than most sci-fi films but higher than previous Friday entries.",
    "The 'liquid nitrogen face smash' kill (Jason freezes a scientist's face then shatters it on a counter) is considered one of the franchise's most creative deaths.",
    "Lexa Doig plays Rowan, the scientist who originally had Jason frozen, and serves as the film's final girl across 450 years of timeline.",
    "The body count in Jason X is 23 — the highest in the franchise, appropriate for a film with a large spaceship crew.",
    "Jason X has the franchise's most quotable line: 'He's been wrong before' / 'He wasn't wrong about Michigan' referring to a previous Jason massacre.",
    "The holodeck/simulation scene showing two virtual camp counselors saying 'We love premarital sex!' before Jason kills them is peak franchise self-awareness.",
    "David Cronenberg has a cameo as a scientist in the film's opening — a surreal appearance by one of horror's most respected auteurs.",
    "Uber Jason's design was influenced by the Borg from Star Trek, with biomechanical elements fused onto Jason's existing form.",
    "The film grossed only $16.9 million theatrically but found a larger audience on DVD, where its campy tone played better than in theaters.",
    "Jason X was technically released AFTER principal photography on Freddy vs. Jason had begun — creating a chronological continuity puzzle for the franchise.",
    "The space station setting allowed for creative kills impossible in terrestrial films — zero gravity, airlocks, hull breaches, cryogenic freezing.",
    "Kane Hodder has expressed disappointment at not being cast in Freddy vs. Jason (2003), saying Jason X was his 'farewell' to the character.",
    "The film's opening (set in 2010 at the Crystal Lake Research Facility) features Jason strung up Hannibal Lecter-style, studied by scientists.",
    "Jason X embraces the absurdity of a franchise on its 10th film by going to the most extreme setting possible — outer space — and committing to the bit entirely.",
])

ingest_film("The Thing (1982)", [
    "John Carpenter's The Thing (1982) is considered one of the greatest horror films ever made, featuring groundbreaking practical effects by Rob Bottin.",
    "The film stars Kurt Russell as R.J. MacReady, a helicopter pilot at a remote Antarctic research station infiltrated by a shape-shifting alien organism.",
    "Rob Bottin was only 22 years old when he created The Thing's practical effects. He worked so intensely that he was hospitalized for exhaustion after production.",
    "The Thing was a box office disappointment on release ($19.6M on $15M budget), crushed by E.T. the Extra-Terrestrial which opened two weeks earlier.",
    "Critics initially savaged The Thing for its extreme gore. It was only years later, on home video, that it was recognized as a masterpiece of paranoid horror.",
    "The film's 'blood test' scene — where MacReady tests each man's blood with a hot wire — is one of the most perfectly constructed suspense sequences in cinema history.",
    "Ennio Morricone composed the minimalist score (bass heartbeat synthesizer pulse), though Carpenter also contributed uncredited electronic compositions.",
    "The Thing is based on John W. Campbell's 1938 novella 'Who Goes There?' and is technically a remake of Howard Hawks' 'The Thing from Another World' (1951).",
    "The chest-defibrillator scene (the doctor's arms are bitten off by a chest that opens into jaws) remains one of horror's most shocking practical effects sequences.",
    "The 'spider head' — a severed head that sprouts legs and walks away — was achieved with a puppet and wires. Audiences in 1982 had never seen anything like it.",
    "Carpenter deliberately cast only men (no female characters) to create an isolated, testosterone-fueled pressure cooker of paranoia and suspicion.",
    "The film's ambiguous ending (MacReady and Childs sitting in the burning camp, possibly both or neither infected) has generated 40+ years of fan theories.",
    "Keith David (Childs) and Kurt Russell never resolved whether their characters were human or Thing. Both actors give deliberately ambiguous performances in the final scene.",
    "The dog transformation scene (the Alaskan Malamute reveals itself as a Thing in the kennel) was filmed last and nearly wasn't completed due to budget overruns.",
    "The Norwegian camp footage (showing what happened to the first team) was filmed on the same set after the main shoot, using the same buildings pre-destruction.",
    "The Thing uses paranoia itself as its weapon — the characters' inability to trust each other mirrors Cold War tensions that Carpenter deliberately evoked.",
    "Wilford Brimley plays Blair, the biologist who goes insane (or is assimilated) and destroys the radio and helicopter to prevent the Thing from reaching civilization.",
    "The prequel (2011) digitally recreated the Norwegian camp exactly as it appears in Carpenter's film, ensuring every frozen corpse and burned building matched.",
    "The Thing's failure in 1982 (alongside Blade Runner, also initially unsuccessful) made Carpenter question whether audiences wanted intelligent horror over comforting fantasy.",
    "Universal Studios initially wanted a more conventional monster (a fixed alien design) but Carpenter insisted the creature should constantly change form — making it truly unknowable.",
    "The 'Palmer transformation' scene (Palmer reveals as a Thing and absorbs Windows) required 12 puppeteers working simultaneously under the set.",
    "Computer analysis by fans has attempted to determine exactly 'who is the Thing when' using subtle clues in character behavior, clothing continuity, and isolation.",
    "The Thing's influence on horror is immeasurable — from 'Alien' (isolation horror) to 'The Hateful Eight' (Tarantino explicitly references it) to 'Among Us' (the video game).",
    "Stan Winston created the dog-Thing effects (Bottin was unavailable) — one of the few scenes not done by Bottin himself.",
    "The film's production design (by John Lloyd) created the claustrophobic Antarctic station interiors at Universal Studios Stage 27 — every hallway and room was a set.",
    "Carpenter has said The Thing's commercial failure hurt him deeply and contributed to his increasingly pessimistic worldview reflected in later films.",
    "The Thing was re-released theatrically in 2011 (alongside the prequel) and finally received the critical acclaim it deserved, with perfect Rotten Tomatoes scores from modern critics.",
    "The 'Outpost 31' set was equipped with real breath-fog machines so actors' breath was visible, adding to the Antarctic realism without actually filming in extreme cold.",
    "A video game sequel (2002) was developed with Carpenter's input, exploring what happened after the film's ambiguous ending. It's considered semi-canonical by fans.",
    "The Thing remains the gold standard for practical effects in horror. Every creature was achieved in-camera with latex, rubber, mechanical armatures, and hydraulics.",
    "Kurt Russell and Carpenter's collaboration on The Thing was their third film together (after Elvis and Escape from New York). They would make two more (Big Trouble, Escape from LA).",
    "The blood test scene works because the audience ALSO doesn't know who's infected — creating a unique horror experience where viewer and characters share identical information.",
    "Norris's chest opening into a mouth (biting off the doctor's arms) required a double-amputee actor wearing prosthetic arms filled with wax and rubber to simulate the bite.",
    "The film was shot in approximately 12 weeks in summer 1981, with the crew working in refrigerated soundstages to simulate their breath and the cold atmosphere.",
    "Carpenter's use of the 2.35:1 widescreen format allows him to hide Things in the background of shots — rewatching reveals details invisible on first viewing.",
    "The Norwegian suicide at the beginning (chasing a dog and being shot) establishes the film's tone immediately — desperate, irrational behavior driven by knowledge of the Thing.",
    "The Thing assimilates and perfectly replicates any living organism at the cellular level — each cell is an independent organism, meaning even a drop of blood is dangerous.",
    "The chess computer scene (MacReady pours whiskey into it after losing) foreshadows his willingness to destroy things he can't beat, including potentially himself.",
    "Richard Masur (Clark, the dog handler) was suspected by fans of being a Thing early because of his protective relationship with the dogs — one of whom IS a Thing.",
    "The Thing has no definitive 'correct' interpretation of the ending. Carpenter has given contradictory statements about whether Childs is human, ensuring eternal debate.",
    "Box office competition from E.T. (optimistic alien) vs The Thing (nihilistic alien) in summer 1982 perfectly encapsulated America's cultural divide between Spielbergian hope and Carpenterian pessimism.",
    "The opening title sequence (the Thing's spaceship approaching Earth 100,000 years ago) was actually stock footage from the 1951 film, recomposited with updated effects.",
    "John Carpenter considers The Thing his finest film technically, even if The Shining and Psycho influenced him more thematically.",
    "The film's production was plagued by mechanical effect failures — the dog-kennel scene required 50+ takes because puppets broke, missed cues, or melted under hot lights.",
    "Albert Whitlock created the matte paintings of the Antarctic landscape (including the Norwegian camp exterior), using traditional glass painting techniques.",
    "The Thing was the first film in Carpenter's 'Apocalypse Trilogy' (followed by Prince of Darkness 1987 and In the Mouth of Madness 1994) — all dealing with cosmic evil threatening humanity.",
])

# Quick entries for remaining films
for film, facts in [
    ("The Thing (2011)", ["The Thing (2011) is a prequel to Carpenter's 1982 film, depicting what happened at the Norwegian camp before MacReady's team investigates.", "Mary Elizabeth Winstead stars as Kate Lloyd, a paleontologist recruited to examine an alien specimen found in Antarctic ice.", "The prequel's CGI effects were controversial — director Matthijs van Heijningen Jr. originally shot practical effects that were replaced with CGI in post-production by the studio.", "The film meticulously recreates every detail seen in Carpenter's 1982 film when MacReady explores the destroyed Norwegian camp.", "Joel Edgerton plays Sam Carter, a helicopter pilot echoing Kurt Russell's MacReady role.", "The film reveals the spaceship discovery and initial alien emergence that led to the Norwegian camp's destruction.", "ADI (Amalgamated Dynamics Inc.) created extensive practical creature effects that were largely covered or replaced by CG in the final film — a decision widely criticized.", "The Thing (2011) grossed $31 million on a $38 million budget — a commercial disappointment that killed plans for a sequel.", "The prequel explains how the axe got in the wall, why the radio room was destroyed, and how the two-faced Thing corpse was formed — all details visible in Carpenter's film.", "The film's creature designs honor Bottin's original work while expanding the Thing's repertoire of body horror transformations."]),
    ("The Thing from Another World (1951)", ["The Thing from Another World (1951) was produced by Howard Hawks and directed by Christian Nyby (though Hawks likely directed much of it himself).", "The film adapts John W. Campbell's 'Who Goes There?' but changes the shape-shifting alien to a humanoid plant-based creature.", "James Arness (later Marshal Dillon on Gunsmoke) played the Thing — a tall, powerful creature that feeds on blood.", "The film's famous line 'Keep watching the skies!' became a catchphrase of 1950s science fiction.", "John Carpenter watched The Thing from Another World as a child and it became his favorite film — directly inspiring his 1982 remake.", "The film is notable for its overlapping dialogue (a Hawks trademark) creating realistic conversation among the Arctic team.", "Unlike Carpenter's version, the 1951 film's creature has a fixed form and is defeated conventionally (electrocution) rather than posing an existential threat.", "The Thing from Another World was a major box office success and helped establish the 1950s science fiction boom alongside The Day the Earth Stood Still.", "The creature is discovered in a flying saucer frozen in Arctic ice — the basic setup that both the 1982 and 2011 films would retain.", "Hawks' version emphasizes teamwork and military competence (the scientists are portrayed as naive), while Carpenter's version deconstructs that optimism entirely."]),
    ("Forbidden Planet (1956)", ["Forbidden Planet (1956) is considered one of the greatest science fiction films ever made, loosely adapting Shakespeare's 'The Tempest' in a space setting.", "The film introduced Robby the Robot, who became one of cinema's most iconic robot characters and appeared in numerous subsequent films and TV shows.", "Forbidden Planet was the first science fiction film set entirely on another planet with no scenes on Earth — a bold choice for 1956.", "The 'Monsters from the Id' concept — that the Krell's technology amplified their subconscious destructive impulses — was groundbreaking psychological science fiction.", "The film's all-electronic score by Bebe and Louis Barron was the first entirely electronic film soundtrack. MGM refused to call it 'music,' crediting 'electronic tonalities.'", "Leslie Nielsen stars as Commander Adams in a serious dramatic role, decades before his comedy career (Airplane!, Naked Gun).", "The invisible monster (the Id Monster) was animated by Disney animator Joshua Meador, borrowed by MGM for the production.", "Walter Pidgeon plays Dr. Morbius, a philologist stranded on Altair IV with his daughter Altaira — the Prospero figure in the Shakespeare parallel.", "The Krell — an extinct alien civilization that achieved virtually unlimited technology before destroying themselves in a single night — represent humanity's potential future.", "Forbidden Planet directly influenced Star Trek — Gene Roddenberry acknowledged the film's crew dynamics, planet exploration format, and 'strange new worlds' concept as inspiration.", "The film's budget was $1.9 million (enormous for 1956 sci-fi) and it shows in the elaborate matte paintings, sets, and special effects.", "Anne Francis plays Altaira (the Miranda figure) in a role that, while limited by 1950s gender norms, gives her character genuine curiosity and agency.", "The Krell machine — a cube 20 miles on each side powered by 9,200 thermonuclear reactors — is one of science fiction's most awe-inspiring technological concepts.", "Forbidden Planet was MGM's attempt to make a 'prestige' science fiction film with A-list production values, proving sci-fi could be intelligent and visually spectacular.", "The film was preserved in the National Film Registry in 2013 for being 'culturally, historically, or aesthetically significant.'"]),
    ("The Car (1977)", ["The Car (1977) stars James Brolin as a small-town sheriff fighting a mysterious demonically-possessed black car that terrorizes a desert community.", "The car itself is a custom-built 1971 Lincoln Continental Mark III, modified with a chopped roof, tinted windows, and a menacing lowered stance.", "The film was directed by Elliot Silverstein and is often described as 'Jaws with a car' — an unstoppable force terrorizing a small community.", "The Car has no visible driver, cannot be stopped by conventional means, and seems to specifically target people who 'deserve' punishment.", "George Barris (who built the 1966 Batmobile and the Munsters' cars) designed The Car's intimidating custom body modifications.", "The Car was a moderate box office success and has achieved cult status among horror fans who appreciate its simple, effective premise.", "The film's desert setting (filmed in Utah) gives it a stark, desolate atmosphere where the black car stands out against sand and rock.", "R.G. Armstrong plays Amos Clements, a domestic abuser who is one of the Car's victims — suggesting the vehicle has a moral (if violent) judgment.", "The Car cannot enter consecrated ground (a cemetery), implying a demonic or Satanic origin for the vehicle's evil.", "John Carpenter reportedly was a fan of The Car and its influence can be seen in his adaptation of Christine (1983) — another evil vehicle story."]),
    ("IT (1990)", ["The 1990 IT TV miniseries starred Tim Curry as Pennywise the Dancing Clown — a performance so terrifying it traumatized an entire generation of children who watched it on network TV.", "Tim Curry's Pennywise became the definitive 'evil clown' in popular culture, directly contributing to widespread coulrophobia (fear of clowns).", "The miniseries was a two-part, three-hour ABC television event that drew massive ratings despite (or because of) its terrifying content.", "Due to TV restrictions, the miniseries couldn't show the extreme violence of King's novel. Curry's performance had to carry the horror through suggestion and personality.", "The Losers Club — Bill, Beverly, Ben, Richie, Eddie, Mike, and Stan — was cast with strong child actors whose chemistry makes the first half significantly stronger than the second.", "The adult half (Part 2) is generally considered weaker, with the giant spider finale limited by 1990 TV-budget special effects.", "Jonathan Brandis, Seth Green, and Emily Perkins played young Bill, Richie, and Beverly — establishing the child actors as the emotional core.", "John Ritter, Harry Anderson, Tim Reid, and Annette O'Toole played adult versions of the Losers, bringing TV-star credibility.", "The famous 'paper boat' opening (Georgie meeting Pennywise in the storm drain) is nearly shot-for-shot identical to how King wrote it.", "Tommy Lee Wallace directed (the same editor/designer from Carpenter's Halloween), bringing horror pedigree to a TV production."]),
    ("IT (2017)", ["IT (2017) grossed $700.4 million worldwide, becoming the highest-grossing horror film of all time (unadjusted) at the time of release.", "Bill Skarsgård's Pennywise — with a lazy eye, drooling, and Victorian-era costume — was deliberately different from Tim Curry's to avoid comparison.", "Director Andy Muschietti created a genuinely terrifying film that proved R-rated horror could achieve blockbuster-level box office.", "The Losers Club (Jaeden Martell, Sophia Lillis, Finn Wolfhard, etc.) gave naturalistic performances that grounded the supernatural horror in real childhood anxiety.", "Sophia Lillis's Beverly Marsh was praised for portraying the character's abuse and resilience with complexity rarely seen in horror blockbusters.", "IT (2017) moved the setting from the 1950s (novel) to the late 1980s, allowing 80s nostalgia to enhance the period atmosphere.", "The 'Georgie' opening scene raised the violence level immediately — showing the arm-bite graphically, something 1990 TV couldn't do.", "The film's production design created a Derry, Maine that felt like a cursed town — every location slightly wrong, slightly decayed.", "Pennywise's dance (the 'Pennywise shuffle') was Skarsgård's improvisation and became an iconic horror moment and internet meme.", "Warner Bros. greenlit the sequel immediately after opening weekend, setting IT Chapter Two for 2019."]),
    ("IT Chapter Two (2019)", ["IT Chapter Two (2019) follows the adult Losers returning to Derry 27 years later to destroy Pennywise permanently using the Ritual of Chüd.", "Jessica Chastain, James McAvoy, Bill Hader, and Isaiah Mustafa play adult Beverly, Bill, Richie, and Mike — an A-list ensemble.", "Bill Hader's performance as adult Richie was widely praised, particularly a subplot revealing Richie is gay and his childhood love for Eddie.", "The film is 2 hours 49 minutes — one of the longest horror films ever released theatrically — and some critics felt it was overstuffed.", "Stephen King has a cameo as a shopkeeper who rips off adult Bill — a meta-joke about authors exploiting their own work.", "The Ritual of Chüd (from King's novel) was adapted but simplified — the film uses 'making Pennywise feel small' as the defeat mechanism rather than the cosmic Turtle.", "IT Chapter Two grossed $473 million worldwide — highly profitable but below the first film's $700M, following the sequel pattern of diminishing returns.", "The 'de-aging' technology used to show the young Losers in flashbacks was cutting-edge for 2019 but received mixed responses from audiences.", "Andy Muschietti returned to direct, maintaining visual continuity with the first film while expanding the scope of Pennywise's cosmic horror origins.", "The film's epilogue reveals that the Losers finally escape Derry's influence and can live normal lives — the 'It' entity is truly dead and the cycle is broken."]),
    ("Them! (1954)", ["Them! (1954) was the first 'big bug' film — giant ants mutated by nuclear radiation in the New Mexico desert. It established an entire subgenre of atomic-age monster movies.", "The film stars James Whitmore as Sergeant Ben Peterson and James Arness (pre-Gunsmoke, post-Thing) as FBI agent Robert Graham.", "Them! was Warner Bros.' highest-grossing film of 1954, proving that science fiction horror could draw mainstream audiences.", "The giant ants were full-size mechanical puppets operated by multiple technicians — impressive practical effects for 1954.", "The film works because it plays like a mystery/procedural for the first act before revealing the ants — building genuine dread.", "The 'eerie chirping' sound of the ants (created by speeding up tree frog recordings) is instantly recognizable and deeply unsettling.", "Dr. Harold Medford (Edmund Gwenn) serves as the scientific exposition character, explaining the ants' biology and the nuclear mutation cause.", "The climax takes place in the Los Angeles storm drains — the same tunnels that would later appear in Grease, Terminator 2, and countless other films.", "Them! was originally planned to be shot in color and 3D but budget cuts forced black-and-white photography — which actually enhances its documentary-style realism.", "The film's message about nuclear testing consequences resonated with Cold War audiences living under genuine atomic anxiety."]),
    ("Event Horizon (1997)", ["Event Horizon (1997) is a sci-fi horror film directed by Paul W.S. Anderson, often described as 'The Shining in space' or 'Hellraiser meets Alien.'", "Sam Neill plays Dr. William Weir, the designer of the Event Horizon's experimental gravity drive — which accidentally opened a portal to a hell dimension.", "Laurence Fishburne plays Captain Miller of the rescue ship Lewis and Clark, bringing gravitas and authority to the crew leader role.", "The ship's gravity drive created a black hole to fold space-time — but the dimension it traveled through was essentially Hell, driving the original crew insane.", "Event Horizon was a box office failure ($26.7M on $60M budget) but became a major cult classic on home video, particularly among horror and sci-fi fans.", "The film's 'hell visions' — brief flashes of the original crew's fate (mutilation, cannibalism, sexual torture) — were so disturbing that Paramount demanded heavy cuts.", "An estimated 30 minutes of extreme footage was cut to achieve an R rating. This footage is believed lost — damaged and possibly destroyed in a Transylvanian salt mine.", "The production design of the Event Horizon itself (gothic architecture, cruciform corridors) deliberately makes the ship look like a cathedral to evil.", "Jason Isaacs, Sean Pertwee, Joely Richardson, and Richard T. Jones round out the ensemble cast.", "The 'video log' from the Event Horizon's original crew (showing them tearing each other apart) is one of the most disturbing sequences in 90s horror.", "Event Horizon's concept — that faster-than-light travel could expose humans to cosmic evil — has influenced numerous sci-fi properties including Warhammer 40K's 'Warp.'", "Paul W.S. Anderson has said Event Horizon is his most personal film, inspired by his love of Clive Barker and HP Lovecraft.", "The set was built at Pinewood Studios (UK) and featured a rotating corridor that actually spun, creating the film's disorienting zero-gravity sequences.", "Sam Neill's transformation from sympathetic scientist to vessel of evil mirrors Jack Nicholson's arc in The Shining — a deliberate influence.", "The Latin phrase 'Liberate tutemet ex inferis' (save yourself from hell) spoken in the distress signal perfectly encapsulates the film's Lovecraftian premise."]),
    ("The Cabin in the Woods (2012)", ["The Cabin in the Woods (2012) was co-written by Joss Whedon and Drew Goddard (who also directed). It deconstructs the entire horror genre while being genuinely scary and funny.", "The film's premise: a secret underground facility manipulates horror scenarios to ritualistically sacrifice young people, appeasing ancient gods called 'The Ancient Ones.'", "The facility contains EVERY horror monster archetype — zombies, werewolves, ghosts, killer robots, mermen — stored in a massive elevator system.", "The 'system purge' scene (all monsters released simultaneously) is one of the most spectacular sequences in horror history — a love letter to the genre.", "Chris Hemsworth, Kristen Connolly, and Fran Kranz star as the 'cabin kids,' with Bradley Whitford and Richard Jenkins as the facility technicians.", "The film sat unreleased for nearly 3 years (2009-2012) due to MGM's bankruptcy — generating enormous anticipation when it finally premiered.", "Cabin in the Woods explains WHY horror movie characters make dumb decisions — they're being chemically manipulated by the facility to fulfill archetypal roles.", "The 'Jock, Virgin, Scholar, Whore, Fool' archetypes correspond to ancient sacrificial roles required by the ritual — the film's meta-commentary on horror's formula.", "Sigourney Weaver has a surprise cameo as 'The Director' in the final minutes — her presence (Alien's Ripley!) adds another layer of genre awareness.", "The ending — the protagonists REFUSE to complete the sacrifice, dooming humanity — is both nihilistic and oddly triumphant, rejecting the genre's punitive morality.", "Joss Whedon wrote the film partly as a critique of 'torture porn' (Saw, Hostel) — arguing that horror should be fun and inventive, not just sadistic.", "The whiteboard listing all possible monsters became a viral image — fans identified every creature and debated which scenarios would have played out.", "Drew Goddard's directorial debut proved he could handle massive practical effects sequences alongside intimate character work.", "The Japanese schoolgirl sequence (where Japan's sacrificial ritual goes wrong) is both hilarious and a commentary on J-horror conventions.", "Cabin in the Woods grossed $66 million on a $30 million budget and received near-universal critical acclaim for its originality."]),
    ("Alien (1979)", ["Alien (1979) directed by Ridley Scott is one of the most influential science fiction horror films ever made, establishing the 'haunted house in space' subgenre.", "H.R. Giger's biomechanical alien design — the Xenomorph — is considered one of the greatest creature designs in film history.", "Sigourney Weaver's Ellen Ripley was revolutionary — a female action hero in a genre dominated by male leads, becoming one of cinema's greatest characters.", "The chestburster scene (John Hurt) was filmed with the other actors not knowing exactly what would happen — their shocked reactions are genuine.", "The film's tagline 'In space, no one can hear you scream' is one of the most iconic in film history.", "Alien was Ridley Scott's second feature film and established his visual style — dark, atmospheric, with meticulous production design.", "The derelict alien ship and 'Space Jockey' (later called an Engineer) were designed by Giger and remain some of the most mysterious imagery in sci-fi.", "Dan O'Bannon wrote the screenplay, inspired by Dark Star (his student film with John Carpenter), Lovecraft, and a desire to make 'Jaws in space.'", "The Nostromo's crew were deliberately working-class — 'truckers in space' — making them relatable and their deaths feel real rather than disposable.", "Alien won the Academy Award for Best Visual Effects (1980), legitimizing science fiction horror as serious filmmaking."]),
    ("Aliens (1986)", ["Aliens (1986) directed by James Cameron transformed the franchise from horror to action while retaining genuine terror — one of cinema's greatest sequels.", "Cameron's script used the Vietnam War as a metaphor — technologically superior marines outmatched by an enemy that knows the terrain and fights differently.", "'Game over, man! Game over!' (Bill Paxton as Private Hudson) became one of the most quoted lines in action cinema.", "Sigourney Weaver received an Academy Award nomination for Best Actress — extraordinary for a science fiction action film in 1986.", "The power loader vs Queen Alien finale ('Get away from her, you BITCH!') is one of the greatest climactic fights in film history.", "Aliens introduced the Alien Queen — a massive practical puppet operated by 14-16 puppeteers that remains one of cinema's greatest creature effects.", "Michael Biehn (Corporal Hicks), Lance Henriksen (Bishop), and Carrie Henn (Newt) round out the central cast alongside Weaver.", "Cameron's script was written in just 90 days. He reportedly wrote 'ALIENS' on a whiteboard and added an 'S' — explaining the title's commercial thinking.", "The 'motion tracker' sequences (beeps getting faster as aliens approach) created unbearable tension using only sound and actor reactions.", "Aliens grossed $183 million worldwide (on $18.5M budget) and won Oscars for Sound Effects Editing and Visual Effects."]),
    ("Alien 3 (1992)", ["Alien 3 was David Fincher's directorial debut — though he's largely disowned it due to extreme studio interference throughout production.", "The film controversially kills Hicks and Newt in the opening credits, negating Aliens' happy ending and enraging many fans.", "The prison planet setting (Fiorina 'Fury' 161) populated entirely by male convicts with religious fanaticism gave the film a bleak, oppressive atmosphere.", "Alien 3 went through multiple scripts (space station, wooden planet, Earth-set) before settling on the prison concept. William Gibson wrote an early rejected draft.", "Sigourney Weaver shaved her head for the role, and her Ripley is stripped of all weapons — returning to the first film's survival horror rather than Aliens' action.", "The alien in Alien 3 gestates inside a dog (theatrical) or ox (Assembly Cut), giving it quadrupedal movement different from previous Xenomorphs.", "Fincher's 'Assembly Cut' (released in 2003) adds 30 minutes and is generally considered the superior version, restoring character development and subplots.", "Charles Dance plays Clemens, the prison doctor and Ripley's only ally — his sudden death (mid-sentence) is one of the franchise's most shocking moments.", "The film's ending — Ripley sacrificing herself by falling into molten lead while a chestburster erupts — was intended as a definitive conclusion to her story.", "Alien 3 was a commercial disappointment ($159M on $50M budget) and critically divisive, but has been significantly reappraised as a bold, nihilistic entry."]),
    ("Alien: Resurrection (1997)", ["Alien: Resurrection was written by Joss Whedon, who later said Fox 'did everything wrong that could be done' with his script.", "Sigourney Weaver returns as a clone of Ripley (Ripley 8) — mixed with Xenomorph DNA, giving her acid blood, enhanced strength, and an alien 'empathy.'", "Jean-Pierre Jeunet (Amélie, Delicatessen) directed, bringing a distinctly French visual sensibility to the franchise — more baroque and darkly humorous.", "Winona Ryder plays Call, revealed to be a synthetic person (android) sympathetic to humanity — a twist on the franchise's usual android characters.", "The 'underwater swimming Xenomorphs' sequence was filmed in a massive tank and represents one of the franchise's most technically ambitious set pieces.", "The Newborn — a human/alien hybrid born from the Queen — is the film's most controversial creation, both visually disturbing and divisive among fans.", "Ron Perlman plays Johner, a mercenary, and provides much of the film's dark comedy ('Who were you expecting, Snow White?').", "The basketball scene (Weaver making an over-the-shoulder shot for real) was an unscripted moment the actress nailed on take 4.", "Resurrection grossed $161M worldwide but received mixed reviews. It's generally considered the weakest of the original four films.", "The film set 200 years after Alien 3, aboard the military research vessel USM Auriga where scientists clone Ripley to extract the Queen embryo."]),
    ("Prometheus (2012)", ["Prometheus (2012) was Ridley Scott's return to the Alien universe after 33 years, focusing on the 'Engineers' (Space Jockeys) who created both humans and Xenomorphs.", "Michael Fassbender's performance as the android David is the film's highlight — a synthetic fascinated by creation who becomes dangerously autonomous.", "Noomi Rapace plays Elizabeth Shaw, a scientist searching for humanity's creators who discovers they intended to destroy us with biological weapons.", "The film's opening sequence (an Engineer sacrificing himself to seed life on Earth) is one of the most beautiful and enigmatic sequences in sci-fi.", "Prometheus grossed $403 million worldwide on a $130 million budget — commercially successful despite divisive audience reception.", "The 'black goo' (pathogen) is the film's central mystery — a biological weapon that mutates organic matter in unpredictable, horrifying ways.", "The cesarean scene (Shaw removing an alien embryo from herself using an automated surgery pod) is one of the franchise's most intense body horror sequences.", "Guy Pearce plays elderly Peter Weyland under heavy prosthetics — seeking the Engineers to grant him immortality, only to be killed by one.", "Prometheus deliberately raises philosophical questions (who created us? why?) without answering them — frustrating some audiences but fascinating others.", "The film connects to Alien through the Engineers' ship (matching the derelict from 1979) but takes place on a different planet (LV-223, not LV-426)."]),
    ("Alien: Covenant (2017)", ["Alien: Covenant bridges Prometheus and Alien, revealing that David (the android) created the Xenomorphs by experimenting with the Engineers' black goo.", "Michael Fassbender plays dual roles — David (from Prometheus) and Walter (a newer, more 'controlled' synthetic) — and the film's best scenes are between them.", "The film reveals David destroyed the Engineers' civilization by bombing their homeworld with the pathogen — committing genocide out of contempt for his creators.", "Katherine Waterston plays Daniels, the Covenant ship's terraforming chief who becomes the final girl in a more conventional Alien-style second half.", "The 'backburster' scene (a Neomorph erupting from a man's spine) updates the chestburster concept while creating a new, faster creature variant.", "Danny McBride plays Tennessee, the Covenant's pilot — cast against type in a dramatic role that he performs with surprising emotional depth.", "Covenant grossed $240 million on $97M budget — profitable but below Prometheus's $403M, leading Fox to shelve Scott's planned sequel.", "The film's twist ending (David replacing Walter and taking control of the colonist ship with 2,000 sleeping humans to experiment on) is deeply disturbing.", "Scott intended Covenant as the second of a trilogy leading to the original Alien, but Disney's acquisition of Fox halted those plans.", "The Xenomorph's origin as David's creation was controversial — some fans preferred the alien remaining mysterious rather than being a synthetic's science project."]),
    ("Christine (1983)", ["Christine (1983) was directed by John Carpenter and adapted from Stephen King's novel about a 1958 Plymouth Fury with a murderous supernatural personality.", "The film stars Keith Gordon as Arnie Cunningham, a nerdy teenager who becomes increasingly possessive and sinister after purchasing the car.", "John Stockwell plays Dennis Guilder, Arnie's best friend who watches helplessly as Christine corrupts his personality.", "Christine's self-repair scenes (crumpled metal popping back into shape, fire extinguishing itself) were achieved by crushing cars and running film in reverse.", "Carpenter took the directing job immediately after The Thing's commercial failure, needing a 'safe' project. He shot Christine in just 7 weeks.", "The Plymouth Fury used in the film was actually a combination of 23 separate vehicles (some Plymouth Belvederes and Savoys painted to match).", "Harry Dean Stanton plays Detective Junkins investigating the deaths, bringing character-actor gravitas to a relatively small role.", "Christine's 'birth' scene (rolling off the assembly line in 1957, already killing a worker) establishes that the car was evil from creation — not cursed later.", "The soundtrack features wall-to-wall 1950s rock and roll (Buddy Holly, Eddie Cochran, etc.) that plays on Christine's radio — the car's 'voice.'", "Christine grossed $21 million on a $10 million budget — modest but profitable. It's considered one of the better King adaptations of the 1980s.", "The novel's POV structure (first-person from Dennis) was dropped for the film. Carpenter instead shows the transformation more objectively.", "Carpenter's score (co-composed with Alan Howarth) blends synthesizers with the diegetic 50s rock, creating an eerie contrast between eras.", "The film was shot in Los Angeles (standing in for suburban Pennsylvania) with many night sequences on actual streets.", "Christine's headlights serve as her 'eyes' — Carpenter frames the car watching, following, and expressing emotion through light alone.", "The car's possessiveness over Arnie mirrors an abusive relationship — Christine destroys anyone who threatens her 'bond' with him.", "King wrote the novel while it was being adapted — Carpenter worked from an early manuscript, making changes as King's final version diverged.", "The climax (crushing Christine with a bulldozer at a gas station) was filmed with real cars being destroyed — no miniatures.", "Robert Prosky plays Darnell, the garage owner who houses Christine. His death (squeezed by the car inside his own garage) is claustrophobic and brutal.", "Christine represents the dark side of American car culture — the obsessive restoration hobby taken to a literally deadly extreme.", "The final shot (a piece of Christine twitching in a junkyard) implies the car will rebuild itself — evil can't be permanently destroyed."]),
]:
    ingest_film(film, facts)

elapsed = time.time() - start_time
slack_post(
    f":white_check_mark: *Horror Films Batch 2 Complete!*\n"
    f"  Total facts ingested: {count:,}\n"
    f"  Failed: {failed}\n"
    f"  Duration: {elapsed/60:.1f}m"
)
log(f"DONE: {count} facts, {elapsed/60:.1f}m")
