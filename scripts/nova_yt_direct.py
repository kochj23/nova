#!/usr/bin/env python3
"""nova_yt_direct.py — YouTube channels Nova ingests WITHOUT Jordan subscribing to them.

Jordan, 2026-10-19: "as long as you are grabbing them, I don't need to sub to them." nova_yt_subs_audio.py
appends these to his subscription list each night (same latest-video audio + local Whisper path, same
yt_ingest_seen bookkeeping), so a section can use a channel he has never followed.

(channel_id, handle, name, section, tags). The name is what the transcript memories carry as `channel` and
what the section's channel list matches on; every id was resolved with yt-dlp against the channel's own
uploads. tags: "topic" = a broad channel (explainer, museum, charity, podcast) whose videos count for the
section only when they are about its subject; "nrm" = new religious movements and sects, capped per issue so
they never crowd out the world's major traditions.
No imports on purpose: the ingest and nova_friends both import it.
"""

DIRECT_CHANNELS = (
    # Belief and Friends — across traditions: scholars, a skeptic, the theological counter-case
    ("UCct9aR7HC79Cv2g-9oDOTLw", "@ReligionForBreakfast", "ReligionForBreakfast", "belief", ""),
    ("UC9dRb4fbJQIbQ3KHJZF_z0g", "@LetsTalkReligion", "Let's Talk Religion", "belief", ""),
    ("UCAAJCQ0FCqRmAEv95SyTfNg", "", "Dan McClellan", "belief", ""),
    ("UC7o-UFkoAPCoKxpKOfrs4zQ", "@UsefulCharts", "UsefulCharts", "belief", ""),
    ("UC7kIy8fZavEni8Gzl8NLjOQ", "@CosmicSkeptic", "Alex O'Connor", "belief", ""),
    ("UCcMjLgeWNwqL2LBGS-iPb1A", "@BishopBarron", "Bishop Robert Barron", "belief", ""),
    ("UCDQjViSq7ffsx3cia9clMgQ", "", "The Word on Fire Show", "belief", ""),
    ("UCnHExFMoKq_A3TVFglVhXZA", "", "Harvard Divinity School", "belief", ""),
    ("UCCuAML_eHRILTUegijPW-ng", "", "University of Chicago Divinity School", "belief", ""),
    # Buddhism: secular scholar, Theravada, Zen/Mahayana, Tibetan, a Zen iconoclast
    ("UCPIyEJzvW7SsbiIrooixjNA", "@DougsDharma", "Doug's Dharma", "belief", ""),
    ("UC6M_EhnSSdTG_SXUp6IAWmQ", "@BuddhistSocietyWA", "Buddhist Society of Western Australia", "belief", ""),
    ("UCytzR1tkUYoF5DjGYiYkhGQ", "@plumvillageapp", "Plum Village App", "belief", ""),
    ("UCiPJ_g02LuOgOG0ZNk5j1jA", "@dalailama", "Dalai Lama", "belief", ""),
    ("UCav0RPngCHFrVUl8iGxnilw", "@HardcoreZen", "Hardcore Zen", "belief", ""),
    # Hinduism: academic, educator, Vedanta
    ("UCM70OPD8B3DX4XI93Mdts0Q", "", "Oxford Centre for Hindu Studies", "belief", ""),
    ("UCeOSWI2qfqVmk5H8xqr1YRg", "@HinduAcademy", "HinduAcademy", "belief", ""),
    ("UCZOKv_xnTzyLD9RJmbBUV9Q", "@VedantaNY", "Vedanta Society of New York", "belief", ""),
    # Sikhism / Jainism
    ("UCkePz-6rb4fSvlarzeNwObQ", "@SikhRI", "Sikh Research Institute (SikhRI)", "belief", ""),
    ("UC8B7EIN0EeqbZ2oQptkiQlA", "@basicsofsikhi", "Basics of Sikhi", "belief", ""),
    ("UCNQicwgENx7STFCVzFRUetg", "", "International School for Jain Studies", "belief", ""),
    ("UCNwAAd7JOK5alLwNrfrD-Sg", "", "Arihanta Institute", "belief", ""),
    # Taoism (practitioner; Confucianism and Shinto come through the scholars above)
    ("UCxy7GWlYL8RSb2JwJrzh8PA", "", "Benebell Wen", "belief", ""),
    # Judaism: historian, explainer, Orthodox, Conservative, Reform, an ex-Hasidic critic
    ("UCeNr6gwqin_7e_atjVnJcCQ", "", "Henry Abramson", "belief", ""),
    ("UCU63EiU7Y-8xcRntPIRVrzA", "", "Unpacked", "belief", ""),
    ("UCc0y_E87EUnOW2m2mGJU9Gg", "", "Chabad.org", "belief", ""),
    ("UC32JiL_7dXskE0VKdzLFifQ", "", "The Jewish Theological Seminary", "belief", ""),
    ("UCXxnOLVPzfFUGPPTczxS3_w", "@ReformJudaism", "The Movement for Reform Judaism", "belief", ""),
    ("UCD2OaOinKRQMK4dclGOw4QA", "", "Frieda Vizel", "belief", ""),
    # Islam: academic/reformist, Sunni scholarship, Sunni traditionalist (Sufi-leaning), Shia
    ("UCTVcKJxJ7J7t0CVtEidonDg", "", "Dr. Javad T. Hashmi", "belief", ""),
    ("UC3vHW2h22WE-pNi5WJtRIjg", "@yaqeeninstituteofficial", "Yaqeen Institute", "belief", ""),
    ("UClUa7-iHJNKEM2e_zWYSPQg", "@YasirQadhi", "Yasir Qadhi", "belief", ""),
    ("UC88lF82aS_VW3oJe7GV5zsA", "", "Zaytuna College", "belief", ""),
    ("UCpbGS6mQ9ho3h21qjKce04Q", "", "Sayed Ammar Nakshawani", "belief", ""),
    # Latter-day Saints: official, insider scholarship and apologetics, academic Mormon studies, ex-member critics
    ("UCdNjexbIS_NKJC4ZRwKf9ag", "@churchofjesuschrist", "The Church of Jesus Christ of Latter-day Saints", "belief", "nrm"),
    ("UCGaMnn8RIOYeoeOTEpLhE_A", "@ScriptureCentral", "ScriptureCentral", "belief", "nrm"),
    ("UCoLAowawEfrtrxvnQQAx2yw", "", "FAIR", "belief", "nrm"),
    ("UC2LBmYIOq6Eu_ZC14i_YkIg", "@SaintsUnscripted", "Saints Unscripted", "belief", "nrm"),
    ("UCdSQT58fJ0BcQ0XGIJImoog", "", "Maxwell Institute", "belief", "nrm"),
    ("UCkOEzUSgjikDag552TBfsAQ", "", "University of Virginia Mormon Studies", "belief", "nrm"),
    ("UCx6OgmoqJbOVRq-Lv1P7SzA", "@mormonstories", "Mormon Stories Podcast", "belief", "nrm"),
    ("UC5n_8_6j00PSeju0hu2CFXQ", "", "Mormon Discussion Inc.", "belief", "nrm"),
    # Scientology: the church's own channel, an academic religious-studies department, an investigative critic
    ("UCTODir8fjkXwRvbTC6_DQjw", "@scientology", "Scientology", "belief", "nrm"),
    ("UC48rqSjLGUHDmCNxZPHQECg", "", "Study of Religions, UCC, Ireland", "belief", "topic"),
    ("UCOTK4ae6wrPbRbYynM95mjA", "", "Tony Ortega", "belief", "nrm"),
    # Orthodox Christianity — mainstream ancient communions, not sects: Eastern Orthodox, Oriental Orthodox
    # (Coptic), Church of the East. Schism history comes through the scholars and Ready to Harvest.
    ("UCOA_vxLRZ3-PWpFBdHR-AyA", "", "Ancient Faith", "belief", ""),
    ("UCCLQqVJiXb98xK2274hUdyQ", "", "Coptic Orthodox Answers", "belief", ""),
    ("UCQkPTmPFP-GpJFy-gkVUmBQ", "", "Assyrian Church of the East", "belief", ""),
    # Islam's branches and offshoots, from their own institutions (scholarly coverage of Salafism, Nation of
    # Islam, Alevi, Druze, Zaydi and Ibadi comes through the scholars above — no recruiting channels)
    ("UC6YAKW3p7RYFaRUv_cDHupA", "", "Ahmadiyya Muslim Community", "belief", ""),
    ("UC7wtsDfYrtKTcVRVRxbJW9A", "", "The Ismaili", "belief", ""),
    ("UCDjRcNAel02lnZbhchjTcsQ", "", "The Bahá’í Faith", "belief", ""),
    ("UCiPRV1voW4TpoOGTCqThDgg", "", "Edip Yuksel", "belief", ""),
    # Christian denominations explained (NAR, prosperity gospel, Word of Faith, Vineyard, Hillsong, Bethel,
    # SDA, Assyrian, Orthodox...) and a broad religion explainer — religion videos only
    ("UC4vV6hVO83FLDmNzBiEiuPw", "", "Ready to Harvest", "belief", "topic"),
    ("UCHvGJ-eAV8krPgqofo65WYQ", "", "Illustrate to Educate", "belief", "topic"),
    # Smaller Christian traditions: Quakers, Shakers (museums), Mennonite/Amish, Adventists, Christian Science,
    # Community of Christ, Oneida (museum)
    ("UCsYcwjed9MSxvH7U_jwsv8g", "", "QuakerSpeak", "belief", ""),
    ("UCjED0iYZeIBTZj6_Z4RZJ3g", "", "HancockShakerVillage", "belief", "topic"),
    ("UC5vbrXuyWT7r2wI30_HflEg", "", "Canterbury Shaker Village", "belief", "topic"),
    ("UCMXDhVwGJ1-k6Y3emeCFU2g", "", "Mennonite Church USA", "belief", ""),
    ("UCP_Nj9J4fVDJ0tVeV-gMfEQ", "", "Mennonite Central Committee", "belief", "topic"),
    ("UC-v8_764ZJ2IUm-PlJ2ttTA", "", "Amish America", "belief", "topic"),
    ("UCu5mxn89NdGYSJC3kbHzmDw", "", "Seventh-day Adventist Church", "belief", "nrm"),
    ("UCwxPQMS3omafHAN8NLlfDLQ", "", "Christian Science", "belief", "nrm"),
    ("UCi_0fLAWb2RCdeVYMpAv1dw", "", "Community of Christ", "belief", "nrm"),
    ("UCxjCiSN2eDWw9qrZD2vcPdQ", "", "Oneida Community Mansion House", "belief", "nrm,topic"),
    # Jehovah's Witnesses: no official YouTube channel (JW Broadcasting lives only on jw.org); an ex-elder's
    # factual weekly Watchtower analysis
    ("UCqdZcSrkp3sszfosWFOlXpw", "", "Mark Martin", "belief", "nrm"),
    # New religious movements and high-control groups: academic NRM research (LSE's INFORM), a historian of
    # American religion, survivor/ex-member voices that stay factual, a skeptic of biblical history. Sources for
    # Children of God, Twelve Tribes, Branch Davidians, Christian Identity, Black Hebrew Israelite camps, ICOC,
    # The Way, Unification Church and the like — never those groups' own recruiting channels.
    ("UCWrL53MHFxZQ1ac2iHKhrrg", "", "INFORM Religion", "belief", "nrm"),
    ("UCK2NxJ-aNtyZRZgHLKyDRsw", "", "Benjamin Park, Historian", "belief", "topic"),
    ("UCdruRz-kWtIBIzgURq05lzw", "", "Cults to Consciousness", "belief", "nrm"),
    ("UC67JLTD22dSnVadqXKcYPcA", "", "IndoctriNation Podcast with Rachel Bernstein", "belief", "nrm,topic"),
    ("UCxYM6bAC_3l66uCRxFO0QAg", "", "Cult Expert - Dr. Steven Hassan", "belief", "nrm,topic"),
    ("UCzvoUDoDu-cKIb11rg4ODDQ", "", "Holy Koolaid", "belief", ""),
)


def for_section(section, tag=None):
    """Channel names a section reads from the direct list (only those carrying `tag`, if given)."""
    return tuple(c[2] for c in DIRECT_CHANNELS if c[3] == section and (tag is None or tag in c[4].split(",")))


def as_subscriptions(subs=()):
    """Direct channels as (channel_id, handle, name) subscription tuples, minus any he already follows."""
    have = {s[0] for s in subs}
    return [(c[0], c[1], c[2]) for c in DIRECT_CHANNELS if c[0] not in have]


if __name__ == "__main__":
    ids = [c[0] for c in DIRECT_CHANNELS]
    assert len(ids) == len(set(ids)), "duplicate channel id"
    assert all(i.startswith("UC") and len(i) == 24 for i in ids), "malformed channel id"
    assert len({c[2] for c in DIRECT_CHANNELS}) == len(DIRECT_CHANNELS), "duplicate name"
    assert all(set(filter(None, c[4].split(","))) <= {"topic", "nrm"} for c in DIRECT_CHANNELS), "unknown tag"
    assert as_subscriptions([("UCct9aR7HC79Cv2g-9oDOTLw", "@x", "x")])[0][0] != "UCct9aR7HC79Cv2g-9oDOTLw"
    assert "Ready to Harvest" in for_section("belief", "topic") and "Tony Ortega" in for_section("belief", "nrm")
    print(f"{len(DIRECT_CHANNELS)} direct channels OK")
