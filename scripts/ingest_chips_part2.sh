#!/bin/bash
# Ingest remaining CHiPs memories (286-500) into Nova's vector memory
# Continuation from ingest_chips.sh

API="http://127.0.0.1:18790/remember"
SLACK_TOKEN=$(security find-generic-password -a nova -s nova-slack-bot-token -w)
SLACK_CHANNEL="C0ATAF7NZG9"
COUNT=285
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

  if [ $((COUNT % 25)) -eq 0 ] && [ $COUNT -gt 285 ]; then
    curl -s -X POST "https://slack.com/api/chat.postMessage" \
      -H "Authorization: Bearer $SLACK_TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"channel\": \"$SLACK_CHANNEL\", \"text\": \"📺 TV Ingest: CHiPs — $COUNT/500 complete\"}" > /dev/null
    echo "Progress: $COUNT/500 sent ($ERRORS errors)"
  fi
}

echo "Continuing CHiPs memory ingest — memories 286-500"

# ============================================================
# CULTURE continued (286-325)
# ============================================================

send_memory "CHiPs aired during a period known as the 'jiggle television' era, when networks emphasized attractive casts and lighthearted action. The show fit squarely into this programming trend." "culture"

send_memory "The CHiPs fan club was one of the largest television fan organizations of the late 1970s, distributing newsletters, photos, and exclusive merchandise to members." "culture"

send_memory "CHiPs novelizations were published during the show's run, adapting popular episodes into paperback book form. These tie-in novels expanded the audience for the series beyond television viewers." "culture"

send_memory "The show's depiction of California as a land of sunshine, beautiful people, and exciting car chases contributed to the broader cultural mythology of the Golden State." "culture"

send_memory "CHiPs coloring books and activity books targeted younger fans, featuring illustrations of Ponch, Jon, and their motorcycles in various adventures." "culture"

send_memory "The show influenced Halloween costume choices throughout its run, with CHP officer costumes becoming perennial favorites among children and adults." "culture"

send_memory "CHiPs was among the most-watched shows in Japan during the early 1980s, where American action television was particularly popular with viewers." "culture"

send_memory "The show's portrayal of motorcycle riding inspired many viewers to pursue motorcycle licenses and purchase bikes, contributing to a boom in motorcycle sales during the late 1970s." "culture"

send_memory "CHiPs View-Master reels were produced during the show's peak popularity, allowing fans to see 3D images from the series on the popular stereoscopic viewer toy." "culture"

send_memory "The CHP officers' mirrored aviator sunglasses, prominently featured on CHiPs, became a fashion accessory strongly associated with the show and with law enforcement in general." "culture"

send_memory "CHiPs pinball machines were manufactured and placed in arcades during the show's original run, featuring artwork of Ponch and Jon alongside flashing lights and motorcycle sounds." "culture"

send_memory "The show aired during the height of the energy crisis, and some episodes addressed fuel shortages and their impact on California motorists and highway patrol operations." "culture"

send_memory "CHiPs was among the most frequently rerun television series of the 1980s and 1990s, maintaining visibility with audiences long after original episodes stopped airing." "culture"

send_memory "The show's appeal crossed generational lines, attracting both adult viewers who enjoyed the police drama elements and younger viewers drawn to the motorcycle action and humor." "culture"

send_memory "CHiPs trading cards were produced by Donruss during the show's run, featuring images of cast members, vehicles, and scenes from popular episodes." "culture"

# ============================================================
# BEHIND THE SCENES continued (301-340)
# ============================================================

send_memory "The CHiPs writing staff was challenged to create fresh vehicle accident scenarios for each episode, leading to increasingly creative and elaborate crash setups as the series progressed." "behind_scenes"

send_memory "Erik Estrada's personal life became tabloid fodder during CHiPs' run, with his dating life and lifestyle frequently covered by celebrity magazines and entertainment news shows." "behind_scenes"

send_memory "The CHiPs production team included a dedicated transportation department that maintained not just the hero motorcycles and patrol cars but also all civilian vehicles used in filming." "behind_scenes"

send_memory "Location scouting for CHiPs was an ongoing process, with scouts identifying new stretches of freeway, intersections, and off-ramp locations that could serve as backdrops for episodes." "behind_scenes"

send_memory "The show's prop department created realistic traffic citation booklets, radio equipment, and other CHP gear that added verisimilitude to the officers' daily patrol scenes." "behind_scenes"

send_memory "CHiPs' first assistant directors were responsible for managing the complex logistics of freeway filming, including coordinating with police escorts, traffic control, and safety personnel." "behind_scenes"

send_memory "Erik Estrada and Larry Wilcox's on-set tension occasionally affected filming schedules, with directors having to adjust shooting plans to minimize the time the two actors spent together." "behind_scenes"

send_memory "The show's motorcycle training program for actors was supervised by professional riders who taught the basics of motorcycle handling and safety before filming began each season." "behind_scenes"

send_memory "CHiPs' special effects team used breakaway materials for guardrails, fences, and other obstacles that vehicles crashed through, ensuring both visual impact and performer safety." "behind_scenes"

send_memory "The production maintained multiple identical sets of CHP uniforms for each regular cast member, allowing quick changes when wardrobe was damaged or soiled during physical scenes." "behind_scenes"

send_memory "CHiPs' camera operators developed specialized techniques for shooting from moving motorcycles, including gyro-stabilized mounts that produced smooth footage at highway speeds." "behind_scenes"

send_memory "Rick Rosner drew on conversations with actual CHP officers for story ideas, and several episodes were directly inspired by real incidents reported to him by his CHP contacts." "behind_scenes"

send_memory "The show's editing team faced the challenge of assembling coherent chase sequences from footage shot over multiple days and at different locations, requiring meticulous continuity matching." "behind_scenes"

send_memory "CHiPs' craft services department had to accommodate the large crews required for location shoots, setting up catering facilities at freeway adjacent staging areas." "behind_scenes"

send_memory "The CHiPs production office received letters from real CHP officers who both praised the show's entertainment value and gently corrected procedural inaccuracies." "behind_scenes"

send_memory "Tom Reilly's casting as Bobby Nelson in the final season was partly motivated by his motorcycle riding ability, which reduced the need for a stunt double in basic riding scenes." "behind_scenes"

send_memory "The behind-the-scenes crew of CHiPs numbered over 100 people during location shoots, making it one of the largest regular television production operations of its era." "behind_scenes"

send_memory "CHiPs' directors included several who went on to significant careers in film and television, using the show's action-heavy format as a training ground for directing complex sequences." "behind_scenes"

send_memory "The show's post-production facility at MGM handled the significant task of mixing dialogue, sound effects, and music for episodes that were heavily dependent on vehicular audio." "behind_scenes"

send_memory "Budget negotiations between MGM Television and NBC were contentious during CHiPs' later seasons, as the rising cost of stunt work and location filming strained the per-episode budget." "behind_scenes"

# ============================================================
# GUEST STARS continued (341-370)
# ============================================================

send_memory "Toni Tennille of Captain and Tennille fame appeared on CHiPs, joining the list of musical artists who made guest appearances during the show's run." "guest_stars"

send_memory "Dick Van Patten, star of Eight Is Enough, guest starred on CHiPs in one of the many cross-show guest appearances that NBC facilitated among its prime-time series." "guest_stars"

send_memory "Danny Bonaduce, former child star of The Partridge Family, made an appearance on CHiPs as the former teen idols of 1970s television transitioned to guest roles on other series." "guest_stars"

send_memory "Mackenzie Phillips appeared on CHiPs, adding another recognizable face from the 1970s television landscape to the show's extensive guest star roster." "guest_stars"

send_memory "Professional baseball players made guest appearances on CHiPs during episodes that featured sports-related storylines, reflecting the show's practice of incorporating real athletes." "guest_stars"

send_memory "John Ireland, a veteran film actor and Academy Award nominee, appeared on CHiPs late in his career, bringing old Hollywood gravitas to a guest role." "guest_stars"

send_memory "CHiPs featured appearances by professional stunt performers playing themselves in episodes that dealt with movie filming or stunt shows, blending the show's fiction with real stunt culture." "guest_stars"

send_memory "Bubba Smith, the NFL player who later became famous for the Police Academy films, made an appearance on CHiPs, adding athletic star power to an episode." "guest_stars"

send_memory "June Lockhart, known for Lassie and Lost in Space, guest starred on CHiPs, representing a connection between classic television and the newer generation of 1970s-80s shows." "guest_stars"

send_memory "CHiPs' guest casting strategy prioritized recognizable faces that would attract casual viewers during sweeps months, when Nielsen ratings determined advertising rates for the upcoming quarter." "guest_stars"

send_memory "Several Playboy playmates appeared on CHiPs in minor roles, reflecting the network television practice of casting attractive models in small speaking parts during the late 1970s." "guest_stars"

send_memory "Arnold Schwarzenegger was offered a guest role on CHiPs but scheduling conflicts prevented the appearance. The show frequently pursued high-profile guests to maintain ratings momentum." "guest_stars"

send_memory "The show featured several former child actors who were transitioning to adult roles, giving them visibility during a critical career period." "guest_stars"

send_memory "CHiPs' casting directors maintained relationships with talent agencies across Hollywood, ensuring a steady flow of guest performers for the show's episodic storylines." "guest_stars"

send_memory "Real-life race car drivers appeared on CHiPs in episodes that featured automotive competition storylines, lending authenticity to the show's vehicle-centric action." "guest_stars"

# ============================================================
# EPISODES continued (356-395)
# ============================================================

send_memory "The CHiPs episode 'Supercycle' featured a high-tech motorcycle that became central to the plot, combining the show's love of vehicles with a futuristic technology angle." "episodes"

send_memory "CHiPs' Season 2 episode 'Peaks and Valleys' sent Ponch and Jon into mountainous terrain, demonstrating the CHP's jurisdiction extended beyond urban freeways." "episodes"

send_memory "The episode 'Hustle' dealt with con artists operating on the highways, combining CHiPs' traffic enforcement premise with a classic crime-of-the-week detective story." "episodes"

send_memory "CHiPs' Season 1 finale helped establish the show as a ratings success, convincing NBC to order a full second season and invest more heavily in the series." "episodes"

send_memory "The episode 'Crack-Up' featured one of the most elaborate multi-vehicle accident sequences in CHiPs history, requiring several days of stunt preparation and filming." "episodes"

send_memory "CHiPs occasionally featured courtroom scenes where Ponch or Jon testified about traffic incidents, showing the legal follow-through that real officers deal with after highway incidents." "episodes"

send_memory "The episode 'Drive, Lady, Drive' centered on a female truck driver facing harassment, allowing CHiPs to address gender discrimination while maintaining its action-oriented format." "episodes"

send_memory "CHiPs' Season 3 episode 'Hot Cars' dealt with an organized auto theft ring, one of the show's most common crime-of-the-week premises." "episodes"

send_memory "The episode 'Neighborhood Watch' saw Ponch and Jon working with civilian volunteers, reflecting the real-world community policing initiatives of the early 1980s." "episodes"

send_memory "CHiPs addressed the issue of hitchhiker safety in several episodes, reflecting public concern about the dangers of hitchhiking that were prevalent in the late 1970s." "episodes"

send_memory "The episode 'Force Seven' featured an elite unit within the CHP, allowing the show to explore different aspects of highway patrol operations beyond routine motorcycle patrol." "episodes"

send_memory "CHiPs' Season 5 included episodes that reflected the early 1980s shift away from disco culture, with storylines incorporating new wave and rock music instead." "episodes"

send_memory "The episode 'Speedway Fever' featured auto racing elements, combining CHiPs' vehicular action expertise with the excitement of competitive motorsports." "episodes"

send_memory "CHiPs addressed the problem of highway littering and illegal dumping in episodes that combined environmental concerns with the show's law enforcement premise." "episodes"

send_memory "The Season 4 episode 'Alarmed' dealt with false alarm responses and their impact on emergency services, a public policy issue that resonated with real-world CHP concerns." "episodes"

send_memory "CHiPs featured episodes set during heavy rain, creating unusual visual conditions for a show typically known for its sunny California aesthetic and adding driving hazard tension." "episodes"

send_memory "The episode 'Ponch and the Priceless Pussycat' featured a lighter, comedic tone typical of CHiPs episodes that balanced the show's action with humorous character moments." "episodes"

send_memory "CHiPs occasionally depicted the officers conducting speed trap operations, showing the routine enforcement activity that constituted much of real CHP motorcycle officers' daily work." "episodes"

send_memory "The episode 'Tow Truck Lady' featured a female tow truck operator who assisted the officers, expanding the show's supporting cast of recurring civilian characters." "episodes"

send_memory "CHiPs' Season 6 episodes had a noticeably different feel from earlier seasons, with faster pacing and more emphasis on action to compensate for the loss of the established Ponch-Jon dynamic." "episodes"

# ============================================================
# PRODUCTION continued (376-405)
# ============================================================

send_memory "CHiPs' title stands for California Highway Patrol, with the lowercase 'i' included for stylistic reasons. The show's logo used a distinctive font that became immediately recognizable." "production"

send_memory "The show's title card and logo underwent minor redesigns between seasons, though the basic CHiPs lettering remained consistent throughout the series' six-year run." "production"

send_memory "CHiPs was shot on 35mm film, giving it a higher visual quality than many contemporary television series that used videotape for studio scenes." "production"

send_memory "The series' aspect ratio was the standard 4:3 television format of the era. Later DVD and streaming releases maintained this original aspect ratio." "production"

send_memory "CHiPs' pilot was directed by veteran television director E.W. Swackhamer, who established the visual style and tone that would guide the series going forward." "production"

send_memory "The show's music department used a mix of original score and licensed popular music, with the licensed tracks helping date specific episodes to particular moments in late 1970s-early 1980s pop culture." "production"

send_memory "CHiPs' prop master maintained an extensive inventory of vehicle parts, including breakaway windshields, pre-scored body panels, and rigged doors for crash sequences." "production"

send_memory "The production rented freeway space from Caltrans for filming, paying fees that covered traffic management, officer escorts, and liability insurance for the duration of shooting." "production"

send_memory "CHiPs' art department created detailed briefing room sets that included authentic-looking CHP bulletin boards, duty rosters, and procedural charts visible in background shots." "production"

send_memory "The series employed a medical advisor who ensured that the first aid and emergency medical procedures depicted in CHiPs were reasonably accurate for the era." "production"

send_memory "CHiPs' visual effects were limited to practical elements: smoke, fire, and breakaway materials. The show predated the CGI era and relied entirely on physical stunts and effects." "production"

send_memory "The production team faced ongoing challenges with permits for freeway filming, as increasing traffic volumes in Los Angeles made it progressively harder to secure filming windows." "production"

send_memory "CHiPs' Season 1 had a shorter episode order than subsequent seasons, as the show was initially a midseason entry before being picked up for a full season." "production"

send_memory "The series' final season in 1983 coincided with NBC's overall ratings decline, which was a factor in the network's decision not to renew CHiPs for a seventh season." "production"

send_memory "CHiPs' lighting crew adapted to the challenges of filming outdoors in Southern California's harsh sunlight, using reflectors and scrims to maintain consistent exposure during exterior scenes." "production"

send_memory "The show's unit production manager coordinated between studio work and location filming, ensuring that the production schedule maximized the use of expensive freeway shooting days." "production"

send_memory "CHiPs' script development process typically began with writers pitching crash scenarios and crime-of-the-week premises, which were then woven together with character-driven B-stories." "production"

send_memory "The series maintained a consistent visual palette of warm, sunny tones that reinforced its optimistic tone and Southern California setting, even during episodes with darker storylines." "production"

send_memory "CHiPs' production company employed a full-time Kawasaki mechanic whose sole job was maintaining the fleet of police motorcycles used in filming." "production"

send_memory "The show's end credit sequences often featured outtakes or behind-the-scenes moments, giving viewers a glimpse of the production process and the cast's off-screen personalities." "production"

# ============================================================
# LEGACY continued (406-435)
# ============================================================

send_memory "CHiPs' influence can be seen in later motorcycle-themed shows and films, which borrowed its visual language of sun-drenched highway riding and dramatic pursuit sequences." "legacy"

send_memory "The show established the template for television programs about specialized law enforcement units, paving the way for series about SWAT teams, coast guard, and other agencies." "legacy"

send_memory "CHiPs' non-violent approach to police drama has been cited as an example of how action television can entertain without relying heavily on gunplay and graphic violence." "legacy"

send_memory "The series is studied in media courses as an example of 1970s-80s television production, including its approach to stunts, location filming, and character-driven ensemble storytelling." "legacy"

send_memory "CHiPs memorabilia has become collectible, with original lunch boxes, action figures, and production materials commanding significant prices at auction and in collector markets." "legacy"

send_memory "The show's influence on the California Highway Patrol's recruitment and public image lasted well beyond the series' cancellation, with the CHP continuing to reference the show in public relations." "legacy"

send_memory "CHiPs' format of blending buddy-cop dynamics with vehicular action directly influenced later shows like Nash Bridges and Pacific Blue." "legacy"

send_memory "Erik Estrada's association with CHiPs has been so enduring that he continues to be invited to CHP events and motorcycle-related functions decades after the show ended." "legacy"

send_memory "The 2017 CHiPs movie, despite its mixed reception, demonstrated that the property still had brand recognition nearly 35 years after the original series ended." "legacy"

send_memory "CHiPs' pilot episode has been analyzed by television historians as an effective example of establishing a series premise, introducing characters, and demonstrating a show's formula within a single episode." "legacy"

send_memory "The show's international syndication deals in over 60 countries made it one of the most widely distributed American television series of its era." "legacy"

send_memory "CHiPs reunion events have drawn large crowds of fans, particularly when Erik Estrada and other cast members make joint appearances at nostalgia conventions." "legacy"

send_memory "The series helped define the look and feel of late 1970s action television, influencing cinematography, editing, and stunt coordination practices across the industry." "legacy"

send_memory "CHiPs' depiction of the CHP as a modern, professional organization helped shift public perception of traffic police from mundane enforcement agents to heroic first responders." "legacy"

send_memory "The show's complete series DVD set, released in stages from 2007 to 2012, allowed a new generation to discover CHiPs decades after its original broadcast." "legacy"

# ============================================================
# STUNTS continued (436-460)
# ============================================================

send_memory "CHiPs' stunt team pioneered the use of multiple cameras for crash sequences, ensuring that expensive one-take stunts were captured from several angles simultaneously." "stunts"

send_memory "The show featured some of the most elaborate freeway pileup sequences ever attempted for television, with some setups involving ten or more vehicles in a single coordinated crash." "stunts"

send_memory "Motorcycle pursuit sequences on CHiPs required stunt riders to maintain precise spacing while riding at speed, as the cameras needed consistent framing for the chase scenes." "stunts"

send_memory "CHiPs' stunt department maintained a workshop where vehicles were prepared for crash sequences, including removing glass, loosening body panels, and installing roll cages for performer safety." "stunts"

send_memory "The show used air cannons to flip vehicles during crash sequences, creating the dramatic rolling and tumbling effects that made CHiPs' pileups visually spectacular." "stunts"

send_memory "CHiPs motorcycle jumps were among the most dangerous stunts performed on the show, requiring ramps, precise speed calculations, and landing zones prepared with dirt or padding." "stunts"

send_memory "The production's insurance company required detailed stunt plans and safety assessments before approving any vehicular sequence, leading to extensive pre-production planning for action episodes." "stunts"

send_memory "CHiPs occasionally used real vehicle accidents reported on scanner frequencies as inspiration for staging their own fictional crash sequences, ensuring a degree of realism in the scenarios." "stunts"

send_memory "The show's pursuit sequences through urban streets required closing intersections and coordinating with multiple city agencies, making these some of the most logistically complex scenes to film." "stunts"

send_memory "CHiPs' fire stunts, involving burning vehicles and controlled explosions, required the presence of a dedicated fire safety team on set whenever pyrotechnic effects were used." "stunts"

send_memory "Stunt performers on CHiPs wore protective padding under their costumes during motorcycle falls and vehicle crashes, concealed by wardrobe to maintain visual authenticity." "stunts"

send_memory "The show's stunt coordinator was one of the highest-paid crew members on CHiPs, reflecting the critical importance of the vehicular action to the show's appeal and the expertise required." "stunts"

send_memory "CHiPs occasionally featured bicycle stunts in episodes involving cycling, expanding the show's repertoire of two-wheeled action beyond its standard motorcycle sequences." "stunts"

send_memory "The show's stunt team developed quick-change vehicle setups that allowed them to film multiple crash takes with the same vehicles by using replaceable body panels and breakaway elements." "stunts"

send_memory "CHiPs' legacy in the stunt world is significant, as many of the show's stunt performers and coordinators went on to define action filmmaking in Hollywood for decades." "stunts"

# ============================================================
# VEHICLES continued (461-480)
# ============================================================

send_memory "CHiPs featured the Pontiac Firebird in several episodes as a civilian vehicle, often driven by characters involved in street racing or evading the CHP officers." "vehicles"

send_memory "The CHP's Dodge St. Regis appeared in later seasons of CHiPs as patrol car fleets were updated, reflecting the real-world fleet transitions in California law enforcement." "vehicles"

send_memory "CHiPs showcased numerous European sports cars including Mercedes-Benz, BMW, and Ferrari models, typically in episodes involving wealthy characters or exotic car theft rings." "vehicles"

send_memory "The show's motorcycle chase scenes frequently featured the distinctive sound of the Kawasaki inline-four engine, which became aurally synonymous with CHiPs for many viewers." "vehicles"

send_memory "CHiPs depicted various commercial vehicles including tanker trucks, flatbed trailers, and delivery vans as hazards in freeway accident scenarios, reflecting real-world commercial traffic patterns." "vehicles"

send_memory "The CHP unmarked detective cars shown on CHiPs were typically American-made sedans in muted colors, contrasting with the high-visibility black-and-white patrol vehicles." "vehicles"

send_memory "CHiPs featured ambulances and paramedic vehicles from the Los Angeles County Fire Department in many episodes, showing the interagency cooperation required during major highway incidents." "vehicles"

send_memory "The show occasionally depicted CHP motorcycle officers switching to patrol cars during night shifts or inclement weather, reflecting actual CHP operational practices of the era." "vehicles"

send_memory "CHiPs showcased the evolution of vehicle safety features over its six-season run, with newer model cars featuring improved crumple zones and safety glass compared to older vehicles in crash scenes." "vehicles"

send_memory "The Kawasaki motorcycles used on CHiPs were modified with police-specific equipment packages that added approximately 80 pounds of additional weight to the standard civilian motorcycle." "vehicles"

# ============================================================
# CHARACTERS continued (481-495)
# ============================================================

send_memory "Ponch Poncherello's apartment on CHiPs reflected his bachelor lifestyle, featuring contemporary 1970s decor and serving as a location for scenes showing his off-duty social life." "characters"

send_memory "Jon Baker's ranching hobby was depicted as a grounding counterpoint to the high-speed action of his CHP work, giving his character depth beyond the law enforcement role." "characters"

send_memory "Sergeant Getraer occasionally accompanied his officers on patrol in CHiPs episodes, particularly during high-stakes situations, demonstrating his willingness to lead from the front." "characters"

send_memory "Grossie's weight and eating habits were played for laughs on CHiPs, reflecting the broader television comedy conventions of the era. His character was nonetheless portrayed as a competent officer." "characters"

send_memory "Officer Sindy Cahill's brief tenure on CHiPs during Season 2 introduced the concept of female motorcycle officers to the show's universe before Bonnie Clark became the permanent female lead." "characters"

send_memory "Harlan the mechanic's relationship with the officers' motorcycles was treated almost reverentially on CHiPs, with his pride in the machines providing gentle humor throughout the series." "characters"

send_memory "Ponch's ability to charm witnesses and suspects was a recurring character trait that distinguished him from Jon's more straightforward interview technique." "characters"

send_memory "Jon Baker's character was depicted as a skilled marksman in the rare episodes that involved firearms, consistent with his military background as established in early seasons." "characters"

send_memory "The officers' locker room at Central Division served as a setting for character development scenes on CHiPs, where personal conversations and comedic interactions took place between patrol sequences." "characters"

send_memory "Ponch and Jon's friendship was the emotional core of CHiPs, transcending their professional partnership and depicted through scenes of them spending off-duty time together." "characters"

# ============================================================
# FINAL BATCH: MIXED (496-500)
# ============================================================

send_memory "CHiPs premiered in the same television season as The Love Boat and Fantasy Island on ABC, making the 1977-78 season one of the most significant launching pads for escapist entertainment in TV history." "production"

send_memory "The real California Highway Patrol has approximately 7,600 sworn officers and 3,600 civilian employees as of the 2020s, a force much larger than the small Central Division depicted on CHiPs." "culture"

send_memory "Erik Estrada has publicly stated that playing Ponch on CHiPs was the role of a lifetime and that he embraces his association with the character rather than trying to distance himself from it." "legacy"

send_memory "CHiPs' depiction of Southern California car culture, from muscle cars to sports cars to everyday commuter vehicles, provides a valuable visual archive of the automotive landscape of the late 1970s and early 1980s." "vehicles"

send_memory "The final season of CHiPs aired in 1983, the same year that other iconic television series including M*A*S*H came to an end, marking the close of an era in American network television." "legacy"

echo ""
echo "========================================="
echo "CHiPs memory ingest part 2 complete!"
echo "Total stored: $COUNT"
echo "Total errors: $ERRORS"
echo "========================================="

# Final Slack notification
curl -s -X POST "https://slack.com/api/chat.postMessage" \
  -H "Authorization: Bearer $SLACK_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"channel\": \"$SLACK_CHANNEL\", \"text\": \"📺 TV Ingest: CHiPs — COMPLETE! $COUNT/500 memories stored ($ERRORS errors)\"}" > /dev/null
