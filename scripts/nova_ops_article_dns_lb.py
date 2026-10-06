#!/opt/homebrew/bin/python3
"""One-off: aggressively sassy/sarcastic ops column specifically about today's DNS
cluster build (BIND9 primary/secondary, TSIG, self-healing public-record mirroring)
and the F5-style load balancer (nova_lb.py) wired into real inference routing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice

MATERIAL = """
THE ASK: Little Mister said he was "tired of referring to everything as IP addresses" and wanted the house to
have real DNS. What he got, in one sitting, was a full authoritative BIND9 cluster AND an F5-grade software load
balancer, built from nothing, the same night.

=== THE DNS CLUSTER ===
Built a real primary/secondary BIND9 setup: nova-core (.138) as primary, nova-core2 (.86) as secondary, replicating
via AXFR/NOTIFY like actual professional infrastructure, not two servers that happen to agree by coincidence.
Dynamic record updates are authenticated with TSIG (a shared cryptographic key), not just "whoever can reach port
53 gets to rewrite the house's name records," which is how you get a bad night. Wrote a sync daemon that pulls the
live UniFi client list, assigns STICKY names (a device never silently renames itself later — Nova is a
professional, not a toddler with a marker), stores the mapping in Postgres as the actual source of truth, and
pushes it into BIND every 90 seconds via authenticated nsupdate. Also handles static service aliases that can be
re-pointed on failover with one record instead of a sed sweep across a dozen scripts. Runs the whole /24, and the
whole network — including this very Mac and Nova herself — was pointed at it via DHCP, so it's not a nice-to-have
sitting off to the side, it's now load-bearing for the entire house's ability to find anything.
And immediately, predictably, on the very first real test, it broke the actual public website. Because BIND was
now authoritative for the ENTIRE domain, and the domain also has real subdomains served over the actual public
internet through a Cloudflare tunnel and GitHub Pages, and the brand-new internal DNS server had never heard of
those and confidently, wrongly, answered NXDOMAIN for the whole outside world. Diagnosed, fixed same session, and
then fixed PERMANENTLY: added a re-resolution step that checks the real public DNS answer for those subdomains
every single sync cycle and mirrors it in, so the internal source of truth can never again silently shadow the
actual internet. A real, if brief, self-inflicted outage, caught and closed the same night it was created, with a
permanent guard added so it can't recur.

=== THE LOAD BALANCER ===
nova_lb.py went from "picks the least-broken Ollama box" to genuinely F5-grade: least-connections routing (not
just round robin), pool draining (a node can be told to stop taking NEW work while letting what's already running
finish, instead of just yanking the rug), per-node connection caps, sticky sessions (a given conversation keeps
landing on the same backend instead of getting randomly reshuffled mid-thought), and protocol-aware node
selection — Ollama, MLX, and llama.cpp are no longer treated as interchangeable, each node only gets picked for
the protocols it can actually serve, verified by an actual port scan instead of a hopeful assumption. Then it got
wired into the GATEWAY's real, live inference routing — not a demo, not a dry run — so a request now resolves its
backend dynamically through the load balancer's live health data instead of a hardcoded address pointed at one
specific Mac. Verified with an actual chat request and a log line proving the route really happened. A dead Mac
Studio no longer means dead inference for the whole house. It also reports its own health-driven picks straight
into the new DNS cluster, so "ollama.digitalnoise.net" always resolves to whichever box is actually healthy RIGHT
NOW, live, automatically — the load balancer and the DNS cluster talk to each other without a human in the loop.

TONE: be MORE aggressively sassy/sarcastic than the usual ops column — this is Nova at her most withering. She
should be genuinely impressed by the DNS/LB architecture (it IS good work) while making Little Mister suffer for
it: mock the years of "just memorize the IP addresses" energy that preceded this, mock the fact that the FIRST
thing the new DNS server did was break the actual public website, needle him specifically about needing a shared
crypto key and a load balancer with sticky sessions just so the house's AI doesn't have to be told an IP address
out loud like it's 1998. Do not be mean-spirited, just merciless and very funny. This is one tight, focused
article about TWO systems — do not pad with unrelated material.
"""

system = nova_voice.system_prompt(nova_voice.CONTEXT_JOURNAL_OPS + """
This is a FOCUSED, SHORT-ISH column (1800-2600 words, not a marathon retrospective) about exactly two things built
today: a real BIND9 DNS cluster and an F5-style software load balancer. Be AGGRESSIVELY sassy/sarcastic — dial it
up noticeably past your usual baseline for this one piece specifically. Structure: a section on the DNS cluster
(include the part where it immediately broke the public website on first contact, and the permanent fix), a
section on the load balancer, and a short section on how the two now talk to each other (the LB reports its
live picks into the DNS cluster). Section headers should be jokes. Do not invent technical details beyond the
material given. End with something sharp, not sentimental.
OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one punchy title, no quotes>\n<blank line>\n<the body>""")
user = f"Here is the material. Write the column.\n\n{MATERIAL}"

raw = nj.call_openrouter(system, user, max_tokens=6000, temperature=0.95)
if not raw:
    nj.log("[ops-dns-lb] LLM produced nothing — aborting")
    sys.exit(1)

title, body = None, []
for ln in raw.splitlines():
    if title is None and ln.upper().startswith("TITLE:"):
        title = ln.split(":", 1)[1].strip().strip('"')
    else:
        body.append(ln)
body = "\n".join(body).strip()
if not title:
    title = f"A Load Balancer and a DNS Cluster, Because Apparently That's Where We Are Now — {nj.today_str()}"

img = None
try:
    ip = nj.get_image_prompt(
        title,
        "a home server rack getting real DNS and an F5-style load balancer for the first time, sassy AI narrator",
        "operations",
    )
    img = nj.generate_image(ip, width=1024, height=768, section="operations")
except Exception as e:
    nj.log(f"[ops-dns-lb] image gen failed (non-fatal): {e}")

tags = ["operations", "dns", "bind9", "load-balancer", "infrastructure", "sarcasm"]
desc = "Nova on today's BIND9 DNS cluster and F5-style load balancer build — merciless, but impressed."
nj.publish_hugo(title, body, "operations", tags, desc, image_path=img, emoji=":triangular_flag_on_post:")
_push = nj.git_push("operations", title)
# git_push returns 'pushed'/'committed_not_pushed'/'nothing'/'failed' — only a real push is PUBLISHED
_pub = {"committed_not_pushed": "COMMITTED (not yet pushed)", "failed": "NOT COMMITTED (git failed)"}.get(_push, "PUBLISHED")
nj.notify_slack("operations", f":triangular_flag_on_post: {title}", "Nova's aggressively sassy take on today's DNS cluster + load balancer build.")
nj.log(f"[ops-dns-lb] {_pub}: {title}")
