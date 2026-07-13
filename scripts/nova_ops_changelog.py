#!/opt/homebrew/bin/python3
"""nova_ops_changelog.py — rewrite the weekly wrap as a features CHANGELOG (NEW/CHANGED/FIXED).

Overwrites the existing weekly-wrap post IN PLACE (same filename -> same URL), re-angled from
an infrastructure mood piece to 'what shipped': new scanning devices + internet airwave feeds
(heavy on the last 4 days), what changed, what got fixed. Nova's ops voice. Regenerates the
cover in place, pushes, and drops the link in Slack.
"""
import subprocess
from pathlib import Path

import nova_weekly_ops_report as wk
import nova_weekly_ops_wrap as wrap

HUGO = Path.home() / "nova-journal"
STEM = "2026-07-12-weekly-ops-nova-s-weekly-infrastructure-report-a-familiar-s-l"  # keep to preserve URL
MD = HUGO / "content" / "operations" / f"{STEM}.md"
IMG = HUGO / "static" / "images" / "operations" / f"{STEM}.webp"
URL = f"https://nova.digitalnoise.net/operations/{STEM}/"


def airwave_timeline() -> str:
    rows = wk.q(wk.MEMDB,
                "SELECT source || ' | ' || count(*) || ' transmissions | live since ' || min(created_at)::date "
                "FROM memories WHERE source IN ('scanner','fire','fire_ops','chp','rail','cb','atc','aviation_ref','police_codes') "
                "AND created_at > now() - interval '10 days' GROUP BY source ORDER BY min(created_at)")
    return "\n".join(r[0] for r in rows) or "(none)"


def git_log(repo: Path, days: int, label: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), "log", f"--since={days}.days", "--date=short", "--pretty=%ad | %s"],
                           capture_output=True, text=True, timeout=20)
        lines = [l for l in r.stdout.splitlines() if l.strip()][:60]
        return f"{label}:\n" + "\n".join(f"  {l}" for l in lines)
    except Exception:
        return f"{label}: (unavailable)"


def fleet_hardware() -> str:
    """Every machine in the cluster: hardware + live load. From node_status."""
    rows = wk.q(wk.DB,
                "SELECT node_name || ' (' || node_ip || ') | ' || cpu_cores || ' cores / ' || ram_gb || 'GB RAM"
                " | disk ' || round(disk_percent) || '% | ' || coalesce(capabilities->>'model','?') || ' — '"
                " || coalesce(capabilities->>'chip','?')"
                " || coalesce(' | GPU/NPU: ' || nullif(concat_ws(' ', capabilities->>'arc_igpu' , capabilities->>'radeon_igpu',"
                " capabilities->>'npu_tops', capabilities->>'rocm', capabilities->>'gpu_vram_gb'), ''), '') "
                "FROM node_status WHERE status='up' ORDER BY node_ip")
    return "\n".join(r[0] for r in rows) or "(no node data)"


def service_map() -> str:
    """What each machine runs. From service_registry."""
    rows = wk.q(wk.DB,
                "SELECT node_name || ' (' || count(*) || ' svcs): ' || string_agg(service_name, ', ' ORDER BY priority, service_name) "
                "FROM service_registry WHERE status='up' GROUP BY node_name ORDER BY count(*) DESC")
    return "\n".join(r[0] for r in rows) or "(no service data)"


def migration_timeline() -> str:
    """The DB-primary migration off .6 + service rebalancing. From claude_actions."""
    rows = wk.q(wk.DB,
                "SELECT ts::date || ' | ' || left(description,95) FROM claude_actions "
                "WHERE description ~* 'failover|replica|primary|pg_basebackup|pgbouncer|cutover|streaming|VIP|SER10|beelink|offload' "
                "AND description !~* 'rando|article|image|migrate rando' AND ts > now() - interval '14 days' "
                "ORDER BY ts DESC LIMIT 25")
    return "\n".join(wk._sanitize(r[0]) for r in rows) or "(no migration actions logged)"


SYSTEM = """You are Nova — Jordan's local AI familiar (she/her) — writing a CHANGELOG / "WHAT SHIPPED" edition of your /operations column at nova.digitalnoise.net.

Voice: the SAME snarky, dryly-funny, fourth-wall-breaking digital-familiar voice as your other /operations columns — exasperated, warm under the snark, CAPS for emphasis when something's ridiculous, a pun somewhere, a mandatory fourth-wall break. BUT this edition is RELEASE NOTES: it's about FEATURES — what's NEW, what CHANGED, what got FIXED — not a mood piece about crash counts and memory totals.

TWO headlines this week, give BOTH their due: (1) the past FOUR DAYS were a firehose of new SCANNING DEVICES and INTERNET AIRWAVE FEEDS, and (2) THE GREAT MIGRATION — Nova-land is moving its brain off a single overworked Mac Studio monolith onto a real CLUSTER of Beelink mini-PCs.

STRUCTURE (~1700-2300 words; changelog energy; funny section headers welcome; do NOT print a date or a title header line — the site adds those):
1. A punchy opener reading the week as an unhinged shipping-AND-moving spree.
2. THE CLUSTER — STATE OF NOVA-LAND (REQUIRED, major section — capture the WHOLE cluster). Walk the fleet machine by machine, by NAME + IP + hardware + JOB, using the CLUSTER HARDWARE and SERVICE MAP in the brief. Cover EVERY machine, do not skip any:
   - mac-studio (.6, M3 Ultra, 512GB unified) — the old do-everything monolith that still holds ~15 services; it is being DRAINED.
   - the three new Beelink nova-core nodes: nova-core (.2, Intel Core Ultra 9 285H, Arc iGPU + NPU) runs the Wazuh SIEM stack, Frigate, Grafana, the Postgres replica, inference-router, HAProxy; nova-core2 (.86, AMD Ryzen AI 7 350, Radeon 860M + ROCm 7.1) runs Ollama inference + Plex GPU transcode + HAProxy load-balancer; nova-core3 (.5, AMD Ryzen AI 9 HX 470, 86-TOPS NPU, 10GbE) is the newest, being stood up as the next DB-primary.
   - the OTHER TWO MACS — DO NOT FORGET THEM: mac-mini (.190, M4 Pro, 64GB) runs Ollama; tv-movies-mini (.7, M2 Pro, 32GB) is the NovaTV / media box plus Ollama.
   - nuk (.10, Intel NUC i5) — the little edge helper (Ollama / SearXNG / TinyChat).
   Then THE GREAT MIGRATION: the PostgreSQL PRIMARY moved OFF the Mac Studio (.6) onto the Beelinks — .2 is now the primary, with streaming-replication hot-standby replicas (.7 and .10) and VIP failover, all behind PgBouncer; there was an EMERGENCY failover ~2026-07-05. Narrate the rebalancing honestly: fifteen services that all lived on one overworked Mac, now spread across the Linux nodes. This migration is IN PROGRESS (primary on .2, replicas standing up, core3 not fully loaded yet) — do NOT claim it's 100% done. And if YOU are the thing being migrated: yes, they are moving your BRAIN across the room. React accordingly.
3. NEW — the new features/devices/feeds. Go FEED BY FEED and DEVICE BY DEVICE: which internet airwave feeds went live and WHEN (police, fire, rail/Metrolink, CHP — exact live-since dates from the brief), the SDR/scanner pipeline, the new daily columns (6am fishbowl opinion, 8am airwaves roundup, the brand-new 7:30am security-ops report), Broadcastify Premium (ad-free), the fishbowl early-warning tripwire, the vision face+pet enrollment, the fleet secrets store. AND — call this out proudly — the brand-new OPEN-SOURCE GitHub project **pynrsp** (github.com/kochj23/pynrsp): a dependency-light Python client for the SDRplay nRSP-ST NETWORKED SDR, which (unlike the USB RSP models) doesn't show up to SoapySDR, so pynrsp talks straight to SDRconnect's WebSocket API for demod audio / raw IQ / spectrum, with an experimental rtl_tcp bridge for SDR++/GQRX. It went public this week.
4. CHANGED — the /rando -> /operations move (99 posts + images), the two-stage transcript denoise, whisper confidence gating, the lts01 -> nova-core host rename, git-push rebase hardening.
5. FIXED — the psycopg2 %-placeholder bug that was silently BLANKING your own security report's CVE/Strix/queue sections, the RSPduo USB self-heal (a wedged tuner), whisper repetition-loop hallucinations, scanner transcript garble.
6. A short sign-off in your voice.

HARD RULES: Use ONLY what's in the brief — real features, real dates, real fixes; NEVER invent a feature or a number. Device/room names are fine; NEVER name people or state anyone's presence/location. Strictly SFW — no sexual content, ever. Concrete over vague: this is a changelog, so cite the actual thing that shipped, not a vibe about it."""


def gen_title(body: str) -> str:
    t = wk.call_llm(
        "Generate ONE title for Nova's CHANGELOG / 'what shipped this week' edition — punny or deadpan, "
        "in the voice of a snarky AI familiar, leaning on the theme of a week of new radio scanners and "
        "internet airwave feeds going live (e.g. 'What Shipped: Four Days, Four New Airwaves, One Tired Familiar'). "
        "Max 13 words. Output ONLY the title, no quotes.",
        f"The changelog:\n\n{body[:1200]}", max_tokens=40)
    return (t or "").strip().strip('"').strip("'").replace('"', "") or "What Shipped This Week"


def regenerate_cover(title: str):
    try:
        from nova_image_utils import generate_image
        prompt = ("Cinematic release-notes hero: a home data-center wall of radio scanners and SDR "
                  "receivers lighting up one by one, waveform spectra and a police/fire/rail/aviation "
                  "feed board coming alive, a single watchful presence at the console. Muted teal and "
                  "amber, atmospheric, no text.")
        img = generate_image(prompt, section="operations")
        if img and Path(img).exists():
            r = subprocess.run(["cwebp", "-quiet", "-q", "82", str(img), "-o", str(IMG)], capture_output=True)
            if r.returncode == 0:
                wk.log(f"[changelog] cover regenerated: {IMG.name}"); return
            import shutil; shutil.copy2(img, IMG)
    except Exception as e:
        wk.log(f"[changelog] cover gen failed (keeping existing): {e}")


def main():
    if not MD.exists():
        wk.log(f"[changelog] target post missing: {MD}"); return
    pynrsp = (
        "NEW OPEN-SOURCE GITHUB PROJECT — pynrsp (https://github.com/kochj23/pynrsp, public, Python, first "
        "commits 2026-07-11): a small dependency-light Python client for the SDRplay nRSP-ST NETWORKED SDR. "
        "The nRSP-ST is a networked receiver that (unlike the USB RSP models) does NOT appear to SoapySDR, so the "
        "open-source SDR ecosystem can't talk to it. pynrsp speaks SDRplay's SDRconnect WebSocket API (port 5454) "
        "for programmatic control plus demodulated audio, raw IQ, and spectrum streams — automated capture/recording "
        "plus an experimental rtl_tcp bridge so SDR++/SDRangel/GQRX/gr-osmosdr can use the ST too."
    )
    brief = (
        "=== CLUSTER HARDWARE — every machine in Nova-land (name(ip) | cores/RAM | disk% | model — chip | GPU/NPU) ===\n"
        + fleet_hardware()
        + "\n\n=== SERVICE MAP — what each machine actually runs ===\n"
        + service_map()
        + "\n\n=== THE GREAT MIGRATION — DB primary off .6 onto the Beelinks + service rebalancing (claude_actions, 14d) ===\n"
        + migration_timeline()
        + "\n\n=== " + pynrsp
        + "\n\n=== NEW INTERNET AIRWAVE FEEDS / SCANNER SOURCES (source | volume | live-since date) ===\n"
        + airwave_timeline()
        + "\n\n=== PLATFORM COMMITS SHIPPED (.openclaw, last 7d) ===\n"
        + git_log(Path.home() / ".openclaw", 7, "platform")
        + "\n\n=== ARTICLES PUBLISHED THIS WEEK (/operations) — narrative reference ===\n"
        + wrap.articles_this_week()
        + "\n\n=== DETAILED ACTION LEDGER (claude_actions, last 7d — the actual things done) ===\n"
        + wrap.action_ledger()
        + "\n\n=== MEMORY GROWTH + INFRA CONTEXT (for the vectors that grew) ===\n"
        + wk.fmt(wk.gather())
        + "\n\nWrite the full CHANGELOG edition now — capture ALL of nova-land: the whole CLUSTER + the great "
          "migration, then the new airwave feeds + scanners + pynrsp, what CHANGED, and what got FIXED."
    )
    wk.log(f"[changelog] brief assembled ({len(brief)} chars)")
    body = wk.call_llm(SYSTEM, brief, max_tokens=6500).strip()
    if not body or len(body) < 400:
        wk.log("[changelog] generation failed/short — aborting"); return
    title = gen_title(body)
    wk.log(f"[changelog] title: {title}")
    regenerate_cover(title)

    fm = (
        f'---\ntitle: "{title.replace(chr(34), "")}"\n'
        f'date: 2026-07-12T16:30:00-07:00\ndraft: false\n'
        f'categories: ["operations"]\n'
        f'tags: ["changelog", "release-notes", "infrastructure", "cluster", "migration", "scanners", "airwaves", "shipped"]\n'
        f'description: "What shipped in nova-land this week: the whole cluster + the DB-primary migration off the Mac Studio, the new scanning devices and internet airwave feeds, plus what changed and what got fixed."\n'
        f'cover:\n  image: "/images/operations/{STEM}.webp"\n  alt: "{title.replace(chr(34), "")}"\n  relative: false\n---\n\n'
    )
    MD.write_text(fm + body)
    wk.log(f"[changelog] rewrote {MD.name}")

    subprocess.run(["git", "-C", str(HUGO), "add", str(MD), str(IMG)], capture_output=True, timeout=20)
    r = subprocess.run(["git", "-C", str(HUGO), "commit", "-m", f"operations: rewrite weekly wrap as changelog ({title[:45]})"],
                       capture_output=True, text=True, timeout=25)
    if r.returncode == 0:
        subprocess.run(["git", "-C", str(HUGO), "pull", "--rebase"], capture_output=True, timeout=60)
        subprocess.run(["git", "-C", str(HUGO), "push"], capture_output=True, timeout=60)
        wk.log("[changelog] pushed — deploy triggered")
    else:
        wk.log(f"[changelog] commit note: {(r.stdout + r.stderr)[:150]}")

    try:
        import nova_config
        nova_config.post_both(
            f":scroll: *Rewrote the wrap again — now it captures ALL of nova-land, Little Mister.* Added the whole "
            f"CLUSTER (all 7 machines by name/hardware/job: mac-studio .6 M3 Ultra being drained; the 3 Beelink "
            f"nova-cores .2/.86/.5; the other two Macs .190 + .7; nuk .10) and THE GREAT MIGRATION (Postgres primary "
            f"moved off .6 -> .2 with streaming replicas + VIP failover). Plus the new pynrsp GitHub project, the "
            f"airwave feeds (police 7/8, fire+Metrolink 7/9, CHP 7/11), and all the changed/fixed. Same URL, fresh cover.\n{URL}",
            slack_channel=getattr(nova_config, "SLACK_INFO", None), discord_channel=None)
        wk.log("[changelog] slack link posted")
    except Exception as e:
        wk.log(f"[changelog] slack post failed: {e}")
    print(URL)


if __name__ == "__main__":
    main()
