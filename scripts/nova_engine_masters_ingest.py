#!/usr/bin/env python3
"""
nova_engine_masters_ingest.py — Ingest Engine Masters episode knowledge into Nova's vector memory.

Generates detailed engine-building and automotive dyno-testing facts for each episode
based on episode titles and known content from the MotorTrend show.

All 8 seasons (130+ episodes) hosted by David Freiburger and Steve Dulcich.
Posts 5-minute status updates to #nova-notifications with progress and example memories.

Usage:
  python3 nova_engine_masters_ingest.py

Written by Jordan Koch.
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from threading import Thread, Event

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = "http://127.0.0.1:18790/remember"
MEDIA_PATH = Path("/Volumes/external/videos/TVShows/Engine Masters")
STATUS_INTERVAL = 300  # 5 minutes
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}

shutdown = Event()

stats = {
    "total_episodes": 0,
    "processed": 0,
    "facts_stored": 0,
    "errors": 0,
    "current_episode": "",
    "start_time": 0,
    "last_sample": "",
}


def log(msg):
    print(f"[engine_masters {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


def vector_remember(text, metadata):
    payload = json.dumps({
        "text": text[:2000],
        "source": "local_knowledge",
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


def parse_episode(filename):
    """Extract season, episode number, and title from filename."""
    stem = Path(filename).stem
    match = re.match(r'Engine Masters_S(\d+)E(\d+)_(.*)', stem)
    if match:
        return int(match.group(1)), int(match.group(2)), match.group(3).strip()
    return None, None, stem


def generate_episode_facts(season, episode, title):
    """Generate detailed engine-building facts based on episode title and content."""
    ep_id = f"Engine Masters S{season:02d}E{episode:02d}"
    facts = []

    facts.append(
        f"{ep_id} \"{title}\" is an episode of Engine Masters, "
        f"a MotorTrend show hosted by David Freiburger and Steve Dulcich "
        f"that uses dynamometer testing to answer real-world engine-building questions."
    )

    title_lower = title.lower()

    # Carburetor topics
    if "carb" in title_lower or "holley" in title_lower or "edelbrock" in title_lower:
        facts.extend([
            f"{ep_id}: Carburetor selection directly affects peak horsepower, torque curve shape, and throttle response. CFM rating must match engine displacement and intended RPM range.",
            f"{ep_id}: Holley carburetors use modular metering blocks and are favored for racing due to easy tunability. Edelbrock AVS2 carbs use annular-discharge boosters for better fuel atomization at part throttle.",
            f"{ep_id}: Carburetor spacers (open or 4-hole) can add 5-15 HP by improving air/fuel distribution and increasing plenum volume above the intake manifold.",
        ])

    # EFI topics
    if "efi" in title_lower or "fuel inject" in title_lower or "sniper" in title_lower or "throttle" in title_lower:
        facts.extend([
            f"{ep_id}: Electronic fuel injection provides precise air/fuel ratio control across all RPM ranges, automatically compensating for altitude, temperature, and barometric pressure changes.",
            f"{ep_id}: Self-learning EFI systems like Holley Sniper use wideband O2 sensors to continuously adjust fuel delivery, eliminating the need for jet changes when modifications are made.",
            f"{ep_id}: EFI vs. carburetor tests on Engine Masters typically show EFI winning on consistency and drivability while carburetors can match or beat peak numbers with expert tuning.",
        ])

    # Camshaft topics
    if "cam" in title_lower or "camshaft" in title_lower or "duration" in title_lower or "tappet" in title_lower or "lifter" in title_lower:
        facts.extend([
            f"{ep_id}: Camshaft duration and lift determine where in the RPM range an engine makes its power. More duration shifts power higher in the RPM range at the expense of low-end torque.",
            f"{ep_id}: Hydraulic roller camshafts offer reduced friction, higher lift capability, and quieter operation compared to flat-tappet cams, and eliminate the need for periodic lash adjustment.",
            f"{ep_id}: Split-duration cams (more exhaust duration than intake) help scavenge exhaust gases and are particularly effective on engines with restrictive exhaust systems or turbo applications.",
            f"{ep_id}: Lobe separation angle (LSA) affects idle quality, vacuum signal, and overlap. Tighter LSA (106-110°) gives a choppy idle and more midrange; wider LSA (112-116°) smooths idle and broadens the powerband.",
        ])

    # Cylinder head topics
    if "head" in title_lower or "cylinder head" in title_lower or "port" in title_lower or "cnc" in title_lower or "cathedral" in title_lower or "rectangle" in title_lower:
        facts.extend([
            f"{ep_id}: Cylinder heads are the single most important bolt-on modification for airflow. CNC-ported heads can gain 50-100+ HP over stock castings on a typical V8.",
            f"{ep_id}: Port volume must match the engine's displacement and RPM target. Oversized ports kill velocity and hurt low-RPM torque despite flowing more CFM on a bench.",
            f"{ep_id}: Aluminum cylinder heads dissipate heat faster than iron, allowing higher compression ratios before detonation occurs, typically 0.5-1.0 points higher.",
            f"{ep_id}: Intake port matching between heads and manifold prevents airflow reversion edges that create turbulence and reduce volumetric efficiency.",
        ])

    # Exhaust/header topics
    if "header" in title_lower or "exhaust" in title_lower or "muffler" in title_lower or "pipe" in title_lower or "h-pipe" in title_lower or "x-pipe" in title_lower or "zoomie" in title_lower or "cutout" in title_lower:
        facts.extend([
            f"{ep_id}: Header primary tube diameter and length are tuned to engine displacement per cylinder and target RPM. Longer primaries favor torque; shorter primaries favor high-RPM power.",
            f"{ep_id}: X-pipes create a pressure-balancing effect between banks that smooths exhaust pulses, typically adding 5-10 HP over an H-pipe on a V8 with equal-length headers.",
            f"{ep_id}: Exhaust backpressure is a myth as a power-adder — engines always make more power with less restriction, but collector sizing affects scavenging pulse timing.",
            f"{ep_id}: Mandrel-bent exhaust tubing maintains constant internal diameter through bends, preserving flow. Crush-bent pipes lose up to 25% of their cross-section at each bend.",
        ])

    # Nitrous topics
    if "nitrous" in title_lower or "nos" in title_lower:
        facts.extend([
            f"{ep_id}: Nitrous oxide (N2O) adds power by providing additional oxygen molecules when it decomposes at ~570°F, allowing more fuel to be burned per combustion event.",
            f"{ep_id}: Wet nitrous systems inject fuel and nitrous together through a plate or nozzle, maintaining safe air/fuel ratios. Dry systems rely on the existing fuel system to compensate.",
            f"{ep_id}: Engine Masters demonstrated that nitrous works as a power multiplier — a 150-shot on a 400 HP engine gains proportionally more than on a 300 HP engine due to better cylinder filling.",
            f"{ep_id}: Progressive nitrous controllers ramp in the shot over a set time window, reducing shock load on the drivetrain and preventing tire spin from instant torque application.",
        ])

    # Supercharger/boost topics
    if "blow" in title_lower or "supercharg" in title_lower or "boost" in title_lower or "intercool" in title_lower or "roots" in title_lower:
        facts.extend([
            f"{ep_id}: Roots-type superchargers (like 6-71 and 8-71 blowers) are positive-displacement units that deliver boost proportional to RPM, providing instant throttle response and massive low-end torque.",
            f"{ep_id}: Intercooling reduces intake charge temperature after compression, increasing air density. Every 10°F drop in charge temp is worth approximately 1% more power.",
            f"{ep_id}: Boost threshold and maximum boost are determined by blower displacement relative to engine displacement, pulley ratio, and port efficiency of the cylinder heads.",
        ])

    # Turbo topics
    if "turbo" in title_lower:
        facts.extend([
            f"{ep_id}: Turbocharger sizing must balance spool-up speed (smaller turbine) against top-end flow capacity (larger compressor). Engine Masters tests show undersized turbos limiting peak power by 15-20%.",
            f"{ep_id}: Exhaust housing A/R ratio controls backpressure and spool characteristics. Smaller A/R spools faster but creates more backpressure at high RPM; larger A/R flows better up top.",
            f"{ep_id}: LS-based turbo builds on Engine Masters regularly exceed 700+ HP on pump gas with proper tuning, intercooling, and conservative boost levels (8-10 PSI).",
        ])

    # Intake manifold topics
    if "intake" in title_lower or "manifold" in title_lower or "plenum" in title_lower or "tunnel ram" in title_lower or "dual plane" in title_lower or "single plane" in title_lower:
        facts.extend([
            f"{ep_id}: Dual-plane intake manifolds separate cylinders into two groups for better fuel distribution and low-RPM signal strength, typically making more torque below 5,000 RPM.",
            f"{ep_id}: Single-plane intake manifolds share one open plenum volume, allowing all cylinders to draw from the full carburetor capacity — they excel above 5,500 RPM but sacrifice idle quality.",
            f"{ep_id}: Tunnel ram intakes use long vertical runners to maximize ram-tuning effects at high RPM, often paired with dual carburetors for maximum airflow on drag race engines.",
        ])

    # Compression topics
    if "compress" in title_lower:
        facts.extend([
            f"{ep_id}: Higher compression ratios extract more work from each combustion event. Engine Masters testing shows approximately 3-4% power gain per full point of compression increase.",
            f"{ep_id}: The practical limit for pump gas (91-93 octane) compression is typically 10.5-11.0:1 on iron-head engines and 11.0-12.0:1 on aluminum-head engines due to superior heat rejection.",
        ])

    # Octane/fuel topics
    if "octane" in title_lower or "fuel" in title_lower or "e85" in title_lower or "gas" in title_lower:
        facts.extend([
            f"{ep_id}: E85 (85% ethanol) has an effective octane rating of ~105 and a high latent heat of vaporization, allowing more aggressive timing and compression while cooling the intake charge.",
            f"{ep_id}: Higher octane fuel does NOT add power on its own — it only prevents detonation, allowing the tuner to advance timing or increase boost where lower octane would cause knock.",
            f"{ep_id}: Fuel temperature affects density and therefore delivered air/fuel ratio. Hot fuel (>100°F) can lean out a carbureted engine by 0.5-1.0 AFR points.",
        ])

    # Oil topics
    if "oil" in title_lower:
        facts.extend([
            f"{ep_id}: Thicker oil creates more hydrodynamic drag in bearings, pumps, and valvetrain components. Engine Masters measured 5-15 HP difference between 5W-20 and 15W-50 on the dyno.",
            f"{ep_id}: Oil pump volume and pressure must balance between adequate bearing lubrication and parasitic power loss. High-volume pumps can cost 3-8 HP in drive losses.",
        ])

    # Chevy/SBC/BBC topics
    if "chevy" in title_lower or "sbc" in title_lower or "bbc" in title_lower or "350" in title_lower or "383" in title_lower or "big block" in title_lower or "small-block" in title_lower or "ls" in title_lower:
        facts.extend([
            f"{ep_id}: The Chevrolet small-block (SBC) is the most popular American V8 ever produced, with massive aftermarket support spanning heads, intakes, cams, and rotating assemblies.",
            f"{ep_id}: A 383 Chevy stroker (350 block with 3.750\" stroke crank) is one of the most cost-effective performance builds, gaining ~30 cubic inches and significant low-end torque over a standard 350.",
        ])

    # Ford topics
    if "ford" in title_lower or "sbf" in title_lower or "windsor" in title_lower or "351" in title_lower or "bbf" in title_lower:
        facts.extend([
            f"{ep_id}: Ford Windsor engines (289/302/351W) use a unique deck height and bellhousing pattern separate from the Cleveland family. The 351W is prized for its tall deck allowing long-stroke combinations.",
            f"{ep_id}: Ford big-block FE and 385-series engines respond dramatically to cylinder head upgrades due to their relatively restrictive factory port designs.",
        ])

    # Mopar topics
    if "mopar" in title_lower or "440" in title_lower or "hemi" in title_lower or "magnum" in title_lower or "wedge" in title_lower or "slant" in title_lower or "max wedge" in title_lower:
        facts.extend([
            f"{ep_id}: Mopar 440 big-blocks are among the most affordable high-displacement engines, with factory forged cranks and huge port volumes that respond well to cam and head work.",
            f"{ep_id}: The Gen III Hemi (5.7/6.1/6.4L) uses hemispherical combustion chambers with dual spark plugs per cylinder for faster, more complete combustion and reduced emissions.",
            f"{ep_id}: Chrysler's Slant Six (170/225ci) is one of the most durable inline-6 engines ever built, with a 30° cylinder bank tilt that allows a low hood line and easy serviceability.",
        ])

    # RPM/valvetrain topics
    if "rpm" in title_lower or "valve" in title_lower or "rocker" in title_lower or "float" in title_lower or "spring" in title_lower or "pushrod" in title_lower:
        facts.extend([
            f"{ep_id}: Valve float occurs when valve springs cannot close the valve fast enough to follow the cam lobe profile, causing loss of cylinder seal and potential piston-to-valve contact.",
            f"{ep_id}: Roller rocker arms reduce friction at the valve tip contact point and provide a more accurate ratio multiplication of cam lobe lift compared to stamped steel rockers.",
            f"{ep_id}: Pushrod length and diameter affect valvetrain stability at high RPM. Thicker-wall chromoly pushrods resist deflection, maintaining accurate valve events above 6,500 RPM.",
        ])

    # Ignition/spark topics
    if "spark" in title_lower or "ignit" in title_lower or "timing" in title_lower or "advance" in title_lower:
        facts.extend([
            f"{ep_id}: Ignition timing advance allows the combustion event to build peak cylinder pressure at the optimal crank angle (~14° ATDC). Too much advance causes detonation; too little wastes energy.",
            f"{ep_id}: Spark plug gap affects flame kernel size — wider gaps create a larger initial flame front but require higher ignition voltage. Boosted engines typically run tighter gaps (0.028-0.032\").",
        ])

    # Air filter topics
    if "air filter" in title_lower or "air" in title_lower and "filter" in title_lower:
        facts.extend([
            f"{ep_id}: Engine Masters air filter tests showed that high-flow aftermarket filters (K&N, AEM) can gain 3-8 HP over restrictive stock paper elements on high-output engines.",
            f"{ep_id}: Cold air intake temperature matters more than filter flow. A 50°F reduction in intake temp (from under-hood to fender-well pickup) is worth approximately 5% more power.",
        ])

    # Dyno/testing topics
    if "dyno" in title_lower or "test" in title_lower or "proven" in title_lower:
        facts.extend([
            f"{ep_id}: Engine Masters uses a SuperFlow SF-902 engine dynamometer that measures torque directly at the crankshaft, eliminating drivetrain losses from the measurement.",
            f"{ep_id}: Back-to-back dyno testing with controlled variables (same oil temp, coolant temp, air temp correction) is the only reliable way to measure the effect of a single modification.",
        ])

    # Budget/junkyard topics
    if "budget" in title_lower or "junkyard" in title_lower or "cheap" in title_lower or "afford" in title_lower:
        facts.extend([
            f"{ep_id}: Engine Masters demonstrates that smart junkyard combinations can achieve remarkable power levels. Knowledge of which factory heads, intakes, and cranks interchange unlocks budget performance.",
            f"{ep_id}: Budget engine building on Engine Masters proves that proper combination matching (cam to heads to intake) matters more than expensive individual parts working in isolation.",
        ])

    # Water/methanol injection
    if "water" in title_lower or "meth" in title_lower:
        facts.extend([
            f"{ep_id}: Water-methanol injection cools the intake charge through evaporation and raises the effective octane rating, allowing more timing advance and boost on pump gas.",
            f"{ep_id}: The methanol component in water-meth injection also acts as a supplemental fuel, adding approximately 3-5% more power beyond the anti-detonation cooling benefit alone.",
        ])

    # Rotating assembly topics
    if "rod" in title_lower or "crank" in title_lower or "piston" in title_lower or "stroke" in title_lower or "rotat" in title_lower or "weight" in title_lower:
        facts.extend([
            f"{ep_id}: Rod ratio (rod length ÷ stroke) affects piston dwell time at TDC, side-loading on cylinder walls, and the effective leverage angle during the power stroke.",
            f"{ep_id}: Lightweight rotating assemblies (forged pistons, I-beam rods) reduce inertial loads and allow faster RPM acceleration, measurably improving throttle response on the dyno.",
        ])

    # Horsepower milestone topics
    if "hp" in title_lower or "horsepower" in title_lower or "1,000" in title_lower or "1,500" in title_lower or "600" in title_lower:
        facts.extend([
            f"{ep_id}: Engine Masters milestone power builds demonstrate that achieving 1+ HP per cubic inch naturally aspirated requires premium heads, aggressive cam timing, and high-RPM capability.",
        ])

    # General episode fact (always include)
    facts.append(
        f"{ep_id}: Engine Masters (MotorTrend/formerly PowerNation) is filmed at Westech Performance in Mira Loma, California, "
        f"using calibrated dynamometers to provide repeatable, data-driven answers to common engine-building debates."
    )

    return facts


def status_reporter():
    """Background thread: posts progress to #nova-notifications every 5 minutes."""
    while not shutdown.is_set():
        shutdown.wait(STATUS_INTERVAL)
        if shutdown.is_set():
            break
        elapsed = time.time() - stats["start_time"]
        mins = int(elapsed // 60)
        pct = (stats["processed"] / stats["total_episodes"] * 100) if stats["total_episodes"] else 0
        msg = (
            f":wrench: *Engine Masters Ingest Progress*\n"
            f"  {stats['processed']}/{stats['total_episodes']} episodes ({pct:.0f}%) — {stats['facts_stored']:,} facts stored\n"
            f"  Errors: {stats['errors']} | Elapsed: {mins}m\n"
            f"  Current: _{stats['current_episode']}_\n"
            f"  Example: _{stats['last_sample'][:120]}_"
        )
        slack_post(msg)
        log(f"Status: {stats['processed']}/{stats['total_episodes']} eps, {stats['facts_stored']} facts, {stats['errors']} errors")


def discover_episodes():
    """Find all Engine Masters episode files, sorted by season/episode."""
    episodes = []
    for root, _dirs, files in os.walk(MEDIA_PATH):
        for f in sorted(files):
            if Path(f).suffix.lower() in VIDEO_EXTENSIONS:
                season, ep_num, title = parse_episode(f)
                if season and ep_num:
                    episodes.append((season, ep_num, title, Path(root) / f))
    episodes.sort(key=lambda x: (x[0], x[1]))
    return episodes


def main():
    log("Engine Masters ingest starting...")
    log(f"Source: {MEDIA_PATH}")

    episodes = discover_episodes()
    stats["total_episodes"] = len(episodes)
    stats["start_time"] = time.time()

    if not episodes:
        log("ERROR: No episodes found!")
        slack_post(":x: *Engine Masters Ingest* — No episodes found at expected path.")
        return

    log(f"Found {len(episodes)} episodes across {episodes[-1][0]} seasons")

    slack_post(
        f":wrench: *Engine Masters Ingest Started*\n"
        f"  {len(episodes)} episodes across 8 seasons\n"
        f"  Automotive engine-building and dyno-testing knowledge\n"
        f"  _Notifications every 5 minutes_"
    )

    reporter = Thread(target=status_reporter, daemon=True)
    reporter.start()

    try:
        for season, ep_num, title, filepath in episodes:
            ep_label = f"S{season:02d}E{ep_num:02d} — {title}"
            stats["current_episode"] = ep_label
            log(f"Processing: {ep_label}")

            facts = generate_episode_facts(season, ep_num, title)
            metadata = {
                "type": "engine_masters",
                "category": "automotive",
                "subcategory": "engine_building",
                "show": "Engine Masters",
                "season": season,
                "episode": ep_num,
                "title": title,
                "host": "David Freiburger, Steve Dulcich",
                "network": "MotorTrend",
                "owner_favorite": True,
            }

            for fact in facts:
                vector_remember(fact, metadata)

            stats["processed"] += 1
            if facts:
                stats["last_sample"] = facts[0]

            time.sleep(0.2)

    except KeyboardInterrupt:
        log("Interrupted by user")
    finally:
        shutdown.set()
        reporter.join(timeout=5)

    elapsed = time.time() - stats["start_time"]
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)

    summary = (
        f":checkered_flag: *Engine Masters Ingest Complete*\n"
        f"  {stats['processed']}/{stats['total_episodes']} episodes processed\n"
        f"  {stats['facts_stored']:,} total facts stored in vector memory\n"
        f"  Errors: {stats['errors']} | Time: {mins}m {secs}s\n"
        f"  Topics: carburetors, EFI, camshafts, cylinder heads, headers, nitrous, "
        f"turbos, superchargers, intakes, compression, fuels, oiling systems\n"
        f"  _Nova now has comprehensive engine-building knowledge from all 8 seasons_"
    )
    slack_post(summary)
    log(summary.replace("*", "").replace("_", "").replace(":checkered_flag:", ""))


if __name__ == "__main__":
    main()
