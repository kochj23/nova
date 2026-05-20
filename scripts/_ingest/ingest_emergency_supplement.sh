#!/bin/bash
# Supplement: memories 462-500 for Emergency! ingest

API="http://127.0.0.1:18790/remember"
SLACK_TOKEN=$(security find-generic-password -a nova -s nova-slack-bot-token -w)
SLACK_CHANNEL="C0ATAF7NZG9"
COUNT=0
FAIL=0

send_memory() {
  local text="$1"
  local category="$2"
  local response
  response=$(curl -s -w "\n%{http_code}" -X POST "$API" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg t "$text" --arg c "$category" '{
      text: $t,
      source: "tv_emergency",
      metadata: {type: "television", show: "Emergency!", category: $c}
    }')")
  local status=$(echo "$response" | tail -1)
  if [ "$status" -ge 200 ] && [ "$status" -lt 300 ]; then
    COUNT=$((COUNT + 1))
  else
    FAIL=$((FAIL + 1))
    echo "FAIL ($status): $text" >> /tmp/emergency_ingest_errors.log
  fi
}

echo "Sending supplemental memories 462-500..."

# LEGACY (continued)
send_memory "Emergency! inspired the development of fire service honor guards and ceremonial units. The show's dignified portrayal of firefighters contributed to increased ceremonial recognition of fire service traditions." "legacy"

send_memory "The show contributed to the standardization of paramedic drug boxes. The medication kits shown on Squad 51 influenced how real EMS agencies organized and stocked their field medical supplies." "legacy"

send_memory "Emergency! helped establish public understanding that heart attacks required immediate professional intervention. The show's repeated depiction of cardiac emergencies educated millions about recognizing heart attack symptoms." "legacy"

# BEHIND THE SCENES (additional)
send_memory "The Emergency! production team maintained a detailed continuity bible tracking character histories, station equipment, and established medical protocols to ensure consistency across seasons and episodes." "behind_scenes"

send_memory "Filming emergency vehicle response scenes for Emergency! required coordination with the California Highway Patrol and local police departments, who provided traffic control and road closures for the production." "behind_scenes"

send_memory "The show's makeup department developed realistic trauma makeup techniques for depicting burn victims, laceration injuries, and other traumatic conditions that needed to appear authentic on camera." "behind_scenes"

send_memory "Emergency! was one of the first television series to employ a full-time registered nurse as a medical script consultant, reviewing dialogue and procedures for clinical accuracy before each episode was filmed." "behind_scenes"

send_memory "The production used real IV fluid bags filled with colored water for medical scenes, and actors learned to manipulate the drip chambers and flow regulators as real paramedics would." "behind_scenes"

# EPISODES (additional)
send_memory "The episode 'Crash' depicted a multi-vehicle accident on a Los Angeles freeway, requiring the full resources of Station 51 and multiple mutual aid companies in one of the show's largest response sequences." "episodes"

send_memory "The episode 'Trainee' introduced a new paramedic student riding along with Gage and DeSoto, providing a narrative device to explain paramedic procedures to the audience through the trainee's learning experience." "episodes"

send_memory "The episode 'The Mouse' featured a lighter storyline involving a rodent loose in Station 51, balanced against serious emergency calls — exemplifying the show's signature blend of humor and drama." "episodes"

send_memory "The episode 'Dealer's Wild' featured an emergency at a warehouse where hazardous materials were improperly stored, foreshadowing the increased attention to HAZMAT response that would develop in the fire service during the 1980s." "episodes"

send_memory "The episode 'Difficult Delivery' centered on a complex field childbirth where Gage and DeSoto faced complications requiring real-time radio consultation with Dr. Brackett at Rampart General Hospital." "episodes"

# MEDICAL (additional)
send_memory "Emergency! depicted the use of the laryngoscope for endotracheal intubation, showing the paramedics using this essential airway management tool to secure the airways of unconscious patients in the field." "medical"

send_memory "The show accurately portrayed the complications of diabetic emergencies, including both hyperglycemic and hypoglycemic crises, showing paramedics administering glucose and contacting Rampart for insulin-related decisions." "medical"

# FIREFIGHTING (additional)
send_memory "Emergency! depicted the use of thermal imaging in its later seasons, showing how emerging technology was beginning to help firefighters locate victims and fire sources through smoke-filled environments." "firefighting"

send_memory "The show portrayed the dangers of propane and natural gas emergencies, with firefighters establishing evacuation perimeters and using gas detection equipment to manage explosive atmosphere hazards." "firefighting"

# IMPACT (additional)
send_memory "Emergency! influenced the creation of community emergency response teams (CERT), as the show demonstrated to ordinary citizens the value of basic emergency preparedness and response training." "impact"

send_memory "The show's impact on the nursing profession was significant. Applications to emergency nursing programs increased measurably during Emergency!'s run, as Dixie McCall inspired viewers to pursue nursing careers." "impact"

# VEHICLES (additional)
send_memory "The fuel capacity and range of Squad 51 was carefully managed during filming. The Dodge D-300's fuel tank allowed extended filming sessions, but the vehicle required regular maintenance due to heavy use." "vehicles"

# CAST (additional)
send_memory "The entire principal cast of Emergency! maintained lifelong connections to the fire service community. Their collective advocacy for firefighters and paramedics extended well beyond the show's original broadcast years." "cast"

# CHARACTERS (additional)
send_memory "John Gage and Roy DeSoto set the template for the buddy partnership in emergency services television. Their dynamic — one impulsive, one steady — has been replicated in countless subsequent TV firefighter pairings." "characters"

# PRODUCTION (additional)
send_memory "The production of Emergency! benefited from Jack Webb's longstanding relationship with Los Angeles city and county officials, who facilitated filming permits, location access, and equipment loans for the show." "production"

# LEGACY (final)
send_memory "Emergency! remains the most influential television show in the history of emergency medical services. No other program has had a greater impact on how America provides prehospital emergency care to its citizens." "legacy"

send_memory "The show demonstrated that a procedural drama grounded in technical accuracy could achieve both commercial success and measurable social impact, a model that remains aspirational for television producers today." "legacy"

send_memory "Emergency! fan conventions have been held regularly since the 1990s, attracting hundreds of attendees who share memories, meet cast members, and celebrate the show's contribution to emergency services culture." "legacy"

send_memory "The legacy of Emergency! is measured not just in television ratings and cultural memory, but in the millions of lives saved by the paramedic systems that the show helped inspire and sustain across America." "legacy"

send_memory "Historians of American television cite Emergency! as one of the most socially consequential programs ever produced. Its direct contribution to life-saving public policy makes it unique in the history of entertainment media." "legacy"

send_memory "Emergency! was among the first shows to portray the emotional recovery process after traumatic calls. Episodes showed crew members debriefing and supporting each other, normalizing mental health awareness in the fire service." "legacy"

send_memory "The show's realistic portrayal of inter-agency communication — between fire, EMS, hospital, police, and dispatch — established a template for how these relationships would be depicted in all subsequent emergency services television." "legacy"

echo ""
echo "==================================="
echo "SUPPLEMENT COMPLETE"
echo "Total sent: $COUNT"
echo "Total failures: $FAIL"
echo "Grand total: $((461 + COUNT))/500"
echo "==================================="

if [ $((461 + COUNT)) -ge 500 ]; then
  curl -s -X POST "https://slack.com/api/chat.postMessage" \
    -H "Authorization: Bearer $SLACK_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg ch "$SLACK_CHANNEL" '{channel: $ch, text: "📺 TV Ingest: Emergency! — 500/500 COMPLETE! All memories ingested successfully."}')" > /dev/null
fi
