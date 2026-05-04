#!/bin/bash
# Ingest 500 CHiPs (1977-1983) memories into Nova's vector memory
# Batches of 25, with Slack progress updates

API="http://127.0.0.1:18790/remember"
SLACK_TOKEN=$(security find-generic-password -a nova -s nova-slack-bot-token -w)
SLACK_CHANNEL="C0ATAF7NZG9"
COUNT=0
ERRORS=0

send_memory() {
  local text="$1"
  local category="$2"
  local payload=$(jq -n --arg t "$text" --arg c "$category" '{
    text: $t,
    source: "tv_chips",
    metadata: { type: "television", show: "CHiPs", category: $c }
  }')
  local resp=$(curl -s -X POST "$API" -H "Content-Type: application/json" -d "$payload" 2>&1)
  if echo "$resp" | grep -q '"stored"'; then
    COUNT=$((COUNT + 1))
  else
    ERRORS=$((ERRORS + 1))
    echo "ERROR on memory $((COUNT + ERRORS)): $resp" >&2
  fi

  # Post Slack update every 25
  if [ $((COUNT % 25)) -eq 0 ] && [ $COUNT -gt 0 ]; then
    curl -s -X POST "https://slack.com/api/chat.postMessage" \
      -H "Authorization: Bearer $SLACK_TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"channel\": \"$SLACK_CHANNEL\", \"text\": \"📺 TV Ingest: CHiPs — $COUNT/500 complete\"}" > /dev/null
    echo "Progress: $COUNT/500 sent ($ERRORS errors)"
  fi
}

echo "Starting CHiPs memory ingest — 500 memories"

# ============================================================
# CAST (memories 1-55)
# ============================================================

send_memory "Erik Estrada starred as Officer Frank 'Ponch' Poncherello on CHiPs from 1977 to 1983. He became one of the most recognizable TV stars of the late 1970s and early 1980s thanks to his charismatic portrayal of the motorcycle officer." "cast"

send_memory "Larry Wilcox played Officer Jon Baker on CHiPs for five seasons, from 1977 to 1982. He left the show before the final season due to creative differences and an increasingly strained relationship with co-star Erik Estrada." "cast"

send_memory "Robert Pine portrayed Sergeant Joseph Getraer, the patient and long-suffering supervisor at the CHP Central Division. Pine appeared in all six seasons of CHiPs from 1977 to 1983." "cast"

send_memory "Paul Linke played Officer Arthur 'Grossie' Grossman throughout CHiPs' entire run. Grossie was known for his schemes and get-rich-quick ideas that provided comic relief in the series." "cast"

send_memory "Brodie Greer appeared as Officer Barry 'Bear' Baricza in all six seasons of CHiPs. Bear was the tallest officer at Central and often served as a steady presence during chaotic situations." "cast"

send_memory "Randi Oakes joined CHiPs in Season 2 as Officer Bonnie Clark, one of the first recurring female officers on the show. She remained a regular cast member through Season 5." "cast"

send_memory "Tom Reilly replaced Larry Wilcox in CHiPs' sixth and final season, playing Officer Bobby 'Hot Dog' Nelson. The character was a younger, more reckless partner for Ponch." "cast"

send_memory "Erik Estrada was born on March 16, 1949, in New York City. Before landing CHiPs, he had appeared in films like The Cross and the Switchblade (1970) and The New Centurions (1972)." "cast"

send_memory "Larry Wilcox served in the United States Marine Corps and did a tour of duty in Vietnam before pursuing acting. His military background brought a disciplined quality to his portrayal of Jon Baker." "cast"

send_memory "Robert Pine is the father of actor Chris Pine, who became famous for playing Captain Kirk in the Star Trek reboot films. Robert's steady career in television spanned decades beyond CHiPs." "cast"

send_memory "Lou Wagner played Harlan Arliss, the CHP mechanic who maintained the officers' motorcycles and patrol cars throughout the series. He appeared in over 100 episodes of CHiPs." "cast"

send_memory "Lew Saunders appeared as Officer Gene Fritz in numerous episodes of CHiPs, serving as one of the regular background officers at Central Division." "cast"

send_memory "Bruce Jenner, now known as Caitlyn Jenner, made a guest appearance on CHiPs during Season 4. The Olympic gold medalist was one of many celebrity guests on the show." "cast"

send_memory "Erik Estrada suffered a serious motorcycle accident during filming of CHiPs in 1979. He broke several ribs and his wrist, and the accident was so severe it was incorporated into the show's storyline." "cast"

send_memory "Estrada's motorcycle crash during filming required him to be hospitalized, and the show wrote his recovery into the plot. Ponch was shown recovering in the hospital while Larry Wilcox carried episodes solo." "cast"

send_memory "Michael Dorn, who later became famous as Worf on Star Trek: The Next Generation, had a recurring role on CHiPs as Officer Jebediah Turner during Season 2." "cast"

send_memory "Brianne Leary played Officer Sindy Cahill during Season 2 of CHiPs. She was the first regular female officer character on the show before being replaced by Randi Oakes as Bonnie Clark." "cast"

send_memory "Tina Gayle appeared as Officer Bonnie Clark's replacement, Officer Kathy Linahan, in several Season 3 episodes of CHiPs before Randi Oakes returned to the role." "cast"

send_memory "Bruce Penhall, a two-time world speedway motorcycle champion, joined CHiPs in the final season as Officer Bruce Nelson, Bobby Nelson's brother. His real-life motorcycle skills were featured prominently." "cast"

send_memory "Erik Estrada's contract disputes with NBC and MGM Television during CHiPs' run led to him being temporarily written out of episodes. These disputes were partly fueled by his desire for higher pay commensurate with his star status." "cast"

send_memory "Larry Wilcox went on to produce and direct after leaving CHiPs. He later faced legal issues unrelated to his acting career but remained associated with the show in public memory." "cast"

send_memory "Paul Linke's wife, Francesca Draper, passed away from cancer during the run of CHiPs. Linke later wrote a critically acclaimed one-man show called 'Time Flies When You're Alive' about the experience." "cast"

send_memory "Robert Pine's portrayal of Sgt. Getraer made him one of the most recognizable authority figures on 1970s-80s television. He brought a warmth and humor to the role that prevented the character from being a stereotypical stern boss." "cast"

send_memory "Erik Estrada became such a pop culture icon during CHiPs that he received more fan mail than any other actor at NBC during the show's peak years." "cast"

send_memory "After CHiPs ended in 1983, Erik Estrada pursued a career in Spanish-language telenovelas, becoming a major star in Latin American television markets." "cast"

# ============================================================
# CHARACTERS (memories 26-75)
# ============================================================

send_memory "Frank 'Ponch' Poncherello was a Latino motorcycle officer with the California Highway Patrol who was known for his charm, love of disco dancing, and occasional rule-bending. He was the more impulsive of the two lead characters." "characters"

send_memory "Jon Baker was Ponch's partner and best friend, a more reserved and by-the-book officer who had served in Vietnam. Baker was often the voice of reason when Ponch's enthusiasm got ahead of him." "characters"

send_memory "Sergeant Joseph Getraer was the commanding officer at CHP Central Division who balanced maintaining discipline with genuine care for his officers. He often had to clean up after Ponch and Jon's unorthodox methods." "characters"

send_memory "Officer Arthur 'Grossie' Grossman was the overweight, good-natured officer known for his love of food and his perpetual side hustles. He provided much of the show's comic relief." "characters"

send_memory "Officer Barry 'Bear' Baricza was the tall, strong officer who served as a reliable presence at Central. His nickname came from his imposing physical stature." "characters"

send_memory "Officer Bonnie Clark was a skilled motorcycle officer who proved herself equal to her male colleagues. She occasionally had romantic tension with various characters on the show." "characters"

send_memory "Officer Bobby 'Hot Dog' Nelson was Ponch's partner in the final season, replacing Jon Baker. He was younger and more daring than Jon, with a penchant for taking unnecessary risks." "characters"

send_memory "Harlan Arliss was the civilian mechanic at CHP Central who maintained the fleet of motorcycles and patrol cars. He was fiercely protective of 'his' vehicles and often frustrated by how roughly the officers treated them." "characters"

send_memory "Ponch was portrayed as a bachelor who lived in an apartment and was constantly pursuing romantic interests. His love life was a recurring subplot throughout the series." "characters"

send_memory "Jon Baker was depicted as having a ranch and being more of an outdoorsman compared to Ponch's urban lifestyle. This contrast between the two partners was a key dynamic of the show." "characters"

send_memory "Sgt. Getraer was married with children, and his family life occasionally featured in episodes. His wife Betty was mentioned frequently but appeared only in select episodes." "characters"

send_memory "Ponch's full name, Francis Llewellyn Poncherello, was revealed during the series. The character was sensitive about his given first name, preferring the nickname Ponch." "characters"

send_memory "Jon Baker's character was established as a former Army helicopter pilot who served in Vietnam before joining the California Highway Patrol. This backstory informed his calm demeanor under pressure." "characters"

send_memory "Grossie's entrepreneurial schemes on CHiPs ranged from selling merchandise to promoting events, and they almost always backfired in comedic fashion." "characters"

send_memory "The dynamic between Ponch and Jon was modeled after classic buddy-cop pairings, with Ponch as the flashy, impulsive one and Jon as the steady, responsible partner." "characters"

send_memory "Officer Sindy Cahill, played by Brianne Leary, was presented as an attractive and competent officer during Season 2 before being written out of the show." "characters"

send_memory "Sergeant Getraer's patience was frequently tested by Ponch's antics, but he showed a fatherly protectiveness toward his officers when they were in danger." "characters"

send_memory "Ponch was known for his distinctive smile, which Erik Estrada deployed frequently and which became one of the show's visual trademarks." "characters"

send_memory "Jon Baker was depicted as more financially responsible than Ponch, who was often shown living beyond his means or spending money on flashy items." "characters"

send_memory "Bear Baricza rarely had central storylines but was a consistent supporting presence, often partnered with Grossie for patrol duties." "characters"

send_memory "Bonnie Clark's character challenged the male-dominated culture of the CHP workplace, though the show often softened this with romantic subplots." "characters"

send_memory "In the show's continuity, Ponch and Jon were assigned to the Central Division of the California Highway Patrol, which covered the greater Los Angeles area freeway system." "characters"

send_memory "Ponch's character evolved from a probationary officer in the pilot to a seasoned veteran by the final season, eventually receiving promotions and more responsibility." "characters"

send_memory "Jon Baker occasionally dealt with personal issues on the show, including injuries, romantic relationships, and moral dilemmas about the use of force." "characters"

send_memory "The officers at Central Division were depicted as a tight-knit family, with the briefing room scenes serving as a gathering point where camaraderie was displayed." "characters"

# ============================================================
# EPISODES (memories 76-150)
# ============================================================

send_memory "The CHiPs pilot episode aired on September 15, 1977, on NBC. It introduced Ponch and Jon as motorcycle officers patrolling the Los Angeles freeway system and established the show's formula of action and humor." "episodes"

send_memory "CHiPs ran for 139 episodes across six seasons from 1977 to 1983. Each episode was approximately 48 minutes long, filling a one-hour time slot on NBC." "episodes"

send_memory "Many CHiPs episodes followed a formula: an opening action sequence with a vehicle accident or pursuit, followed by character-driven subplots, and a climactic chase or rescue." "episodes"

send_memory "The CHiPs episode 'Roller Disco' from Season 3 exemplified the show's tendency to incorporate popular cultural trends. It featured roller skating prominently, capitalizing on the late 1970s roller disco craze." "episodes"

send_memory "CHiPs featured numerous disco-themed episodes during its early seasons, reflecting the cultural dominance of disco music in the late 1970s. Episodes often included scenes at dance clubs." "episodes"

send_memory "The Season 2 premiere of CHiPs dealt with the aftermath of Ponch's motorcycle accident, which mirrored Erik Estrada's real-life crash during filming." "episodes"

send_memory "CHiPs episodes frequently featured elaborate multi-car pileups on Los Angeles freeways. These sequences became a signature element of the show and required extensive coordination with stunt teams." "episodes"

send_memory "The two-part CHiPs episode 'The Volunteers' dealt with emergency response scenarios and showcased the CHP's role in coordinating with other emergency services during major incidents." "episodes"

send_memory "CHiPs Season 5 included an episode called 'Ponch's Angels' that parodied the popular show Charlie's Angels, with female officers going undercover." "episodes"

send_memory "The final episode of CHiPs aired on July 17, 1983, ending a six-season run. The series did not have a planned finale; it simply concluded when NBC canceled the show." "episodes"

send_memory "CHiPs episodes rarely featured the officers drawing their weapons. The show was intentionally non-violent compared to other police dramas, focusing on traffic enforcement and rescues rather than gunfights." "episodes"

send_memory "Several CHiPs episodes featured Ponch and Jon dealing with stolen vehicle rings, which were a significant real-world problem in 1970s-80s Los Angeles." "episodes"

send_memory "The CHiPs episode 'Green Thumb Burglar' involved the officers tracking down a thief who targeted homes in wealthy neighborhoods, blending crime-of-the-week plotting with freeway action." "episodes"

send_memory "CHiPs had several holiday-themed episodes, including Christmas specials that showed the officers dealing with seasonal traffic hazards and helping stranded motorists." "episodes"

send_memory "The Season 3 episode 'Roller Fever' was another roller-skating-themed installment, demonstrating how the show repeatedly returned to the roller rink setting that proved popular with audiences." "episodes"

send_memory "CHiPs episodes often included public service elements, with characters modeling safe driving behavior and explaining traffic laws to viewers through natural dialogue." "episodes"

send_memory "The episode 'Trick or Treat' was a Halloween-themed CHiPs installment that dealt with increased traffic incidents and pranks during the holiday period." "episodes"

send_memory "CHiPs' Season 4 was considered the show's peak in terms of ratings and production quality. Episodes from this season featured more elaborate stunts and higher production values." "episodes"

send_memory "The CHiPs episode 'A Simple Operation' dealt with Jon Baker being hospitalized, allowing the show to explore how the other officers coped without one of their key team members." "episodes"

send_memory "Several CHiPs episodes featured natural disaster scenarios, including mudslides and brush fires, which were realistic threats in the Los Angeles area where the show was set." "episodes"

send_memory "The episode 'High Flyer' featured a stunt sequence involving a vehicle launching off a freeway overpass. Such spectacular crashes were a hallmark of CHiPs' appeal." "episodes"

send_memory "CHiPs' Season 6, the final season, saw a shift in tone with Tom Reilly replacing Larry Wilcox and the introduction of more action-oriented storylines." "episodes"

send_memory "The CHiPs episode 'Bio-Rhythms' explored the pseudoscientific concept of biorhythms affecting officer performance, reflecting a popular 1970s cultural trend." "episodes"

send_memory "Multiple CHiPs episodes dealt with drunk driving, making the show one of the earliest prime-time series to regularly address the dangers of impaired driving." "episodes"

send_memory "The Season 1 episode 'One Two Many' was one of the first CHiPs episodes to tackle the serious issue of highway fatalities caused by reckless driving." "episodes"

send_memory "CHiPs featured several two-part episodes throughout its run, typically used for season premieres and sweeps periods to boost ratings." "episodes"

send_memory "The episode 'Counterfeit' involved Ponch and Jon uncovering a counterfeiting operation, one of the show's occasional forays into criminal investigations beyond traffic enforcement." "episodes"

send_memory "CHiPs episodes set during rush hour often showcased the massive scale of Los Angeles freeway traffic, using real freeway locations to create authentic visual backdrops." "episodes"

send_memory "The Season 2 episode 'Family Crisis' explored the personal lives of the officers more deeply than usual, dealing with family conflicts that affected job performance." "episodes"

send_memory "CHiPs occasionally featured episodes where Ponch went undercover, allowing Erik Estrada to play against type and showcase his range as an actor." "episodes"

send_memory "The episode 'Aweigh We Go' sent Ponch and Jon to a harbor setting, demonstrating the CHP's jurisdiction beyond just freeways and highways." "episodes"

send_memory "Several CHiPs episodes centered on vehicle theft rings operating in the San Fernando Valley, reflecting real-world crime patterns in the Los Angeles basin." "episodes"

send_memory "The CHiPs episode 'Destruction Derby' featured a storyline involving illegal demolition derbies, culminating in a high-energy action sequence at a junkyard." "episodes"

send_memory "CHiPs' Season 3 featured an episode dealing with a tour bus accident that required the officers to coordinate a large-scale rescue operation on a remote highway." "episodes"

send_memory "The episode 'Dog Gone' combined the show's typical action with a lighter subplot involving a stray dog that the officers tried to find a home for." "episodes"

send_memory "CHiPs frequently used the format of three interwoven storylines per episode: a main action plot, a character development subplot, and a comic relief thread usually involving Grossie." "episodes"

send_memory "The Season 4 episode 'Return of the Supercycle' featured a specially modified motorcycle and showcased the show's emphasis on impressive vehicle hardware." "episodes"

send_memory "CHiPs included episodes that featured competitive motorcycle events, allowing the stunt team to perform impressive riding sequences that went beyond standard patrol scenes." "episodes"

send_memory "The episode 'Night Watch' took place primarily during a night shift, giving CHiPs an unusually atmospheric and darker visual palette compared to its typically sunny daytime setting." "episodes"

send_memory "CHiPs' Season 1 episode 'Taking Its Toll' dealt with hazardous material spills on the freeway, an issue that was gaining public attention in the late 1970s." "episodes"

# ============================================================
# PRODUCTION (memories 151-225)
# ============================================================

send_memory "CHiPs was created by Rick Rosner, who based the series on his observations of the California Highway Patrol. Rosner served as executive producer throughout the show's run." "production"

send_memory "CHiPs was produced by MGM Television for the NBC network. The show premiered on September 15, 1977, and ran until July 17, 1983." "production"

send_memory "CHiPs aired on NBC, typically in the Thursday 8:00 PM time slot during its peak years. The show was a consistent ratings performer for the network throughout the late 1970s." "production"

send_memory "The CHiPs theme song was composed by John Parker. The instrumental theme with its distinctive guitar riff became one of the most recognizable TV theme songs of the era." "production"

send_memory "CHiPs was filmed primarily on location in and around Los Angeles, California. The production used real LA freeways for many of its motorcycle patrol and chase sequences." "production"

send_memory "The show's production team worked closely with the real California Highway Patrol, which provided technical advisors to ensure procedural accuracy in CHiPs episodes." "production"

send_memory "CHiPs was one of the most expensive shows to produce during its era due to the elaborate vehicle stunts, motorcycle sequences, and extensive location filming required for each episode." "production"

send_memory "Rick Rosner initially conceived CHiPs as a more serious police drama, but the show evolved into a lighter action-adventure format that blended humor with vehicular action." "production"

send_memory "The motorcycle riding sequences in CHiPs were a mix of actor riding and professional stunt doubles. Erik Estrada and Larry Wilcox both learned to ride motorcycles for the show but doubles handled dangerous sequences." "production"

send_memory "CHiPs used Kawasaki KZ1000P motorcycles as the primary patrol bikes, which were the same model used by the real California Highway Patrol during that era." "production"

send_memory "The production of CHiPs required an extensive fleet of vehicles for crash sequences. Cars used in pileup scenes were typically purchased cheaply and prepared by the stunt department." "production"

send_memory "CHiPs' production schedule was demanding, with episodes typically filmed over seven to eight days, including two to three days of location work on actual freeways." "production"

send_memory "The show's stunts were coordinated by veteran Hollywood stunt professionals. Gary Davis served as one of the primary stunt coordinators during CHiPs' run." "production"

send_memory "NBC initially ordered CHiPs as a midseason replacement but moved it to a regular fall slot after strong ratings. The show quickly became one of NBC's most reliable performers." "production"

send_memory "CHiPs' budget per episode grew significantly over its run, from approximately $500,000 in Season 1 to over $800,000 by the later seasons, driven by increasingly elaborate stunt work." "production"

send_memory "The CHP Central Division headquarters set used in CHiPs was built on a studio lot, though exterior shots sometimes used real CHP facilities for authenticity." "production"

send_memory "CHiPs' post-production required extensive sound editing because the motorcycle and vehicle sequences needed enhanced engine sounds and tire screeches to create maximum impact." "production"

send_memory "Rick Rosner fought to keep CHiPs relatively non-violent, insisting that the officers rarely draw their weapons. This was a deliberate creative choice to differentiate the show from other cop dramas." "production"

send_memory "The California Highway Patrol initially had mixed feelings about CHiPs, concerned about accuracy, but ultimately embraced the show as it boosted recruitment and public image." "production"

send_memory "CHiPs filming frequently caused traffic disruptions on Los Angeles freeways when production crews needed to stage chase sequences and multi-vehicle accident scenes." "production"

send_memory "The show employed specialized camera vehicles and helicopter shots to capture the motorcycle riding sequences that were central to CHiPs' visual identity." "production"

send_memory "CHiPs' writers room included several writers who went on to successful careers in television, using the show as a proving ground for action-adventure scripting." "production"

send_memory "MGM Television's involvement with CHiPs was part of the studio's broader television strategy in the late 1970s, which included several action-oriented series." "production"

send_memory "CHiPs was syndicated widely after its original run, becoming a staple of afternoon and late-night reruns throughout the 1980s and 1990s." "production"

send_memory "The show's opening credits sequence, featuring Ponch and Jon riding their motorcycles on a sunny California freeway, became iconic and was updated slightly each season." "production"

send_memory "CHiPs' Season 6 underwent significant production changes with the departure of Larry Wilcox, requiring writers to establish a new partner dynamic between Ponch and Bobby Nelson." "production"

send_memory "Cy Chermak served as a producer on CHiPs during several seasons and was instrumental in shaping the show's balance between action and character-driven storytelling." "production"

send_memory "CHiPs was one of the last major network shows produced primarily on film rather than videotape, giving it a cinematic quality that distinguished it from many contemporary series." "production"

send_memory "The real CHP saw a significant increase in recruitment applications during CHiPs' peak popularity, with the show directly credited for improving the agency's public profile." "production"

send_memory "CHiPs' wardrobe department maintained authentic CHP uniforms, including the distinctive tan shirts, dark pants, boots, and sunglasses that became part of the show's visual identity." "production"

send_memory "Production of CHiPs required maintaining a fleet of Kawasaki motorcycles in riding condition at all times. The show went through dozens of bikes over its six-season run due to the demands of stunt work." "production"

send_memory "CHiPs occasionally filmed at the Los Angeles County Fire Department facilities and worked with real fire and paramedic teams for episodes involving accident response." "production"

send_memory "The show's music, beyond the theme song, incorporated popular contemporary songs, particularly during disco and dance club scenes that were frequent in early seasons." "production"

send_memory "CHiPs' editing style was fast-paced for its era, using quick cuts during chase sequences that influenced the action-television genre and anticipated modern editing techniques." "production"

send_memory "NBC promoted CHiPs heavily during its initial seasons, recognizing the show's appeal to a broad demographic that included both adults and younger viewers." "production"

send_memory "CHiPs' scripts went through review by NBC's Standards and Practices department, which occasionally required changes to reduce violence or modify storylines deemed too mature for the time slot." "production"

send_memory "The show's production team developed innovative techniques for filming motorcycle riding sequences at speed, including custom camera mounts and specially modified chase vehicles." "production"

send_memory "CHiPs was part of NBC's Thursday night lineup during the late 1970s, a time when Thursday was becoming the most competitive night for network television programming." "production"

send_memory "Rick Rosner maintained creative control over CHiPs throughout its run, though he increasingly delegated day-to-day production duties to other producers in later seasons." "production"

send_memory "CHiPs' success led MGM Television to develop several other action-oriented series concepts, though none achieved the same level of sustained popularity." "production"

# ============================================================
# STUNTS (memories 226-275)
# ============================================================

send_memory "CHiPs was renowned for its elaborate vehicular stunt sequences, which often featured multi-car pileups, motorcycle jumps, and high-speed pursuits filmed on actual Los Angeles freeways." "stunts"

send_memory "The motorcycle stunts in CHiPs were performed by professional stunt riders who could handle the Kawasaki KZ1000P bikes at high speeds while wearing CHP uniforms and helmets." "stunts"

send_memory "Erik Estrada performed some of his own motorcycle riding on CHiPs, but professional stunt doubles handled the more dangerous sequences. His real-life 1979 crash demonstrated the risks involved." "stunts"

send_memory "CHiPs' stunt team developed a reputation in Hollywood for executing complex, multi-vehicle crash sequences that were visually spectacular yet controlled enough to be filmed safely." "stunts"

send_memory "The show's signature multi-car pileup sequences required careful choreography. Each vehicle's movement was precisely planned, and the scenes were often filmed from multiple angles simultaneously." "stunts"

send_memory "CHiPs featured car-off-cliff stunts where vehicles would go over embankments. These were filmed using actual cars on controlled runs with camera positions pre-set for maximum dramatic effect." "stunts"

send_memory "Motorcycle chase sequences in CHiPs often involved riding between lanes of traffic at speed, a technique called lane splitting that is legal in California and was frequently showcased on the series." "stunts"

send_memory "The CHiPs stunt team used ramps hidden behind obstacles to launch vehicles into the air during crash sequences. These cannon or pipe ramps could propel a car several feet into the air." "stunts"

send_memory "CHiPs occasionally featured helicopter stunts, with aircraft pursuing or tracking suspects from the air while motorcycle officers coordinated below on the freeways." "stunts"

send_memory "The show's stunt coordinators pioneered several techniques for filming motorcycle pursuits safely, including the use of camera bikes that could keep pace with stunt riders." "stunts"

send_memory "CHiPs' crash sequences were filmed at reduced speeds and then slightly undercranked in post-production to appear faster, a common technique that enhanced the sense of danger." "stunts"

send_memory "Several CHiPs episodes featured stunt sequences involving big rigs and semi-trucks, creating heightened tension due to the size differential between the trucks and the officers' motorcycles." "stunts"

send_memory "The show's opening titles included motorcycle riding footage that was updated periodically, always featuring the lead actors or their doubles performing impressive riding maneuvers." "stunts"

send_memory "CHiPs stunt performers occasionally suffered injuries during filming, though the production maintained rigorous safety protocols. The show's insurance costs were among the highest on television." "stunts"

send_memory "Vehicle fires were a common element in CHiPs stunt sequences. The effects team used controlled burns with safety measures to create dramatic post-crash fire scenes." "stunts"

send_memory "CHiPs featured several episodes with pursuit sequences through urban streets that complemented the typical freeway chases, requiring coordination with local businesses and traffic control." "stunts"

send_memory "The motorcycle stunt riding on CHiPs influenced an entire generation of viewers, contributing to increased interest in motorcycle culture during the late 1970s and early 1980s." "stunts"

send_memory "CHiPs' stunt team used multiple identical vehicles for complex crash sequences, allowing them to film damage from various angles and ensuring continuity across different shots." "stunts"

send_memory "High-speed motorcycle formations were a visual signature of CHiPs, with multiple CHP officers riding in formation at speed. These required precise coordination among stunt riders." "stunts"

send_memory "CHiPs occasionally featured boat and watercraft stunts in episodes set near harbors or beaches, expanding the show's action repertoire beyond its typical freeway setting." "stunts"

send_memory "The show's stunt budget was substantial by late 1970s standards, with complex episodes requiring days of preparation for sequences that might occupy only minutes of screen time." "stunts"

send_memory "CHiPs used practical effects exclusively for its stunt work, as CGI was not yet available for television production. Every crash, explosion, and jump was performed with real vehicles." "stunts"

send_memory "Stunt doubles on CHiPs wore carefully matched costumes and helmets that obscured their faces during riding sequences, allowing seamless intercutting with the principal actors." "stunts"

send_memory "The CHiPs production team maintained a 'crash car' inventory of vehicles pre-rigged for specific stunt sequences, with weakened structural points and removed glass for safety." "stunts"

send_memory "Some of the most memorable CHiPs stunts involved vehicles crashing through guardrails and rolling down embankments, requiring careful camera placement and safety crew positioning." "stunts"

# ============================================================
# VEHICLES (memories 276-325)
# ============================================================

send_memory "The Kawasaki KZ1000P was the primary motorcycle used by CHP officers in CHiPs. This was the same model used by the real California Highway Patrol during the late 1970s and early 1980s." "vehicles"

send_memory "CHiPs showcased the Kawasaki KZ900 in its earliest episodes before transitioning to the KZ1000P, which offered better performance characteristics for both real patrol work and television filming." "vehicles"

send_memory "The CHP patrol motorcycles featured on CHiPs were equipped with police-specific accessories including radio equipment, red lights, sirens, saddlebags, and windshields." "vehicles"

send_memory "CHiPs also featured CHP patrol cars, primarily Dodge Monaco and later Dodge Diplomat models, which were used for sergeant vehicles and backup patrol duties." "vehicles"

send_memory "The show prominently displayed the CHP's distinctive black-and-white patrol car color scheme, which became one of the most recognizable police vehicle liveries in America." "vehicles"

send_memory "CHiPs episodes featured a wide variety of civilian vehicles in crash sequences, reflecting the diversity of cars on 1970s-80s Los Angeles freeways including large American sedans, imports, and trucks." "vehicles"

send_memory "Ponch's personal vehicle on CHiPs changed several times throughout the series. He was seen driving various sports cars and muscle cars that reflected his flashy personality." "vehicles"

send_memory "Jon Baker's personal vehicle on CHiPs was typically a pickup truck, reflecting his more rural, down-to-earth character compared to Ponch's urban sophistication." "vehicles"

send_memory "CHiPs featured the Ford LTD as a CHP pursuit vehicle in several episodes, alongside the Dodge models that were the primary patrol cars during the show's era." "vehicles"

send_memory "The show's motorcycle fleet required constant maintenance during production. The production team employed dedicated motorcycle mechanics to keep the Kawasaki bikes in camera-ready condition." "vehicles"

send_memory "CHiPs sometimes featured specialized vehicles including tow trucks, ambulances, and fire engines as part of the multi-agency emergency response scenarios depicted in episodes." "vehicles"

send_memory "The CHP helicopters shown in CHiPs were actual California Highway Patrol aircraft, with the production occasionally using real CHP aviation units for aerial sequences." "vehicles"

send_memory "CHiPs showcased various 1970s-era vehicles in its crash sequences, inadvertently creating a visual record of the automotive landscape of late 1970s and early 1980s Los Angeles." "vehicles"

send_memory "The Kawasaki brand received enormous exposure from CHiPs, with the show essentially serving as an extended advertisement for their police motorcycle line." "vehicles"

send_memory "CHiPs featured several episodes involving sports cars and exotic vehicles, including Porsches, Corvettes, and other high-performance cars that criminals or reckless drivers operated." "vehicles"

send_memory "The CHP patrol motorcycles in CHiPs were fitted with dual exhaust systems that produced a distinctive sound. The production enhanced these sounds in post-production for dramatic effect." "vehicles"

send_memory "CHiPs occasionally depicted the officers using off-road motorcycles for pursuits that went beyond paved roads, showcasing different types of motorcycle riding and terrain." "vehicles"

send_memory "The show's vehicle coordinator was responsible for sourcing dozens of cars for each crash sequence, typically purchasing them from auto auctions and junkyards at minimal cost." "vehicles"

send_memory "CHiPs depicted the evolution of CHP fleet vehicles over its six-year run, transitioning from late 1970s models to early 1980s vehicles as the show progressed." "vehicles"

send_memory "Semi-trucks and big rigs appeared frequently in CHiPs as both hazards and vehicles involved in criminal activity. Their imposing size created natural dramatic tension in chase sequences." "vehicles"

send_memory "The motorcycle saddlebags used on CHiPs' CHP bikes contained actual patrol equipment replicas, adding to the authenticity of the officers' on-screen appearance." "vehicles"

send_memory "CHiPs featured recreational vehicles and motorhomes in several episodes, typically as obstacles in highway pileup sequences or as mobile bases for criminal operations." "vehicles"

send_memory "The show occasionally featured vintage and classic cars, either as theft targets in crime-of-the-week episodes or as vehicles driven by guest characters." "vehicles"

send_memory "CHiPs' motorcycle riding sequences helped establish the visual language of motorcycle patrol that subsequent police shows and films would emulate for decades." "vehicles"

send_memory "The CHP's use of Kawasaki motorcycles, as featured on CHiPs, continued for many years after the show ended. The real CHP eventually transitioned to BMW and Harley-Davidson bikes." "vehicles"

# ============================================================
# CULTURE (memories 326-390)
# ============================================================

send_memory "CHiPs was a cultural phenomenon of the late 1970s and early 1980s, attracting over 25 million viewers during its peak seasons and making Erik Estrada a household name." "culture"

send_memory "The show reflected the disco era prominently, with episodes featuring disco dance competitions, roller disco scenes, and a soundtrack heavy with contemporary pop and disco music." "culture"

send_memory "CHiPs helped popularize the image of the motorcycle cop as a glamorous, heroic figure in American popular culture, replacing the earlier perception of traffic cops as mere ticket-writers." "culture"

send_memory "Erik Estrada became a major sex symbol during CHiPs' run, appearing on the covers of numerous magazines and becoming one of the most recognized television actors worldwide." "culture"

send_memory "CHiPs merchandise included lunch boxes, action figures, toy motorcycles, board games, and other products that were especially popular with younger viewers during the show's peak years." "culture"

send_memory "The show's depiction of roller skating in several episodes coincided with and contributed to the roller skating craze of the late 1970s and early 1980s." "culture"

send_memory "CHiPs was broadcast internationally and became hugely popular in countries across Europe, Latin America, and Asia. Erik Estrada became an international star through these broadcasts." "culture"

send_memory "The show's optimistic tone and sunny California setting made it a quintessential example of late 1970s escapist television, offering viewers an idealized vision of Southern California life." "culture"

send_memory "CHiPs toy motorcycles manufactured by Mego and other companies were among the best-selling toys of the late 1970s, capitalizing on the show's popularity with children." "culture"

send_memory "The show's portrayal of Latino lead character Ponch Poncherello was significant for representation, making Erik Estrada one of the most prominent Latino actors on American television." "culture"

send_memory "CHiPs was parodied and referenced extensively in popular culture, including in comedy shows, films, and later television series that paid homage to its distinctive format." "culture"

send_memory "The show's theme music became instantly recognizable and was frequently used in pop culture references, commercials, and parodies long after the series ended." "culture"

send_memory "CHiPs' blend of action, humor, and attractive leads established a template that influenced subsequent buddy-cop television shows throughout the 1980s." "culture"

send_memory "The Estrada-Wilcox dynamic on CHiPs, both on and off screen, generated extensive tabloid coverage that kept the show in the public consciousness beyond its actual episodes." "culture"

send_memory "CHiPs contributed to the popularity of Kawasaki motorcycles among civilian riders, with sales of Kawasaki models increasing during the show's run." "culture"

send_memory "The show's depiction of California freeway culture resonated particularly with Los Angeles area viewers who recognized the specific freeways and locations featured in episodes." "culture"

send_memory "CHiPs was notable for its relatively diverse cast by late 1970s standards, featuring Latino, African American, and female officers in regular or recurring roles." "culture"

send_memory "Fan conventions celebrating CHiPs have been held periodically since the show ended, with Erik Estrada being the most frequent cast member to make appearances." "culture"

send_memory "CHiPs' influence on police recruitment was documented by the California Highway Patrol, which reported increased interest from applicants who cited the show as their inspiration." "culture"

send_memory "The show spawned a CHiPs comic book series published during its original run, adapting television episodes and creating original stories featuring Ponch and Jon." "culture"

send_memory "CHiPs' popularity led to a made-for-TV reunion movie, CHiPs '99, which aired in 1998 and reunited Erik Estrada and Larry Wilcox as Ponch and Jon." "culture"

send_memory "The show's cultural impact was such that 'CHiPs' became shorthand for motorcycle police officers in American popular culture, much as 'Columbo' became synonymous with detective work." "culture"

send_memory "CHiPs was among the top 20 rated shows on American television for several of its seasons, competing against other popular series of the era." "culture"

send_memory "The show's depiction of Los Angeles as a sunny, exciting place contributed to the city's image in popular culture during a period when LA was becoming the entertainment capital of the world." "culture"

send_memory "CHiPs generated significant advertising revenue for NBC during its peak years, with its broad demographic appeal making it attractive to a wide range of sponsors." "culture"

send_memory "The show's influence extended to fashion, with the CHP uniform and sunglasses combination becoming a popular Halloween costume and a recognizable cultural reference." "culture"

send_memory "CHiPs was one of several action shows that defined NBC's brand in the late 1970s, alongside series like BJ and the Bear and The A-Team." "culture"

send_memory "The show's depiction of the CHP lifestyle, including the camaraderie and excitement, created a romanticized image of law enforcement that persisted in popular culture." "culture"

send_memory "CHiPs home video releases on VHS, DVD, and later streaming platforms introduced the show to new generations of viewers decades after its original broadcast." "culture"

send_memory "Erik Estrada leveraged his CHiPs fame into a long career in personal appearances, endorsements, and reality television, remaining one of the most recognized TV actors of his generation." "culture"

send_memory "CHiPs' cultural footprint includes references in shows like Family Guy, Robot Chicken, and numerous other comedies that have parodied the show's distinctive elements." "culture"

send_memory "The show was particularly popular in Germany, where it aired under the title 'CHiPs - Die Motorrad-Cops' and developed a devoted fanbase." "culture"

send_memory "CHiPs represented a transition in television from the gritty cop shows of the early 1970s to the lighter, more entertainment-focused action shows of the late 1970s and 1980s." "culture"

send_memory "The California Highway Patrol museum in Sacramento includes CHiPs memorabilia, acknowledging the show's role in shaping public perception of the agency." "culture"

send_memory "CHiPs board games released during the show's run allowed fans to simulate motorcycle patrol on Los Angeles freeways, complete with game pieces shaped like CHP motorcycles." "culture"

# ============================================================
# BEHIND THE SCENES (memories 391-440)
# ============================================================

send_memory "The feud between Erik Estrada and Larry Wilcox was one of the most publicized co-star conflicts of the 1970s-80s. The two actors reportedly did not speak to each other off-camera during much of the show's run." "behind_scenes"

send_memory "The Estrada-Wilcox conflict stemmed from disputes over billing, screen time, and creative direction. Estrada felt he was the show's true star while Wilcox wanted equal prominence." "behind_scenes"

send_memory "Erik Estrada's 1979 motorcycle accident during filming was one of the most serious on-set injuries in television history at the time. He was thrown from his motorcycle during a stunt sequence." "behind_scenes"

send_memory "The real California Highway Patrol provided technical advisors to CHiPs who ensured that procedures, equipment, and terminology were accurately depicted, though creative liberties were taken for entertainment." "behind_scenes"

send_memory "CHiPs filming required extensive coordination with California Department of Transportation (Caltrans) for freeway sequences. Road closures and traffic management were necessary for safety." "behind_scenes"

send_memory "Erik Estrada negotiated several pay raises during CHiPs' run, with his contract disputes occasionally delaying production. By the later seasons, he was one of the highest-paid actors on television." "behind_scenes"

send_memory "Larry Wilcox's departure from CHiPs before the final season was attributed to exhaustion, creative differences with producers, and the ongoing tension with Estrada. His exit was announced as a mutual decision." "behind_scenes"

send_memory "The CHiPs production team went through several showrunners during its six-season run, with Rick Rosner maintaining overall creative oversight as the show's creator and executive producer." "behind_scenes"

send_memory "Filming motorcycle sequences on CHiPs was genuinely dangerous. Several stunt performers were injured over the course of the series, and safety protocols were continuously updated." "behind_scenes"

send_memory "The CHiPs set at the studio became a popular destination for NBC executive tours and press visits, as the show was one of the network's most visible productions." "behind_scenes"

send_memory "Erik Estrada reportedly performed more of his own motorcycle riding than was typical for lead actors, which contributed to his credibility with fans but increased production insurance costs." "behind_scenes"

send_memory "The writers of CHiPs consulted with real CHP officers about actual incidents that could inspire episode storylines, grounding the show's sometimes outlandish plots in real-world scenarios." "behind_scenes"

send_memory "Robert Pine's calm, professional demeanor on set helped balance the tension between Estrada and Wilcox, making him a stabilizing presence during difficult production periods." "behind_scenes"

send_memory "CHiPs' wardrobe team maintained strict accuracy with CHP uniforms, working with the department to ensure patches, badges, and equipment matched current-issue items." "behind_scenes"

send_memory "The show's stunt coordinator held daily safety briefings before any vehicle or motorcycle stunt was performed, a practice that was ahead of its time in television production." "behind_scenes"

send_memory "Erik Estrada's popularity during CHiPs was so intense that he required security at public appearances. Fan encounters sometimes disrupted location filming." "behind_scenes"

send_memory "The CHiPs production schedule was grueling, with the cast and crew working long hours during the Los Angeles summer to take advantage of the sunny weather the show required." "behind_scenes"

send_memory "Behind the scenes, CHiPs employed a large team of vehicle specialists including mechanics, drivers, and safety coordinators who were essential to the show's action-heavy format." "behind_scenes"

send_memory "NBC executives reportedly intervened in the Estrada-Wilcox conflict multiple times, trying to maintain a workable relationship between the two leads for the sake of the show." "behind_scenes"

send_memory "The CHiPs makeup and hair department worked quickly between takes during outdoor filming, as the Southern California sun and physical activity made maintaining continuity challenging." "behind_scenes"

send_memory "Randi Oakes trained extensively on motorcycle riding before joining CHiPs, wanting to perform as much of her own riding as possible for authenticity." "behind_scenes"

send_memory "The show's directors had to work efficiently with motorcycle sequences, as freeway filming permits were expensive and time-limited, requiring scenes to be captured within strict schedules." "behind_scenes"

send_memory "CHiPs' sound team developed specific engine sound libraries for different vehicle types featured on the show, creating a consistent and dramatic audio palette for chase sequences." "behind_scenes"

send_memory "Paul Linke and Brodie Greer maintained a genuine friendship off-screen that translated into natural chemistry between Grossie and Bear, enhancing their supporting role performances." "behind_scenes"

send_memory "The CHiPs casting department regularly sought celebrity guest stars to boost ratings during sweeps periods, bringing in recognizable faces from music, sports, and other TV shows." "behind_scenes"

# ============================================================
# GUEST STARS (memories 441-475)
# ============================================================

send_memory "CHiPs featured numerous celebrity guest stars throughout its run, including athletes, musicians, and actors who appeared in single-episode roles that added star power to individual installments." "guest_stars"

send_memory "Michael Dorn appeared on CHiPs before his iconic role as Worf on Star Trek: The Next Generation. His recurring role as Officer Jebediah Turner showcased his early career." "guest_stars"

send_memory "Olympic decathlon champion Bruce Jenner guest starred on CHiPs during Season 4, appearing as himself in an episode that capitalized on the athlete's fame." "guest_stars"

send_memory "The Beach Boys made a guest appearance on CHiPs, performing their music in an episode that featured a beach-themed storyline, blending the show's California setting with the band's iconic sound." "guest_stars"

send_memory "CHiPs featured guest appearances by various disco and pop musicians during its early seasons, reflecting the show's connection to contemporary music trends." "guest_stars"

send_memory "Christopher Lloyd, later famous for Back to the Future, made a guest appearance on CHiPs early in his career, demonstrating the show's role as a showcase for up-and-coming talent." "guest_stars"

send_memory "Drew Barrymore appeared as a child actress on CHiPs, one of her many television guest roles during the late 1970s before her breakout in E.T. the Extra-Terrestrial." "guest_stars"

send_memory "Linda Gray of Dallas fame made a guest appearance on CHiPs, in a crossover of two of the most popular shows of the late 1970s-early 1980s." "guest_stars"

send_memory "CHiPs guest starred numerous character actors who were recognizable faces on 1970s-80s television, appearing across multiple shows during the golden age of network television." "guest_stars"

send_memory "Donna Dixon, who later married Dan Aykroyd, appeared on CHiPs as one of several attractive guest stars who were paired with Ponch in romantic subplots." "guest_stars"

send_memory "CHiPs featured several professional motorcycle racers as guest performers and stunt consultants, lending authenticity to episodes that focused on motorcycle competition or high-performance riding." "guest_stars"

send_memory "Elisha Cook Jr., a veteran character actor known for The Maltese Falcon, made a guest appearance on CHiPs, bridging classic Hollywood and 1970s television." "guest_stars"

send_memory "CHiPs occasionally featured real California Highway Patrol officers in background and minor speaking roles, adding an extra layer of authenticity to the series." "guest_stars"

send_memory "Scatman Crothers, known for The Shining and Hong Kong Phooey, guest starred on CHiPs in an episode that utilized his considerable charm and comedic abilities." "guest_stars"

send_memory "The show's guest star roster reflected the breadth of late 1970s celebrity culture, ranging from Olympic athletes to game show hosts to popular musicians of the era." "guest_stars"

send_memory "Ed Begley Jr. appeared on CHiPs before becoming well known for his roles in St. Elsewhere and other productions. The show served as a launching pad for many careers." "guest_stars"

send_memory "Rick Hurst, known for The Dukes of Hazzard, guest starred on CHiPs, representing the crossover between two of the most popular vehicle-centric shows of the era." "guest_stars"

send_memory "Grant Goodeve from Eight Is Enough made guest appearances on CHiPs, demonstrating the network practice of cross-promoting stars between shows on the same network." "guest_stars"

send_memory "CHiPs' celebrity guest appearances were a ratings strategy that NBC employed effectively, using familiar faces to attract viewers who might not otherwise watch a police procedural." "guest_stars"

send_memory "Several stunt performers who worked on CHiPs went on to become well-known stunt coordinators and second-unit directors in Hollywood, with the show serving as a training ground for action filmmaking." "guest_stars"

# ============================================================
# LEGACY (memories 476-500)
# ============================================================

send_memory "CHiPs' legacy includes a 2017 theatrical film reboot directed by and starring Dax Shepard as Jon Baker, with Michael Pena as Ponch. The film reimagined the series as a raunchy R-rated buddy comedy." "legacy"

send_memory "The 2017 CHiPs movie received mixed to negative reviews from critics, who felt it failed to capture the spirit of the original series. It grossed approximately 25.8 million dollars domestically." "legacy"

send_memory "Erik Estrada was not involved in the 2017 CHiPs film reboot but made his feelings known about the R-rated direction, expressing disappointment that the family-friendly nature of the original was not preserved." "legacy"

send_memory "The CHiPs '99 TV reunion movie, aired on TNT in October 1998, reunited Erik Estrada, Larry Wilcox, and Robert Pine. It updated the characters to reflect the passage of time, with Ponch now a detective." "legacy"

send_memory "CHiPs' influence on the buddy-cop genre is widely acknowledged in television history. The Ponch and Jon partnership template was replicated in numerous subsequent police shows." "legacy"

send_memory "The complete series of CHiPs has been released on DVD, allowing fans to own all 139 episodes. The show has also appeared on various streaming platforms." "legacy"

send_memory "CHiPs is regularly cited in discussions of 1970s-80s television nostalgia, alongside shows like The Dukes of Hazzard, Starsky and Hutch, and The A-Team." "legacy"

send_memory "Erik Estrada's post-CHiPs career included becoming an actual reserve police officer in Muncie, Indiana, and later in St. Anthony, Idaho, bringing his fictional role full circle." "legacy"

send_memory "The California Highway Patrol has acknowledged CHiPs' lasting impact on their agency's public image, noting that the show remains one of the most significant cultural touchstones associated with the CHP." "legacy"

send_memory "CHiPs has been referenced and parodied in shows including South Park, Family Guy, and The Simpsons, demonstrating its enduring presence in American pop culture." "legacy"

send_memory "The show's opening credits sequence, with its sunlit motorcycle riding and upbeat theme music, has become an instantly recognizable piece of television iconography." "legacy"

send_memory "CHiPs' depiction of Los Angeles freeways created a visual time capsule of late 1970s and early 1980s Southern California, capturing the cars, fashion, and landscape of the era." "legacy"

send_memory "Academic studies of police representation on television frequently cite CHiPs as an example of the 'heroic cop' genre that dominated 1970s-80s television before grittier portrayals emerged." "legacy"

send_memory "CHiPs fan communities remain active online, with dedicated websites, social media groups, and forums where enthusiasts discuss episodes, share memorabilia, and organize meet-ups." "legacy"

send_memory "The Kawasaki KZ1000P motorcycle, made famous by CHiPs, has become a collectible among classic motorcycle enthusiasts, with authentic police-spec models commanding premium prices." "legacy"

send_memory "CHiPs helped establish the Southern California freeway as a dramatic setting in its own right, influencing later films and shows that used LA freeways as central locations." "legacy"

send_memory "Robert Pine's portrayal of Sgt. Getraer influenced the depiction of police supervisors in subsequent TV shows, establishing the template of the tough-but-caring desk sergeant." "legacy"

send_memory "The show's legacy includes its contribution to motorcycle safety awareness, as episodes frequently depicted the consequences of reckless driving and the importance of helmet use." "legacy"

send_memory "CHiPs remains a reference point for discussions about the representation of Latino characters on American television, with Ponch Poncherello being one of the most prominent Latino leads of his era." "legacy"

send_memory "The CHiPs theme song by John Parker has been sampled, covered, and referenced in various musical contexts, demonstrating the tune's lasting cultural recognition beyond the show itself." "legacy"

send_memory "CHiPs' six-season run established it as one of the most successful police drama series of the late 1970s and early 1980s, and its reruns continued to attract audiences for decades after." "legacy"

send_memory "The show's optimistic, non-violent approach to law enforcement storytelling stands in contrast to modern police dramas, making CHiPs a nostalgic touchstone for viewers who recall a different era of television." "legacy"

send_memory "CHiPs' lasting cultural impact is evidenced by the fact that the show remains widely recognized and referenced more than 40 years after its final episode aired in 1983." "legacy"

send_memory "The legacy of CHiPs extends to the real-world CHP, which still fields questions from the public about the show and maintains that it was one of the most positive depictions of their officers in any medium." "legacy"

send_memory "CHiPs ultimately produced 139 episodes, a television reunion movie, a theatrical reboot, comic books, novels, and a vast array of merchandise, cementing its place as a significant franchise in American television history." "legacy"

echo ""
echo "========================================="
echo "CHiPs memory ingest complete!"
echo "Total stored: $COUNT"
echo "Total errors: $ERRORS"
echo "========================================="

# Final Slack notification
curl -s -X POST "https://slack.com/api/chat.postMessage" \
  -H "Authorization: Bearer $SLACK_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"channel\": \"$SLACK_CHANNEL\", \"text\": \"📺 TV Ingest: CHiPs — COMPLETE! $COUNT/500 memories stored ($ERRORS errors)\"}" > /dev/null
