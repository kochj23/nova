#!/opt/homebrew/bin/python3
"""
nova_llm_budget_guard.py — hard $10/day cap on OpenRouter spend.

OpenRouter enforces a per-KEY *total* USD limit natively (402 when exceeded) but has
no per-day reset. So each new day we ratchet that limit: limit = today's_baseline_usage
+ DAILY. Within any day the key can spend at most DAILY before OpenRouter blocks it.

Runs daily at 00:05. Idempotent — safe to run repeatedly; only advances the ceiling
when the calendar day changes.

Enforcement needs a PROVISIONING key (Keychain: nova-openrouter-provisioning-key) to
PATCH the runtime key's limit. Without it -> monitor-only (logs the ceiling it WOULD
set + alerts if today's spend already blew past DAILY).
"""
import nova_dsn as _nova_dsn  # noqa: E402
import subprocess, json, urllib.request, urllib.error, sys, os
from datetime import datetime

DAILY = 10.0
RUNTIME_KEY = "nova-openrouter-api-key"
PROV_KEY = "nova-openrouter-provisioning-key"
API = "https://openrouter.ai/api/v1"

sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))


def kc(name):
    r = subprocess.run(["security", "find-generic-password", "-a", "nova", "-s", name, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def _get(url, key):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def ceiling_for(day_state, today, usage):
    """Pure: given stored state + today's date + current total usage, return
    (new_state, target_limit). New calendar day -> baseline resets to current usage."""
    if day_state.get("day") != today:
        baseline = usage                      # start the day's meter here
    else:
        baseline = day_state.get("baseline", usage)
    return {"day": today, "baseline": baseline}, round(baseline + DAILY, 2)


def load_state():
    try:
        import psycopg2
        c = psycopg2.connect(_nova_dsn.pg_dsn("nova_ops")); cur = c.cursor()
        cur.execute("SELECT value FROM service_config WHERE service='nova' AND key='llm_budget'")
        row = cur.fetchone(); c.close()
        return row[0] if row else {}
    except Exception:
        return {}


def save_state(st):
    import psycopg2
    c = psycopg2.connect(_nova_dsn.pg_dsn("nova_ops")); cur = c.cursor()
    cur.execute("""INSERT INTO service_config (service,key,value) VALUES ('nova','llm_budget',%s)
                   ON CONFLICT (service,key) DO UPDATE SET value=EXCLUDED.value""", (json.dumps(st),))
    c.commit(); c.close()


def main():
    rk = kc(RUNTIME_KEY)
    if not rk:
        print("no runtime key in Keychain"); return
    data = _get(f"{API}/credits", rk)["data"]
    usage = data["total_usage"]
    today = datetime.now().strftime("%Y-%m-%d")
    st = load_state()
    new_st, target = ceiling_for(st, today, usage)
    spent_today = round(usage - new_st["baseline"], 2)
    print(f"usage=${usage:.2f} today_baseline=${new_st['baseline']:.2f} spent_today=${spent_today:.2f} target_limit=${target}")

    prov = kc(PROV_KEY)
    if not prov:
        msg = f"[monitor-only] would cap key at ${target}. No provisioning key — add one to enforce."
        if spent_today >= DAILY:
            msg = f"⚠️ OpenRouter spent ${spent_today:.2f} today (≥${DAILY} cap) and CANNOT be auto-frozen — no provisioning key in Keychain."
            try:
                import nova_notify
                nova_notify.notify(msg, level="warning", category="cost", source="nova_llm_budget_guard")
            except Exception:
                pass
        print(msg)
        save_state(new_st); return

    # enforce: find the runtime key's hash, PATCH its limit to the day's ceiling
    keys = _get(f"{API}/keys", prov).get("data", [])
    khash = next((k.get("hash") for k in keys
                  if k.get("name", "").lower().find("nova") >= 0 or k.get("label", "") == RUNTIME_KEY), None)
    if not khash and len(keys) == 1:
        khash = keys[0].get("hash")
    if not khash:
        print(f"could not identify runtime key among {len(keys)} keys — set label to '{RUNTIME_KEY}'"); save_state(new_st); return
    body = json.dumps({"limit": target}).encode()
    req = urllib.request.Request(f"{API}/keys/{khash}", data=body, method="PATCH",
                                 headers={"Authorization": f"Bearer {prov}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        json.load(r)
    print(f"✓ OpenRouter key limit set to ${target} — hard ${DAILY}/day cap active")
    save_state(new_st)


def selftest():
    # new day -> baseline resets to current usage; same day -> baseline sticks
    s1, t1 = ceiling_for({}, "2026-07-02", 1813.0)
    assert s1 == {"day": "2026-07-02", "baseline": 1813.0} and t1 == 1823.0, (s1, t1)
    s2, t2 = ceiling_for(s1, "2026-07-02", 1817.0)          # same day, spent $4
    assert s2["baseline"] == 1813.0 and t2 == 1823.0, (s2, t2)   # ceiling unchanged
    s3, t3 = ceiling_for(s2, "2026-07-03", 1820.0)          # next day
    assert s3["baseline"] == 1820.0 and t3 == 1830.0, (s3, t3)
    print("selftest OK")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        main()
