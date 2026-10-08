#!/usr/bin/env python3
"""nova_unifi_ctl.py — the smallest unit of force: block / unblock one client at the UDM Pro.

Used by the gateway tools quarantine_device / unquarantine_device (2026-10-03, Jordan said yes
to gated quarantine after the security organ shipped). Reuses nova_unifi_poller's authenticated
session (API key from the fleet store via the `security` shim). Refuses to touch infrastructure:
any MAC that belongs to a fleet node, the UDM, NVR, NAS, UNAS, switches, APs, Zigbee coordinators
or the Hue/Lutron bridges (looked up live from the UDM device list + the client names below).

CLI: nova_unifi_ctl.py block|unblock|status <mac> [--reason "..."]
ponytail: name-pattern guard + live UniFi device list; add a telemetry.net_inventory tier check
if the naming ever drifts.
"""
import json, re, sys, time, urllib.request
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import nova_unifi_poller as u  # noqa: E402

CMD_URL = f"{u.CONTROLLER_BASE}/proxy/network/api/s/{u.SITE}/cmd/stamgr"
INFRA_NAME_RX = re.compile(r"nova-core|mac-studio|office-m4|jordans-mini|jordans-mac|office-m2|tv-movies|rack \d|unas|unvr|unifi-nvr|synology|udm|slzb|hue bridge|lutron|dream machine|u6 |usw|pro-48|poe", re.I)
INFRA_IPS = {"192.168.1.1","192.168.1.2","192.168.1.5","192.168.1.6","192.168.1.7","192.168.1.9","192.168.1.10","192.168.1.11","192.168.1.69","192.168.1.77","192.168.1.86","192.168.1.125","192.168.1.250","192.168.1.252"}


def _post(payload: dict) -> dict:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if getattr(u, "_api_key", None):
        headers["X-API-Key"] = u._api_key
    req = urllib.request.Request(CMD_URL, data=data, headers=headers, method="POST")
    with u._opener.open(req, timeout=15) as r:
        return json.loads(r.read().decode() or "{}")


def find_client(mac: str) -> dict | None:
    mac = mac.lower()
    for c in u._fetch_clients() or []:
        if (c.get("mac") or "").lower() == mac:
            return c
    return None


def is_infrastructure(mac: str, client: dict | None) -> str | None:
    """Return a reason string if this MAC must never be blocked, else None."""
    mac = mac.lower()
    for d in u._fetch_devices() or []:          # the UniFi gear itself
        if (d.get("mac") or "").lower() == mac:
            return f"UniFi device '{d.get('name')}'"
    if client:
        name = f"{client.get('name') or ''} {client.get('hostname') or ''}"
        if INFRA_NAME_RX.search(name):
            return f"infrastructure name '{name.strip()}'"
        if client.get("ip") in INFRA_IPS:
            return f"infrastructure address {client.get('ip')}"
    return None


def block(mac: str, reason: str = "") -> str:
    if not u._unifi_login():
        return "[error: UniFi login failed]"
    c = find_client(mac)
    why = is_infrastructure(mac, c)
    if why:
        return f"REFUSED: {mac} is {why}. Nova does not quarantine her own fleet or the network gear."
    # P2 (Proteus rule): never cut a person's line to the outside world. A household device
    # (telemetry.device_owner, or a household name) is refused; unreadable ownership fails closed.
    try:
        import nova_safety_guards as _g
        names = [(c or {}).get("name"), (c or {}).get("hostname")]
        ok, gwhy = _g.comms_guard("", macs=[mac], names=[n for n in names if n])
    except Exception as e:  # noqa: BLE001
        ok, gwhy = False, f"comms guard unavailable ({e})"
    if not ok:
        try:
            oc = _g._ops_cursor()
            _g.report_block(oc, source="unifi_ctl", action=f"block {mac} {names}", reason=gwhy, guard="comms")
            oc.connection.close()
        except Exception:
            pass
        return f"REFUSED: {gwhy}"
    r = _post({"cmd": "block-sta", "mac": mac.lower()})
    ok = (r.get("meta") or {}).get("rc") == "ok"
    who = f"{(c or {}).get('name') or (c or {}).get('hostname') or 'unnamed'} ({(c or {}).get('ip') or 'no ip'})"
    return (f"quarantined {mac} = {who} at the UDM Pro (blocked from the LAN). Reason: {reason or '-'}. "
            f"Undo: unquarantine {mac}") if ok else f"[error: UDM refused block-sta: {json.dumps(r)[:200]}]"


def unblock(mac: str) -> str:
    if not u._unifi_login():
        return "[error: UniFi login failed]"
    r = _post({"cmd": "unblock-sta", "mac": mac.lower()})
    return f"unblocked {mac} at the UDM Pro" if (r.get("meta") or {}).get("rc") == "ok" else f"[error: UDM refused unblock-sta: {json.dumps(r)[:200]}]"


def status(mac: str) -> str:
    u._unifi_login()
    c = find_client(mac)
    if not c:
        return f"{mac}: not currently connected"
    return (f"{mac}: {c.get('name') or c.get('hostname') or 'unnamed'} ip={c.get('ip')} wired={bool(c.get('is_wired'))} "
            f"ssid={c.get('essid') or '-'} blocked={bool(c.get('blocked'))} infra={is_infrastructure(mac, c) or 'no'}")


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("block", "unblock", "status"):
        sys.exit("usage: nova_unifi_ctl.py block|unblock|status <mac> [--reason '...']")
    reason = sys.argv[sys.argv.index("--reason") + 1] if "--reason" in sys.argv else ""
    print({"block": lambda: block(sys.argv[2], reason), "unblock": lambda: unblock(sys.argv[2]), "status": lambda: status(sys.argv[2])}[sys.argv[1]]())
