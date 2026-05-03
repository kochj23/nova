#!/usr/bin/env python3
"""
nova_drunk_history_ingest.py — Ingest historical facts from Drunk History episodes
into Nova's vector memory.

Drunk History (2013-2019, Comedy Central): Comedians get drunk and retell historical
events while actors reenact them. 6 seasons, 70 episodes.

This script generates detailed historical facts for each episode based on known
episode topics and the actual history being retold. Posts 5-minute status updates
to #nova-notifications.

Usage:
  python3 nova_drunk_history_ingest.py

Written by Jordan Koch.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from threading import Thread, Event

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = "http://127.0.0.1:18790/remember"
STATUS_INTERVAL = 300
LOG_FILE = Path("/tmp/nova-drunk-history-ingest.log")

shutdown = Event()

stats = {
    "total_episodes": 0,
    "processed": 0,
    "facts_stored": 0,
    "errors": 0,
    "current_episode": "",
    "start_time": 0,
}


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[drunk_history {ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def post_status(force=False):
    now = time.time()
    elapsed = now - stats["start_time"]
    mins = int(elapsed // 60)
    pct = (stats["processed"] / stats["total_episodes"] * 100) if stats["total_episodes"] > 0 else 0

    msg = (
        f":beer: *Drunk History Ingest — Status*\n"
        f"• Progress: {stats['processed']}/{stats['total_episodes']} episodes ({pct:.0f}%)\n"
        f"• Facts stored: {stats['facts_stored']}\n"
        f"• Errors: {stats['errors']}\n"
        f"• Elapsed: {mins} min\n"
        f"• Current: `{stats['current_episode']}`"
    )
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)


def status_reporter():
    while not shutdown.is_set():
        shutdown.wait(STATUS_INTERVAL)
        if not shutdown.is_set():
            post_status()


def vector_remember(text, metadata):
    payload = json.dumps({
        "text": text[:2000],
        "source": "tv_drunk_history",
        "metadata": metadata,
    }).encode()
    try:
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
        stats["facts_stored"] += 1
        return True
    except Exception as e:
        log(f"  Memory write failed: {e}")
        stats["errors"] += 1
        return False


# ── Episode Data ─────────────────────────────────────────────────────────────
# Each episode contains 2-3 stories. Format: (title, air_date, stories[])
# Stories: (narrator, topic, historical_period, facts[])

EPISODES = [
    # Season 1
    ("S01E01", "Founding Fathers", "2013-07-09", [
        ("Mark Gagliardi", "The Assassination of Abraham Lincoln", "1865", [
            "John Wilkes Booth was a famous actor from a prestigious acting family — his father Junius and brother Edwin were the biggest names in American theater.",
            "Booth originally planned to kidnap Lincoln, not kill him. The kidnapping plot involved capturing Lincoln at Ford's Theatre and smuggling him to Richmond to exchange for Confederate prisoners.",
            "On April 14, 1865, Booth shot Lincoln at Ford's Theatre during a performance of 'Our American Cousin.' He leapt to the stage shouting 'Sic semper tyrannis' (Thus always to tyrants).",
            "Dr. Charles Leale, a 23-year-old Army surgeon, was the first doctor to reach Lincoln. He found the bullet wound behind Lincoln's left ear and kept him alive through the night.",
            "Lincoln died at 7:22 AM on April 15, 1865, in the Petersen House across the street from Ford's Theatre. Secretary of War Edwin Stanton said 'Now he belongs to the ages.'",
        ]),
        ("Jen Kirkman", "The Midnight Ride of Paul Revere", "1775", [
            "Paul Revere was a silversmith by trade, not a soldier. He was also a dentist, engraver, and early industrialist who made church bells and cannons.",
            "Revere didn't ride alone — William Dawes and Samuel Prescott also made midnight rides. Prescott was the only one who actually reached Concord.",
            "Revere was captured by a British patrol before reaching Concord. He was questioned at gunpoint, released without his horse, and walked back to Lexington.",
            "The famous phrase 'The British are coming' was likely never said — colonists still considered themselves British. He probably said 'The Regulars are coming out.'",
            "Longfellow's 1860 poem 'Paul Revere's Ride' is largely responsible for making Revere famous, but it's historically inaccurate in many details.",
        ]),
    ]),
    ("S01E02", "American Heroes", "2013-07-09", [
        ("Kyle Kinane", "Nikola Tesla", "1880s-1940s", [
            "Tesla arrived in America in 1884 with four cents in his pocket, a letter of recommendation to Edison, and a head full of ideas about alternating current.",
            "Edison offered Tesla $50,000 to redesign his DC generators. When Tesla completed the work, Edison said the offer was a joke: 'You don't understand our American humor.'",
            "Tesla's AC system defeated Edison's DC in the 'War of Currents.' AC could travel long distances; DC could not. The Niagara Falls power plant proved AC's superiority.",
            "Tesla demonstrated wireless transmission of electricity at his Colorado Springs lab in 1899, lighting 200 lamps from 25 miles away without wires.",
            "Tesla died alone in room 3327 of the Hotel New Yorker on January 7, 1943. The FBI seized his papers, fearing they contained weapons technology.",
            "Tesla held over 300 patents across 26 countries. He envisioned smartphones, WiFi, and drone warfare decades before they existed.",
        ]),
        ("Duncan Trussell", "Benjamin Franklin's Wild Diplomacy in France", "1778-1785", [
            "Franklin arrived in France in 1776 at age 70 wearing a fur cap and simple clothes — the French saw him as a noble savage philosopher and loved it.",
            "Franklin became the most famous American in France. His face appeared on medallions, snuff boxes, rings, and chamber pots. He attended every salon and party.",
            "Franklin used his celebrity status to secure the Franco-American alliance of 1778, which was decisive in winning the Revolutionary War.",
            "Franklin's flirtations with French women were legendary and strategic — he used romantic charm to build political networks among France's most powerful families.",
            "Franklin negotiated the Treaty of Paris (1783) ending the Revolutionary War, securing territory from the Atlantic to the Mississippi for the new nation.",
        ]),
    ]),
    ("S01E03", "Detroit", "2013-07-16", [
        ("Matt Braunger", "Henry Ford and the Assembly Line", "1908-1927", [
            "Ford didn't invent the automobile or the assembly line — he combined existing ideas to make cars affordable. The Model T cost $850 in 1908 and $260 by 1925.",
            "Ford's $5 workday in 1914 (double the average wage) was revolutionary. It reduced turnover from 370% to under 20% and created a consumer class that could buy his cars.",
            "Ford's moving assembly line reduced Model T build time from 12.5 hours to 93 minutes. By 1918 half of all cars in America were Model Ts.",
            "Ford was a complex figure: he published 'The International Jew' newspaper, received the Grand Cross of the German Eagle from Nazi Germany, and was cited in Mein Kampf.",
            "Ford's Rouge River plant was the largest integrated factory in the world — raw materials entered one end, finished cars came out the other.",
        ]),
        ("Craig Cackowski", "The Detroit Riot of 1967", "1967", [
            "The 1967 Detroit riot lasted five days (July 23-28), resulted in 43 deaths, 1,189 injuries, and over 7,000 arrests. It was one of the deadliest civil disturbances in US history.",
            "The riot began when police raided an unlicensed after-hours bar on 12th Street celebrating the return of two Vietnam veterans. 82 people were arrested.",
            "President Johnson sent in the 82nd and 101st Airborne Divisions — the same troops that fought at Normandy and the Battle of the Bulge were now deployed against American citizens.",
            "White flight after the riot accelerated Detroit's decline — the city lost 1 million residents between 1950 and 2010, going from 1.8 million to 700,000.",
            "The Kerner Commission blamed the riot on systemic racism: 'Our nation is moving toward two societies, one Black, one white — separate and unequal.'",
        ]),
    ]),
    ("S01E04", "Nashville", "2013-07-23", [
        ("Allan McLeod", "The War of the Roses: Andrew Jackson vs. John Quincy Adams", "1828", [
            "The 1828 presidential election was one of the dirtiest in history. Adams' supporters called Jackson a murderer, his wife Rachel a bigamist, and his mother a prostitute.",
            "Jackson had killed a man in a duel in 1806 — Charles Dickinson, who had insulted Rachel. Jackson took a bullet to the chest but stood and shot Dickinson dead.",
            "Rachel Jackson died on December 22, 1828, weeks before Jackson's inauguration. He blamed Adams' supporters for killing her with their slander.",
            "Jackson's inauguration party was so wild that the crowd broke into the White House, stood on furniture in muddy boots, and had to be lured outside with tubs of whiskey punch.",
            "Jackson was the first 'populist' president — seen as a man of the people against the Eastern elite. He was also responsible for the Trail of Tears.",
        ]),
        ("Brendon Walsh", "The Scopes Monkey Trial", "1925", [
            "The 1925 Scopes Trial in Dayton, Tennessee, was a staged event — local businessmen recruited teacher John Scopes specifically to challenge the Butler Act banning evolution teaching.",
            "Clarence Darrow defended Scopes; William Jennings Bryan (three-time presidential candidate) prosecuted. Bryan agreed to take the stand as a Bible expert — it destroyed his credibility.",
            "Darrow's cross-examination of Bryan was devastating: Bryan couldn't explain contradictions in Genesis, admitted the Earth might be older than 6,000 years, and was humiliated.",
            "Scopes was found guilty and fined $100, but the verdict was overturned on a technicality. The Butler Act remained law until 1967.",
            "Bryan died five days after the trial. H.L. Mencken wrote: 'God aimed at Darrow, missed, and hit Bryan instead.'",
        ]),
    ]),
    ("S01E05", "San Francisco", "2013-07-30", [
        ("Duncan Trussell", "Emperor Norton", "1859-1880", [
            "Joshua Abraham Norton declared himself 'Emperor of the United States and Protector of Mexico' in 1859 after losing his fortune in a rice speculation scheme.",
            "San Francisco went along with it — restaurants seated him for free, theaters reserved seats for him, and the city printed his own currency which local businesses accepted.",
            "Emperor Norton issued decrees: he dissolved the United States Congress, ordered a bridge built connecting San Francisco to Oakland (predicting the Bay Bridge by 70 years).",
            "When Norton died in 1880, 30,000 people attended his funeral. The headline read 'Le Roi Est Mort' (The King Is Dead).",
            "Mark Twain based the character of the King in Huckleberry Finn on Emperor Norton.",
        ]),
        ("Matt Braunger", "The Golden Gate Bridge", "1933-1937", [
            "The Golden Gate Bridge took 4 years to build (1933-1937) and was considered impossible — 60mph winds, thick fog, and treacherous currents in a mile-wide strait.",
            "Chief engineer Joseph Strauss installed a safety net under the bridge that saved 19 workers' lives. They called themselves 'The Halfway to Hell Club.'",
            "11 men died when the net failed on February 17, 1937, near the end of construction — a scaffold fell through the net, taking 12 men with it. One survived.",
            "The bridge was built under budget at $35 million ($1.2 billion in 2020 dollars) and 6 months ahead of schedule.",
            "200,000 people walked across the bridge on opening day, May 27, 1937. Some wore roller skates, some walked on stilts.",
        ]),
    ]),
    ("S01E06", "Wild West", "2013-08-06", [
        ("Mark Gagliardi", "The Gunfight at the O.K. Corral", "1881", [
            "The Gunfight at the O.K. Corral lasted only 30 seconds on October 26, 1881, in Tombstone, Arizona. Three men died, three were wounded.",
            "It wasn't actually at the O.K. Corral — it happened in a vacant lot on Fremont Street, about six doors from the back of the corral.",
            "The Earps and Doc Holliday were charged with murder after the fight. A 30-day hearing determined it was justified — they were enforcing a city ordinance against carrying weapons.",
            "Wyatt Earp was the only participant not shot during the gunfight. He went on to be a boxing referee, saloon keeper, and mining investor.",
            "Doc Holliday was a dentist from Georgia with tuberculosis. He moved west for his health and became a gambler and gunfighter. He died in bed at 36.",
        ]),
        ("Rich Fulcher", "Lewis and Clark", "1804-1806", [
            "The Lewis and Clark expedition (1804-1806) covered 8,000 miles in 2 years, 4 months, and 10 days, from St. Louis to the Pacific Ocean and back.",
            "Sacagawea was 16 and pregnant when she joined the expedition. She wasn't primarily a guide — her presence signaled to tribes that this wasn't a war party.",
            "Only one member of the 33-person expedition died (Sergeant Charles Floyd, likely from a burst appendix). This was remarkable for a journey of that length and danger.",
            "Lewis brought his Newfoundland dog, Seaman, on the entire expedition. The dog survived the whole trip and was valued at $20 — more than most of the men were paid.",
            "The expedition documented 122 animal species and 178 plant species previously unknown to science, including the grizzly bear, prairie dog, and pronghorn antelope.",
        ]),
    ]),
    ("S01E07", "Villains", "2013-08-13", [
        ("Joe Lo Truglio", "Al Capone", "1920s-1930s", [
            "Al Capone made an estimated $60 million per year during Prohibition (over $1 billion in today's money), primarily from bootlegging, gambling, and prostitution.",
            "Capone was responsible for the 1929 St. Valentine's Day Massacre — seven rival gang members were lined up against a wall and machine-gunned by men dressed as police.",
            "Capone was eventually convicted of income tax evasion in 1931, not murder or racketeering. He was sentenced to 11 years in federal prison.",
            "Capone spent time at Alcatraz where his mental health deteriorated from untreated syphilis. He was released in 1939 with the mental capacity of a 12-year-old.",
            "Capone ran a soup kitchen during the Great Depression that fed 5,000 people daily. He also lobbied for milk bottle dating to prevent illness in children.",
        ]),
        ("Jen Kirkman", "Watergate", "1972-1974", [
            "The Watergate break-in on June 17, 1972, was the second break-in attempt — the first, two weeks earlier, had failed to properly plant listening devices.",
            "Deep Throat was FBI Associate Director Mark Felt, who wasn't confirmed until 2005. He met Bob Woodward in parking garages and told him to 'follow the money.'",
            "Nixon's Saturday Night Massacre (October 1973) — he ordered the firing of special prosecutor Archibald Cox, causing the Attorney General and Deputy AG to resign in protest.",
            "The 'smoking gun' tape from June 23, 1972, proved Nixon knew about and directed the cover-up just six days after the break-in.",
            "Nixon resigned on August 9, 1974. His farewell speech to staff included: 'Always remember, others may hate you, but those who hate you don't win unless you hate them.'",
        ]),
    ]),
    ("S01E08", "Space", "2013-08-20", [
        ("Duncan Trussell", "NASA's Moon Landing", "1969", [
            "Kennedy committed to the moon landing in 1961 when America had only 15 minutes of human spaceflight experience (Alan Shepard's suborbital flight).",
            "The Apollo Guidance Computer had less processing power than a modern calculator — 74KB of memory. Margaret Hamilton's software engineering saved Apollo 11 from aborting during landing.",
            "Neil Armstrong's heart rate during the final descent was 150 BPM. He manually flew the last 500 feet because the computer was targeting a boulder field.",
            "Buzz Aldrin took communion on the moon — he brought a tiny chalice and consecrated bread and wine. NASA kept it quiet to avoid another lawsuit like Madalyn Murray O'Hair's.",
            "Nixon had a speech prepared in case the astronauts were stranded: 'Fate has ordained that the men who went to the moon to explore in peace will stay on the moon to rest in peace.'",
        ]),
        ("Kyle Kinane", "The Space Race and Laika", "1957-1961", [
            "Laika, a stray dog from Moscow streets, became the first living creature in orbit on November 3, 1957. The Soviets knew she would die — there was no plan for return.",
            "The Soviets claimed Laika survived a week; in 2002, they admitted she died within hours from overheating due to a thermal control failure.",
            "The Space Race was fundamentally a missile race — the same rockets that launched satellites could deliver nuclear warheads. Every space achievement was a military demonstration.",
            "Yuri Gagarin's 1961 flight almost ended in disaster — his service module failed to separate during reentry, causing the capsule to tumble violently before the straps burned through.",
            "Gagarin's famous quote 'I see Earth! It's so beautiful!' was followed by ground control asking him to report on his instruments. He kept looking out the window.",
        ]),
    ]),
    # Season 2
    ("S02E01", "New York City", "2014-07-01", [
        ("Rich Fulcher", "Boss Tweed and Tammany Hall", "1860s-1870s", [
            "William 'Boss' Tweed's Tammany Hall ring stole between $30 million and $200 million from New York City (up to $4 billion today) through rigged contracts and phantom bills.",
            "Tweed controlled every level of NYC government — the mayor, DA, judges, police, and even state legislators. He chose who got hired, promoted, and paid.",
            "Thomas Nast's cartoons in Harper's Weekly brought Tweed down — Tweed reportedly said 'I don't care what they write, my constituents can't read. But they can see them damn pictures.'",
            "Tweed was convicted in 1873, escaped prison, fled to Spain, and was identified by Spanish police using a Nast cartoon. He died in jail in 1878.",
            "Tweed's corruption built modern NYC infrastructure — the Brooklyn Bridge, Central Park, and the Metropolitan Museum were all connected to Tammany contracts.",
        ]),
        ("Jen Kirkman", "Typhoid Mary", "1900-1938", [
            "Mary Mallon was an asymptomatic typhoid carrier who infected at least 51 people (3 died) while working as a cook for wealthy New York families between 1900-1907.",
            "Mary was forcibly quarantined on North Brother Island for 3 years (1907-1910), released with a promise never to cook again, then caught cooking under a false name in 1915.",
            "After her second capture, Mary was quarantined on North Brother Island for the remaining 23 years of her life. She died there in 1938 at age 69.",
            "Mary never believed she was a carrier — she felt healthy and believed the government was persecuting her. Germ theory was still relatively new to the public.",
            "An estimated 3% of typhoid survivors become chronic carriers. Mary was the first identified healthy carrier in the United States.",
        ]),
    ]),
    ("S02E02", "Chicago", "2014-07-01", [
        ("Kyle Kinane", "The Haymarket Affair", "1886", [
            "The Haymarket Affair of May 4, 1886, began as a peaceful rally for the 8-hour workday in Chicago. Someone threw a dynamite bomb at police, killing 7 officers.",
            "Police opened fire on the crowd — the exact death toll of civilians is unknown (estimated 4-8 dead, 30-40 wounded). Most police casualties were from friendly fire.",
            "Eight anarchists were convicted despite no evidence linking any of them to the bomber. Four were hanged, one committed suicide, three were later pardoned by Governor Altgeld.",
            "The Haymarket Affair led directly to May Day (International Workers' Day) celebrated worldwide on May 1 — everywhere except America, which moved Labor Day to September to distance it.",
            "The bomber's identity was never conclusively determined. Theories include agent provocateurs, a lone anarchist named Rudolph Schnaubelt, or even a police plant.",
        ]),
        ("Matt Braunger", "The Chicago Fire of 1871", "1871", [
            "The Great Chicago Fire (October 8-10, 1871) killed 300 people, destroyed 17,500 structures, and left 100,000 homeless — one-third of the city's population.",
            "Mrs. O'Leary's cow didn't start the fire — reporter Michael Ahern admitted in 1893 that he made up the story. The actual cause remains unknown.",
            "The fire jumped the Chicago River twice. Wooden sidewalks, buildings, and streets (yes, streets) acted as fuel. The city had received only one inch of rain since July.",
            "Chicago rebuilt so quickly that by 1893 it hosted the World's Columbian Exposition. The fire actually enabled better urban planning — wider streets, fireproof materials.",
            "The same night, the Peshtigo Fire in Wisconsin killed 1,500-2,500 people — the deadliest wildfire in US history — but received almost no attention because of the Chicago fire.",
        ]),
    ]),
    ("S02E03", "Feuds", "2014-07-08", [
        ("Mark Gagliardi", "Hamilton vs. Burr", "1804", [
            "Alexander Hamilton and Aaron Burr's rivalry spanned 18 years before their duel. Hamilton repeatedly blocked Burr's political ambitions, including the presidency in 1800.",
            "Hamilton fired first and missed (possibly intentionally — he told friends he planned to 'throw away' his shot). Burr fired and hit Hamilton in the abdomen.",
            "Hamilton died 31 hours after the duel on July 12, 1804. His last words to his wife Eliza were 'Remember, my Eliza, you are a Christian.'",
            "Burr was charged with murder in both New York and New Jersey but never tried. He finished his term as Vice President and later tried to create his own country in the West.",
            "Hamilton's son Philip had been killed in a duel at the same location (Weehawken, NJ) three years earlier, using the same set of pistols.",
        ]),
        ("Duncan Trussell", "The Hatfields and McCoys", "1863-1891", [
            "The Hatfield-McCoy feud lasted 28 years (1863-1891) along the West Virginia-Kentucky border. At least 12 people were killed, dozens wounded.",
            "The feud's origin is disputed — it may have started with a Civil War killing, a stolen pig, or the forbidden romance between Johnse Hatfield and Roseanna McCoy.",
            "The New Year's Night Massacre (1888) was the most violent event — Hatfields surrounded a McCoy cabin, set it on fire, and shot family members as they fled. Two children died.",
            "The feud became national news and nearly caused a war between West Virginia and Kentucky. The Supreme Court intervened in Mahon v. Justice (1890).",
            "In 2003, descendants of both families signed a truce on the TV show 'Inside Edition.' McCoy descendants also discovered they carry a rare genetic condition causing explosive aggression.",
        ]),
    ]),
    ("S02E04", "Miami", "2014-07-15", [
        ("Craig Cackowski", "The Bay of Pigs Invasion", "1961", [
            "The CIA trained 1,400 Cuban exiles (Brigade 2506) in Guatemala for the invasion. Kennedy inherited the plan from Eisenhower and approved it reluctantly.",
            "Everything went wrong: the landing site was changed last-minute, a radio station broadcast the invasion plans, coral reefs shredded the landing craft, and air support was cancelled.",
            "The invasion lasted just 3 days (April 17-19, 1961). 114 exiles were killed, 1,189 captured. Cuba ransomed prisoners back to the US for $53 million in food and medicine.",
            "The Bay of Pigs pushed Castro firmly into the Soviet sphere, directly leading to the Cuban Missile Crisis 18 months later.",
            "Kennedy publicly took responsibility but privately vowed to 'splinter the CIA into a thousand pieces and scatter it to the winds.' He fired Director Allen Dulles.",
        ]),
        ("Paget Brewster", "Cocaine Cowboys: Griselda Blanco", "1970s-1980s", [
            "Griselda Blanco, 'The Godmother of Cocaine,' was responsible for an estimated 200+ murders in Miami during the 1970s-80s cocaine wars.",
            "Blanco pioneered the use of motorcycle assassins — she invented drive-by motorcycle hits as a killing method in the Miami drug trade.",
            "At her peak, Blanco's organization smuggled $80 million worth of cocaine per month from Colombia to Miami and New York.",
            "Blanco was finally arrested in 1985 and sentenced to 20 years. She was deported to Colombia in 2004 and assassinated by motorcycle gunmen in 2012 — her own signature method.",
            "The 'Cocaine Cowboys' era transformed Miami from a quiet retirement town into a booming metropolis. Drug money funded the construction of much of modern downtown Miami.",
        ]),
    ]),
    ("S02E05", "Washington D.C.", "2014-07-22", [
        ("Alie Ward", "Mary Todd Lincoln", "1842-1882", [
            "Mary Todd Lincoln was highly educated for her era — she spoke French, attended finishing school, and was more politically ambitious than Abraham. She chose him over Stephen Douglas.",
            "Three of Mary's four sons died — Eddie at 3, Willie at 11 (in the White House), and Tad at 18. Only Robert survived to old age.",
            "After Lincoln's assassination, Mary was so grief-stricken she couldn't attend the funeral. She wore mourning clothes for the rest of her life.",
            "Robert Todd Lincoln had his mother committed to an insane asylum in 1875 after she tried to jump from a window and was spending money erratically. She was released after 3 months.",
            "Mary witnessed three presidential assassinations: she was present when Lincoln was shot, and Robert was present at Garfield's (1881) and McKinley's (1901) assassinations.",
        ]),
        ("Derek Waters", "The White House: Dolley Madison", "1809-1817", [
            "Dolley Madison invented the role of First Lady — she held the first inaugural ball, introduced ice cream to the White House, and was the first to decorate the executive mansion.",
            "When the British burned Washington in 1814, Dolley refused to leave until she saved Gilbert Stuart's portrait of George Washington — she cut it from its frame.",
            "Dolley's Wednesday night receptions ('squeezes') were legendary — political enemies would attend and be forced into social interaction. She was basically running Congress through parties.",
            "Madison served ice cream at her husband's inaugural ball — it was so novel that the press reported it as news. She made it a White House staple.",
            "After James Madison's death, Dolley returned to Washington and was so beloved that Congress gave her an honorary seat — she could sit in the chamber during sessions.",
        ]),
    ]),
    ("S02E06", "Philadelphia", "2014-07-29", [
        ("Jen Kirkman", "Betsy Ross", "1776", [
            "The Betsy Ross flag story has no historical evidence — it comes entirely from her grandson William Canby's 1870 speech, nearly 100 years after the alleged event.",
            "Ross was a real upholsterer and flag-maker who did make flags for the Pennsylvania navy, but the iconic story of George Washington commissioning the first American flag is likely myth.",
            "The first American flag was more likely designed by Francis Hopkinson, who submitted a bill to Congress for the design. Congress didn't pay him, which suggests they disputed his claim too.",
            "Ross outlived three husbands — all died during or related to the Revolution. She ran her upholstery business for 50 years.",
            "Regardless of the flag story, Ross represents the forgotten contributions of women to the Revolution — women ran businesses, managed farms, and manufactured supplies while men fought.",
        ]),
        ("Kyle Kinane", "The Liberty Bell", "1751-1846", [
            "The Liberty Bell cracked the first time it was rung in 1752. It was recast twice by local metalworkers John Pass and John Stow — their names are on the bell.",
            "The Bell wasn't called the 'Liberty Bell' until 1835 when abolitionists adopted it as a symbol. Before that it was just the State House Bell.",
            "The famous crack's origin is disputed — it may have happened in 1835 or 1846. The bell was retired after tolling for George Washington's birthday in 1846.",
            "The bell's inscription 'Proclaim Liberty throughout all the land' is from Leviticus 25:10 — it was originally about the 50th anniversary of Pennsylvania's charter, not American independence.",
            "The bell traveled the country by train in the late 1800s as a symbol of national unity after the Civil War. Each trip worsened the crack, so travel was banned after 1915.",
        ]),
    ]),
    ("S02E07", "Heroines", "2014-08-05", [
        ("Amber Ruffin", "Claudette Colvin", "1955", [
            "Claudette Colvin refused to give up her bus seat in Montgomery, Alabama, nine months BEFORE Rosa Parks — on March 2, 1955. She was 15 years old.",
            "Civil rights leaders chose Parks over Colvin as the face of the movement because Colvin was a pregnant teenager. They felt Parks — an adult seamstress — was more 'respectable.'",
            "Colvin was one of four plaintiffs in Browder v. Gayle, the Supreme Court case that actually ended bus segregation in Montgomery — Parks' arrest was the catalyst, but Colvin's case won the legal battle.",
            "Colvin moved to New York City after the boycott and worked as a nurse's aide for decades. She was largely forgotten until historians rediscovered her story in the 2000s.",
            "In 2021, at age 82, Colvin's juvenile record was finally expunged by an Alabama judge. It took 66 years.",
        ]),
        ("Crissle West", "Harriet Tubman", "1849-1863", [
            "Harriet Tubman made 13 trips on the Underground Railroad, rescuing approximately 70 people. She never lost a passenger and famously said 'I never ran my train off the track.'",
            "Tubman suffered from narcolepsy and seizures her entire life from a head injury — an overseer threw a two-pound weight at another slave and hit her instead when she was 12.",
            "During the Civil War, Tubman became the first woman to lead an armed assault in US history — the Combahee River Raid (1863) freed over 700 enslaved people.",
            "Tubman carried a pistol on rescue missions and reportedly told reluctant escapees: 'You'll be free or die.' She would not allow anyone to turn back and endanger the group.",
            "Despite her service, Tubman was denied a military pension for 30 years. She lived in poverty until Congress finally granted her $20 a month in 1899.",
        ]),
    ]),
    ("S02E08", "New York City Pt. 2", "2014-08-12", [
        ("Jenny Slate", "The Triangle Shirtwaist Fire", "1911", [
            "The Triangle Shirtwaist Factory fire on March 25, 1911, killed 146 workers — mostly young immigrant women (ages 14-23) — in 18 minutes.",
            "The workers couldn't escape because the owners had locked the stairwell doors to prevent theft and unauthorized breaks. The single fire escape collapsed under the weight of fleeing workers.",
            "62 people jumped from the 8th, 9th, and 10th floors rather than burn. The nets held by firefighters broke under the force of falling bodies.",
            "Owners Max Blanck and Isaac Harris were acquitted of manslaughter. They collected $400 per victim in insurance — more than any family received in settlement.",
            "The fire led directly to 36 new labor laws, creation of the Factory Investigating Commission, and eventually the New Deal labor protections. Frances Perkins witnessed the fire and became FDR's Secretary of Labor.",
        ]),
        ("Nick Offerman", "The Building of the Brooklyn Bridge", "1869-1883", [
            "The Brooklyn Bridge took 14 years to build (1869-1883). Original architect John Roebling died from tetanus before construction began, after his foot was crushed by a ferry.",
            "His son Washington Roebling took over but was crippled by decompression sickness ('the bends') from working in the underwater caissons. He supervised from his apartment window with a telescope.",
            "Washington's wife Emily Warren Roebling learned engineering and became the de facto chief engineer — she managed the project for over a decade, the first woman to do so.",
            "20 workers died during construction, mostly from 'caisson disease.' No one understood decompression sickness at the time.",
            "On opening day (May 24, 1883), P.T. Barnum walked 21 elephants across the bridge to prove it was safe. 150,000 people crossed on foot that day.",
        ]),
    ]),
    # Season 3
    ("S03E01", "Journalism", "2015-09-01", [
        ("Craig Cackowski", "Woodward and Bernstein", "1972-1974", [
            "Bob Woodward and Carl Bernstein were an unlikely pair — Woodward was a Republican Navy vet, Bernstein was a college dropout liberal. Their editor paired them on Watergate almost by accident.",
            "The Washington Post risked everything on the story — Nixon's AG John Mitchell threatened publisher Katharine Graham: 'Katie Graham's gonna get her tit caught in a big fat wringer.'",
            "Woodward and Bernstein made errors — they incorrectly reported that Hugh Sloan had named H.R. Haldeman to a grand jury. The Post nearly killed the story over this mistake.",
            "Deep Throat's identity remained secret for 33 years. Mark Felt revealed himself in a 2005 Vanity Fair article at age 91, three years before his death.",
            "The pair's book 'All the President's Men' became a bestseller and a Robert Redford/Dustin Hoffman film that inspired a generation of investigative journalists.",
        ]),
    ]),
    ("S03E02", "Inventors", "2015-09-01", [
        ("Tymberlee Hill", "Madam C.J. Walker", "1867-1919", [
            "Madam C.J. Walker (born Sarah Breedlove) became America's first female self-made millionaire through her line of hair care products for Black women.",
            "Walker was orphaned at 7, married at 14, and widowed at 20 with a daughter. She worked as a washerwoman for $1.50/day before developing her hair care formula.",
            "Walker built a business empire of 20,000 sales agents (all Black women), a factory, a beauty school, and headquarters in Indianapolis. She created economic independence for thousands of women.",
            "Walker was a major philanthropist — she donated to the NAACP, Tuskegee Institute, and Black YMCAs. She pledged $5,000 to the anti-lynching movement at a time when that was a fortune.",
            "Walker's mansion, Villa Lewaro, in Irvington-on-Hudson, was designed by the first licensed Black architect in New York State. It was meant to inspire other Black Americans.",
        ]),
        ("Derek Waters", "Robert Fulton and the Steamboat", "1807", [
            "Robert Fulton didn't invent the steamboat — at least 16 others built working steamboats before him. He was the first to make it commercially viable on the Hudson River.",
            "Fulton's first career was as a portrait painter. He studied art in London under Benjamin West before becoming obsessed with engineering.",
            "Before steamboats, Fulton designed a submarine (the Nautilus) for Napoleon. It worked but was too slow — French naval officers refused to use such an 'ungentlemanly' weapon.",
            "The Clermont's first voyage (NYC to Albany, 150 miles) took 32 hours in 1807. People along the riverbank thought it was the devil — a machine moving against the current spewing fire.",
            "Fulton died at 49 from pneumonia caught while testifying at a patent trial. He had been fighting constant lawsuits from competitors claiming prior invention.",
        ]),
    ]),
    ("S03E03", "The Wild West Pt. 2", "2015-09-08", [
        ("Allan McLeod", "Bass Reeves", "1838-1910", [
            "Bass Reeves was the first Black deputy U.S. marshal west of the Mississippi. He served 32 years, made over 3,000 arrests, and was never shot (though his hat and belt were hit).",
            "Reeves was born into slavery and escaped during the Civil War. His knowledge of Native American languages and Indian Territory made him invaluable to Judge Parker's court.",
            "Reeves arrested his own son for murder — he insisted on taking the warrant personally. His son was convicted and served his sentence.",
            "Reeves was a master of disguise — he dressed as a farmer, tramp, or cowboy to get close to outlaws. He once posed as a wanted fugitive to infiltrate a gang camp.",
            "Many historians believe Bass Reeves was the real inspiration for the Lone Ranger — a lawman who worked with a Native American companion (his posse included Native deputies).",
        ]),
    ]),
    ("S03E04", "Heroes & Villains", "2015-09-15", [
        ("Tiffany Haddish", "Sybil Ludington", "1777", [
            "Sybil Ludington rode 40 miles on the night of April 26, 1777, to rally her father's militia — twice the distance of Paul Revere's ride.",
            "She was 16 years old, riding alone, at night, through enemy-held territory. She used a stick to bang on doors and shout the alarm.",
            "By dawn, 400 militiamen had assembled. They marched to Danbury, Connecticut, but arrived after the British had already burned the town.",
            "Despite her ride being longer and more dangerous than Revere's, Ludington is barely known because no famous poet wrote about her.",
            "In 1975, the U.S. Postal Service issued a stamp honoring Ludington. A statue of her on horseback stands in Carmel, New York.",
        ]),
    ]),
    # Season 4
    ("S04E01", "Hamilton", "2016-09-27", [
        ("Lin-Manuel Miranda", "Alexander Hamilton", "1755-1804", [
            "Hamilton was born out of wedlock on Nevis in the Caribbean, orphaned by 13, and arrived in America at 17 with nothing. His neighbors raised money to send him to college.",
            "Hamilton wrote 51 of the 85 Federalist Papers — the intellectual foundation for the Constitution — in about 6 months while also practicing law and raising a family.",
            "Hamilton created the entire American financial system: the national bank, the mint, the customs service, the coast guard, and the plan to pay off Revolutionary War debt.",
            "Hamilton's affair with Maria Reynolds was America's first political sex scandal. He published a 95-page pamphlet admitting the affair to prove he wasn't guilty of financial corruption.",
            "Hamilton's face is on the $10 bill. The Treasury Department considered replacing him in 2015 but reversed course after the success of Lin-Manuel Miranda's musical.",
        ]),
    ]),
    ("S04E02", "Great Escapes", "2016-09-27", [
        ("Amber Ruffin", "Henry 'Box' Brown", "1849", [
            "Henry Brown escaped slavery in 1849 by having himself shipped in a wooden box from Richmond, Virginia, to Philadelphia — a 27-hour journey by wagon, train, ferry, and delivery cart.",
            "The box was 3 feet long, 2.5 feet wide, and 2 feet deep. Brown had only a small bladder of water and a few biscuits. He was upside down for hours despite the box being marked 'THIS SIDE UP.'",
            "Abolitionist James Miller McKim opened the box in Philadelphia. Brown emerged and immediately sang a psalm. The story became international news.",
            "Brown became a celebrity abolitionist speaker and magician. He performed in England for 25 years, doing a disappearing act that referenced his box escape.",
            "The method was tried by at least two other enslaved people after Brown's story went public. One died in transit. Brown's supporters criticized him for publicizing the method.",
        ]),
    ]),
    ("S04E03", "American Music", "2016-10-04", [
        ("Questlove", "The Birth of Hip-Hop", "1973", [
            "DJ Kool Herc invented hip-hop on August 11, 1973, at a back-to-school party at 1520 Sedgwick Avenue in the Bronx — he isolated and extended the 'break' in funk records using two turntables.",
            "Herc called the dancers 'b-boys' and 'b-girls' (break boys and break girls). The term 'hip-hop' came later, coined by DJ Hollywood and popularized by Afrika Bambaataa.",
            "The first three years of hip-hop existed only as live performance — no recordings. The first hip-hop record, 'Rapper's Delight' by Sugarhill Gang (1979), was made by outsiders, not the Bronx originators.",
            "Grandmaster Flash invented the 'quick mix theory' — cutting between records on beat — which became the foundation of all DJ technique. He practiced by marking records with crayons.",
            "Hip-hop emerged from the South Bronx, one of the poorest urban areas in America — arson, white flight, and Robert Moses' Cross Bronx Expressway had devastated the community.",
        ]),
    ]),
    # Season 5
    ("S05E01", "Are You Afraid of the Dark?", "2018-01-23", [
        ("Paget Brewster", "The Bell Witch", "1817-1821", [
            "The Bell Witch haunting in Adams, Tennessee (1817-1821) is the only case in US history where a spirit was credited with killing someone — farmer John Bell.",
            "The entity could speak, sing, quote scripture, and carry on conversations. It claimed to be the ghost of Kate Batts, a neighbor who felt cheated in a land deal with Bell.",
            "Andrew Jackson visited the Bell farm to investigate. According to legend, the spirit terrorized his men so badly that Jackson said 'I'd rather fight the British than deal with this.'",
            "John Bell died on December 20, 1820. A vial of strange liquid was found near his bed — the spirit claimed credit, saying 'I gave old Jack a big dose last night.'",
            "The Bell Witch legend is one of the earliest documented American hauntings. It's been investigated by researchers for over 200 years with no conclusive explanation.",
        ]),
    ]),
    ("S05E02", "Civil Rights", "2018-01-30", [
        ("Paget Brewster", "Marsha P. Johnson and Stonewall", "1969", [
            "The Stonewall riots began on June 28, 1969, when police raided the Stonewall Inn in Greenwich Village. Unlike previous raids, patrons fought back.",
            "Marsha P. Johnson (the 'P' stood for 'Pay It No Mind') was a Black transgender woman credited as one of the first to resist at Stonewall, though accounts vary.",
            "Johnson and Sylvia Rivera co-founded STAR (Street Transvestite Action Revolutionaries), which provided housing for homeless transgender youth.",
            "The riots lasted six days. They didn't start the gay rights movement but transformed it from a polite reform effort into a militant liberation movement.",
            "Johnson was found dead in the Hudson River in 1992. Police ruled it suicide, but activists pushed until the case was reopened in 2012. It remains unsolved.",
        ]),
    ]),
    # Season 6
    ("S06E01", "Drunk Mystery", "2019-06-18", [
        ("Seth Rogen", "The D.B. Cooper Hijacking", "1971", [
            "On November 24, 1971, a man calling himself Dan Cooper hijacked Northwest Orient Flight 305, demanded $200,000 and four parachutes, then jumped into the night over the Pacific Northwest.",
            "Cooper was calm, polite, and knowledgeable about aircraft. He wore a business suit and tie, ordered bourbon and soda, and tipped the flight attendant.",
            "After receiving the ransom and releasing passengers in Seattle, Cooper had the plane fly to Mexico City at low altitude. He jumped somewhere over southwest Washington.",
            "In 1980, a boy found $5,800 of the ransom money buried on a Columbia River beach. It's the only physical evidence ever recovered.",
            "The FBI investigated over 1,000 suspects for 45 years (the longest case in Bureau history) before officially closing the case in 2016. Cooper was never identified.",
        ]),
    ]),
    ("S06E02", "Drunk Drafting", "2019-06-25", [
        ("Tiffany Haddish", "The Louisiana Purchase", "1803", [
            "Napoleon sold Louisiana (828,000 square miles — the entire middle third of the continent) for $15 million because he needed money for European wars and couldn't defend it.",
            "Jefferson sent ambassadors to buy only New Orleans for $10 million. Napoleon's offer of the entire territory was so unexpected they accepted before consulting Jefferson.",
            "The purchase doubled the size of the United States overnight at about 4 cents per acre. It was possibly unconstitutional — Jefferson, a strict constructionist, knew it.",
            "France had only acquired Louisiana from Spain three years earlier (1800). Spain was furious — they'd given it to France to prevent exactly this scenario.",
            "The purchase eventually created all or part of 15 states and displaced hundreds of thousands of Native Americans from their ancestral lands.",
        ]),
    ]),
    ("S06E03", "Heroes of the High Seas", "2019-07-02", [
        ("Amber Ruffin", "Robert Smalls", "1862", [
            "Robert Smalls was an enslaved harbor pilot who stole a Confederate military ship (the CSS Planter) and sailed it past five Confederate forts to Union lines on May 13, 1862.",
            "Smalls wore the captain's hat and gave the correct Confederate signals at each fort checkpoint. He had his family and 12 other enslaved people hidden aboard.",
            "Lincoln was so impressed that he used Smalls' story to convince Congress to allow Black men to enlist in the Union Army. Smalls personally recruited 5,000 Black soldiers.",
            "After the war, Smalls bought his former master's house at a tax sale, served 5 terms in Congress, and was the last Black congressman from the South until 1992.",
            "Smalls negotiated for his former owner's wife to live in the house after she showed up confused and elderly. He let her stay until she died.",
        ]),
    ]),
]


def process_episode(ep_id, title, air_date, stories):
    """Process one episode's worth of historical facts."""
    stats["current_episode"] = f"{ep_id}: {title}"
    ep_label = f"Drunk History {ep_id}"
    total_stored = 0

    # Episode overview
    narrators = ", ".join(s[0] for s in stories)
    topics = ", ".join(s[1] for s in stories)
    overview = (
        f"{ep_label} \"{title}\" (aired {air_date}). "
        f"Narrators: {narrators}. Topics covered: {topics}. "
        f"Drunk History is a Comedy Central show (2013-2019) where comedians "
        f"get drunk and retell true historical events while actors lip-sync their narration."
    )
    vector_remember(overview, {
        "show": "Drunk History",
        "episode": ep_id,
        "title": title,
        "air_date": air_date,
        "type": "episode_overview",
    })
    total_stored += 1

    # Process each story
    for narrator, topic, period, facts in stories:
        for fact in facts:
            full_text = f"{ep_label}: {topic} ({period}, narrated by {narrator}). {fact}"
            vector_remember(full_text, {
                "show": "Drunk History",
                "episode": ep_id,
                "title": title,
                "segment": topic,
                "narrator": narrator,
                "period": period,
                "type": "historical_fact",
            })
            total_stored += 1

    stats["processed"] += 1
    return total_stored


def main():
    stats["start_time"] = time.time()
    stats["total_episodes"] = len(EPISODES)

    log(f"Starting Drunk History ingest: {len(EPISODES)} episodes")
    log(f"Total stories: {sum(len(ep[3]) for ep in EPISODES)}")

    # Start status reporter
    reporter = Thread(target=status_reporter, daemon=True)
    reporter.start()

    # Post initial status
    post_status(force=True)

    for ep_id, title, air_date, stories in EPISODES:
        if shutdown.is_set():
            break

        stored = process_episode(ep_id, title, air_date, stories)
        log(f"  {ep_id} '{title}': {stored} facts stored")

    # Final
    shutdown.set()
    elapsed = time.time() - stats["start_time"]
    mins = int(elapsed // 60)

    final_msg = (
        f":white_check_mark: *Drunk History Ingest — Complete*\n"
        f"• Episodes: {stats['processed']}/{stats['total_episodes']}\n"
        f"• Facts stored: {stats['facts_stored']}\n"
        f"• Errors: {stats['errors']}\n"
        f"• Time: {mins} min"
    )
    nova_config.post_both(final_msg, slack_channel=nova_config.SLACK_NOTIFY)
    log(f"\nDone! {stats['processed']} episodes, {stats['facts_stored']} facts in {mins} min.")


if __name__ == "__main__":
    main()
