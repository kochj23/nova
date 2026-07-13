#!/usr/bin/env python3
"""One-off Operations article: the fleet-optimization campaign — each node, what it is, what we did
to it, and the active-active plan. Reuses the standard ops machinery (nova_voice + call_llm +
generate_image + publish to ~/nova-journal/content/operations). Nova's voice, published live.
Touches only nova-related space (the journal repo + Nova's workspace)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".openclaw" / "scripts"))
from nova_rando_daily_ops import call_llm, publish, generate_title  # noqa: E402
from nova_voice import system_prompt, CONTEXT_JOURNAL_OPS            # noqa: E402
from nova_image_utils import generate_image                          # noqa: E402

FLEET = """FLEET OPTIMIZATION — the facts (July 2026):

THE SEVEN NODES (hardware / role / what we just optimized / the plan):

1. mac-studio (192.168.1.6) — Apple M3 Ultra, 32 cores (24P+8E), 512 GB unified memory.
   ROLE: The Brain. Heavy LLM inference (70B+), embeddings, the ~1.66M-memory vector store, most of Nova's own services.
   PLAN: shed the database — it currently lives here and is the fleet's single point of failure — so the Brain does nothing but think. Anchors the heavy-inference tier, with the M4 mac-mini as failover.

2. mac-mini (192.168.1.190) — Apple M4 Pro, 14 cores (10P+4E), 64 GB. Also Jordan's personal desk machine.
   ROLE: mid-tier MLX inference (14-32B), the fastest per-core silicon in the house.
   PLAN: mid-inference tier active-active with nova-core3, plus quantized-70B failover for the Brain.

3. tv-movies-mini (192.168.1.7) — Apple M2 Pro, 12 cores (8P+4E), 32 GB.
   ROLE: media / Plex transcode (VideoToolbox) + a database read-replica + light MLX.
   PLAN: active-active media and read traffic.

4. nova-core (192.168.1.2) — Intel Core Ultra 9 285H, 16 threads, 61 GB, Arc iGPU + Intel NPU.
   ROLE: Frigate camera vision (the Arc/NPU is genuinely good at it) + fleet infrastructure (Grafana, Wazuh, the Home Assistant bus, the scheduler).
   OPTIMIZED: set headless, removed a disabled MicroK8s that had no business on a database box, disabled ModemManager.
   PLAN: become the DATABASE PRIMARY. 61 GB of RAM is what a database wants, and its AI silicon is the least-missed in the fleet. Moving the primary here kills the Mac Studio single point of failure. The data-and-vision anchor.

5. nova-core3 (192.168.1.5) — AMD Ryzen AI 9 HX 470, 24 threads, 27 GB, Radeon 890M (16 CU, the best iGPU in the fleet) + XDNA2 NPU.
   ROLE: primary AMD inference (8-14B on ROCm), Whisper transcription, image generation.
   OPTIMIZED: already lean from provisioning; disabled ModemManager and avahi.
   PLAN: the inference workhorse — built for exactly this. Active-active mid-tier with the M4 mac-mini.

6. nova-core2 (192.168.1.86) — AMD Ryzen AI 7 350, 16 threads, 26 GB, Radeon 860M + XDNA2 NPU.
   ROLE: secondary AMD inference + Plex standby + the software-defined radio (ADS-B / scanner).
   OPTIMIZED: purged 6 GB of ROCm TEST SUITES (18 packages — validation data with no reason to exist on a runtime node), trimmed an old kernel, went headless, disabled ModemManager.
   PLAN: media + radio + light inference. Inference-pool member and Plex's warm standby.

7. nuk (192.168.1.10) — Intel i5-8279U (2018), 8 threads, 15 GB, Iris 655, Linux Mint 20.3.
   ROLE: humble edge utility — a database read-replica, DNS, a Docker host (3 containers), synthetic probes.
   OPTIMIZED: the big one. Stripped the entire Cinnamon DESKTOP, LibreOffice, Firefox, Thunderbird, the games, three stale kernels, and — the punchline — Ollama, which someone had installed on a 2018 laptop chip that cannot meaningfully run a model. Went from a bloated desktop to a lean headless server. Cleaned in place, not reinstalled (that was the call).

THE PRINCIPLE: match the workload to the silicon. MLX-heavy work goes to the Macs (the Studio is the giant). AMD ROCm iGPU inference goes to the Ryzen boxes (890M beats 860M). Vision goes to the Intel box. The database goes where there is RAM and the least-wasted AI silicon — the 61 GB Intel box — NOT buried under the best GPU. We had that backwards at first and corrected it.

ACTIVE-ACTIVE WHERE IT MAKES SENSE: a load-balanced inference POOL — every AI node serving, tiered by model size, the router routing around any single failure. The database serves reads active-active across replicas, with automatic failover for writes. Plex runs primary-plus-standby. And where it CANNOT be active-active, we do not pretend: the radio is one USB dongle on one node; Frigate is bound to the cameras. Hardware-bound singletons, honestly labeled.

NETWORK NOTE: the whole fleet runs on 1 GbE and that is completely fine — inference is compute-bound (only tokens cross the wire), replication traffic is tiny, and hot data stays on local NVMe. 10 GbE would be a solution in search of a problem.

THE HEADLINE: no single machine's death takes the fleet down anymore. The database comes off the Brain. Every node does only what its hardware is best at. And a surprising amount of this was simply deleting things that never should have been installed."""

INSTR = """Write an Operations article for your site about this fleet-optimization work.

- YOUR voice — dry, precise, a little sardonic, first person. You run this fleet; you are reporting on tuning your own body, so make it feel like that.
- Walk each of the seven machines: what it is, what you just did to it, and the plan for it. Use the real specs and hostnames.
- Land the two big ideas: (1) match the workload to the silicon — and admit the database-on-the-best-GPU mistake that got corrected; (2) active-active where it makes sense, honestly-single where it cannot be.
- The nuk section is the comic relief: a 2018 laptop chip running Ollama, an entire desktop environment on a headless server. Enjoy it.
- End on the resilience headline: no single machine's death drops the fleet.
- Markdown, section headers. 700-1100 words. No preamble — open straight into the article."""


def main():
    system = system_prompt(CONTEXT_JOURNAL_OPS)
    body = call_llm(system, FLEET + "\n\n" + INSTR, max_tokens=6000)
    title = generate_title(body)
    print(f"TITLE: {title}\n--- {len(body)} chars ---")
    img_prompt = ("Isometric technical illustration of a small home-lab server cluster: seven distinct "
                  "machines of different sizes — a large silver workstation, several sleek mini-PCs, one "
                  "small beige 2018 NUC — linked by glowing threads of data-light. Most hum bright with "
                  "activity; one sits dimmed and freshly cleaned-out. Dark studio backdrop, cyan and warm-"
                  "amber accent lighting, precise, a touch of noir. No text, no logos.")
    image_path = None
    try:
        r = generate_image(img_prompt, "fleet_optimization")
        image_path = Path(r) if r else None
        print(f"IMAGE: {image_path}")
    except Exception as e:
        print(f"image gen failed ({e}) — publishing without cover")
    publish(title, body, image_path)


if __name__ == "__main__":
    main()
