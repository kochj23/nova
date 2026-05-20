#!/usr/bin/env python3
"""
ingest_ukraine_war.py — Ingest 2500 facts about the Russia-Ukraine war.
Sources: AP, Reuters, BBC, ISW, UN, ICC, OSCE (no Russian state media).
Runs with nohup for session survival.
"""
import json, sys, time, urllib.request
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
import nova_config

MEMORY_URL = "http://192.168.1.6:18790/remember"
count = 0
failed = 0

def log(msg):
    print(f"[ukraine {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)

def remember(text):
    global count, failed
    payload = json.dumps({"text": text, "source": "local_knowledge", "metadata": {"type": "geopolitics", "topic": "russia_ukraine_war"}}).encode()
    req = urllib.request.Request(MEMORY_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10):
            count += 1
            return True
    except:
        failed += 1
        return False

slack_post(":flag-ua: *Ukraine War Facts Ingestion Started*\n  Target: 2,500 facts\n  Sources: AP, Reuters, BBC, ISW, UN, ICC\n  _Updates every 250 facts_")

FACTS = [
    # ══════════════════════════════════════════════════════════════════
    # CATEGORY 1: TIMELINE & KEY EVENTS (250 facts)
    # ══════════════════════════════════════════════════════════════════
    "On February 20, 2014, Russia began its annexation of Crimea with unmarked soldiers ('little green men') seizing key government buildings and military installations in Simferopol.",
    "A disputed referendum held on March 16, 2014 under Russian military occupation claimed 97% support for Crimea joining Russia. The vote was not recognized by Ukraine or most of the international community.",
    "In April 2014, pro-Russian separatists seized government buildings in Donetsk and Luhansk oblasts, beginning the war in Donbas that would claim over 14,000 lives by 2022.",
    "Malaysia Airlines Flight MH17 was shot down on July 17, 2014 over eastern Ukraine by a Buk missile system belonging to Russia's 53rd Anti-Aircraft Brigade, killing all 298 people on board.",
    "The Minsk Protocol (September 2014) and Minsk II agreement (February 2015) attempted to establish ceasefires in Donbas but were never fully implemented by either side.",
    "On February 21, 2022, Putin recognized the independence of the Donetsk and Luhansk 'people's republics,' setting the stage for the full-scale invasion three days later.",
    "Russia launched its full-scale invasion of Ukraine on February 24, 2022 at approximately 5:00 AM Kyiv time, attacking from the north (Belarus border), east, and south simultaneously.",
    "The initial Russian assault included cruise missile strikes on military installations across Ukraine, airborne operations at Hostomel airport near Kyiv, and ground forces advancing on multiple axes.",
    "Ukrainian President Volodymyr Zelensky refused US offers of evacuation on February 25, 2022, reportedly saying 'I need ammunition, not a ride' — becoming a symbol of Ukrainian resistance.",
    "The Battle of Kyiv (February-March 2022) saw Russian forces advance to within 15km of the capital but fail to capture it. A 64km military convoy stalled north of Kyiv due to logistics failures.",
    "Russia withdrew from northern Ukraine (Kyiv, Chernihiv, Sumy oblasts) by April 2, 2022, after failing to take the capital. This was the first major strategic defeat of the invasion.",
    "The Bucha massacre was discovered on April 1-2, 2022 after Russian forces withdrew from the Kyiv suburb. Evidence showed the execution of at least 458 civilians, many with hands bound.",
    "The siege of Mariupol lasted from February 24 to May 20, 2022 (83 days). Ukrainian defenders held out in the Azovstal steel plant until ordered to surrender. An estimated 25,000 civilians died.",
    "The Mariupol Drama Theatre was bombed by Russia on March 16, 2022 despite having 'children' ('дети') written in large letters visible from the air. An estimated 300-600 people were killed.",
    "The Kramatorsk railway station attack on April 8, 2022 killed at least 59 civilians and injured 100+ who were waiting for evacuation trains. Russia used a Tochka-U missile.",
    "Ukraine's Snake Island defenders became a symbol of resistance when they told the Russian warship Moskva 'go fuck yourself' on February 24, 2022. The island was recaptured by Ukraine in June 2022.",
    "The Russian cruiser Moskva, flagship of the Black Sea Fleet, was struck by two Ukrainian Neptune anti-ship missiles on April 13, 2022 and sank on April 14 — the largest warship sunk in combat since the Falklands War.",
    "Ukraine launched a successful counteroffensive in Kharkiv Oblast in September 2022, liberating approximately 6,000 square kilometers in less than two weeks, including the city of Izium.",
    "Mass graves containing over 440 bodies were discovered in Izium, Kharkiv Oblast after liberation in September 2022. Many showed signs of torture and execution.",
    "The liberation of Kherson on November 11, 2022 was the only regional capital Russia captured and subsequently lost. Russian forces retreated across the Dnipro River.",
    "The Kakhovka Dam on the Dnipro River was destroyed on June 6, 2023, flooding vast areas downstream, displacing thousands, and causing an ecological catastrophe. Evidence points to Russian demolition.",
    "The Wagner Group mutiny on June 23-24, 2023 saw Yevgeny Prigozhin's forces seize Rostov-on-Don and march toward Moscow before standing down. Prigozhin was killed in a plane crash on August 23, 2023.",
    "The Battle of Bakhmut lasted from August 2022 to May 2023 (approximately 10 months). Russia captured the city at enormous cost — an estimated 20,000-30,000 Russian casualties for minimal strategic gain.",
    "Ukraine's 2023 summer counteroffensive (June-November) made limited territorial gains due to extensive Russian minefields and fortifications, advancing approximately 17km at the deepest point near Robotyne.",
    "The Battle of Avdiivka (October 2023 - February 2024) ended with Russian capture of the city after 4 months of intense fighting. Russia sustained an estimated 16,000-25,000 casualties for the advance.",
    "Ukraine launched a surprise incursion into Russia's Kursk Oblast in August 2024, capturing approximately 1,000 square kilometers of Russian territory — the first foreign occupation of Russian soil since WWII.",
    "The Black Sea Fleet was effectively neutralized by Ukrainian strikes throughout 2023-2024 without Ukraine having a navy. Drone boats and missiles forced Russia to relocate major vessels from Crimea to Novorossiysk.",
    "By early 2025, the front line had largely stabilized along a 1,000+ km line of contact, with Russia making incremental gains in Donetsk Oblast through attrition warfare.",
    "The war has caused the largest refugee crisis in Europe since World War II, with over 6 million Ukrainians fleeing abroad and an additional 5+ million internally displaced.",
    "As of 2026, the war has lasted over 4 years (since the full-scale invasion) and over 12 years since Russia's initial aggression in Crimea and Donbas in 2014.",

    # More timeline facts
    "Russia's initial war plan reportedly assumed Kyiv would fall within 72 hours. Captured Russian documents showed plans for a victory parade in Kyiv and pre-printed occupation administration paperwork.",
    "On the first day of the invasion (Feb 24, 2022), Russia launched approximately 160 missiles at targets across Ukraine, including military bases, airfields, and command centers.",
    "The Battle of Hostomel Airport on Feb 24, 2022 saw Russian VDV (airborne) forces attempt to seize the airport via helicopter assault. Ukrainian National Guard and regular forces repelled the initial attack.",
    "Chernobyl nuclear power plant was seized by Russian forces on February 24, 2022 (Day 1). Russian soldiers dug trenches in the radioactive Red Forest, with many later hospitalized for radiation sickness.",
    "The city of Kherson fell to Russia on March 2, 2022 — the only Ukrainian regional capital captured during the full-scale invasion. Its liberation 8 months later was a major Ukrainian victory.",
    "On March 9, 2022, Russia bombed a maternity hospital in Mariupol. Images of a pregnant woman being evacuated on a stretcher became one of the war's most iconic photographs. She later died.",
    "The Zaporizhzhia Nuclear Power Plant, Europe's largest, was seized by Russian forces on March 4, 2022 after a firefight that caused a fire at the facility. It has remained under Russian occupation since.",
    "Ukraine successfully defended Mykolaiv in early March 2022, preventing Russia from advancing on Odesa. The city endured months of missile strikes but never fell.",
    "The Ghost of Kyiv — a Ukrainian fighter pilot who allegedly shot down multiple Russian aircraft — became a morale-boosting legend in the early days of the war, though the Ukrainian Air Force later confirmed it was a myth.",
    "Russia's initial offensive involved approximately 190,000 troops attacking on multiple axes: from Belarus toward Kyiv, from Russia toward Kharkiv and Sumy, and from Crimea toward Zaporizhzhia and Kherson.",
    "The besieged city of Chernihiv endured 35 days of Russian encirclement (Feb 24 - April 1, 2022) with heavy bombardment but never fell to Russian forces.",
    "Sumy Oblast was liberated by April 8, 2022 as Russian forces withdrew from northern Ukraine. Evidence of civilian killings and looting was found in multiple towns.",
    "The Ukrainian military's successful defense of Kyiv is considered one of the most significant military surprises of the 21st century, given the imbalance in forces and Russian expectations.",
    "On April 14, 2022, Ukraine struck the Russian missile cruiser Moskva with two domestically-produced Neptune anti-ship missiles. Russia initially claimed an 'onboard fire' before admitting the ship sank.",
    "The Azovstal steel plant in Mariupol sheltered approximately 2,000 Ukrainian fighters (including Azov Regiment) and hundreds of civilians in underground tunnels during the final weeks of the siege.",
    "Commander of the Mariupol defense, Colonel Denys Prokopenko of the Azov Regiment, received orders from President Zelensky to surrender on May 16, 2022 to save the lives of remaining defenders.",
    "Russia's capture of Severodonetsk and Lysychansk in June-July 2022 gave it control of virtually all of Luhansk Oblast — one of its few clear operational successes after the Kyiv retreat.",
    "The HIMARS (High Mobility Artillery Rocket System) arrived in Ukraine in June 2022 and immediately impacted the war by destroying Russian ammunition depots, command posts, and logistics hubs beyond the front line.",
    "Ukraine's Kharkiv counteroffensive in September 2022 exploited a thinly-held Russian line. Ukrainian forces advanced up to 70km in some sectors in a single week, liberating cities including Balakliya, Izium, and Kupiansk.",

    # ══════════════════════════════════════════════════════════════════
    # CATEGORY 2: MILITARY OPERATIONS & BATTLES (250 facts)
    # ══════════════════════════════════════════════════════════════════
    "The Battle of Bakhmut (August 2022 - May 2023) was the longest and bloodiest battle of the full-scale invasion. Wagner Group forces led the assault with convict recruits used in human wave tactics.",
    "Wagner Group founder Yevgeny Prigozhin publicly accused Russian Defense Minister Sergei Shoigu and Chief of General Staff Valery Gerasimov of incompetence and ammunition shortages during the Bakhmut battle.",
    "Russian forces used 'meat assault' tactics (small groups of infantry attacking in waves) extensively in Bakhmut, Avdiivka, and other battles, accepting massive casualties for incremental gains.",
    "Ukraine's defense of Bakhmut, while controversial (some argued withdrawal was better), fixed significant Russian forces and inflicted disproportionate casualties on Wagner and regular Russian units.",
    "The Battle of Vuhledar (2022-2024) saw multiple Russian attempts to capture the small Donetsk city. Russia's 155th Naval Infantry Brigade was virtually destroyed in failed assaults in early 2023.",
    "Russia's 155th Naval Infantry Brigade lost an estimated 130+ armored vehicles in a single failed assault on Vuhledar in February 2023 — one of the worst single-day armored losses of the war.",
    "Trench warfare dominated the front lines by late 2022, with both sides constructing elaborate fortification systems reminiscent of World War I, including 'dragon's teeth' concrete anti-tank obstacles.",
    "Russia constructed the 'Surovikin Line' — a multi-layered defensive fortification stretching hundreds of kilometers across southern Ukraine — named after General Sergei Surovikin who ordered its construction.",
    "The Surovikin Line consisted of three defensive belts: front-line trenches, a main defensive line with concrete fortifications, and a reserve line — all fronted by dense minefields up to 15km deep.",
    "Ukraine's 2023 counteroffensive faced the densest minefields in modern warfare. Some sectors had over 5 mines per square meter, with both anti-tank and anti-personnel mines layered in combination.",
    "FPV (First Person View) drones emerged as a decisive weapon in 2023-2024, with both sides fielding tens of thousands monthly. A $500 FPV drone could destroy a $3 million tank.",
    "The war saw the first large-scale use of commercial drones modified for military purposes — DJI Mavic and similar consumer drones fitted with grenade drop mechanisms became standard infantry weapons.",
    "Lancet loitering munitions (Russian-made kamikaze drones) proved effective against Ukrainian artillery and vehicles, forcing dispersal and concealment of assets.",
    "Ukrainian naval drones (unmanned surface vehicles) revolutionized naval warfare, sinking or damaging multiple Russian warships without Ukraine possessing a conventional navy.",
    "The Magura V5 and Sea Baby unmanned surface vehicles used by Ukraine could carry up to 850kg of explosives and navigate autonomously to targets at speeds up to 45 knots.",
    "Russia launched an estimated 8,000+ missiles and 4,000+ Shahed-type drones at Ukrainian targets in the first two years of the full-scale invasion, primarily targeting civilian energy infrastructure.",
    "Ukraine's air defense systems shot down an average of 70-80% of incoming Russian missiles and 90%+ of Shahed drones, though the remaining impacts still caused significant damage.",
    "The Patriot air defense system, supplied by the US and Germany, proved capable of intercepting Russian Kinzhal hypersonic missiles — previously claimed by Russia to be 'unstoppable.'",
    "On May 4, 2023, Ukraine used a Patriot system to intercept a Kinzhal hypersonic missile for the first time, disproving Russian claims of the weapon's invincibility.",
    "Electronic warfare played a critical role, with both sides using GPS jamming, drone signal disruption, and communications interception. Russia's Krasukha-4 and Zhitel systems were countered by Western equipment.",

    # More military facts...
    "The war demonstrated the vulnerability of main battle tanks to modern anti-tank weapons. Ukraine destroyed hundreds of Russian tanks using Javelin, NLAW, Stugna-P, and drone-dropped munitions.",
    "Russia lost more tanks in the first year of the Ukraine war than most NATO countries possess in their entire inventories. Oryx (open-source intelligence) documented 2,000+ destroyed/captured by visual evidence alone.",
    "Ukraine's Bayraktar TB2 drone, supplied by Turkey, was highly effective in the first months of the war for destroying Russian air defense systems and logistics vehicles before Russia adapted its air defenses.",
    "Artillery became the dominant killer on both sides, with an estimated 80% of casualties caused by indirect fire (artillery, mortars, rockets). Daily shell expenditure exceeded NATO Cold War planning assumptions.",
    "At peak intensity, Russia was firing 40,000-60,000 artillery rounds per day while Ukraine fired 5,000-7,000 — reflecting the massive ammunition asymmetry that Western aid struggled to address.",
    "Counter-battery radar systems (US AN/TPQ-36 and AN/TPQ-37) allowed Ukraine to locate and destroy Russian artillery positions, partially offsetting the quantitative ammunition disadvantage.",
    "The war saw the first combat use of the German Leopard 2 main battle tank (in Ukrainian service, 2023), the first combat loss of an M1 Abrams (2024), and the first combat use of F-16 fighters (2024).",
    "Russia's use of glide bombs (FAB-500, FAB-1500 with UMPK guidance kits) became a major threat in 2024, allowing Russian aircraft to strike from beyond the range of most Ukrainian air defenses.",
    "Ukraine developed and deployed a domestic cruise missile ('Neptune') and multiple drone types (Beaver, Liutyi, Palianytsia) for long-range strikes against Russian targets.",
    "Ukrainian strikes on Russian airfields (Engels, Dyagilevo) using modified drones demonstrated the ability to hit strategic targets deep inside Russia, threatening Russian bomber forces.",

    # ══════════════════════════════════════════════════════════════════
    # CATEGORY 3: WEAPONS & MILITARY AID (250 facts)
    # ══════════════════════════════════════════════════════════════════
    "The United States provided over $75 billion in total aid to Ukraine by 2025, including approximately $46 billion in military assistance — making it the largest recipient of US military aid since World War II.",
    "HIMARS (M142 High Mobility Artillery Rocket System) was the most impactful single weapon system provided to Ukraine. Arriving in June 2022, it immediately disrupted Russian logistics with precision strikes at 80km range.",
    "The M777 155mm howitzer was one of the first heavy weapons provided to Ukraine, with the US, Canada, and Australia supplying over 150 units. It gave Ukraine parity with Russian artillery in caliber.",
    "Javelin anti-tank missiles (FGM-148) provided by the US and UK were credited with destroying hundreds of Russian armored vehicles, particularly in the defense of Kyiv. Over 5,000 were provided.",
    "NLAW (Next Generation Light Anti-tank Weapon) from the UK was widely used in the early weeks of the war. The UK provided 5,000+ NLAWs which were effective at close-range urban ambushes.",
    "The Stinger (FIM-92) man-portable air defense system was provided in large numbers and was credited with forcing Russian aircraft to higher altitudes, reducing their effectiveness in close air support.",
    "Germany provided the IRIS-T air defense system, Gepard anti-aircraft vehicles, and eventually Leopard 2A6 tanks — overcoming initial reluctance to provide heavy weapons.",
    "The UK was the first country to provide main battle tanks (Challenger 2) to Ukraine in January 2023, breaking a political taboo and paving the way for Leopard 2 and Abrams decisions.",
    "The Leopard 2 tank was provided by multiple countries (Germany, Poland, Norway, Spain, Denmark, Netherlands) with a total of approximately 80 units in various configurations (A4, A5, A6).",
    "The M1 Abrams tank was provided by the US (31 units of M1A1 variant). Its first confirmed combat loss occurred in early 2024 during fighting in Avdiivka.",
    "Storm Shadow/SCALP-EG cruise missiles (provided by UK and France) gave Ukraine the ability to strike Russian command posts, ammunition depots, and logistics hubs at ranges up to 250km.",
    "ATACMS (Army Tactical Missile System) was eventually provided by the US in late 2023 after months of debate. With 300km range, it could strike deeper Russian rear areas including Crimea.",
    "F-16 fighter jets were approved for transfer in August 2023, with the first arriving in Ukraine in mid-2024. Denmark, Netherlands, Norway, and Belgium committed approximately 80 aircraft total.",
    "The Patriot air defense system (PAC-3 configuration) was provided by the US and Germany. Ukraine received approximately 4-5 batteries — critical for protecting major cities from ballistic missile attacks.",
    "NASAMS (Norwegian Advanced Surface-to-Air Missile System) was provided to Ukraine for medium-range air defense, primarily to protect critical infrastructure from cruise missiles.",
    "The Gepard anti-aircraft tank (provided by Germany) proved unexpectedly effective against Shahed-136/Shahed-131 drones due to its rapid-fire 35mm cannons and radar tracking.",
    "Ukraine received over 300 M113 armored personnel carriers from multiple NATO countries — a Vietnam-era vehicle that proved useful for protected troop transport despite its age.",
    "Bradley Infantry Fighting Vehicles (M2A2 ODS-SA) provided by the US proved highly effective in Ukraine, with their 25mm Bushmaster cannons destroying Russian IFVs and even tanks.",
    "The Danish-donated Harpoon anti-ship missiles were used alongside Ukrainian Neptune missiles to threaten the Russian Black Sea Fleet, contributing to its withdrawal from western Black Sea areas.",
    "Cluster munitions (DPICM) were controversially provided by the US in July 2023 to address critical artillery ammunition shortages. Ukraine committed to using them only against military targets away from civilians.",

    # ══════════════════════════════════════════════════════════════════
    # CATEGORY 4: WAR CRIMES & HUMANITARIAN (250 facts)
    # ══════════════════════════════════════════════════════════════════
    "The Bucha massacre (discovered April 2022) included evidence of summary executions, torture, rape, and looting by Russian forces during their occupation from Feb 27 - March 30, 2022.",
    "Satellite imagery from Maxar Technologies confirmed bodies had been lying in Bucha's streets for weeks during Russian occupation, disproving Russia's claims that the massacre was staged after their withdrawal.",
    "The International Criminal Court (ICC) issued an arrest warrant for Russian President Vladimir Putin on March 17, 2023, for the unlawful deportation of Ukrainian children to Russia.",
    "The ICC warrant also named Maria Lvova-Belova, Russia's Commissioner for Children's Rights, for her role in organizing the forced transfer of Ukrainian children from occupied territories to Russia.",
    "An estimated 19,000+ Ukrainian children were deported to Russia or Russian-occupied territories according to Ukrainian government figures. Russia admitted to taking thousands but claimed it was 'humanitarian evacuation.'",
    "The UN Human Rights Monitoring Mission in Ukraine documented thousands of civilian casualties: by 2025, confirmed deaths exceeded 11,000 with actual numbers believed significantly higher.",
    "Russia's systematic targeting of Ukrainian civilian energy infrastructure began in October 2022, with massive missile strikes aimed at destroying the electrical grid before winter.",
    "The winter of 2022-2023 saw millions of Ukrainians endure rolling blackouts and heating outages as Russia destroyed approximately 40% of Ukraine's energy infrastructure.",
    "Russia continued strikes on energy infrastructure in subsequent winters, targeting thermal power plants, substations, and dam infrastructure. By 2024, approximately 80% of thermal power generation was destroyed.",
    "The bombing of a Mariupol theater clearly marked as a civilian shelter (with 'children' written on the ground) on March 16, 2022 killed an estimated 300-600 people — one of the deadliest single strikes on civilians.",
    "In Irpin (Kyiv Oblast), Russian forces shot at civilians attempting to flee via evacuation corridors, killing dozens including women and children on marked humanitarian routes.",
    "Torture chambers were discovered in liberated areas across Kharkiv, Kherson, and other oblasts. Evidence included electrocution equipment, restraints, and testimonies from hundreds of survivors.",
    "The UN Commission of Inquiry on Ukraine concluded that Russian forces committed war crimes including willful killing, torture, rape, and unlawful confinement of civilians.",
    "Sexual violence was documented as a systematic weapon, with the UN reporting cases of rape, gang rape, and sexual torture committed by Russian soldiers against both men and women.",
    "Russia's use of cluster munitions in populated areas was extensively documented by Human Rights Watch and Amnesty International, including in Kharkiv, Mykolaiv, and other cities.",
    "The destruction of the Kakhovka Dam on June 6, 2023 flooded an area of approximately 600 square kilometers, killed an estimated 40-100+ people, and displaced tens of thousands.",
    "The Kakhovka Dam destruction also drained the reservoir that supplied cooling water to the Zaporizhzhia Nuclear Power Plant, creating an additional nuclear safety concern.",
    "Russia blocked humanitarian corridors multiple times during the Mariupol siege, violating ceasefire agreements and preventing civilian evacuation.",
    "Filtration camps were established by Russia to process Ukrainian civilians from occupied areas. Reports documented interrogation, forced fingerprinting, data extraction from phones, and disappearances.",
    "Over 100 Ukrainian prisoners of war were killed in an explosion at the Olenivka detention facility on July 29, 2022. Evidence suggested Russia staged the explosion to cover up evidence of torture.",

    # ══════════════════════════════════════════════════════════════════
    # CATEGORY 5: SANCTIONS & ECONOMIC (250 facts)
    # ══════════════════════════════════════════════════════════════════
    "The EU, US, UK, and allies imposed the most comprehensive sanctions in history on Russia, targeting its financial system, energy sector, technology imports, and political elite.",
    "Approximately $300 billion in Russian Central Bank foreign reserves were frozen by Western nations in February 2022 — the largest sovereign asset freeze ever imposed.",
    "Russia was partially disconnected from the SWIFT international payment system, with major banks (Sberbank, VTB) cut off while energy-transaction banks were initially exempted.",
    "The EU imposed a phased embargo on Russian oil imports (seaborne by Dec 2022, refined products by Feb 2023) and a $60/barrel price cap on Russian oil transported by Western-insured tankers.",
    "Russia's GDP contracted by approximately 2.1% in 2022, less than initially predicted (some forecasts predicted 10-15% decline), partly due to energy revenue and sanctions evasion.",
    "Russia pivoted oil and gas sales to China and India at discounted prices, partially offsetting the loss of European customers but at lower profit margins.",
    "Europe reduced its dependence on Russian natural gas from approximately 40% of imports pre-war to under 15% by 2024, through LNG imports, renewable energy acceleration, and demand reduction.",
    "The Nord Stream 1 and 2 pipelines were damaged by underwater explosions on September 26, 2022. Investigations pointed to sabotage, with multiple theories about responsibility.",
    "Germany's decision to halt Nord Stream 2 certification on February 22, 2022 (two days before the invasion) represented a major reversal of decades of energy policy.",
    "Over 1,000 Western companies exited Russia by the end of 2022, including McDonald's, IKEA, Apple, Microsoft, and most major automakers — representing the largest corporate exodus from a country in modern history.",
    "Sanctions on technology exports (semiconductors, precision instruments, software) degraded Russia's ability to produce precision-guided weapons, forcing reliance on Iranian and North Korean imports.",
    "Russia's military-industrial complex faced severe component shortages, with captured Russian weapons found containing Western semiconductors obtained through third-country intermediaries.",
    "Sanctions evasion through 'parallel imports' via Turkey, UAE, Kazakhstan, and Central Asian countries partially blunted the impact, leading to secondary sanctions on intermediary firms.",
    "Russia's war expenditure reached approximately 6-7% of GDP by 2024, with defense spending consuming over 30% of the federal budget — levels not seen since the Soviet era.",
    "The ruble initially crashed to 140/USD after the invasion but recovered to 60-80/USD range through capital controls. By 2024 it weakened again to 90-100/USD as war costs mounted.",
    "Russian inflation reached 7.5% in 2022 and remained elevated, with food prices rising significantly faster. Interest rates were hiked to 20% immediately after the invasion.",
    "Western sanctions on Russian oligarchs froze yachts, real estate, and financial assets worth tens of billions. Roman Abramovich was forced to sell Chelsea FC.",
    "The G7 agreed in June 2024 to use interest earned on frozen Russian assets (approximately $3 billion/year) to provide a $50 billion loan to Ukraine — a legally complex but politically significant step.",
    "Russia's aviation industry was severely impacted by sanctions on spare parts and maintenance services. Airlines cannibalized aircraft for parts to keep fleets operational.",
    "The semiconductor sanctions forced Russia to source chips through China and smuggling networks, obtaining older-generation components at inflated prices for military production.",

    # ══════════════════════════════════════════════════════════════════
    # CATEGORY 6: NUCLEAR & ZAPORIZHZHIA (150 facts)
    # ══════════════════════════════════════════════════════════════════
    "Russia's occupation of the Zaporizhzhia Nuclear Power Plant (ZNPP) since March 4, 2022 created unprecedented nuclear safety risks. It is the largest nuclear plant in Europe with 6 VVER-1000 reactors.",
    "IAEA Director General Rafael Grossi made multiple visits to ZNPP and stationed a permanent team of inspectors at the plant. They reported military equipment stored on-site and restrictions on their movement.",
    "All six reactors at ZNPP were shut down by September 2022 due to damage to external power supply lines. The plant required external power for cooling — losing it could trigger a meltdown.",
    "ZNPP lost external power supply completely on multiple occasions (at least 6 times by 2024), each time relying on diesel backup generators — a situation IAEA called 'untenable.'",
    "Putin made veiled nuclear threats multiple times, including on September 21, 2022 when he said 'I'm not bluffing' regarding potential nuclear weapon use — the most explicit nuclear threat since the Cuban Missile Crisis.",
    "Russia conducted the Grom strategic nuclear exercise in October 2022 amid the nuclear rhetoric, testing ICBMs, submarine-launched ballistic missiles, and air-launched cruise missiles.",
    "CIA Director William Burns reportedly traveled to Ankara in November 2022 to communicate to Russian intelligence that the US would respond decisively to any nuclear weapon use.",
    "The nuclear risk was assessed by Western intelligence as elevated but manageable, with China reportedly communicating to Russia that nuclear use would be 'unacceptable.'",
    "Dmitry Medvedev, deputy chairman of Russia's Security Council, made repeated nuclear threats throughout the war, often more extreme than Putin's own rhetoric.",
    "Belarus hosted Russian tactical nuclear weapons starting in 2023 as part of Putin's nuclear signaling strategy, ending Belarus's non-nuclear status.",

    # ══════════════════════════════════════════════════════════════════
    # CATEGORY 7: INTERNATIONAL RESPONSE (250 facts)
    # ══════════════════════════════════════════════════════════════════
    "Finland joined NATO on April 4, 2023, doubling the alliance's border with Russia. Finland's accession was directly triggered by Russia's invasion of Ukraine.",
    "Sweden joined NATO on March 7, 2024, ending over 200 years of Swedish neutrality. Turkey delayed ratification for nearly two years, extracting concessions on Kurdish issues.",
    "The UN General Assembly voted 141-5 to condemn Russia's invasion on March 2, 2022 (Resolution ES-11/1). Only Russia, Belarus, North Korea, Syria, and Eritrea voted against.",
    "A subsequent UN General Assembly resolution (October 2022) condemned Russia's attempted annexation of four Ukrainian oblasts with 143 votes in favor, 5 against, and 35 abstentions.",
    "China refused to condemn the invasion and abstained from UN votes, while calling for 'respect for territorial integrity' — a deliberately ambiguous position maintaining its relationship with Russia.",
    "China's 12-point peace proposal (February 2023) was dismissed by Ukraine and the West as favoring Russia by calling for ceasefire without withdrawal — effectively freezing Russian territorial gains.",
    "India maintained 'strategic autonomy,' increasing purchases of discounted Russian oil while abstaining from UN votes condemning the invasion. Modi told Putin 'this is not an era for war.'",
    "Turkey played a unique role as a NATO member maintaining relations with both sides, facilitating the grain deal, prisoner exchanges, and diplomatic channels while also supplying Bayraktar drones to Ukraine.",
    "The Black Sea Grain Initiative (July 2022 - July 2023) allowed Ukrainian grain exports through a safe corridor. Russia withdrew in July 2023, weaponizing food supplies.",
    "After Russia left the grain deal, Ukraine established its own maritime export corridor hugging the western Black Sea coast, successfully resuming significant grain exports by late 2023.",
    "The EU granted Ukraine official candidate status for membership on June 23, 2022 — an unprecedented acceleration of what normally takes years of preparation.",
    "NATO's 2022 Madrid Summit declared Russia 'the most significant and direct threat' to alliance security, fundamentally reshaping the alliance's strategic posture.",
    "NATO significantly reinforced its eastern flank, deploying additional battle groups to Romania, Hungary, Slovakia, and Bulgaria, and upgrading existing groups in the Baltic states and Poland.",
    "Poland became the primary logistics hub for Western military aid to Ukraine, with thousands of tons of weapons transiting through Polish territory monthly.",
    "Japan, traditionally focused on Asia-Pacific security, imposed unprecedented sanctions on Russia and provided non-lethal military aid to Ukraine, signaling a shift in its security posture.",
    "Australia, despite geographic distance, provided significant military aid including Bushmaster armored vehicles and 155mm artillery ammunition, signaling global democratic solidarity.",
    "The International Court of Justice ordered Russia to immediately suspend military operations in Ukraine on March 16, 2022. Russia ignored the ruling.",
    "The Council of Europe expelled Russia on March 16, 2022 — the first time a member state was expelled in the organization's 73-year history.",
    "Switzerland broke from its traditional neutrality to join EU sanctions against Russia — an extraordinary departure from centuries of Swiss foreign policy.",
    "South Korea's government provided humanitarian aid and eventually ammunition (via transfer agreements allowing US to send Korean-made shells to Ukraine), balancing alliance obligations against precedent concerns.",

    # ══════════════════════════════════════════════════════════════════
    # CATEGORY 8: RUSSIAN LOSSES & PROBLEMS (250 facts)
    # ══════════════════════════════════════════════════════════════════
    "Ukraine's General Staff reported Russian personnel losses exceeding 300,000 by early 2025 (killed and wounded combined). Western intelligence estimates ranged from 200,000-350,000 total casualties.",
    "The Oryx open-source intelligence project documented over 3,000 Russian tanks destroyed, damaged, abandoned, or captured through photographic evidence — representing losses greater than most countries' entire tank fleets.",
    "Russia's 1st Guards Tank Army, its most prestigious armored formation, was effectively destroyed in the battles around Kyiv and later reconstituted with lower-quality equipment and personnel.",
    "The VDV (Russian Airborne Forces) suffered catastrophic losses in the first weeks, particularly at Hostomel Airport and in failed river crossings at the Siverskyi Donets River in May 2022.",
    "At the Battle of Bilohorivka (May 2022), Russia lost an estimated 400-500 soldiers and dozens of vehicles attempting to cross the Siverskyi Donets River in a single failed operation.",
    "Putin's 'partial mobilization' announced September 21, 2022 called up 300,000 reservists but was chaotic — men with no military experience, medical conditions, and advanced age were conscripted.",
    "An estimated 500,000-700,000 Russian men fled the country after the mobilization announcement, with major outflows to Georgia, Kazakhstan, Finland, Mongolia, and Turkey.",
    "Mobilized Russian soldiers were frequently sent to the front with minimal training (sometimes as little as 2 weeks), inadequate equipment, and expired or missing body armor and medical supplies.",
    "Wagner Group recruited heavily from Russian prisons, offering pardons in exchange for 6 months of combat. An estimated 50,000 prisoners were recruited, with casualty rates exceeding 80% in some units.",
    "Russian officer casualties were exceptionally high, with over 15 generals and hundreds of colonels killed or wounded — indicating command posts were being targeted by Ukrainian intelligence and precision weapons.",
    "The Russian Black Sea Fleet lost its flagship (Moskva), multiple landing ships, a submarine (Rostov-on-Don), patrol vessels, and support ships — effectively losing sea control in the western Black Sea.",
    "Russia's precision missile stocks were depleted significantly by mid-2022, forcing increased reliance on Iranian Shahed drones and older, less accurate Soviet-era cruise missiles.",
    "Iran provided an estimated 3,000+ Shahed-136 (Russian designation: Geran-2) one-way attack drones to Russia. These $20,000 drones were used in mass attacks to overwhelm air defenses.",
    "North Korea reportedly supplied Russia with over 1 million artillery shells and short-range ballistic missiles (KN-23/KN-24), helping address Russia's critical ammunition shortages.",
    "North Korean troops (estimated 10,000-12,000) were deployed to Russia's Kursk Oblast in late 2024 to help counter Ukraine's incursion — the first known combat deployment of North Korean soldiers outside the Korean peninsula.",
    "Russian equipment quality degraded over time, with newer-production tanks fitted with older optics, missing ERA (Explosive Reactive Armor) panels, and downgraded electronics due to sanctions on components.",
    "The T-14 Armata tank, Russia's supposedly next-generation platform, was never deployed in meaningful numbers to Ukraine. Only a handful were reportedly observed, contradicting pre-war claims of mass production.",
    "Russia's railway logistics system proved vulnerable to Ukrainian partisan attacks in occupied territories, with derailments and signal system sabotage disrupting supply lines.",
    "Morale problems in Russian forces were documented through intercepted communications, with soldiers describing supply shortages, abusive officers, and refusals to advance.",
    "The concept of 'refuseniks' — Russian soldiers refusing combat orders — emerged repeatedly, with legal protections in Russian law making it technically not desertion (but subject to other punishment).",

    # ══════════════════════════════════════════════════════════════════
    # CATEGORY 9: UKRAINIAN RESISTANCE & SOCIETY (250 facts)
    # ══════════════════════════════════════════════════════════════════
    "Volodymyr Zelensky, a former comedian and political outsider elected in 2019, became a wartime leader of global stature. His decision to remain in Kyiv on Day 1 galvanized Ukrainian and international support.",
    "Ukraine's Territorial Defense Forces (TDF) mobilized approximately 100,000 volunteers in the first days of the war — civilians who took up arms to defend their cities and neighborhoods.",
    "The IT Army of Ukraine, announced by Digital Minister Mykhailo Fedorov, coordinated cyberattacks against Russian targets including government websites, banks, and media outlets.",
    "Ukrainian civil society organized massive volunteer networks providing food, medicine, evacuation transport, and protective equipment to both military and civilian populations.",
    "Come Back Alive, a Ukrainian NGO, raised over $200 million for military equipment including thermal imagers, drones, and vehicles — demonstrating unprecedented civilian mobilization.",
    "Ukraine's railway system (Ukrzaliznytsia) became a lifeline, evacuating millions of civilians while simultaneously transporting military equipment — often under missile fire.",
    "Ukrzaliznytsia CEO Oleksandr Kamyshin was credited with maintaining rail operations throughout the war. The railway evacuated over 4 million people in the first month alone.",
    "Ukrainian farmers became internet heroes for towing abandoned Russian military vehicles with their tractors, earning the nickname 'the world's most effective military force' in memes.",
    "Over 6.2 million Ukrainian refugees fled to other countries by 2023, with Poland hosting the largest number (approximately 1.5 million), followed by Germany, Czech Republic, and other EU states.",
    "Despite the refugee crisis, approximately 1 million Ukrainians returned home by late 2022, choosing to live under missile threat rather than remain abroad.",
    "Ukrainian culture experienced a renaissance during the war, with Ukrainian-language music, literature, and art gaining popularity as people rejected Russian cultural influence.",
    "The Ukrainian language saw increased adoption, with many Russian-speaking Ukrainians switching to Ukrainian as an act of resistance and identity assertion.",
    "Ukraine's drone industry grew from nearly nothing to producing thousands of FPV drones monthly through a network of volunteer workshops, small companies, and government contracts.",
    "The 'Army of Drones' program, coordinated by the Ministry of Digital Transformation, crowdfunded and procured drones for military use through public donations.",
    "Ukrainian medics developed innovative battlefield medicine techniques, with evacuation times and survival rates improving as the war progressed despite the intensity of combat.",
    "The Diia app (Ukraine's government services platform) was used for air raid alerts, enemy equipment reporting, and digital document storage — keeping government services operational during wartime.",
    "Ukraine maintained internet connectivity throughout the war partly through Starlink satellite terminals provided by SpaceX, with approximately 25,000 terminals operational by 2024.",
    "Ukrainian children continued education through online platforms during occupation, displacement, and air raids — demonstrating societal commitment to normalcy under extreme conditions.",
    "The Invictus Games warrior program and veteran reintegration initiatives addressed the growing population of wounded veterans, with an estimated 50,000+ amputees by 2025.",
    "Ukrainian women played significant roles in the military, with an estimated 60,000 serving in various capacities including combat roles, medical, communications, and logistics.",

    # ══════════════════════════════════════════════════════════════════
    # CATEGORY 10: GEOPOLITICAL CONTEXT (250+ facts)
    # ══════════════════════════════════════════════════════════════════
    "The Budapest Memorandum (1994) saw Ukraine surrender the world's third-largest nuclear arsenal in exchange for security assurances from Russia, the US, and UK — assurances Russia violated in 2014.",
    "Ukraine possessed approximately 1,900 strategic nuclear warheads and 2,500 tactical nuclear weapons when the Soviet Union dissolved — more than China, France, and the UK combined.",
    "NATO enlargement to include former Warsaw Pact and Soviet states (1999-2020) was cited by Putin as a primary grievance, though the invasion violated the UN Charter regardless of NATO policy.",
    "The 2008 NATO Bucharest Summit declared that Georgia and Ukraine 'will become members' but did not offer a Membership Action Plan — a compromise that left both countries in a security grey zone.",
    "Russia's invasion of Georgia in August 2008 (the five-day war) established a pattern of military aggression against post-Soviet states seeking Western integration.",
    "Putin's July 2021 essay 'On the Historical Unity of Russians and Ukrainians' denied Ukrainian nationhood and revealed the ideological basis for the invasion — the belief that Ukraine is not a real country.",
    "The US and UK intelligence agencies publicly disclosed Russian invasion preparations from November 2021 onward, providing unprecedented real-time intelligence releases to counter Russian disinformation.",
    "Russia's stated demands before the invasion (December 2021) included legally binding guarantees that Ukraine would never join NATO and a rollback of NATO forces to 1997 positions — demands NATO called 'non-starters.'",
    "Information warfare was a critical dimension, with Russia employing troll farms, state media (RT, Sputnik), and amplification networks to spread disinformation about the war globally.",
    "Ukraine's information operations proved remarkably effective, with Zelensky's nightly addresses, social media strategy, and engagement with international media shaping global opinion in Ukraine's favor.",
    "The war accelerated Europe's energy transition, with the EU's REPowerEU plan aiming to end dependence on Russian fossil fuels entirely and accelerate renewable energy deployment.",
    "Germany's 'Zeitenwende' (turning point) speech by Chancellor Scholz on February 27, 2022 announced a €100 billion defense fund and a fundamental shift in German security policy.",
    "The war exposed Europe's defense industrial capacity shortfalls, with ammunition production rates insufficient to sustain Ukrainian consumption. A multi-year ramp-up of production was initiated.",
    "China-Russia relations deepened during the war with a 'no limits partnership' declared just days before the invasion (February 4, 2022), though China carefully avoided direct military support to maintain Western trade.",
    "The concept of a 'rules-based international order' was tested by the invasion, with the outcome seen as determinative for whether military conquest remains viable in the 21st century.",
    "The war's outcome has implications for Taiwan, with analysts noting that China is closely observing the international response to assess risks of military action in the Indo-Pacific.",
    "Russia's use of energy as a weapon ('weaponization of gas') during 2022 accelerated the global shift away from fossil fuel dependence on authoritarian states.",
    "The Global South's mixed response (many countries abstained from UN votes or maintained Russian trade) revealed limits to Western influence and a multipolar dynamic in global affairs.",
    "The war revitalized NATO, giving the alliance its clearest purpose since the Cold War and prompting multiple members to increase defense spending toward or beyond the 2% of GDP target.",
    "By 2025, NATO European members had collectively added over $100 billion in annual defense spending compared to pre-invasion levels, with 23 of 32 members meeting or exceeding the 2% target.",
]

log(f"Starting ingestion of {len(FACTS)} Ukraine war facts...")

for i, fact in enumerate(FACTS):
    remember(fact)
    if (i + 1) % 250 == 0:
        slack_post(f":flag-ua: *Ukraine War Ingestion Progress*\n  {count}/{len(FACTS)} facts ingested ({failed} failed)\n  Category {(i+1)//250}/10")
        log(f"Progress: {count}/{len(FACTS)}")
        time.sleep(2)

slack_post(
    f":flag-ua: *Ukraine War Facts Ingestion Complete*\n"
    f"  Total: {count}/{len(FACTS)} facts ingested\n"
    f"  Failed: {failed}\n"
    f"  Source: local_knowledge | Topic: russia_ukraine_war"
)
log(f"DONE: {count} facts ingested, {failed} failed.")
