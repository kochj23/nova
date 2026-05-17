#!/usr/bin/env python3
"""Ingest LA-area gang facts into Nova's vector memory for local safety awareness.

Sources: LAPD gang territory maps, LA City Attorney injunction documents,
LA Times reporting, LASD gang database (public), academic research (UCLA/UCI).

Written by Jordan Koch.
"""
import json
import sys
import time
import urllib.request

MEMORY_URL = "http://192.168.1.6:18790/remember"

def remember(text):
    payload = json.dumps({
        "text": text,
        "source": "local_knowledge",
        "metadata": {"type": "gang_intelligence", "region": "los_angeles"}
    }).encode()
    req = urllib.request.Request(MEMORY_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:
        print(f"  Failed: {e}", file=sys.stderr)
        return False

FACTS = [
    # ── San Fernando Valley: Pacoima / Sun Valley / Sylmar ────────────────
    "Pacoima Flats (PxF) is a Sureño 13 gang operating in Pacoima, primarily around Laurel Canyon Blvd and Van Nuys Blvd between Arleta and San Fernando. Active since the 1970s.",
    "Project Boy Gangsters (PBG) operate in the San Fernando Gardens housing project in Pacoima, near Herrick Ave and Pinney St. Primarily Hispanic Sureño-affiliated.",
    "Humphrey Boys (HB) is a Sureño clique in Pacoima operating near Humphrey Ave. They are rivals of Pacoima Flats.",
    "The Vineland Boys (VBS13) were a major Sureño 13 gang in Sun Valley operating along Vineland Ave between Roscoe and Saticoy. Subject to a RICO prosecution in 2009 that dismantled leadership.",
    "Sun Valley Armenian gang activity centers around Sunland Blvd and Vineland Ave, with ties to Armenian Power 13 in Glendale.",
    "Barrio Van Nuys (BVN13) is a Sureño 13 gang operating in Van Nuys, primarily around Van Nuys Blvd between Sherman Way and Victory Blvd.",
    "San Fernando (San Fer 13 / SF13) is one of the oldest Sureño gangs in the northeast Valley, operating in the city of San Fernando near Brand Blvd and Maclay Ave.",
    "Sylmar is home to multiple Sureño cliques including Blythe Street and Barrio Sylmar (BSL), operating near Foothill Blvd and Hubbard St.",
    "The Blythe Street gang (BSG) operates primarily in Panorama City and Van Nuys around Blythe St between Van Nuys Blvd and Sepulveda Blvd. One of the largest SFV gangs.",
    "Columbus Street (CS) is a Panorama City Sureño gang operating near Columbus Ave and Roscoe Blvd.",
    "Langdon Street gang operates in North Hollywood near Langdon Ave and Victory Blvd. Sureño-affiliated.",
    "Radford Street gang operates in North Hollywood near Radford Ave. Known for rivalry with Langdon Street.",
    "Mara Salvatrucha (MS-13) has cells in the San Fernando Valley, particularly in Panorama City, North Hollywood, and Van Nuys. Identifiable by the '503' area code tattoo (El Salvador) and devil horns hand sign.",
    "18th Street gang (XV3/XVIII) is the largest street gang in Los Angeles with an estimated 15,000+ members. They have cliques throughout the Valley including Van Nuys, Panorama City, and North Hollywood.",
    "18th Street Panorama City clique operates along Van Nuys Blvd between Roscoe and Nordhoff in Panorama City.",
    "Canoga Park Alabama (CPA) is a Sureño gang in Canoga Park near Alabama Ave and Sherman Way. Subject to gang injunction.",
    "Strathern Street (STRS) is a Sureño gang in Panorama City/Sun Valley operating near Strathern St and Laurel Canyon.",
    "Toonerville (TNR) is one of the oldest gangs in the northeast San Fernando Valley, operating in Atwater Village and Glendale near the LA River between Los Feliz and Glendale Blvd. Founded in the 1930s.",
    "Clanton 14 Street (C14) has cliques throughout Los Angeles including the San Fernando Valley. One of the largest Sureño gangs in the city.",
    "Barrio Elmwood (BE13) is a Sureño gang in the Sun Valley/North Hollywood area near Elmwood Ave.",

    # ── Glendale / Burbank / Eagle Rock / Pasadena ────────────────────────
    "Armenian Power 13 (AP13) is the largest Armenian street gang in Los Angeles, primarily based in Glendale, Hollywood, and East Hollywood. Founded in the 1980s in Glendale High School. Uses the colors black and red.",
    "Armenian Power operates throughout Glendale along Brand Blvd, Broadway, and Glenoaks Blvd. Involved in identity theft, extortion, fraud, and drug trafficking.",
    "AP13 was the subject of a major federal RICO indictment in 2011 that charged 90 members with racketeering, identity theft, bank fraud, and extortion totaling over $20 million.",
    "Armenian Power has a documented alliance with the Mexican Mafia (La Eme) since the early 2000s, giving them Sureño 13 status.",
    "The Glendale area also has smaller Armenian tagging crews and cliques including Armenian Pride (AP), Glendale Locos (GL), and Armenian Eagles.",
    "Burbank does not have significant native gang presence but experiences overflow activity from Pacoima (south), Sun Valley (east), and Glendale (east) gangs.",
    "Sun Valley's proximity to Burbank means graffiti from Vineland Boys and other SFV Sureño cliques occasionally appears on Burbank's eastern border near the 5 freeway.",
    "Highland Park (HLP13) is a Sureño gang in Highland Park near Figueroa St and York Blvd. One of the oldest gangs in Northeast LA, founded in the 1920s.",
    "Avenues (AVE13) is a large Sureño gang in Highland Park/Cypress Park/Glassell Park. Subject to hate crime prosecution for targeting Black residents.",
    "Frogtown (FT13) operates in Elysian Valley along the LA River near Fletcher Drive. Named after the frogs in the river.",
    "Glassell Park gang operates in Glassell Park near Eagle Rock and Glendale. Sureño-affiliated.",
    "Pasadena Denver Lanes (PDL) is a Blood gang operating in Northwest Pasadena near Denver Ave and Lincoln Ave. One of the northernmost Blood sets in LA County.",
    "Pasadena has several active gangs including Raymond Street Crips, Pasadena Denver Lanes (Bloods), and various Sureño cliques in the Orange Grove Blvd area.",
    "Altadena Blocc Crips (ABC) operate in Altadena near Fair Oaks Ave and Altadena Dr. Known rivals of Pasadena Denver Lanes.",
    "Eagle Rock has historically low gang activity due to demographics but borders Highland Park and Glassell Park gang territories.",
    "The city of Glendale implemented anti-gang injunctions targeting Armenian Power in 2008 covering areas around Brand Blvd, Central Ave, and Broadway.",

    # ── East Los Angeles / Boyle Heights ──────────────────────────────────
    "White Fence (WF13) is one of the oldest gangs in Los Angeles, founded in Boyle Heights in the 1930s. Operates near Wabash Ave and Whittier Blvd.",
    "Varrio Nuevo Estrada (VNE) operates in Boyle Heights near Estrada Courts housing project on Olympic Blvd. One of the most notorious East LA gangs.",
    "Primera Flats (PF13) operates in Boyle Heights near First Street and Lorena St. Rivals of White Fence.",
    "Maravilla gangs are a collection of Sureño cliques in unincorporated East LA (Maravilla area) near Arizona Maravilla, Ford Maravilla, and others. Among the oldest Hispanic gangs in the US.",
    "El Sereno (ELS13) is a Sureño gang in El Sereno near Huntington Drive. Active since the 1940s.",
    "City Terrace (CT13) is a Sureño gang in City Terrace near City Terrace Drive and Hazard Ave.",
    "Hazard Grande (HZG) operates in Ramona Gardens housing project in Boyle Heights. Long-standing rivalry with other Eastside gangs.",
    "Lincoln Heights has multiple Sureño cliques including Lincoln Heights Locos and Barrio Lincoln Heights, operating near Broadway and Lincoln Park.",
    "The East LA area is dominated by Sureño 13 gangs with deep roots dating to the 1930s-1940s. Nearly all Hispanic gangs in this area claim 13/Sureño affiliation.",
    "Eastside 18th Street operates in the Rampart/Westlake area and has cliques extending into East LA. Rivals of MS-13.",

    # ── South Los Angeles / Compton / Watts / Inglewood ───────────────────
    "Rollin 60s Neighborhood Crips (R60s NHC) is one of the largest Crip sets in LA, operating in the Hyde Park/Crenshaw area between Slauson and Florence, east of Crenshaw Blvd. Estimated 1,600+ members.",
    "Eight Tray Gangster Crips (83GC / ETG) operate in the Florence-Firestone area south of Manchester Ave. Longtime deadly rivals of the Rollin 60s.",
    "The rivalry between Rollin 60s NHC and Eight Tray Gangster Crips is one of the longest-running and most violent intra-Crip conflicts in Los Angeles history, dating to the 1970s.",
    "Grape Street Crips operate in the Jordan Downs housing project in Watts. Identifiable by purple/grape colors in addition to blue. Named after Grape Street in Watts.",
    "Bounty Hunter Bloods (BHB) operate in the Nickerson Gardens housing project in Watts, the largest public housing project west of the Mississippi. One of the largest Blood sets in LA.",
    "PJ Watts Crips operate in the Imperial Courts housing project in Watts. Rivals of Bounty Hunter Bloods.",
    "Florencia 13 (F13) is a large Sureño gang in South LA operating in the Florence-Firestone unincorporated area. Known for targeting Black residents, resulting in federal hate crime charges in 2008.",
    "Florencia 13 has approximately 3,000 members and operates between Slauson Ave and Firestone Blvd from Central Ave to Alameda St.",
    "Compton has approximately 65 documented gangs including numerous Crip and Blood sets as well as Sureño/Norteño cliques. The Compton area was historically dominated by Black gangs but has seen increasing Hispanic gang activity.",
    "Compton Crip sets include Santana Blocc Compton Crips, Atlantic Drive Compton Crips, Kelly Park Compton Crips, Nutty Blocc Compton Crips, and Palmer Blocc Compton Crips.",
    "Compton Blood/Piru sets include Mob Piru, Lueders Park Piru, Fruit Town Piru, Elm Street Piru, and Cross Atlantic Piru.",
    "Tree Top Piru (TTP) is a Blood/Piru set operating in Compton near Greenleaf Blvd. One of the original Piru sets.",
    "The Piru street gang originated on Piru Street in Compton in 1969 and is considered the original Blood gang. The Bloods formed as a response to Crip expansion.",
    "Inglewood Family Bloods (IFG/IFB) operate in Inglewood near Century Blvd and Prairie Ave. One of the dominant Blood sets in Inglewood.",
    "Crenshaw Mafia Gang (CMG) is a Blood set operating in the Crenshaw/Hyde Park area along Crenshaw Blvd between Slauson and Florence.",
    "Black P Stones (BPS) is a Blood set operating in the Baldwin Village area ('The Jungle') near La Brea Ave and Coliseum St. Named after the Chicago gang.",
    "The Jungle (Baldwin Village) is a dense apartment complex area near La Brea and Rodeo that has been home to Black P Stones since the 1970s. Subject to gang injunction.",
    "Hoover Criminals Gang (52 Hoover, 74 Hoover, 83 Hoover, etc.) operate along Hoover St in South LA. Originally Crips, most Hoover sets dropped the Crip affiliation and became 'Criminals' in the 2000s.",
    "Hoover Criminals are enemies of both Crips and Bloods, making them one of the most violent gang factions in South LA. The 52 Hoover set operates between 52nd and 59th Streets along Hoover.",
    "Main Street Crips operate in South LA near Main St and the 110 freeway between 76th and 84th Streets.",
    "Kitchen Crips operate in the Del Amo area of Carson/North Long Beach. Named after the Kitchen neighborhood.",
    "Hawthorne has several active gangs including Hawthorne Thug Family (Crip) and Lawndale 13 (Sureño) operating near Hawthorne Blvd.",
    "Gardena has both Crip (Gardena Paybacc Crips, Gardena 13 Shotgun Crips) and Sureño (Gardena 13) gangs operating near Western Ave and Rosecrans.",
    "Carson has multiple Crip sets including Carson Side Compton Crips and 190 East Coast Crips.",
    "Willowbrook is an unincorporated community between Compton and Watts with heavy Bounty Hunter Blood presence and rivalry with PJ Watts Crips.",
    "South LA's gang landscape is primarily divided between Crips (blue, many individual sets), Bloods/Pirus (red, many individual sets), and Sureño 13 gangs (blue/gray, primarily Florencia 13 and 18th Street).",
    "The Bloods were formed in 1972 by Sylvester Scott and Vincent Owens on Piru Street in Compton as a response to Crip aggression. The name 'Blood' signifies family/blood kinship.",
    "The Crips were founded in 1969 by Raymond Washington and Stanley 'Tookie' Williams in South Central LA. Originally called 'Baby Avenues' then 'Avenue Cribs' before becoming Crips.",
    "Neighborhood Crips (NHC) is an alliance of Crip sets that includes Rollin 20s, 30s, 40s, 50s, 60s, 90s, and 100s. They are generally allied against Gangster Crip and Hoover sets.",
    "Gangster Crips is an alliance that includes sets like Eight Tray Gangster Crips (83GC), 97 Gangster Crips, and others. They are generally rivals of Neighborhood Crip sets.",

    # ── Central LA / Pico-Union / Koreatown / Rampart ─────────────────────
    "18th Street gang (Barrio 18 / XV3) is the largest street gang in Los Angeles County with an estimated 15,000-20,000 members in LA alone. Originally formed in the Rampart area near 18th St and Union Ave in the 1960s.",
    "18th Street accepts members of all ethnicities, making them one of the most diverse gangs. Originally Mexican-American, they now include Central Americans, African Americans, and others.",
    "18th Street has two main factions: Sureños (traditional, Mexican Mafia-allied) and Revolucionarios (Revo, which broke away). The split occurred in the 2000s.",
    "MS-13 (Mara Salvatrucha) operates in Pico-Union, MacArthur Park, Hollywood, and the San Fernando Valley. Founded by Salvadoran immigrants in LA in the early 1980s.",
    "MS-13 in LA is primarily concentrated around MacArthur Park, Westlake, Koreatown, and Hollywood. Their hand sign is devil horns and they use the color blue (Sureño-affiliated).",
    "The rivalry between 18th Street and MS-13 is one of the most violent in LA. Both are Sureño-affiliated but fight over territory in Pico-Union and MacArthur Park.",
    "Temple Street (TMP) is a Sureño gang in the Temple/Beaudry area near the 101 freeway downtown. One of the older Chicano gangs in LA.",
    "Diamond Street (DS13) operates in the Westlake/MacArthur Park area near the Rampart division.",
    "Crazy Riders (CR13) is a Sureño gang in East Hollywood/Silver Lake area near Sunset Blvd.",
    "Rockwood Street (RWS) is a Sureño gang in the Rampart/Pico-Union area near Rockwood St and Wilshire Blvd.",
    "Head Hunters (HH13) is a Sureño gang in the Pico-Union area near Pico Blvd and Union Ave.",
    "Koreatown has seen conflict between Sureño gangs (18th Street, MS-13) and Korean merchant/community response. The Wilshire Division covers this area.",

    # ── Long Beach ────────────────────────────────────────────────────────
    "Long Beach has approximately 60 documented gangs. Major Crip sets include Insane Crips, Rollin 20s Long Beach Crips, and Baby Insane Crips.",
    "East Side Longos (ESL) is a large Sureño 13 gang in East Long Beach, concentrated east of Cherry Ave. One of the largest Hispanic gangs in Long Beach.",
    "West Side Longos (WSL) operate in West Long Beach near the port and Pacific Ave.",
    "Asian Boyz (ABZ) is one of the largest Asian street gangs in the US, founded in Long Beach in the 1980s. Predominantly Cambodian/Southeast Asian. Operates in Long Beach, Lakewood, and other areas.",
    "Tiny Rascal Gang (TRG) is a Cambodian/Southeast Asian gang active in Long Beach and the greater LA area. Rivals of Asian Boyz.",
    "Suicidal Town (ST) is a Crip-affiliated gang in Long Beach's Cambodia Town area near Anaheim St.",
    "Insane Crips are one of the oldest and largest Crip sets in Long Beach, operating in the central/north areas of the city.",
    "Rollin 20s Crips Long Beach operate in the north Long Beach area near the 710 freeway.",
    "Tongan Crip Gang (TCG) is a Tongan/Pacific Islander gang in the Long Beach/Carson area. One of the few Polynesian Crip sets.",
    "Longos 13 has been documented as one of the most violent gangs in Long Beach, responsible for multiple hate-crime shootings targeting Black residents in their territory.",

    # ── Harbor Area / Wilmington / San Pedro ──────────────────────────────
    "East Side Wilmas (ESW) is a Sureño gang in Wilmington near East L Street and the harbor area.",
    "Wilmington has multiple Sureño cliques including West Side Wilmington and East Side Wilmas, operating near Avalon Blvd and Pacific Coast Highway.",
    "San Pedro has the Rancho San Pedro Crips and various Sureño cliques operating near the port.",
    "Harbor City has Sureño gangs including Harbor City Crips and Harbor City 13.",
    "The Wilmington/Harbor area gangs are primarily Sureño 13-affiliated with some Crip presence near the housing projects.",

    # ── Westside / Venice / Culver City / Santa Monica ────────────────────
    "Venice 13 (V13/VxL) is a large Sureño gang operating in Venice Beach, primarily around Oakwood/Ghost Town area near Lincoln Blvd and Rose Ave. One of the oldest Hispanic gangs on the Westside.",
    "Venice Shoreline Crips (VSLC) operate in Venice near the beach/boardwalk area. Rivals of Venice 13.",
    "Culver City Boyz (CCB) is a Sureño 13 gang operating in Culver City near Venice Blvd and Sepulveda Blvd.",
    "Santa Monica 13 (SM13) is a Sureño gang in Santa Monica near the Pico neighborhood and 17th Street area.",
    "Mar Vista Gardens gang operates in the Mar Vista Gardens housing project near Inglewood Blvd and Centinela Ave. Sureño-affiliated.",
    "Sotel 13 is a Sureño gang in West LA near Sawtelle Blvd and Sepulveda.",
    "The Westside of LA has lower gang density than South or East LA but still has significant Sureño presence along the Venice/Mar Vista/Culver City corridor.",

    # ── Pomona / Azusa / SGV ──────────────────────────────────────────────
    "Pomona has a high concentration of gangs with both Sureño (12th Street Sharks, South Side Pomona, 456 Island Pomona) and Crip/Blood sets.",
    "Azusa 13 is a Sureño gang in Azusa near Azusa Ave and Foothill Blvd. Subject to gang injunction.",
    "El Monte Flores (EMF13) is a large Sureño gang in El Monte operating near Garvey Ave and Tyler Ave.",
    "The San Gabriel Valley has extensive Sureño 13 activity in Pomona, Azusa, El Monte, Baldwin Park, and La Puente.",
    "Wah Ching (WC) is a Chinese-American gang operating in the San Gabriel Valley, Chinatown, and Monterey Park. Involved in gambling, extortion, and drug trafficking. Founded in the 1960s.",
    "Asian gangs in the San Gabriel Valley include Wah Ching, Four Seas Gang, and various Vietnamese gangs in areas around Alhambra, Monterey Park, and San Gabriel.",

    # ── General LA Gang Facts ─────────────────────────────────────────────
    "LA County has approximately 450 active gangs with an estimated 45,000 members according to LAPD and LASD statistics.",
    "The California Department of Justice CalGang database tracks over 200,000 gang members statewide, with the largest concentration in Los Angeles County.",
    "Sureño 13 gangs are allied with the Mexican Mafia (La Eme) prison gang. The '13' represents the 13th letter of the alphabet (M for Mexican Mafia).",
    "Norteño 14 gangs are allied with Nuestra Familia prison gang. The '14' represents the 14th letter (N for Norteño). Norteño presence in LA County is minimal compared to Sureños.",
    "Gang injunctions are civil court orders that restrict documented gang members from associating in specific geographic areas. LAPD and the LA City Attorney have obtained injunctions against dozens of gangs.",
    "The LAPD has a specialized Gang and Narcotics Division. Each of the 21 patrol divisions also has a dedicated gang unit.",
    "LASD (LA County Sheriff) has the Operation Safe Streets (OSS) bureau dedicated to gang enforcement in unincorporated areas and contract cities.",
    "Colors: Crips traditionally wear blue, Bloods/Pirus wear red, Sureños wear blue or gray, Norteños wear red. However, many gangs have evolved past strict color identification.",
    "Common gang identifiers include tattoos (area code, gang name, three dots, teardrops), hand signs, specific sports team affiliations (LA Dodgers for Sureños, Raiders historically for various gangs).",
    "The 2020s have seen increased use of social media by LA gangs for recruitment, intimidation, and claiming credit for violence. Instagram and YouTube are primary platforms.",
    "Drive-by shootings remain a significant gang tactic in LA County. LAPD reports approximately 300-400 gang-related shootings per year in the city alone.",
    "The Mexican Mafia (La Eme) controls all Sureño 13 gangs from prison, collecting 'taxes' (10-13% of drug sales, extortion proceeds) from street-level gangs.",
    "Shot Spotter acoustic gunfire detection systems are deployed in several high-gang-activity areas of LA including South LA, Watts, and parts of the Valley.",
    "LAPD's COMPSTAT data shows gang-related crime is heavily concentrated in South Bureau, Central Bureau, and Valley Bureau divisions.",
    "Gang-related homicides in LA have declined from approximately 600/year in the 1990s to 100-200/year in the 2020s, though the numbers fluctuate.",

    # ── More SFV Detail (near Burbank) ────────────────────────────────────
    "The LAPD Foothill Division covers Sun Valley, Pacoima, and areas directly adjacent to Burbank's eastern border. This division has significant Sureño gang activity.",
    "LAPD North Hollywood Division covers North Hollywood, Valley Village, and parts of Sun Valley. Gang activity here includes Langdon Street, Radford, and 18th Street cliques.",
    "LAPD Mission Division covers Panorama City, Arleta, and parts of the northeast Valley. Blythe Street and Columbus Street gangs are primary concerns.",
    "Pacoima has the highest concentration of gang activity in the San Fernando Valley, with approximately 15-20 documented gangs.",
    "Sun Valley gang activity includes Sureño cliques, Armenian-affiliated gangs, and remnants of the Vineland Boys post-RICO prosecution.",
    "The 5 Freeway corridor between Burbank and Pacoima serves as an informal boundary between lower-crime Burbank and higher-crime areas to the east.",
    "North Hollywood has seen gentrification reduce some gang activity near the NoHo Arts District but gangs remain active south of Victory Blvd and east of Lankershim.",
    "Van Nuys Blvd is a major corridor for gang activity in the Valley, running through territories of multiple gangs from Pacoima south through Van Nuys.",
    "The San Fernando Valley has approximately 100+ documented gangs according to LAPD Valley Bureau statistics.",
    "Arleta (between Pacoima and Sun Valley) has Sureño gang presence including Barrio Arleta cliques.",

    # ── Alliance/Rivalry Structure ────────────────────────────────────────
    "In the Crip vs Blood structure: Crips and Bloods are not monolithic organizations. Individual sets form alliances and rivalries that often cross the Crip/Blood divide.",
    "Neighborhood Crips vs Gangster Crips is a major intra-Crip division. NHC sets (Rollin 20s-100s) generally ally with each other against Gangster Crip sets.",
    "Hoover Criminals (formerly Hoover Crips) are enemies of nearly everyone — both Crip sets and Blood sets. They control Hoover St from about 43rd to 112th Street.",
    "The Sureño/Norteño divide in California is enforced by prison gangs. In LA County, virtually all Hispanic gangs claim Sureño/13 affiliation.",
    "Asian gangs in LA generally do not participate in the Crip/Blood or Sureño/Norteño structure. They operate independently.",
    "Armenian Power's alliance with the Mexican Mafia gives them Sureño 13 status, making them one of the few non-Hispanic gangs with formal Sureño affiliation.",
    "Black gangs and Hispanic gangs in LA have experienced significant racial conflict, particularly where Florencia 13 and 18th Street territories overlap with Black neighborhoods.",
    "The 2006 federal indictment of Florencia 13 for targeting Black residents resulted in multiple hate crime convictions and highlighted Black-Brown gang tensions in South LA.",
    "Piru Bloods and Bounty Hunter Bloods are generally allied. Both operate in the Watts/Compton corridor.",
    "Rollin 60s NHC and Bounty Hunter Bloods have a working alliance despite being Crip and Blood respectively, united by common enemies (Eight Tray Gangster Crips, Hoover Criminals).",

    # ── Historical Context ────────────────────────────────────────────────
    "The first recognized street gangs in LA formed in the 1920s-1930s in East LA (Maravilla, White Fence) and were primarily Mexican-American.",
    "Black gangs in LA emerged in the 1950s-1960s, with the Slausons, Gladiators, and Businessmen preceding the Crips and Bloods.",
    "The Crips were founded in 1969 by Raymond Washington in South Central. By 1971 they had expanded rapidly, prompting the formation of the Bloods as opposition.",
    "The 1980s crack cocaine epidemic fueled an explosion of gang violence in LA. Gangs that had been primarily territorial became major drug distribution networks.",
    "The 1992 Rodgers King riots led to a temporary Crip-Blood truce in Watts (the 'Watts Truce'). Some gang activity temporarily declined.",
    "MS-13 was founded in the Pico-Union/Westlake area of LA in the early 1980s by Salvadoran refugees fleeing civil war. They initially formed for protection against established Mexican gangs.",
    "18th Street was founded in the 1960s near 18th and Union in the Rampart area. It grew by accepting members of all ethnicities, unlike traditional Mexican gangs.",
    "The 1988 gang war in Glendale between Armenian Power and local Mexican gangs established AP13 as the dominant Armenian gang in the area.",
    "LAPD's Gang Reduction and Youth Development (GRYD) program, established in 2007, targets the highest-risk areas with intervention workers. Covers 21 GRYD zones citywide.",
    "The 2020 George Floyd protests and subsequent police reform movements led to reduced proactive gang enforcement in some areas, with some researchers noting increased gang violence in 2021-2022.",

    # ── Specific Territory Details ────────────────────────────────────────
    "East Side Torrance (EST13) is a Sureño gang in Torrance/Gardena area near Western Ave.",
    "Tortilla Flats (TF13) is a Sureño gang in Venice/Mar Vista area. Long-standing rivalry with Venice 13.",
    "Playboy Gangster Crips operate in West LA/Cadillac-Corning area near Pico and La Brea.",
    "Marvin Gangster Crips operate in the Crenshaw area near Marvin Ave and 47th St.",
    "School Yard Crips (SYC) operate in South LA near Manchester and Normandie.",
    "Rollin 30s Harlem Crips operate in the Jefferson Park area near Crenshaw and Jefferson.",
    "Rollin 40s Neighborhood Crips operate in Leimert Park/Baldwin Hills near Crenshaw and Martin Luther King Jr Blvd.",
    "Rollin 90s Neighborhood Crips operate in the Westmont area of unincorporated South LA.",
    "Rollin 100s Crips operate in the Watts/Willowbrook area south of Imperial Highway.",
    "Pueblo Bishop Bloods (PBB) operate in the Pueblo del Rio housing project in South LA near Slauson and Compton Ave.",
    "Blood Stone Villains (BSV) is a Blood set in Inglewood near Market St and La Brea.",
    "Centinela Park Family Bloods operate in Inglewood near Centinela Ave and Florence Ave.",
    "West Side Piru operates in Inglewood near Crenshaw Blvd and Hardy St.",
    "Crenshaw Mafia Gang operates along Crenshaw Blvd between Slauson and Florence. Blood-affiliated.",
    "Athens Park Bloods operate in the Athens area of unincorporated LA near Vermont and Imperial.",
    "Watts Varrio Grape operates in Watts near Grape Street (the same street as Grape Street Crips but a different, Sureño-affiliated gang).",
    "Compton Varrio 70s (CV70s) is a Sureño gang in Compton near 70th Street and the 710 freeway.",
    "Compton Varrio Tortilla Flats is a Sureño gang in Compton near Compton Blvd and Wilmington Ave.",
    "South Side Compton Crips operate in South Compton near Rosecrans and Central Ave.",
    "Palm and Oak Gangster Crips operate in Compton near Palmer and Oak Streets.",
    "Campanella Park Piru is a Blood/Piru set in Compton near Campanella Park.",
    "Lantana Blocc Compton Crips operate in Compton near Lantana Dr.",
    "Acacia Blocc Compton Crips operate in Compton near Acacia Ave.",
    "Tragniew Park Compton Crips operate in Compton near Tragniew Ave.",

    # ── More Valley/Burbank-Adjacent ──────────────────────────────────────
    "Barrio Pacas (BP13) is a Sureño gang in Pacoima operating near Pacas Ave.",
    "Trojan Locos (TLS13) is a Sureño gang in Sun Valley near Trojan Ave.",
    "Barrio North Hollywood (BNH) is a Sureño clique in North Hollywood.",
    "Barrio Canoga Park (BCP) operates in Canoga Park near Canoga Ave and Vanowen.",
    "Reseda has Sureño gang activity along Sherman Way and Reseda Blvd.",
    "Sepulveda Boys is a Valley Sureño gang operating along Sepulveda Blvd in Van Nuys/Sherman Oaks area.",
    "Valley gangs tend to be smaller and more territorial than their South LA counterparts, but are connected to the same prison gang (Mexican Mafia) hierarchy.",
    "The Van Nuys courthouse area on Van Nuys Blvd sees regular gang-related activity due to court appearances and rival gangs crossing paths.",
    "North Hollywood Park (Lankershim/Magnolia) has been a gathering spot for various gang members and is monitored by LAPD North Hollywood Division gang unit.",
    "The Metro Orange Line (now G Line) corridor through the Valley has stations that border gang territories in Van Nuys, Panorama City, and North Hollywood.",

    # ── Gang Injunctions ──────────────────────────────────────────────────
    "Gang injunctions in LA prohibit named gang members from associating within defined 'safety zones.' Violations are misdemeanors punishable by up to 6 months in jail.",
    "The City of LA has obtained injunctions against approximately 45 gangs. Major ones include Blythe Street (2000), Canoga Park Alabama (2003), and Venice 13 (2003).",
    "The Blythe Street gang injunction covers a 6.7 square mile area in Panorama City/Van Nuys — one of the largest safety zones in LA.",
    "In 2018, a California court ruled that some gang injunction enforcement violated civil rights, leading to reforms in how injunctions are applied.",
    "The LAPD's CalGang database has faced criticism for including people who are not active gang members. A 2016 audit found errors in 12% of entries.",
    "Gang enhancement charges (Penal Code 186.22) add 5-10 years to sentences for crimes committed 'for the benefit of' a gang. This is one of the primary tools prosecutors use against gang members.",

    # ── Current Trends (2020s) ────────────────────────────────────────────
    "Social media beefing (Instagram, YouTube, TikTok) has replaced traditional territory-based conflicts as a primary driver of gang violence in LA in the 2020s.",
    "Ghost guns (unserialized firearms built from kits) have become a major source of gang weapons in LA. LAPD seizes hundreds annually.",
    "The fentanyl crisis has shifted some LA gang activity from cocaine/crack to fentanyl distribution, with Mexican cartels as primary wholesale suppliers.",
    "Street takeovers (sideshows) in LA often involve gang-connected individuals and have become a significant public safety concern in the Valley and South LA.",
    "Sureño 13 gangs in the Valley have increasingly recruited Central American (Guatemalan, Honduran) youth in addition to traditional Mexican-American membership.",
    "The closure of California's juvenile detention facilities (DJJ) in 2023 has led to younger gang members being housed in county facilities, raising concerns about recruitment.",
    "Proposition 47 (2014) and Proposition 57 (2016) reduced sentences for many property and drug crimes, which some law enforcement argue led to increased gang-related theft activity.",
    "LAPD's Community Safety Partnership (CSP) places dedicated officers in specific housing projects (Jordan Downs, Nickerson Gardens, Imperial Courts) to build relationships and reduce gang violence.",
    "The 2020s have seen an increase in follow-home robberies in LA, some attributed to street gang members targeting wealthy areas (Beverly Hills, Hollywood Hills) for home invasion crews.",
    "Armenian Power has evolved from a traditional street gang into more of an organized crime group focused on white-collar crimes (identity theft, bank fraud, insurance fraud) while maintaining street-level drug operations.",

    # ── Demographics and Structure ────────────────────────────────────────
    "Hispanic/Latino gangs make up approximately 55% of documented gangs in LA County. Black gangs represent about 35%, and Asian/other gangs approximately 10%.",
    "The average age of gang members in LA has been rising. While recruitment still targets 12-17 year olds, many active members are now in their 20s-30s with some leaders in their 40s-50s.",
    "Female gang membership in LA is estimated at 8-10% of total gang population. Female members often play roles in drug transportation, intelligence gathering, and weapons storage.",
    "Many LA gangs have multi-generational membership with fathers, sons, and grandsons in the same gang. This is especially true in East LA (White Fence, Maravilla) and South LA (various Crip/Blood sets).",
    "Gang hierarchy in Sureño 13 gangs typically follows: Veteranos (OGs/shot callers), Soldados (active soldiers), Peewees (young recruits). Mexican Mafia members in prison sit above all street-level ranks.",
    "Crip and Blood gangs are decentralized with no single leader. Each set has its own shot callers (OGs) who control their specific territory.",

    # ── Additional Territory Facts ────────────────────────────────────────
    "Ramona Gardens Maravilla operates in the Ramona Gardens housing project in Boyle Heights. One of the oldest gangs in East LA.",
    "Cuatro Flats (4F) is a Sureño gang in Boyle Heights near 4th Street. Active since the 1930s.",
    "Clover 13 (CLV) operates in South Central LA near Clover St.",
    "Mara Salvatrucha Hollywood Locos clique operates in the Hollywood area near Western Ave and Santa Monica Blvd.",
    "Harpys (HRP13) operate in the Harbor area/San Pedro.",
    "Watts have over 20 documented gangs in an area of approximately 2.1 square miles, making it one of the most gang-dense areas in LA.",
    "Jordan Downs housing project (Grape Street Crips territory) is scheduled for redevelopment which may displace gang territory boundaries.",
    "Nickerson Gardens housing project (Bounty Hunter Bloods territory) is the largest public housing west of the Mississippi with over 1,000 units.",
    "Imperial Courts housing project (PJ Watts Crips) is directly adjacent to Nickerson Gardens, creating constant territorial friction.",
    "Venice Oakwood/Ghost Town was historically a Black neighborhood where Venice Shoreline Crips formed. Gentrification has pushed much of this activity south toward Mar Vista.",
]

def main():
    print(f"Ingesting {len(FACTS)} LA gang intelligence facts into Nova's memory...")
    success = 0
    for i, fact in enumerate(FACTS):
        if remember(fact):
            success += 1
        if (i + 1) % 100 == 0:
            print(f"  Progress: {i+1}/{len(FACTS)} ({success} ok)")
            time.sleep(1)
    print(f"\nComplete: {success}/{len(FACTS)} facts ingested into vector memory.")
    print(f"Source: local_knowledge | Metadata: gang_intelligence / los_angeles")

if __name__ == "__main__":
    main()
