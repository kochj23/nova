#!/bin/bash
# Ingest final CHiPs memories (431-500) into Nova's vector memory

API="http://127.0.0.1:18790/remember"
SLACK_TOKEN=$(security find-generic-password -a nova -s nova-slack-bot-token -w)
SLACK_CHANNEL="C0ATAF7NZG9"
COUNT=430
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

  if [ $((COUNT % 25)) -eq 0 ] && [ $COUNT -gt 430 ]; then
    curl -s -X POST "https://slack.com/api/chat.postMessage" \
      -H "Authorization: Bearer $SLACK_TOKEN" \
      -H "Content-Type: application/json" \
      -d "{\"channel\": \"$SLACK_CHANNEL\", \"text\": \"📺 TV Ingest: CHiPs — $COUNT/500 complete\"}" > /dev/null
    echo "Progress: $COUNT/500 sent ($ERRORS errors)"
  fi
}

echo "Final CHiPs memory ingest — memories 431-500"

# ============================================================
# EPISODES - deep cuts (431-450)
# ============================================================

send_memory "The CHiPs episode 'Vigilante' explored the moral complexity of citizens taking the law into their own hands after feeling failed by the justice system, a recurring theme in 1970s-80s American television." "episodes"

send_memory "CHiPs' Season 3 episode 'Pocket Money' dealt with a group of juveniles committing robberies along the highway, allowing the show to address youth crime in a sensitive manner." "episodes"

send_memory "The episode 'Return of the Brat Patrol' featured a group of kids who fancied themselves junior detectives, creating complications for Ponch and Jon during a real investigation." "episodes"

send_memory "CHiPs addressed the issue of motorcycle gang activity in several episodes, depicting the tension between outlaw biker groups and the CHP officers who patrolled their territory." "episodes"

send_memory "The Season 4 episode 'Bomb Run' featured a tense sequence involving a vehicle carrying explosive materials on a crowded freeway, creating one of the show's most suspenseful scenarios." "episodes"

send_memory "CHiPs' episode 'Forty Tons of Trouble' centered on an overloaded truck creating a hazard on the highway, reflecting real-world concerns about commercial vehicle weight limits and road safety." "episodes"

send_memory "The episode 'New Guy in Town' introduced a fresh officer to the CHP Central Division, allowing the show to revisit the rookie experience and contrast it with Ponch and Jon's veteran status." "episodes"

send_memory "CHiPs frequently depicted freeway construction zones as settings for accidents and hazards, reflecting the constant state of highway construction in the ever-expanding Los Angeles freeway system." "episodes"

send_memory "The Season 5 episode 'The Hawk and the Hunter' featured a storyline about animal smuggling, broadening CHiPs' crime-of-the-week scope beyond its typical vehicular crimes." "episodes"

send_memory "CHiPs' episode 'High Explosive' featured a bomb threat scenario that was among the show's most intense storylines, pushing the boundaries of the series' typically light tone." "episodes"

# ============================================================
# PRODUCTION - additional details (441-455)
# ============================================================

send_memory "CHiPs' casting of Erik Estrada as Ponch was a deliberate choice to feature a Latino lead in a prime-time action series, a decision that was progressive for 1977 network television." "production"

send_memory "The real CHP allowed CHiPs to use authentic CHP insignia and uniform designs, a courtesy that added significant production value and authenticity to the series." "production"

send_memory "CHiPs' production schedule required the cast and crew to work in Southern California's extreme summer heat, with temperatures on freeway surfaces sometimes exceeding 100 degrees during filming." "production"

send_memory "The show's assistant directors developed a system of hand signals for communicating during noisy motorcycle and vehicle sequences when verbal communication was impossible." "production"

send_memory "CHiPs' syndication package was one of the most lucrative of the early 1980s, with the show's broad appeal making it attractive to local stations across the United States." "production"

send_memory "The production employed animal handlers for episodes featuring dogs, horses, and other animals that appeared in CHiPs storylines, ensuring compliance with animal welfare regulations." "production"

send_memory "CHiPs' Season 1 was initially scheduled as a midseason replacement starting in September 1977, but its strong ratings in early episodes convinced NBC to commit to a full 22-episode season." "production"

send_memory "The show's gaffer and electrical department rigged portable lighting for dawn and dusk motorcycle sequences, creating the golden-hour look that enhanced CHiPs' California aesthetic." "production"

send_memory "CHiPs employed dialect coaches for guest actors playing characters of specific ethnic backgrounds, reflecting a growing awareness of authentic cultural representation in 1970s television." "production"

send_memory "The production's location manager maintained detailed files on every Los Angeles freeway interchange, on-ramp, and stretch of highway that could serve as a potential filming location." "production"

# ============================================================
# CULTURE - additional details (456-465)
# ============================================================

send_memory "CHiPs Underoos, the popular children's underwear featuring TV characters, were among the best-selling character merchandise items during the show's peak years." "culture"

send_memory "The CHiPs phenomenon contributed to the broader 'cop show' trend of the late 1970s, which also included shows like Barney Miller, Police Woman, and Starsky and Hutch." "culture"

send_memory "Erik Estrada's appearance on the cover of People magazine during CHiPs' run confirmed his status as one of the most recognized entertainment figures in America." "culture"

send_memory "CHiPs-branded toy motorcycles featured working kickstands, rubber tires, and articulated officer figures, making them among the most detailed television tie-in toys of the late 1970s." "culture"

send_memory "The show's popularity in Latin America was particularly strong, where Erik Estrada was celebrated as a symbol of Latino achievement in American entertainment." "culture"

send_memory "CHiPs lunchboxes manufactured by Thermos became collector's items, featuring artwork of Ponch and Jon on their motorcycles against a Los Angeles freeway backdrop." "culture"

send_memory "The show's impact on motorcycle helmet design was notable, with the distinctive white CHP-style helmet becoming widely imitated by civilian motorcycle gear manufacturers." "culture"

send_memory "CHiPs was referenced in the 1998 film There's Something About Mary, demonstrating the show's persistence in pop culture references well beyond its original broadcast years." "culture"

send_memory "The show's influence extended to video games, with CHiPs-themed games appearing on Atari and other early home gaming platforms during the early 1980s." "culture"

send_memory "CHiPs-themed party supplies including paper plates, cups, and napkins were popular for children's birthday parties during the show's original run." "culture"

# ============================================================
# LEGACY - final entries (466-470)
# ============================================================

send_memory "CHiPs has been preserved by the Paley Center for Media, with select episodes held in their collection as examples of significant American television from the late 1970s and early 1980s." "legacy"

send_memory "The show's impact on public perception of motorcycle officers extended globally, influencing how highway patrol officers were depicted in television productions in numerous countries." "legacy"

send_memory "CHiPs' complete series has been released digitally on platforms including iTunes and Amazon Prime Video, ensuring continued accessibility for modern audiences." "legacy"

send_memory "Television historians consider CHiPs a quintessential example of the 'smile and sunshine' school of 1970s-80s television, where optimism and entertainment value trumped gritty realism." "legacy"

send_memory "The CHiPs brand remains actively licensed for merchandise, with new products periodically released for the nostalgia market including t-shirts, posters, and replica items." "legacy"

# ============================================================
# ADDITIONAL MEMORIES (471-500)
# ============================================================

send_memory "Rick Rosner reportedly pitched CHiPs to multiple networks before NBC picked it up, with other networks passing on the concept of a motorcycle patrol show as too narrow in scope." "production"

send_memory "CHiPs episodes often opened with a pre-credits teaser sequence showing an accident or crime in progress, hooking viewers before the opening title sequence played." "episodes"

send_memory "The show's briefing room scenes served a dual purpose: advancing the plot by assigning officers to cases and providing exposition that oriented viewers to each episode's story." "episodes"

send_memory "CHiPs' depiction of the CHP dispatch system, with officers receiving calls over their motorcycle radios, was reasonably accurate to the real communication protocols used in the late 1970s." "production"

send_memory "Several CHiPs episodes featured storylines about disabled or elderly drivers, addressing the sensitive issue of whether certain individuals should be allowed to continue operating vehicles." "episodes"

send_memory "The show occasionally depicted CHP officers assisting stranded motorists with flat tires, overheating engines, and other roadside emergencies, showing the service aspect of highway patrol work." "episodes"

send_memory "CHiPs' costuming department sourced authentic CHP riding boots for the cast, which were the same model worn by real officers and contributed to the show's visual credibility." "production"

send_memory "The show's depiction of motorcycle maintenance at the CHP garage provided insight into the mechanical aspects of police motorcycle operations that audiences rarely saw on other shows." "vehicles"

send_memory "CHiPs' Season 4 ratings peaked at approximately 27 million viewers for top episodes, placing it among the most-watched series in America during the 1980-81 television season." "production"

send_memory "The show's decline in ratings during Seasons 5 and 6 coincided with the rise of competing action shows on other networks and a general shift in audience tastes." "production"

send_memory "CHiPs occasionally featured episodes with aviation-related storylines, including small aircraft emergencies near freeways that required CHP coordination with federal aviation authorities." "episodes"

send_memory "The show's depiction of the CHP's jurisdiction, which extends to all California state highways and provides mutual aid throughout the state, educated viewers about the agency's broad mandate." "culture"

send_memory "CHiPs influenced the real California Highway Patrol to invest more heavily in public relations and community outreach, recognizing the show's effectiveness in building positive public perception." "legacy"

send_memory "Erik Estrada's transition from struggling New York actor to television superstar through CHiPs is often cited as one of the great American success stories in entertainment." "cast"

send_memory "Larry Wilcox's quieter approach to fame contrasted with Estrada's celebrity lifestyle, contributing to the real-life tension between the two that mirrored their characters' personality differences." "cast"

send_memory "Robert Pine continued acting steadily after CHiPs ended, appearing in numerous television shows and films. He maintained a lower public profile than Estrada but had a long, productive career." "cast"

send_memory "The CHiPs stunt team's work on the series led to innovations in vehicular stunt filming that were adopted by other television productions and eventually influenced feature film techniques." "stunts"

send_memory "CHiPs' depiction of California traffic court proceedings in select episodes provided viewers with a glimpse into the legal system's handling of traffic violations and accidents." "episodes"

send_memory "The show's theme song by John Parker used a driving guitar riff and upbeat tempo that perfectly captured the energy and optimism of CHiPs' sun-drenched California setting." "production"

send_memory "CHiPs was one of the first major television series to be fully released on DVD with bonus features including cast interviews and retrospective documentaries about the show's production." "legacy"

send_memory "The real California Highway Patrol's Central Division, which inspired the setting of CHiPs, is located in downtown Los Angeles and covers a vast metropolitan freeway network." "culture"

send_memory "CHiPs' treatment of freeway accidents as dramatic spectacles while also showing their human cost struck a balance that made the show both entertaining and occasionally thought-provoking." "episodes"

send_memory "Erik Estrada's real-life work as a reserve police officer after CHiPs ended demonstrated the lasting impact the role had on his personal identity and career direction." "legacy"

send_memory "The Kawasaki KZ1000P police motorcycles featured on CHiPs weighed approximately 650 pounds fully equipped, requiring significant skill and strength to operate at highway speeds." "vehicles"

send_memory "CHiPs represented one of the most successful collaborations between a real law enforcement agency and a television production company, with the CHP benefiting from the show's positive portrayal." "production"

send_memory "The show's warm, comedic tone distinguished it from grittier contemporaries like Hill Street Blues, which premiered in 1981 and signaled a shift toward more realistic police dramas." "culture"

send_memory "CHiPs merchandise revenue during the show's peak years rivaled that of much larger franchises, with the combination of action appeal and character popularity driving strong toy and product sales." "culture"

send_memory "The series finale of CHiPs was not written as a definitive ending, as the cancellation came without advance notice. The final episode aired as a regular installment rather than a farewell." "episodes"

send_memory "Paul Linke's portrayal of Grossie provided CHiPs with a reliable source of physical comedy and situational humor that balanced the show's more serious accident and crime storylines." "cast"

send_memory "CHiPs' production legacy includes establishing best practices for filming vehicular action on public roads that are still referenced by television and film production safety manuals today." "legacy"

send_memory "The show's complete 139-episode run makes it one of the longer-running police dramas of the late 1970s and early 1980s era, outlasting many competitors in the crowded cop-show landscape." "legacy"

send_memory "Brodie Greer's portrayal of Bear on CHiPs earned him a dedicated fanbase despite the character's supporting role. Greer continued to make convention appearances decades after the show ended." "cast"

send_memory "CHiPs occasionally broke the fourth wall with knowing glances or comedic moments that acknowledged the show's entertainment nature, giving it a lighter, more self-aware quality than many police dramas." "episodes"

send_memory "The CHP's motor officer training program at the agency's academy was influenced by the increased public interest generated by CHiPs, leading to more rigorous and publicly visible training procedures." "legacy"

echo ""
echo "========================================="
echo "CHiPs memory ingest FINAL BATCH complete!"
echo "Total stored: $COUNT"
echo "Total errors: $ERRORS"
echo "========================================="

# Final Slack notification
curl -s -X POST "https://slack.com/api/chat.postMessage" \
  -H "Authorization: Bearer $SLACK_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"channel\": \"$SLACK_CHANNEL\", \"text\": \"📺 TV Ingest: CHiPs — COMPLETE! $COUNT/500 memories stored ($ERRORS errors)\"}" > /dev/null
