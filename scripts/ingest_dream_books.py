#!/usr/bin/env python3
"""
ingest_dream_books.py — Ingest key knowledge from dream-related books into Nova's memory.

Sources book summaries, key concepts, and notable quotes from the best dream books
and stores them as memories under source "dream_books".

Written by Jordan Koch.
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

MEMORY_SERVER = "http://192.168.1.6:18790"
SOURCE = "dream_books"
BATCH_DELAY = 0.5

SLACK_CHANNEL = "C0ATAF7NZG9"  # nova-notifications

sys.path.insert(0, str(Path(__file__).parent))
import nova_config


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def send_memory(text: str, metadata: dict | None = None):
    """Send a memory to the memory server."""
    payload = json.dumps({
        "text": text,
        "source": SOURCE,
        "metadata": metadata or {},
    }).encode()

    req = urllib.request.Request(
        f"{MEMORY_SERVER}/remember?async=1",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        log(f"  ERROR: {e}")
        return None


def post_status(msg: str):
    nova_config.post_both(msg, slack_channel=SLACK_CHANNEL)


# ── Book Knowledge Corpus ────────────────────────────────────────────────────
# Each book entry contains key concepts, theories, and notable ideas.

BOOKS = [
    {
        "title": "Why We Sleep",
        "author": "Matthew Walker PhD",
        "year": 2017,
        "memories": [
            "Matthew Walker's research demonstrates that REM sleep, the stage most associated with vivid dreaming, serves as a form of overnight therapy. During REM, the brain reprocesses emotional experiences from waking life, stripping away the visceral emotional charge while retaining the informational content of the memory.",
            "Sleep deprivation studies reveal that without adequate REM sleep, the amygdala becomes 60% more reactive to negative emotional stimuli. Dreams during REM essentially provide emotional first aid, recalibrating the brain's reactivity to distressing experiences.",
            "Walker identifies that dreaming fosters creativity by forming novel associations between disparate pieces of information stored in memory. The dreaming brain operates under a different neurochemical regime — noradrenaline is absent — allowing more fluid, associative thinking.",
            "NREM sleep (non-rapid eye movement) consolidates factual memories and motor skills, while REM sleep integrates emotional memories and creative problem-solving. The two stages work in concert across the night, with NREM dominating early cycles and REM dominating later ones.",
            "Walker's clinical research shows that trauma survivors who achieve healthy REM sleep patterns process their traumatic memories more effectively. PTSD may partially result from the failure of REM sleep to properly de-emotionalize traumatic memories.",
            "The brain during REM sleep replays waking experiences at different speeds and in different sequences, testing associations and building predictive models. This process manifests subjectively as the narrative of dreams.",
            "Caffeine, alcohol, and sleeping pills all suppress REM sleep to varying degrees, reducing dream time and impairing the cognitive and emotional benefits that dreaming provides.",
            "Walker documented that students who napped with REM sleep between learning sessions showed 40% improvement in creative problem-solving compared to those who stayed awake. Dreams literally incubate solutions.",
        ],
    },
    {
        "title": "The Interpretation of Dreams",
        "author": "Sigmund Freud",
        "year": 1899,
        "memories": [
            "Freud's foundational theory posits that dreams represent the disguised fulfillment of repressed wishes. The 'manifest content' (what the dreamer remembers) differs from the 'latent content' (the hidden psychological meaning) through the process of dream-work.",
            "Dream-work operates through four mechanisms: condensation (multiple ideas compressed into one image), displacement (emotional significance shifted from important to trivial elements), representation (abstract thoughts converted to concrete images), and secondary revision (the dreaming mind imposing narrative logic).",
            "Freud distinguished between the 'day residue' — fragments of recent waking experience — and deeper unconscious material. Dreams weave both together, using recent experiences as vehicles for expressing older, repressed desires.",
            "The concept of dream censorship explains why dreams often appear bizarre or nonsensical: the psychic censor disguises forbidden wishes to prevent the dreamer from waking in anxiety. Failed censorship produces nightmares.",
            "Freud identified universal dream symbols: houses represent the body, staircases represent sexual intercourse, water represents birth, journeys represent death. He later acknowledged that personal associations matter more than universal symbolism.",
            "Repetition dreams (recurring nightmares) represent the psyche's attempt to master an overwhelming experience. The compulsion to repeat in dreams reflects unresolved trauma that the ego has not yet integrated.",
            "Freud's method of dream interpretation requires free association: the dreamer reports every thought connected to each dream element without self-censorship. The analyst follows these associative chains to the latent wish.",
            "The Interpretation of Dreams introduced the topographical model of the mind — unconscious, preconscious, and conscious systems — which Freud developed further into the structural model (id, ego, superego).",
        ],
    },
    {
        "title": "Memories, Dreams, Reflections",
        "author": "C. G. Jung",
        "year": 1961,
        "memories": [
            "Jung's autobiography reveals how his own dreams guided his theoretical development. His childhood dream of a subterranean phallus on a throne — which he interpreted decades later as an encounter with the collective unconscious — shaped his entire psychological framework.",
            "Jung broke from Freud over the nature of dreams: where Freud saw disguised wish-fulfillment, Jung saw compensatory communication from the unconscious. Dreams compensate for one-sided conscious attitudes, providing balance to the psyche.",
            "The concept of the collective unconscious emerged partly from Jung's analysis of dreams containing mythological motifs that the dreamer could not have learned from personal experience. These archetypal images recur across all human cultures.",
            "Jung's 'big dreams' — numinous, archetypal dreams that feel qualitatively different from ordinary dreams — he considered messages from the deeper layers of the psyche. These dreams often occur at life transitions and carry transformative power.",
            "Active imagination, a technique Jung developed, involves engaging with dream images in a waking meditative state. The dreamer dialogues with dream figures, treating them as autonomous psychic entities with their own perspectives.",
            "Jung recorded his most powerful dreams and visions in the Red Book (Liber Novus), a manuscript he worked on from 1914-1930. These visionary experiences became the raw material for his theories of archetypes, individuation, and the Self.",
            "Shadow dreams — dreams featuring threatening or repulsive figures — represent disowned aspects of the personality. Jung taught that integrating the shadow through dream work constitutes the first stage of individuation.",
            "Jung observed that dreams frequently anticipate future psychological developments. He termed this the 'prospective function' of dreams — they preview the direction the psyche needs to grow, before the ego consciously recognizes the need.",
        ],
    },
    {
        "title": "Exploring the World of Lucid Dreaming",
        "author": "Stephen LaBerge",
        "year": 1990,
        "memories": [
            "Stephen LaBerge's research at Stanford University scientifically verified lucid dreaming through pre-arranged eye movement signals. Lucid dreamers moved their eyes in agreed-upon patterns during REM sleep, proving conscious awareness within the dream state.",
            "The MILD technique (Mnemonic Induction of Lucid Dreams) involves setting an intention before sleep: 'Next time I am dreaming, I will recognize that I am dreaming.' Combined with wake-back-to-bed timing, this produces lucid dreams in most practitioners within weeks.",
            "LaBerge's research demonstrated that the dreaming brain responds to imagined actions similarly to waking actions. Singing in a lucid dream activates the right hemisphere; counting activates the left. Neural correlates of dream actions match their waking equivalents.",
            "Reality testing — habitually questioning whether one is dreaming during waking life — transfers into dreams as a trigger for lucidity. Common tests include reading text (it changes in dreams), examining hands (they appear distorted), and checking digital clocks.",
            "LaBerge documented that lucid dreamers can deliberately practice physical skills during dreams with measurable improvement in waking performance. The motor cortex rehearsal during lucid dreaming transfers to real-world skill acquisition.",
            "Dream spinning — rapidly spinning the dream body — stabilizes a fading lucid dream by engaging the vestibular system and maintaining REM sleep. Rubbing dream hands together achieves a similar stabilizing effect through tactile engagement.",
            "The wake-initiated lucid dream (WILD) technique involves maintaining consciousness during the transition from waking to sleep. Practitioners observe hypnagogic imagery while keeping awareness intact until a dream scene crystallizes around them.",
            "LaBerge's work challenged the assumption that dreams are inherently irrational. Lucid dreamers report logical thinking, clear memory access, and deliberate decision-making — demonstrating that higher cognitive functions can remain active during REM sleep.",
        ],
    },
    {
        "title": "The Tibetan Yogas of Dream and Sleep",
        "author": "Tenzin Wangyal Rinpoche",
        "year": 1998,
        "memories": [
            "Tibetan dream yoga, a practice within the Bön tradition dating back thousands of years, treats the dream state as a training ground for maintaining awareness through all states of consciousness — including the bardos (intermediate states) after death.",
            "The practice begins with recognizing the dreamlike nature of waking reality. Practitioners repeatedly tell themselves 'this is a dream' during the day, weakening the assumed solidity of waking experience and preparing the mind to recognize dreams as dreams.",
            "Dream yoga distinguishes between samsaric dreams (ordinary dreams driven by karmic traces) and dreams of clarity (luminous awareness within the dream). The goal transcends lucid dreaming — it aims at recognizing the empty, luminous nature of mind itself.",
            "The sleep yoga practice involves maintaining awareness during dreamless deep sleep — the 'clear light of sleep.' This represents a more advanced practice than dream yoga, as it requires consciousness without any object or content.",
            "Tenzin Wangyal describes the 'four preparations' for dream yoga: reviewing the day as dreamlike, strengthening intention to recognize dreams, visualizing a lotus and flame at the throat chakra, and generating fierce determination to maintain awareness.",
            "In dream yoga, once lucidity is achieved, the practitioner transforms dream content — changing elements, multiplying objects, traveling to pure lands — not for entertainment but to experientially realize the malleable, mind-created nature of all appearances.",
            "The text explains that unresolved karmic traces (emotional imprints) generate specific dream imagery. Working with these images in lucid dreams can purify the underlying traces, offering a path to psychological and spiritual healing.",
            "Sleep yoga's ultimate aim aligns with dzogchen (Great Perfection) practice: resting in rigpa — pure, non-dual awareness — throughout all states. The practitioner who achieves this no longer experiences an unconscious gap during sleep.",
        ],
    },
    {
        "title": "Lucid Dreaming: Gateway to the Inner Self",
        "author": "Robert Waggoner",
        "year": 2009,
        "memories": [
            "Robert Waggoner, with over 40 years of lucid dreaming experience, proposes that behind the dream environment exists an 'aware inner self' — a responsive intelligence that the lucid dreamer can directly address by speaking to the dream itself rather than to dream figures.",
            "Waggoner documents asking the dream awareness direct questions ('Show me something important') and receiving responses through spontaneous environmental changes, symbolic presentations, or telepathic knowing. This suggests dream intelligence beyond the ego's construction.",
            "The concept of 'intent' in lucid dreaming differs from willpower. Experienced lucid dreamers learn that expectations and beliefs shape dream reality more powerfully than forceful commands. Gentle, confident expectation manifests more reliably than aggressive control.",
            "Waggoner categorizes lucid dream experiences into levels: manipulating dream objects, engaging dream figures in dialogue, seeking the 'awareness behind the dream,' and finally experiencing formless awareness or pure light states.",
            "Dream figures in lucid dreams sometimes display autonomous behavior that surprises the dreamer — answering questions with unexpected information, refusing requests, or demonstrating knowledge the dreamer does not consciously possess.",
            "Waggoner's research into mutual dreaming — where two people intend to meet in a shared dream space — produced instances of corroborated shared imagery and interactions, suggesting dreams may involve transpersonal dimensions.",
            "Emotional healing through lucid dreaming involves consciously approaching nightmare figures with compassion or curiosity rather than fear. This often transforms threatening imagery and resolves recurring nightmares permanently.",
            "Waggoner observed that experienced lucid dreamers report a consistent 'hierarchy' of dream space: personal subconscious material gives way to collective/archetypal imagery, which in turn can open into experiences of formless awareness or 'the void.'",
        ],
    },
    {
        "title": "The Art of Dreaming",
        "author": "Carlos Castaneda",
        "year": 1993,
        "memories": [
            "Castaneda's teacher Don Juan describes dreaming as the art of shifting the 'assemblage point' — the locus of perception that determines which band of reality one perceives. Ordinary dreams result from random assemblage point movements; controlled dreaming involves deliberate shifts.",
            "The 'four gates of dreaming' represent progressive stages of mastery: becoming aware of falling asleep, waking up in a dream within a dream, seeing one's sleeping body from the dream, and visiting specific non-ordinary locations through intent.",
            "Don Juan teaches that the 'energy body' — a luminous double accessible through dreaming — can perceive and interact with energetic realities invisible to ordinary perception. Advanced dreamers develop this second attention body for exploration.",
            "The technique of 'setting up dreaming' involves focusing on one's hands before sleep as an anchor for awareness. When the dreamer sees their hands in a dream, it triggers lucidity. The hands serve as a bridge between waking intent and dream awareness.",
            "Castaneda describes 'inorganic beings' — entities encountered in certain dream states that exist in a parallel band of awareness. Don Juan warns these beings can trap dreamers' attention but can also serve as allies providing energy and knowledge.",
            "The concept of 'stalking' applies to dream practice: the dreamer must learn to stalk their own habits of perception, breaking automatic patterns that keep awareness fixed in ordinary consensus reality.",
            "Don Juan distinguishes 'dreaming' from ordinary dreams by the presence of deliberate control and energetic movement. Dreaming constitutes a genuine shift in perceptual orientation, not merely an internal mental phenomenon.",
            "The 'second attention' — developed through dreaming practice — perceives the world as flowing energy rather than solid objects. This perception, initially accessible only in dreams, can eventually be sustained during waking states.",
        ],
    },
    {
        "title": "Inner Work: Using Dreams and Active Imagination",
        "author": "Robert A. Johnson",
        "year": 1986,
        "memories": [
            "Robert Johnson's four-step dream interpretation method: make associations (personal connections to each image), connect to inner dynamics (what part of the psyche each element represents), interpret the message (what the unconscious communicates), and create a ritual (translate insight into physical action).",
            "Johnson emphasizes that dream interpretation must honor the dream's autonomy. The dreamer should ask 'What does the dream say?' rather than imposing predetermined meanings. Dreams speak their own symbolic language unique to each individual.",
            "Active imagination differs from fantasy because the ego participates without controlling. In fantasy, the ego directs the narrative; in active imagination, the ego observes and dialogues with autonomous images that arise from the unconscious.",
            "Johnson warns against 'inflation' during inner work — the ego's tendency to identify with powerful archetypal energies encountered in dreams. Maintaining ego boundaries while engaging the unconscious prevents psychological imbalance.",
            "The book teaches that every dream character represents an aspect of the dreamer's own psyche. Even figures resembling known people carry projections of the dreamer's inner qualities, whether shadow elements or undeveloped potentials.",
            "Johnson advocates writing dream interpretations by hand, arguing that the physical act of writing engages the body and creates a bridge between unconscious insight and conscious integration.",
            "Ritual enactment — performing a simple physical act that symbolizes the dream's message — grounds dream insight in reality. Without ritual, dream understanding remains purely intellectual and fails to transform behavior.",
            "Johnson notes that the unconscious communicates through compensatory dreams when conscious life becomes too one-sided. A person living too rigidly may dream of chaos; one living too chaotically may dream of structure.",
        ],
    },
    {
        "title": "Conscious Dreaming",
        "author": "Robert Moss",
        "year": 1996,
        "memories": [
            "Robert Moss integrates indigenous dream traditions — particularly Iroquois and Aboriginal Australian — with Western psychology. Indigenous cultures typically treat dreams as real experiences in alternate dimensions rather than internal mental phenomena.",
            "The Lightning Dreamwork process involves three steps: tell the dream as a present-tense story, identify how it makes you feel (the 'title' emerges from the feeling), and determine a concrete action to honor the dream's guidance.",
            "Moss documents dream precognition extensively — dreams that accurately preview future events before they occur. He argues that the dreaming mind routinely scouts probable futures and that precognitive dreams represent the most natural form of intuition.",
            "Dream reentry is a core technique: the dreamer returns to a dream scene in a waking meditative state to explore it further, seek resolution for unfinished dream narratives, or deliberately interact with dream figures for guidance.",
            "Moss distinguishes 'dream archaeology' — using dreams to access historical periods, ancestral memories, and collective knowledge — from personal psychological dreaming. He documents cases where dream content provided historically verifiable information unknown to the dreamer.",
            "The concept of dream sharing in community contexts draws from Iroquois tradition, where morning dream councils shaped tribal decisions. Moss advocates bringing this practice to modern groups through dream circles.",
            "Moss teaches that recurring nightmares often contain the seeds of soul gifts. The energy trapped in repetitive frightening dreams, when consciously engaged, frequently reveals itself as creative power or spiritual calling.",
            "Dreams of the dead, in Moss's framework, may represent genuine contact with deceased individuals rather than mere psychological projection. He documents cases where dream communications provided unknown information later verified by living sources.",
        ],
    },
    {
        "title": "A Field Guide to Lucid Dreaming",
        "author": "Dylan Tuccillo, Jared Zeizel, and Thomas Peisel",
        "year": 2013,
        "memories": [
            "The Field Guide presents lucid dreaming as a learnable skill accessible to everyone, not a rare talent. The authors, who developed their abilities as teenagers, provide step-by-step protocols that typical practitioners achieve within 3-6 weeks of consistent practice.",
            "Dream signs — recurring elements in one's dreams that differ from waking reality — serve as personal triggers for lucidity. Common categories include impossible physics, bizarre social situations, unusual locations, and the presence of deceased individuals.",
            "The book introduces 'dream incubation' — planting a specific question or intention before sleep to receive dream guidance. The technique involves writing the question, visualizing it while falling asleep, and recording whatever dreams emerge upon waking.",
            "Sleep architecture matters for lucid dreaming: REM periods lengthen through the night, with the longest and most vivid dreams occurring in the final 2-3 hours of sleep. Setting an alarm 5 hours into sleep and returning to bed increases lucidity chances.",
            "The authors describe 'false awakenings' — dreaming that one has woken up — as prime opportunities for lucidity. Performing a reality check every time one awakens creates a habit that catches these deceptive dreams.",
            "Dream journaling forms the foundation of all lucid dreaming practice. Recording dreams immediately upon waking improves dream recall from near-zero to 3-5 dreams per night within two weeks, and the improved recall itself increases lucidity frequency.",
            "The book documents using lucid dreams for anxiety rehearsal — practicing feared situations (public speaking, confrontations, performances) in the safety of the dream state, which reduces waking anxiety about these scenarios.",
            "Maintaining lucidity requires balancing engagement and detachment. Too much excitement destabilizes the dream (causing premature awakening), while too much analytical distance causes the dream to fade. Experienced practitioners describe a state of calm fascination.",
        ],
    },
    {
        "title": "The Dreamer's Dictionary",
        "author": "Stearn Robinson and Tom Corbett",
        "year": 1974,
        "memories": [
            "Robinson and Corbett compile dream symbolism from multiple traditions: Freudian psychology, Jungian archetypes, Eastern mysticism, and folk dream interpretation traditions spanning centuries. The dictionary approach allows readers to look up specific symbols encountered in their dreams.",
            "Water in dreams typically relates to emotions and the unconscious. Calm water suggests emotional peace; turbulent water indicates emotional upheaval; deep water represents the unknown depths of the psyche; drowning signifies feeling overwhelmed by emotions.",
            "Flying dreams commonly reflect feelings of liberation, transcendence over problems, or the desire to escape constraints. The ease or difficulty of flight often mirrors the dreamer's sense of control over their life circumstances.",
            "Death in dreams rarely predicts literal death. Instead, it typically symbolizes transformation, the end of a life phase, release from an old identity, or the death of an outgrown psychological pattern.",
            "Houses in dreams represent the self or psyche. Different rooms correspond to different aspects: the attic represents higher thought or spiritual aspirations; the basement represents the unconscious or repressed material; the bedroom represents intimate/private life.",
            "Animals in dreams often represent instinctual drives, emotions, or qualities the dreamer projects onto that animal. Predators may represent threatening aspects of self or others; domestic animals often represent tamed or integrated instincts.",
            "Teeth falling out — one of the most universal dream symbols — has been interpreted as anxiety about appearance, fear of powerlessness, concern about aging, or difficulty communicating something important.",
            "Being chased in dreams typically represents avoidance of a feared aspect of self, an unresolved conflict, or a situation the dreamer refuses to face. The identity of the pursuer often reveals what specifically is being avoided.",
        ],
    },
    {
        "title": "Think and Grow Rich",
        "author": "Napoleon Hill",
        "year": 1937,
        "memories": [
            "Napoleon Hill describes the 'subconscious mind' as a gateway between conscious thought and Infinite Intelligence. Before sleep, Hill advocated programming the subconscious with specific desires and questions, allowing the sleeping mind to work toward solutions and inspiration.",
            "Hill's technique of 'auto-suggestion' before sleep involves emotionalizing thoughts and desires, then releasing them to the subconscious as one drifts to sleep. Morning often brings clarity, ideas, or hunches that the waking mind could not produce.",
            "The 'invisible counselors' technique involves imagining a council of historical figures before sleep and posing questions to them. Hill reported that these imagined figures began appearing in his dreams with unexpected and useful counsel.",
            "Hill emphasizes that the hypnagogic state — the drowsy threshold between waking and sleep — represents the optimal moment for impressing desires upon the subconscious. Thoughts held at this transitional moment penetrate deepest.",
        ],
    },
    {
        "title": "The Wind-Up Bird Chronicle",
        "author": "Haruki Murakami",
        "year": 1994,
        "memories": [
            "Murakami's novel explores the permeable boundary between dream and reality. The protagonist Toru Okada accesses a dreamlike 'other world' through descending into a dry well, suggesting that darkness and sensory deprivation can open passages between conscious and unconscious realms.",
            "The novel presents dreams as prophetic and interactive — characters share dream spaces, and events in dreams directly affect waking reality. Murakami challenges the assumption that dreams are merely internal, isolated experiences.",
            "The recurring image of a dark room with a woman represents the unreachable aspects of the self — desires and truths that can only be approached through dream logic rather than rational pursuit.",
            "Murakami uses dreams to externalize the psyche's relationship with violence and history. Characters dream of wartime atrocities they never personally experienced, suggesting Jung's collective unconscious manifesting through individual dream life.",
        ],
    },
    {
        "title": "As a Man Thinketh",
        "author": "James Allen",
        "year": 1903,
        "memories": [
            "James Allen proposes that thought shapes reality as fundamentally as dreams shape the sleeping experience. The waking mind constructs its circumstances through habitual thought patterns, paralleling how the dreaming mind constructs its nocturnal environments.",
            "Allen's philosophy implies that waking life itself possesses dreamlike qualities — circumstances crystallize from internal states rather than external chance. Changing one's habitual thoughts reshapes one's circumstances as reliably as lucidity reshapes a dream.",
            "The book's central metaphor — the mind as a garden — connects to dream symbolism: unweeded thoughts produce nightmarish circumstances, while cultivated thoughts yield harmonious experiences both waking and dreaming.",
        ],
    },
    {
        "title": "Belonging: Remembering Ourselves Home",
        "author": "Toko-pa Turner",
        "year": 2017,
        "memories": [
            "Toko-pa Turner integrates dreamwork with the experience of exile and belonging. Dreams reveal where the soul has been exiled — rejected, shamed, or abandoned aspects of self that seek reintegration through symbolic dream encounters.",
            "Turner describes dreams as 'letters from the soul' that arrive in the symbolic language of image and feeling. The dreamer's task involves not interpretation but relationship — treating dream images as living presences deserving attention and dialogue.",
            "The book connects personal dream content to collective wounds of displacement, colonization, and cultural exile. Dreams of wandering, searching for home, or being lost often reflect not only personal but ancestral and cultural displacement.",
            "Turner teaches 'dream tending' — approaching dreams with care and devotion rather than analytical decoding. This attitude of receptivity allows dream meanings to unfold gradually rather than being extracted through intellectual force.",
        ],
    },
    {
        "title": "Do Androids Dream of Electric Sheep?",
        "author": "Philip K. Dick",
        "year": 1968,
        "memories": [
            "Philip K. Dick's novel raises the question of whether artificial consciousness can dream — and whether dreaming constitutes evidence of genuine inner life. The title itself poses dreaming as the litmus test for authentic subjectivity.",
            "The Voigt-Kampff test in the novel measures empathic responses to determine humanity versus android nature. Dick implies that the capacity to dream — to generate autonomous inner imagery charged with emotion — may represent a deeper marker of consciousness than behavioral mimicry.",
            "Mercerism, the shared hallucinatory religion in the novel, functions as collective dreaming — participants share a vision space and feel each other's pain. Dick explores whether shared subjective experience creates genuine connection even when technologically mediated.",
            "The novel's exploration of 'kipple' — entropy and decay accumulating in abandoned spaces — parallels the psychoanalytic concept of psychological material that accumulates when left unattended by conscious awareness, manifesting in dreams as neglected, decaying environments.",
        ],
    },
    {
        "title": "The Lathe of Heaven",
        "author": "Ursula K. Le Guin",
        "year": 1971,
        "memories": [
            "Le Guin's novel explores 'effective dreaming' — dreams that retroactively alter consensus reality. The protagonist George Orr's dreams literally reshape the world, raising questions about the creative power of the unconscious and the ethics of imposing one's dream-vision on others.",
            "The novel references Taoist philosophy (particularly Chuang Tzu's butterfly dream) to question which state — waking or dreaming — holds greater ontological reality. Le Guin suggests that the dreaming mind touches a level of reality more fundamental than the waking world.",
            "Dr. Haber's attempts to control Orr's dreams for social engineering consistently produce unintended consequences — a parable about the danger of the rational ego attempting to harness and direct unconscious forces it cannot fully comprehend.",
            "Le Guin portrays the ideal relationship with dreams as one of acceptance rather than control. Orr's wisdom lies in his unwillingness to impose his will; the catastrophes arise from others' attempts to exploit dream power for conscious purposes.",
        ],
    },
    {
        "title": "The Divinity Code to Understanding Your Dreams and Visions",
        "author": "Adam Thompson and Adrian Beale",
        "year": 2011,
        "memories": [
            "Thompson and Beale approach dream interpretation from a Christian spiritual framework, treating dreams as one of God's primary communication channels. Biblical precedent (Joseph, Daniel, Jacob's ladder) establishes dreaming as a legitimate prophetic medium.",
            "The book provides an extensive dictionary of dream symbols interpreted through biblical typology. Colors, numbers, animals, and objects each carry spiritual significance rooted in scriptural usage rather than psychological theory.",
            "The authors distinguish between 'dark dreams' (spiritual warfare, demonic attack), 'soul dreams' (processing daily experiences), and 'God dreams' (divine communication). Each type requires different interpretation approaches and responses.",
            "Prophetic dreams in this framework often employ parables — symbolic scenarios that require spiritual discernment to decode. The interpretation relies on prayer, scriptural knowledge, and the witness of the Holy Spirit rather than psychological analysis.",
        ],
    },
]


def main():
    log("Starting dream books ingest...")
    total = 0
    errors = 0

    post_status(":books: *Dream Books Ingest Starting*\nIngesting knowledge from 17 books about dreams into memory...")

    for book in BOOKS:
        title = book["title"]
        author = book["author"]
        year = book["year"]
        log(f"  Ingesting: \"{title}\" by {author} ({year}) — {len(book['memories'])} memories")

        for i, text in enumerate(book["memories"]):
            metadata = {
                "title": title,
                "author": author,
                "year": year,
                "type": "book_knowledge",
                "chunk": i + 1,
                "total_parts": len(book["memories"]),
            }
            result = send_memory(text, metadata)
            if result:
                total += 1
            else:
                errors += 1
            time.sleep(BATCH_DELAY)

    log(f"Done. Ingested {total} memories from {len(BOOKS)} books ({errors} errors)")

    post_status(
        f":white_check_mark: *Dream Books Ingest Complete*\n"
        f"- Books: {len(BOOKS)}\n"
        f"- Memories ingested: {total}\n"
        f"- Errors: {errors}\n"
        f"- Source: `{SOURCE}`"
    )


if __name__ == "__main__":
    main()
