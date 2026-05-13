#!/usr/bin/env python3
"""
nova_horror_classics_batch2.py — Ingest horror classics batch 2 into Nova's vector memory.

Films:
  The Wicker Man (1973), Trick 'r Treat (2009), Fright Night (1985/2011),
  Let The Right One In (2008), A Nightmare on Elm Street (1984),
  Bram Stoker's Dracula (1992)

Written by Jordan Koch.
"""

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = "http://192.168.1.6:18790/remember"

stats = {"stored": 0, "errors": 0}


def log(msg):
    print(f"[horror_batch2 {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def store(text, metadata):
    payload = json.dumps({
        "text": text[:2000],
        "source": "movie_script_horror_classics",
        "metadata": metadata,
    }).encode()
    try:
        req = urllib.request.Request(VECTOR_URL, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        stats["stored"] += 1
    except Exception as e:
        stats["errors"] += 1
        log(f"  Store failed: {e}")


def ingest_wicker_man_1973():
    """The Wicker Man (1973) — Robin Hardy directed, Anthony Shaffer wrote."""
    film = "The Wicker Man (1973)"
    meta = {"film": film, "year": "1973", "director": "Robin Hardy", "writer": "Anthony Shaffer"}

    facts = [
        f"{film}: Robin Hardy directed this British folk horror masterpiece from a screenplay by Anthony Shaffer (Sleuth, The Wicker Man). Shaffer adapted David Pinner's 1967 novel 'Ritual' into the screenplay, though the final film diverges significantly from the source.",
        f"{film}: Edward Woodward stars as Sergeant Neil Howie, a devoutly Christian police officer from the Scottish mainland who travels to the remote island of Summerisle to investigate the disappearance of a young girl named Rowan Morrison.",
        f"{film}: Christopher Lee plays Lord Summerisle, the charismatic aristocratic leader of the island's pagan community. Lee considered this his finest performance and his favorite of all his films, frequently stating it publicly throughout his career.",
        f"{film}: The islanders practice a pre-Christian Celtic pagan religion, worshipping the old gods of harvest and fertility. They celebrate Beltane (May Day) with maypole dances, fertility rites, and animal masks — all presented as normal community life.",
        f"{film}: Sergeant Howie discovers that Rowan Morrison is not dead but being prepared as a sacrifice. The islanders have been deceiving him from the moment he arrived, giving contradictory stories to keep him searching.",
        f"{film}: The film's climax reveals that Howie himself is the intended sacrifice — a virgin, a fool who came of his own free will, and a representative of a king (the law). He meets every requirement of their ritual offering.",
        f"{film}: The iconic final image shows the massive wicker man structure — a giant humanoid effigy made of woven wicker — with Howie trapped inside as it is set ablaze on the clifftop at sunset while the islanders sing 'Sumer Is Icumen In.'",
        f"{film}: Britt Ekland plays Willow, the landlord's daughter who performs a nude dance against Howie's wall to tempt him. Ekland's body double for some shots was a local woman; the scene was partially re-shot. Ekland was unaware of the double for years.",
        f"{film}: The film was produced by British Lion Films. The production was troubled — producer Peter Snell clashed with distributor British Lion. Christopher Lee personally funded promotional efforts and fought for the film's release.",
        f"{film}: The original negative was reportedly destroyed or lost, possibly used as landfill under the M3 motorway. Robin Hardy spent years trying to reconstruct a longer cut from surviving prints.",
        f"{film}: Multiple cuts exist: the original 87-minute theatrical cut, an extended 99-minute 'Director's Cut' assembled from a found print in the early 2000s, and a 'Final Cut' released in 2013 that represents Hardy's preferred version.",
        f"{film}: Paul Giovanni composed the folk-inspired soundtrack, featuring original songs and arrangements of traditional Scottish and English folk music. The music is integral to the pagan atmosphere — songs like 'Corn Rigs,' 'Gently Johnny,' and 'Willow's Song' are now folk horror standards.",
        f"{film}: The Wicker Man is considered one of the three defining films of the folk horror genre, alongside Witchfinder General (1968) and The Blood on Satan's Claw (1971). Mark Gatiss coined this 'the Unholy Trinity' of folk horror.",
        f"{film}: Anthony Shaffer's screenplay functions as a theological debate between Howie's rigid Christianity and Summerisle's joyful paganism. Neither side is presented as wholly right — Howie is sympathetic but intolerant, Summerisle is charming but murderous.",
        f"{film}: Christopher Lee worked for scale (minimum pay) because he believed so strongly in the project. He later said the film's initial commercial failure and studio mishandling was the greatest professional disappointment of his career.",
        f"{film}: The film was shot on location in several Scottish villages including Plockton, Culzean Castle, Anwoth, and Kirkcudbright during autumn 1972. The production dressed locations to appear as late spring for the May Day setting.",
        f"{film}: Ingrid Pitt appears as the librarian/registrar who helps Howie investigate. Diane Cilento plays the schoolteacher Miss Rose, who teaches the children pagan rituals in class while Howie watches in horror.",
        f"{film}: The 2006 remake starring Nicolas Cage is widely considered one of the worst remakes ever made. It replaced the theological complexity with unintentional comedy — the 'not the bees' scene became a viral meme.",
        f"{film}: Lord Summerisle explains that his Victorian grandfather brought the old religion back to the island as a way to motivate the farming community. The religion was a calculated social tool that took on a life of its own across generations.",
        f"{film}: The film's lasting influence extends beyond horror into music (Iron Maiden's album), television (the 'folk horror revival' of the 2010s), and video games (Midsommar, The Ritual, and dozens of others cite it as primary inspiration).",
    ]

    for fact in facts:
        store(fact, {**meta, "type": "script_and_production"})


def ingest_trick_r_treat_2009():
    """Trick 'r Treat (2009) — Michael Dougherty directed and wrote."""
    film = "Trick 'r Treat (2009)"
    meta = {"film": film, "year": "2009", "director": "Michael Dougherty", "writer": "Michael Dougherty"}

    facts = [
        f"{film}: Michael Dougherty wrote and directed this anthology horror film that interweaves four Halloween night stories in the fictional town of Warren Valley, Ohio. All stories occur simultaneously and characters cross between segments.",
        f"{film}: Sam (short for Samhain) is the film's mascot and connective thread — a childlike figure in orange footed pajamas and a burlap sack mask who enforces the 'rules' of Halloween. He appears in every segment as observer or punisher.",
        f"{film}: Sam's true face, revealed when his mask is removed, is a pumpkin-like skull with orange skin and hollow eyes. He carries a bitten lollipop that doubles as a sharpened weapon. He punishes anyone who disrespects Halloween traditions.",
        f"{film}: Anna Paquin stars in the 'Surprise Party' segment as Laurie, a virginal young woman attending her first Halloween party. The twist reveals she and her friends are werewolves hunting for male prey — her 'virginity' refers to her first kill.",
        f"{film}: Dylan Baker plays Steven Wilkins, a school principal who is secretly a serial killer. He poisons a child's candy on Halloween night, then buries the body in his backyard while his young son watches from the window and wants to help carve jack-o'-lanterns.",
        f"{film}: The 'School Bus Massacre' segment tells the story of a school bus driver paid by parents to drive eight mentally disabled children off a cliff into a quarry. The children's ghosts return on Halloween to take revenge on the pranksters who disturb their rest.",
        f"{film}: Brian Cox plays Mr. Kreeg, a Halloween-hating hermit whose segment reveals he was the bus driver from the massacre 30 years ago. Sam attacks him in his home in an extended battle sequence. Kreeg survives by offering Sam candy — following the rules.",
        f"{film}: The opening segment shows a couple returning home — the wife blows out a jack-o'-lantern before midnight, violating Halloween rules. Sam kills her. This bookends the film and establishes the consequences of disrespecting the holiday.",
        f"{film}: Warner Bros. originally scheduled the film for theatrical release in October 2007 but shelved it for two years with no explanation. It was finally released direct-to-DVD/Blu-ray in October 2009 and became a massive cult hit through word of mouth.",
        f"{film}: The film's non-linear storytelling reveals connections between segments as it progresses — characters seen in the background of one story become protagonists of another. The full timeline only becomes clear on rewatch.",
        f"{film}: Dougherty based the film on his 1996 animated short 'Season's Greetings,' which introduced the character of Sam. The short was made while Dougherty was a student at NYU's Tisch School of the Arts.",
        f"{film}: The production design emphasizes Halloween iconography in every frame — jack-o'-lanterns, autumn leaves, costumes, and candy corn are omnipresent. Cinematographer Glen MacPherson shot with warm amber tones throughout.",
        f"{film}: Despite its troubled release, Trick 'r Treat became the best-selling horror DVD of 2009. It has since become a perennial Halloween tradition film, with annual screening events and a dedicated fan following.",
        f"{film}: Sam has become a horror icon with his own comic book series (Trick 'r Treat: Days of the Dead, Trick 'r Treat: Halloween Tales), action figures, and Halloween costumes. He is the unofficial mascot of the horror Halloween season.",
        f"{film}: A sequel, Trick 'r Treat 2, has been in development for years. Dougherty has confirmed a script exists but production has been delayed repeatedly. Legendary Entertainment holds the rights.",
    ]

    for fact in facts:
        store(fact, {**meta, "type": "script_and_production"})


def ingest_fright_night_1985():
    """Fright Night (1985) — Tom Holland wrote and directed. Plus 2011 remake notes."""
    film = "Fright Night (1985)"
    meta = {"film": film, "year": "1985", "director": "Tom Holland", "writer": "Tom Holland"}

    facts = [
        f"{film}: Tom Holland wrote and directed this horror-comedy about teenager Charley Brewster (William Ragsdale) who discovers his new next-door neighbor Jerry Dandridge (Chris Sarandon) is a vampire.",
        f"{film}: Chris Sarandon plays Jerry Dandridge as a sophisticated, seductive predator — charming, handsome, and genuinely dangerous. Sarandon modeled the performance on classic Hammer Horror vampires with a modern 1980s sensibility.",
        f"{film}: Roddy McDowall plays Peter Vincent, a washed-up horror TV host (named after Peter Cushing and Vincent Price) who Charley enlists to help kill the vampire. Vincent is a coward who discovers real courage when confronted with an actual vampire.",
        f"{film}: The film is a love letter to classic vampire cinema. Tom Holland wrote it as a reaction to the slasher-dominated horror of the early 1980s — he wanted to bring vampires back with the same suburban setting as Halloween but with Hammer Horror monster rules.",
        f"{film}: Amanda Bearse plays Amy Peterson, Charley's girlfriend who is targeted by Dandridge because she resembles a woman from his past. Her transformation scene — where her mouth opens impossibly wide — is one of the film's most iconic practical effects.",
        f"{film}: Stephen Geoffreys plays 'Evil Ed' Thompson, Charley's eccentric best friend who is turned by Dandridge. His death scene — reverting from wolf form to human while crying — is unexpectedly poignant. Geoffreys' manic performance became a fan favorite.",
        f"{film}: Jerry Dandridge lives with his 'roommate' Billy Cole (Jonathan Stark), who serves as his daytime protector. Billy's true nature is ambiguous — when killed, he dissolves into sand and bones rather than dying like a normal vampire.",
        f"{film}: The film's practical effects were created by Randall William Cook and Entertainment Effects Group. The vampire transformations use a combination of prosthetics, stop-motion animation, and puppetry — all done in-camera.",
        f"{film}: Brad Fiedel (The Terminator) composed the synthesizer-heavy score. The soundtrack also features 'Come to Me' by April Wine and other 1980s pop tracks that ground the horror in suburban teenage life.",
        f"{film}: Columbia Pictures produced the film on a $9 million budget. It grossed $24.9 million domestically, making it a solid commercial hit. The success led to the 1988 sequel Fright Night Part 2 directed by Tommy Lee Wallace.",
        f"{film}: Tom Holland conceived the film after wondering what would happen if a kid in a Hitchcock-style suburban neighborhood discovered a real monster but nobody believed him — essentially 'Rear Window with a vampire.'",
        f"{film}: The Peter Vincent character was written specifically as a tribute to the aging horror hosts of late-night TV who were disappearing in the 1980s. Roddy McDowall saw the script and campaigned for the role.",
        f"{film}: Chris Sarandon performed many of his own stunts, including the scene where Dandridge scales the side of a building. Sarandon studied movement and physicality to make the vampire feel inhuman despite his human appearance.",
        f"Fright Night (2011 Remake): Colin Farrell starred as Jerry Dandridge in the Craig Gillespie-directed remake. Anton Yelchin played Charley, David Tennant played Peter Vincent (reimagined as a Criss Angel-style Las Vegas magician). Set in suburban Las Vegas.",
        f"Fright Night (2011 Remake): The remake was written by Marti Noxon (Buffy the Vampire Slayer) and produced by DreamWorks/Touchstone. It was a modest commercial disappointment ($41M worldwide on a $30M budget) despite strong reviews, partly due to a poorly received 3D conversion.",
    ]

    for fact in facts:
        store(fact, {**meta, "type": "script_and_production"})


def ingest_let_the_right_one_in_2008():
    """Let The Right One In (2008) — Tomas Alfredson directed."""
    film = "Let The Right One In (2008)"
    meta = {"film": film, "year": "2008", "director": "Tomas Alfredson", "writer": "John Ajvide Lindqvist", "language": "Swedish"}

    facts = [
        f"{film}: Tomas Alfredson directed this Swedish-language vampire film based on John Ajvide Lindqvist's 2004 novel 'Lat den ratte komma in.' Lindqvist also wrote the screenplay, adapting his own work with significant changes from the novel.",
        f"{film}: The story follows Oskar (Kare Hedebrant), a bullied, lonely 12-year-old boy living in a drab apartment complex in Blackeberg, a suburb of Stockholm, who befriends Eli (Lina Leandersson), a mysterious child who moves in next door.",
        f"{film}: Eli is a vampire who appears to be about 12 years old but has existed for over 200 years. When Oskar asks if Eli is a girl, the response is 'I'm not a girl' — in the novel, Eli was originally a boy named Elias who was castrated by a vampire lord centuries ago.",
        f"{film}: Hakan (Per Ragnar) is Eli's aging human caretaker who kills people and drains their blood for Eli. The novel implies he is a pedophile who attached himself to Eli — the film keeps this ambiguous but shows his desperate, codependent devotion.",
        f"{film}: When Hakan is captured after a botched murder, he pours acid on his own face to avoid being identified and connected to Eli. Eli visits him in the hospital; he offers his neck and Eli feeds on him before he falls from the window.",
        f"{film}: The film's central metaphor is that Oskar may be becoming the next Hakan — a human caretaker who will age while Eli remains forever young. The ending is simultaneously romantic and deeply disturbing when viewed through this lens.",
        f"{film}: The pool scene in the climax is one of the most celebrated sequences in modern horror. Oskar is held underwater by bullies who intend to drown him. The camera stays submerged — we see only arms and legs being torn apart above the surface as Eli massacres them.",
        f"{film}: The cat scene (where a room full of cats attacks a woman who has been bitten) was achieved with a combination of real cats and CGI. It is one of the film's few visual effects sequences, and the CGI cats were a source of criticism.",
        f"{film}: Shot during winter in Lulea, northern Sweden (standing in for Blackeberg). The perpetual gray light, snow, and cold are essential to the film's atmosphere — Alfredson insisted on shooting in genuine Swedish winter conditions.",
        f"{film}: Alfredson's directorial approach emphasized restraint and stillness. Violence happens at the edges of the frame or off-screen. Long static shots force the audience to sit with uncomfortable moments rather than cutting away.",
        f"{film}: Lina Leandersson provided Eli's physical performance, but the character's voice was dubbed by Elif Ceylan, a young actress whose voice had the androgynous quality Alfredson wanted. Leandersson was not told about the dubbing until post-production.",
        f"{film}: The title refers to vampire mythology — a vampire must be explicitly invited into a home. In one scene, Oskar tests this by not inviting Eli inside, and Eli begins bleeding from every pore, demonstrating the physical consequences of the rule.",
        f"{film}: The film won the Tribeca Film Festival's top prize and over 75 international awards. It is consistently ranked among the greatest vampire films ever made and revitalized the vampire genre alongside Twilight (2008) from the opposite artistic direction.",
        f"{film}: John Ajvide Lindqvist's novel is considerably darker than the film — it includes subplots about a pedophile ring, more graphic violence, and a deeper exploration of Eli's past. Lindqvist approved of the changes, calling the film 'the best adaptation I could hope for.'",
        f"{film}: The Morse code subplot — Oskar and Eli communicate by tapping on the wall between their apartments — was added by Alfredson. It reinforces their isolation and the intimacy of their connection without words.",
        f"{film}: The American remake, Let Me In (2010), was directed by Matt Reeves (Cloverfield, The Batman). Chloe Grace Moretz played Abby (Eli) and Kodi Smit-McPhee played Owen (Oskar). It was critically praised but underperformed commercially.",
        f"{film}: Cinematographer Hoyte van Hoytema (later known for Interstellar, Dunkirk, Oppenheimer) shot the film. His desaturated, cold palette and natural lighting transformed suburban Stockholm into a landscape of loneliness and quiet menace.",
        f"{film}: The film explores the horror of childhood isolation — both Oskar and Eli are profoundly alone. Oskar collects newspaper clippings about murders (secretly Eli's kills), and his fascination with violence mirrors his impotence against his bullies.",
        f"{film}: Alfredson has said the film is ultimately a love story, not a horror film. The horror elements serve the emotional core — two lonely outcasts finding each other across an impossible divide of mortality and morality.",
        f"{film}: The final shot shows Oskar on a train, the box containing Eli beside him, as they tap Morse code to each other. Oskar smiles. The audience is left to decide whether this is a happy ending or the beginning of Oskar's long, doomed service to an immortal predator.",
    ]

    for fact in facts:
        store(fact, {**meta, "type": "script_and_production"})


def ingest_nightmare_on_elm_street_1984():
    """A Nightmare on Elm Street (1984) — Wes Craven wrote and directed."""
    film = "A Nightmare on Elm Street (1984)"
    meta = {"film": film, "year": "1984", "director": "Wes Craven", "writer": "Wes Craven"}

    facts = [
        f"{film}: Wes Craven wrote and directed this slasher film about Fred 'Freddy' Krueger, a deceased child murderer who kills teenagers in their dreams. If you die in Freddy's dream world, you die in reality.",
        f"{film}: Robert Englund plays Freddy Krueger, who wears a red-and-green striped sweater, a brown fedora, and a glove with four razor blades attached to the fingers. Craven chose red and green because they are the hardest colors for the human eye to process together.",
        f"{film}: Heather Langenkamp stars as Nancy Thompson, the resourceful final girl who discovers she can pull objects — and Freddy — out of the dream world. Nancy's intelligence and agency made her one of horror's most respected heroines.",
        f"{film}: Johnny Depp made his film debut as Glen Lantz, Nancy's boyfriend. He was reportedly brought to the audition by a friend and had no acting ambitions. His death scene — sucked into a bed that erupts with a geyser of blood — is one of the film's most iconic moments.",
        f"{film}: Freddy Krueger's backstory: he was a child killer known as the 'Springwood Slasher' who was released on a legal technicality (someone didn't sign a search warrant). The parents of Elm Street burned him alive in his boiler room. He returned through dreams.",
        f"{film}: Craven based the concept on real newspaper articles about Southeast Asian refugees who died in their sleep after suffering intense nightmares. The phenomenon (Sudden Unexpected Nocturnal Death Syndrome) affected Hmong refugees in the early 1980s.",
        f"{film}: The iconic glove was handmade by Craven's team. He chose knives on fingers because it was the most primal weapon he could imagine — an extension of the hand itself, like animal claws. Multiple gloves were made for different shots.",
        f"{film}: The rotating room — used for the scene where Glen is pulled into the bed and for Tina's death on the ceiling — was a fully functional set that could rotate 360 degrees. The camera was locked to the room, making the actors appear to defy gravity.",
        f"{film}: Tina Gray's death scene, where she is dragged across the ceiling and walls by an invisible force while her boyfriend watches, was achieved in the rotating room. Amanda Wyss performed the scene strapped to the set as it turned.",
        f"{film}: The body bag scene — Nancy sees her dead friend Tina in a body bag being dragged through the school hallway, leaving a trail of blood — was shot with a crew member pulling the bag on a dolly. It's a masterclass in daytime school horror.",
        f"{film}: New Line Cinema was a struggling independent distributor when they acquired the film. Its massive commercial success ($25.5 million domestic on a $1.1 million budget) saved the company, earning New Line the nickname 'The House That Freddy Built.'",
        f"{film}: The film's boiler room — Freddy's domain within the dream world — was inspired by the actual boiler room beneath the house where Craven lived as a child. The industrial, steam-filled environment became Freddy's signature lair.",
        f"{film}: Charles Bernstein composed the minimalist electronic score, anchored by the children's jump-rope chant: 'One, two, Freddy's coming for you / Three, four, better lock your door.' The nursery rhyme motif became synonymous with the franchise.",
        f"{film}: The film's bathtub scene — where Freddy's glove rises between Nancy's legs as she dozes off — was shot in a bottomless tub built over a swimming pool. A crew member in scuba gear operated the glove from below.",
        f"{film}: Craven intended a definitive ending where Nancy defeats Freddy by turning her back and refusing to give him power. Producer Robert Shaye insisted on an ambiguous 'twist' ending implying Freddy survived. This creative disagreement led to Craven's departure from most sequels.",
    ]

    for fact in facts:
        store(fact, {**meta, "type": "script_and_production"})


def ingest_bram_stokers_dracula_1992():
    """Bram Stoker's Dracula (1992) — Francis Ford Coppola directed."""
    film = "Bram Stoker's Dracula (1992)"
    meta = {"film": film, "year": "1992", "director": "Francis Ford Coppola", "writer": "James V. Hart"}

    facts = [
        f"{film}: Francis Ford Coppola directed this lavish adaptation of Bram Stoker's 1897 novel with a screenplay by James V. Hart. Coppola insisted on using the full title 'Bram Stoker's Dracula' to emphasize fidelity to the source material.",
        f"{film}: Gary Oldman plays Count Dracula in multiple forms throughout the film — an ancient warrior in ornate red armor, an elderly aristocrat with a towering hairstyle, a young romantic nobleman, a wolf-creature, a bat-monster, and green mist. Oldman spent up to 8 hours daily in makeup.",
        f"{film}: Winona Ryder plays Mina Murray/Harker, the reincarnation of Dracula's lost love Elisabeta. Anthony Hopkins plays Professor Abraham Van Helsing with scene-stealing intensity. Keanu Reeves plays Jonathan Harker — his English accent was widely criticized.",
        f"{film}: Coppola's central creative mandate was that every visual effect had to be achieved in-camera with no computer-generated imagery. He called it a 'trick film' — using techniques from early cinema: forced perspective, reverse photography, double exposure, shadow puppetry, and miniatures.",
        f"{film}: The shadow puppetry technique — where Dracula's shadow moves independently of his body, reaching for victims, creeping along walls — was achieved by having a separate performer act as the shadow on a parallel set, composited in-camera via split lighting.",
        f"{film}: Roman Coppola (Francis's son) served as second unit director and designed many of the in-camera effects. The production drew inspiration from Georges Melies, F.W. Murnau's Nosferatu, and German Expressionist cinema.",
        f"{film}: Eiko Ishioka designed the costumes and won the Academy Award for Best Costume Design. Her work is deliberately anachronistic and theatrical — Dracula's red armor, the wedding dress that transforms into a burial gown, and Lucy's sleepwalking dress are all iconic.",
        f"{film}: The opening sequence depicts the historical Vlad Dracula (Vlad the Impaler) returning from battle against the Turks in 1462 to find that his wife Elisabeta has committed suicide after receiving a false report of his death. He renounces God and becomes the vampire.",
        f"{film}: The blood effects used real-looking practical blood extensively. The scene where Dracula stabs a cross and it bleeds was achieved with hidden tubes. The chapel scene's river of blood used approximately 500 gallons of stage blood.",
        f"{film}: Tom Waits plays Renfield, the insect-eating asylum inmate who serves Dracula. Waits lost 30 pounds for the role and improvised much of Renfield's manic behavior. His performance is one of the most memorable supporting turns in the film.",
        f"{film}: Sadie Frost plays Lucy Westenra, Mina's best friend who is seduced and transformed by Dracula. Her transformation and staking scene is one of the film's most explicit and disturbing sequences, blending eroticism and horror.",
        f"{film}: Wojciech Kilar composed the sweeping, romantic orchestral score. The 'Love Remembered' theme and the bombastic main title are considered among the finest horror film scores. Annie Lennox's end-credits song 'Love Song for a Vampire' was a top-40 hit.",
        f"{film}: The film was a massive commercial success, grossing $215 million worldwide against a $40 million budget. It won three Academy Awards: Best Costume Design (Eiko Ishioka), Best Sound Effects Editing, and Best Makeup.",
        f"{film}: Coppola directed the actors using an unconventional rehearsal process — he had the cast perform the entire story as a stage play before filming began, emphasizing the theatrical, operatic tone he wanted. This contributed to the heightened performance style.",
        f"{film}: Gary Oldman and Winona Ryder's scenes together were shot with genuine emotional intensity. The love story between Dracula and Mina — which is not in Stoker's novel — became the film's emotional core and reframed Dracula as a tragic romantic figure rather than pure evil.",
    ]

    for fact in facts:
        store(fact, {**meta, "type": "script_and_production"})


def main():
    log("Starting Horror Classics Batch 2 ingest")

    nova_config.post_both(
        ":ghost: *Horror Classics Batch 2 — Starting*\nIngesting script/production facts for 6 films: The Wicker Man (1973), Trick 'r Treat (2009), Fright Night (1985), Let The Right One In (2008), A Nightmare on Elm Street (1984), Bram Stoker's Dracula (1992).",
        slack_channel=nova_config.SLACK_NOTIFY
    )

    ingest_wicker_man_1973()
    log(f"  The Wicker Man (1973): {stats['stored']} facts")

    ingest_trick_r_treat_2009()
    log(f"  Trick 'r Treat (2009): {stats['stored']} facts total")

    ingest_fright_night_1985()
    log(f"  Fright Night (1985): {stats['stored']} facts total")

    ingest_let_the_right_one_in_2008()
    log(f"  Let The Right One In (2008): {stats['stored']} facts total")

    ingest_nightmare_on_elm_street_1984()
    log(f"  A Nightmare on Elm Street (1984): {stats['stored']} facts total")

    ingest_bram_stokers_dracula_1992()
    log(f"  Bram Stoker's Dracula (1992): {stats['stored']} facts total")

    nova_config.post_both(
        f":white_check_mark: *Horror Classics Batch 2 — Complete*\n"
        f"• Facts stored: {stats['stored']}\n"
        f"• Errors: {stats['errors']}\n"
        f"• Films: The Wicker Man (1973), Trick 'r Treat (2009), Fright Night (1985/2011), "
        f"Let The Right One In (2008), A Nightmare on Elm Street (1984), Bram Stoker's Dracula (1992)",
        slack_channel=nova_config.SLACK_NOTIFY
    )
    log(f"Done! {stats['stored']} facts stored, {stats['errors']} errors.")


if __name__ == "__main__":
    main()
