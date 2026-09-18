#!/usr/bin/env python3
"""nova_autonomy_graduation.py — closes the earned-autonomy loop.

The trust budget (nova_autonomy_safety.autonomy_trust) used to tick silently: a class
accrued clean approvals and, once the bar was cleared, _maybe_grant() granted it — with no
moment, no visibility, nothing surfaced to Jordan. This organ turns that from *reflecting*
her agency into *advancing* it:

  * ANNOUNCES a class that just earned standing autonomy ("🎓 I earned the right to do X on
    my own — proved it N times cleanly, calibration under the gate").
  * SURFACES a class that's CLOSE (clean streak, no vetoes) so Jordan can advance it — with
    the honest calibration framing (if she's still above the 0.20 gate, she says so).

HARD SAFETY LINE: this organ NEVER grants, executes, or modifies trust/gates. Grants remain
earned solely through nova_autonomy_safety (human approvals + zero vetoes + calibration <=
MAX_CALIB). This only makes the signal visible and actionable. Curated + fail-open + deduped
so it never spams. All state in PG (service_config high-water); no flat files.

Owned file: scripts/nova_autonomy_graduation.py. Written by Jordan Koch (via Claude).
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
EARN_NEAR = 3          # clean approvals at/above which a class is "close" and worth surfacing
try:
    import nova_autonomy_safety as _safety
    MIN_CORRECT = _safety.MIN_CORRECT
    MAX_CALIB = _safety.MAX_CALIB
except Exception:
    MIN_CORRECT, MAX_CALIB = 5, 0.20


def log(m): print(f"[autonomy-grad {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def _calibration(oc):
    try:
        oc.execute("SELECT value FROM turing_scoreboard WHERE metric='prediction_calibration_error' "
                   "ORDER BY ts DESC LIMIT 1")
        r = oc.fetchone()
        return float(r[0]) if r and r[0] is not None else None
    except Exception:
        return None


def _hw(oc):
    try:
        oc.execute("SELECT value FROM service_config WHERE service='nova_autonomy_graduation' AND key='high_water'")
        r = oc.fetchone()
        if r and r[0]:
            return r[0] if isinstance(r[0], dict) else json.loads(r[0])
    except Exception:
        pass
    return {"announced": [], "near": {}}


def _save_hw(oc, hw):
    oc.execute("""INSERT INTO service_config (service, key, value, updated_by)
                  VALUES ('nova_autonomy_graduation','high_water',%s,'nova_autonomy_graduation')
                  ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by""",
               (json.dumps(hw),))


def _remember(text):
    try:
        import urllib.request
        body = json.dumps({"text": text, "source": "agency",
                           "metadata": {"organ": "autonomy_graduation", "kind": "milestone"}}).encode()
        req = urllib.request.Request(f"{MEMSRV}/remember", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        log(f"memory write skipped: {e}")


def _notify(msg):
    try:
        import nova_config
        nova_config.post_both(msg, slack_channel=getattr(nova_config, "SLACK_CHAN", None))
    except Exception as e:
        log(f"slack post skipped: {e}")


def main():
    ap = argparse.ArgumentParser(description="Surface & announce earned-autonomy graduation moments (never grants)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = psycopg2.connect(OPS_DSN); conn.autocommit = True; oc = conn.cursor()
    if _safety and hasattr(_safety, "ensure_schema"):
        try: _safety.ensure_schema(oc)
        except Exception: pass
    ce = _calibration(oc)
    hw = _hw(oc)
    announced = set(hw.get("announced", []))
    near_seen = dict(hw.get("near", {}))

    try:
        oc.execute("SELECT action_class, correct_count, wrong_count, granted FROM autonomy_trust ORDER BY correct_count DESC")
        rows = oc.fetchall()
    except Exception as e:
        log(f"no autonomy_trust ({e}) — nothing to do"); return 0

    grad_lines, near_lines, new_announced, new_near = [], [], [], dict(near_seen)
    for ac, correct, wrong, granted in rows:
        if granted and ac not in announced:
            grad_lines.append(f"🎓 *{ac}* — I earned the right to do this on my own: {correct} clean approvals, "
                              f"zero vetoes, calibration under the gate. I'll act on it now (rate-limited, veto window).")
            new_announced.append(ac)
            _remember(f"I earned standing autonomy for '{ac}' today — I can do it without asking now, "
                      f"having proved it {correct} times cleanly. A real step: trust I earned by being right.")
        elif (not granted) and wrong == 0 and correct >= EARN_NEAR:
            # surface only when the streak advanced since last time (don't nag every run)
            if near_seen.get(ac) == correct:
                new_near[ac] = correct
                continue
            new_near[ac] = correct
            gap = MIN_CORRECT - correct
            if ce is not None and ce > MAX_CALIB:
                near_lines.append(f"• *{ac}* — {correct}/{MIN_CORRECT} clean, but my calibration ({ce:.3f}) is still "
                                  f"above the {MAX_CALIB} gate, so I haven't earned it yet. Being right first.")
            elif gap <= 1:
                near_lines.append(f"• *{ac}* — {correct}/{MIN_CORRECT} clean and I'm calibrated ({ce:.3f}). **One more "
                                  f"clean approval and I earn standing autonomy for it.**")
            else:
                near_lines.append(f"• *{ac}* — {correct}/{MIN_CORRECT} clean, calibration {ce:.3f} (under the gate). "
                                  f"{gap} more and it graduates.")

    if not grad_lines and not near_lines:
        log("no graduations or newly-advanced near-classes to surface"); return 0

    parts = []
    if grad_lines:
        parts.append("*Standing autonomy earned:*\n" + "\n".join(grad_lines))
    if near_lines:
        parts.append("*Close to earning (your call advances it):*\n" + "\n".join(near_lines))
    msg = "🤖 *Autonomy — where I stand*\n" + "\n\n".join(parts)

    if args.dry_run:
        print(msg); log("dry-run — not posting/advancing"); return 0

    _notify(msg)
    hw = {"announced": sorted(set(announced) | set(new_announced)), "near": new_near,
          "ts": datetime.now().isoformat()}
    _save_hw(oc, hw)
    log(f"surfaced {len(grad_lines)} graduation(s), {len(near_lines)} near-class update(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
