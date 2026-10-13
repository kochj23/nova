#!/usr/bin/env python3
"""nova_unifi_toolkit.py — Crosstalk's UI Toolkit (github.com/Crosstalk-Solutions/unifi-toolkit v1.13.1),
rebuilt headless inside Nova (Jordan 2026-10-13). No web UI, no second login store: it reuses
nova_unifi_poller's UDM session (API key from the fleet store / Keychain), writes to nova_ops, alerts via
nova_notify, and Grafana draws the dashboards (nova_grafana_coverage.py group "unifi").

  threats        Threat Watch: IDS/IPS events (v2 traffic-flows, policy_type INTRUSION_PREVENTION) ->
                 unifi_threat_events; unifi_threat_ignore (cidr + max risk) mutes noise; new high-risk
                 events alert.                                                     [scheduler, 5 min]
  stalk          Wi-Fi Stalker: every client's AP/port -> unifi_client_state; connect / disconnect /
                 roam -> unifi_client_events; hourly AP dwell -> unifi_client_presence. Devices in
                 unifi_tracked_devices (alert=true) notify on every event.          [scheduler, 2 min]
  audit          House Arrest's read side: the network isolation matrix (zones, isolation, internet,
                 mDNS, DHCP DNS, per-SSID client isolation), zone/isolation findings, lockdowns that
                 stopped enforcing (MAC rotated / gone) and ALLOWs that out-rank them -> unifi_network_audit;
                 posture drift and broken lockdowns alert.                          [scheduler, hourly]
  track add|rm|list, report <mac>     Stalker watch list and per-device analytics (dwell, favourite AP,
                 day-of-week x hour heatmap, recent events).
  block|unblock <mac>                 via nova_unifi_ctl (infrastructure + household guards).
  lockdown / release / isolate / release-network / dns-lockdown / dns-release / wlan-isolation /
  mdns / dhcp-dns                     House Arrest writes. DRY RUN unless --apply. Every rule carries the
                 upstream "[HouseArrest]" marker and release deletes only marked rules; fleet/network gear is
                 refused; internet-cutting presets on household devices need --override-comms-guard.
                 Every write (and preview) is logged to unifi_toolkit_actions.

Credit: the features, their design and the House Arrest safety model are Crosstalk Solutions' work (UI Toolkit,
MIT, (c) 2025 Crosstalk Solutions, https://github.com/Crosstalk-Solutions/unifi-toolkit). Policy payloads come from
their builder, vendored unmodified (unifi_ha_policies.py); Nova adds the headless wiring and hardening.
See THIRD_PARTY_NOTICES.md.
Security audit of upstream: docs in agent_docs 'unifi-toolkit'.
"""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_unifi_poller as u          # noqa: E402  (session, API key, TLS context)
import unifi_ha_policies as P          # noqa: E402

API = f"{u.CONTROLLER_BASE}/proxy/network"
SITE = u.SITE
RISK_RANK = {"low": 1, "medium": 2, "high": 3}
STALK_INTERVAL_MIN = 2                 # scheduler cadence; presence samples are worth this many minutes
RETAIN_EVENTS_DAYS, RETAIN_PRESENCE_DAYS, RETAIN_THREATS_DAYS = 180, 90, 365

SCHEMA = """
CREATE TABLE IF NOT EXISTS unifi_threat_events (
  flow_id text PRIMARY KEY, ts timestamptz NOT NULL, risk text, action text, signature text,
  signature_id text, category text, src_ip inet, src_port int, src_mac text, src_country text,
  dst_ip inet, dst_port int, dst_mac text, dst_country text, proto text, service text,
  direction text, network text, ignored boolean NOT NULL DEFAULT false,
  alerted boolean NOT NULL DEFAULT false, raw jsonb, seen_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS unifi_threat_events_ts ON unifi_threat_events (ts DESC);
CREATE TABLE IF NOT EXISTS unifi_threat_ignore (
  id serial PRIMARY KEY, cidr cidr NOT NULL,
  max_risk text NOT NULL DEFAULT 'high' CHECK (max_risk IN ('low','medium','high')),
  note text, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS unifi_client_state (
  mac text PRIMARY KEY, name text, hostname text, ip text, oui text, is_wired boolean, ap text,
  essid text, radio text, channel int, signal int, network text, network_id text,
  online boolean NOT NULL, connected_since timestamptz, last_seen timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS unifi_client_events (
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), mac text NOT NULL,
  kind text NOT NULL, from_ap text, to_ap text, detail jsonb);
CREATE INDEX IF NOT EXISTS unifi_client_events_mac_ts ON unifi_client_events (mac, ts DESC);
CREATE INDEX IF NOT EXISTS unifi_client_events_ts ON unifi_client_events (ts DESC);
CREATE TABLE IF NOT EXISTS unifi_client_presence (
  mac text NOT NULL, hour timestamptz NOT NULL, ap text NOT NULL, samples int NOT NULL DEFAULT 0,
  PRIMARY KEY (mac, hour, ap));
CREATE TABLE IF NOT EXISTS unifi_tracked_devices (
  mac text PRIMARY KEY, label text, alert boolean NOT NULL DEFAULT true,
  added_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS unifi_network_audit (
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), digest jsonb NOT NULL,
  findings jsonb NOT NULL, lockdowns jsonb NOT NULL, matrix jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS unifi_toolkit_ledger (
  obj_id text PRIMARY KEY, kind text NOT NULL, label text, payload jsonb, before jsonb,
  created_at timestamptz NOT NULL DEFAULT now(), released_at timestamptz);
CREATE TABLE IF NOT EXISTS unifi_toolkit_actions (
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), action text NOT NULL,
  args jsonb, dry_run boolean NOT NULL, ok boolean, result jsonb, host text, actor text);
"""


def log(msg):
    print(f"[unifi-toolkit] {msg}", flush=True)


# ---------------------------------------------------------------- PG / notify

def _ops():
    import psycopg2
    try:
        import nova_dsn
        dsn = nova_dsn.pg_dsn("nova_ops")
    except ImportError:
        dsn = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
    conn = psycopg2.connect(dsn, connect_timeout=10)
    conn.autocommit = True
    return conn


def ensure_schema(cur):
    cur.execute(SCHEMA)


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def safe(s, n=120) -> str:
    """Attacker-controlled strings (hostnames, SSIDs, IDS signatures) -> inert text for Slack.
    Neutralises <!channel>/<@U..>/<url|label> mrkdwn and control characters, then truncates."""
    s = _CTRL.sub(" ", str(s if s is not None else ""))
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("@", "@​")
    return s[:n] + ("…" if len(s) > n else "")


def alert(title, body, level="warning", dedup_key=None):
    try:
        from nova_notify import notify
        notify(title, body=body, level=level, category="network", dedup_key=dedup_key,
               meta={"host": "udm-pro", "source": "unifi_toolkit"})
    except Exception as e:  # noqa: BLE001 — an alert failure must not lose the data already written
        log(f"notify failed: {e}")


# ---------------------------------------------------------------- UDM API

class UniFiError(RuntimeError):
    pass


def _login():
    if not u._unifi_login():
        raise UniFiError("UniFi login failed (API key missing or UDM unreachable)")


def _call(method, path, body=None, timeout=20, retries=None):
    """(status, parsed JSON). GETs retry transport errors 3x with backoff; writes never auto-retry
    (a create that timed out may have landed — the caller re-reads instead)."""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if getattr(u, "_api_key", None):
        headers["X-API-Key"] = u._api_key
    data = json.dumps(body).encode() if body is not None else None
    tries = retries if retries is not None else (3 if method == "GET" else 1)
    for i in range(tries):
        req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
        try:
            with u._opener.open(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                detail = e.read()[:300].decode(errors="replace")
            except Exception:  # noqa: BLE001
                detail = ""
            return e.code, {"error": detail}
        except (OSError, ValueError) as e:
            if isinstance(getattr(e, "reason", None), u.CertPinError):
                alert("UniFi: UDM certificate pin FAILED — Nova stopped talking to the UDM", safe(e.reason, 300),
                      level="critical", dedup_key="unifi-cert-pin")
                raise UniFiError(f"{method} {path}: {e.reason}") from e
            if i == tries - 1:
                raise UniFiError(f"{method} {path}: {e}") from e
            time.sleep(1.0 * (2 ** i))
    raise UniFiError(f"{method} {path}: no attempts")


def _data(resp):
    status, body = resp
    if status != 200:
        raise UniFiError(f"HTTP {status}: {str(body)[:200]}")
    d = body.get("data", body) if isinstance(body, dict) else body
    return d


def get_list(path):
    d = _data(_call("GET", path))
    return d if isinstance(d, list) else []


def zones():           return get_list(f"/v2/api/site/{SITE}/firewall/zone")
def policies():        return get_list(f"/v2/api/site/{SITE}/firewall-policies")
def networks():
    """vlan normalised to int: this UDM stores some as strings ('2' next to 3), which crashes the upstream
    matrix sort (TypeError int < str) — fixed here so the vendored file stays verbatim."""
    out = get_list(f"/api/s/{SITE}/rest/networkconf")
    for n in out:
        v = n.get("vlan")
        if isinstance(v, str):
            n["vlan"] = int(v) if v.strip().isdigit() else None
    return out
def wlans():           return get_list(f"/api/s/{SITE}/rest/wlanconf")
def known_clients():   return get_list(f"/api/s/{SITE}/rest/user")
def devices():         return get_list(f"/api/s/{SITE}/stat/device")
def content_filters(): return get_list(f"/v2/api/site/{SITE}/content-filtering")


def acl_rules():
    try:
        return get_list(f"/v2/api/site/{SITE}/acl-rules")
    except UniFiError:
        return None


def live_clients() -> dict:
    return {(c.get("mac") or "").lower(): c for c in get_list(f"/api/s/{SITE}/stat/sta") if c.get("mac")}


def global_net_config():
    d = _data(_call("GET", f"/v2/api/site/{SITE}/global/config/network"))
    return d if isinstance(d, dict) else None


def site_setting(key):
    try:
        d = get_list(f"/api/s/{SITE}/rest/setting/{key}")
    except UniFiError:
        return None
    return d[0] if d else None


def _created(resp):
    status, body = resp
    if status not in (200, 201):
        log(f"create refused: HTTP {status} {str(body)[:200]}")
        return None
    if isinstance(body, dict) and "data" in body:
        body = body["data"]
        body = body[0] if isinstance(body, list) and body else body
    return body if isinstance(body, dict) else {}


ID_RX = re.compile(r"^[0-9a-f]{24}$")      # ids of objects WE create; predefined policies have longer ids


def ledger(obj_id, kind, label, payload=None, before=None):
    from psycopg2.extras import Json
    cur = _ops().cursor(); ensure_schema(cur)
    cur.execute("""INSERT INTO unifi_toolkit_ledger (obj_id, kind, label, payload, before) VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (obj_id) DO NOTHING""", (obj_id, kind, label, Json(payload), Json(before)))


def in_ledger(obj_id) -> bool:
    cur = _ops().cursor(); ensure_schema(cur)
    cur.execute("SELECT 1 FROM unifi_toolkit_ledger WHERE obj_id=%s AND released_at IS NULL", (obj_id,))
    return cur.fetchone() is not None


def _release_ledger(obj_id):
    _ops().cursor().execute("UPDATE unifi_toolkit_ledger SET released_at=now() WHERE obj_id=%s", (obj_id,))


def create_policy(p, label=None):
    r = _created(_call("POST", f"/v2/api/site/{SITE}/firewall-policies", p))
    if r and r.get("_id"):
        ledger(r["_id"], "policy", label, p)
    return r


def create_acl(rule, label=None):
    r = _created(_call("POST", f"/v2/api/site/{SITE}/acl-rules", rule))
    if r and r.get("_id"):
        ledger(r["_id"], "acl", label, rule)
    return r


def delete_policy(pid, all_policies=None) -> bool:
    """Deletes ONE policy: only an id Nova's ledger says it created (audit UT-12/'marker-only ownership'),
    that still carries the House Arrest marker and is not predefined — re-checked on a fresh read."""
    pol = next((p for p in (all_policies if all_policies is not None else policies()) if p.get("_id") == pid), None)
    if (pol is None or pol.get("predefined") or not P.is_house_arrest(pol) or not ID_RX.match(str(pid))
            or not in_ledger(pid)):
        log(f"refusing to delete policy {pid}: not a House Arrest policy Nova created")
        return False
    ok = _call("POST", f"/v2/api/site/{SITE}/firewall-policies/batch-delete", [pid], retries=2)[0] in (200, 204)
    if ok:
        _release_ledger(pid)
    return ok


def delete_acl(rule) -> bool:
    rid = str(rule.get("_id"))
    if not P.is_house_arrest_acl(rule) or not ID_RX.match(rid) or not in_ledger(rid):
        log(f"refusing to delete ACL {rid}: not a House Arrest rule Nova created")
        return False
    ok = _call("DELETE", f"/v2/api/site/{SITE}/acl-rules/{rid}", retries=2)[0] in (200, 204)
    if ok:
        _release_ledger(rid)
    return ok


def _put_verified(get_path, put_path, change: dict, strict: bool, put_body=None) -> bool:
    """Read-modify-PUT a whole document, then poll until every changed field reads back (provisioning
    is asynchronous). strict compares values; otherwise truthiness (unset == False for flags)."""
    if put_body is None:
        docs = get_list(get_path)
        if not docs:
            return False
        put_body = dict(docs[0])
        # before-snapshot of ONLY the fields we change (whole WLAN docs carry x_passphrase)
        ledger(f"{put_path}@{time.time():.0f}", "setting", None, change, {k: docs[0].get(k) for k in change})
        put_body.update(change)
    if _call("PUT", put_path, put_body)[0] != 200:
        return False
    waited, delay = 0.0, 1.0
    while waited <= 20.0:
        d = _data(_call("GET", get_path))
        fresh = d[0] if isinstance(d, list) and d else d if isinstance(d, dict) else {}
        if all(((fresh.get(k) if fresh.get(k) is not None else "") == v) if strict else (bool(fresh.get(k)) == bool(v))
               for k, v in change.items()):
            return True
        time.sleep(delay)
        waited += delay
        delay = min(delay * 1.6, 5.0)
    return False


def set_network(nid, strict=False, **fields) -> bool:
    p = f"/api/s/{SITE}/rest/networkconf/{nid}"
    return _put_verified(p, p, fields, strict)


# ---------------------------------------------------------------- Threat Watch

def normalize_flow(e: dict) -> dict:
    src, dst, ips = e.get("source") or {}, e.get("destination") or {}, e.get("ips") or {}
    action = e.get("action") or "alert"
    action = "alert" if action == "allowed" else "block" if action in ("blocked", "dropped", "rejected") else action
    ts = e.get("time") or e.get("flow_start_time")

    def ip(v):
        try:
            return str(ipaddress.ip_address(v)) if v else None
        except ValueError:
            return None

    def port(v):
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    return {
        "flow_id": str(e.get("id") or f"{ts}-{src.get('ip')}-{dst.get('ip')}"),
        "ts": datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if isinstance(ts, (int, float)) else datetime.now(timezone.utc),
        "risk": (e.get("risk") or "").lower() or None,
        "action": action,
        "signature": (ips.get("advanced_information") or ips.get("signature") or "")[:500] or None,
        "signature_id": str(ips.get("signature_id")) if ips.get("signature_id") is not None else None,
        "category": ips.get("category_name") or ips.get("ips_category") or e.get("service"),
        "src_ip": ip(src.get("ip")), "src_port": port(src.get("port")), "src_mac": src.get("mac"),
        "src_country": src.get("region") or src.get("country"),
        "dst_ip": ip(dst.get("ip")), "dst_port": port(dst.get("port")), "dst_mac": dst.get("mac"),
        "dst_country": dst.get("region") or dst.get("country"),
        "proto": e.get("protocol"), "service": e.get("service"), "direction": e.get("direction"),
        "network": (e.get("in") or {}).get("network_name") if isinstance(e.get("in"), dict) else e.get("in"),
    }


def fetch_ips_flows(start_ms: int, end_ms: int, max_pages: int = 50) -> list:
    out = []
    for page in range(max_pages):
        body = {"timestampFrom": start_ms, "timestampTo": end_ms, "pageNumber": page, "pageSize": 100,
                "skip_count": False, "policy_type": ["INTRUSION_PREVENTION"], "search_text": ""}
        status, resp = _call("POST", f"/v2/api/site/{SITE}/traffic-flows", body, timeout=30, retries=3)
        if status != 200:
            raise UniFiError(f"traffic-flows HTTP {status}: {str(resp)[:200]}")
        flows = (resp.get("data") or []) if isinstance(resp, dict) else []
        out += flows
        if len(flows) < 100:
            break
    return out


def cmd_threats(a):
    conn = _ops(); cur = conn.cursor(); ensure_schema(cur)
    cur.execute("SELECT max(ts) FROM unifi_threat_events")
    last = cur.fetchone()[0]
    now_ms = int(time.time() * 1000)
    start_ms = int(last.timestamp() * 1000) - 15 * 60_000 if last else now_ms - a.hours * 3_600_000
    _login()
    rows = [normalize_flow(e) for e in fetch_ips_flows(start_ms, now_ms)]
    new = []
    for r in rows:
        cur.execute("""INSERT INTO unifi_threat_events (flow_id, ts, risk, action, signature, signature_id, category,
                         src_ip, src_port, src_mac, src_country, dst_ip, dst_port, dst_mac, dst_country, proto, service,
                         direction, network)
                       VALUES (%(flow_id)s,%(ts)s,%(risk)s,%(action)s,%(signature)s,%(signature_id)s,%(category)s,
                         %(src_ip)s,%(src_port)s,%(src_mac)s,%(src_country)s,%(dst_ip)s,%(dst_port)s,%(dst_mac)s,
                         %(dst_country)s,%(proto)s,%(service)s,%(direction)s,%(network)s)
                       ON CONFLICT (flow_id) DO NOTHING RETURNING flow_id""", r)
        if cur.fetchone():
            new.append(r["flow_id"])
    if new:   # ignore rules: either end inside the cidr AND risk at or below the rule's max_risk
        cur.execute("""UPDATE unifi_threat_events e SET ignored = true WHERE flow_id = ANY(%s) AND EXISTS (
                         SELECT 1 FROM unifi_threat_ignore i
                         WHERE (e.src_ip <<= i.cidr OR e.dst_ip <<= i.cidr)
                           AND array_position(ARRAY['low','medium','high'], coalesce(e.risk,'low'))
                               <= array_position(ARRAY['low','medium','high'], i.max_risk))""", (new,))
        cur.execute("""SELECT flow_id, ts, risk, action, signature, src_ip::text, src_country, dst_ip::text, dst_port
                       FROM unifi_threat_events WHERE flow_id = ANY(%s) AND NOT ignored AND risk = 'high'
                       ORDER BY ts""", (new,))
        hot = cur.fetchall()
        if hot:
            lines = [f"{t:%m-%d %H:%M} {safe(s, 15)}{f' ({safe(c, 3)})' if c else ''} -> {safe(d, 15)}:{p or '-'} "
                     f"{'BLOCKED' if act == 'block' else 'NOT blocked'} — {safe(sig, 90)}"
                     for _, t, _, act, sig, s, c, d, p in hot[:8]]
            if len(hot) > 8:
                lines.append(f"…and {len(hot) - 8} more (unifi_threat_events)")
            unblocked = sum(1 for h in hot if h[3] != "block")
            key = hashlib.sha1(",".join(sorted({f'{h[5]}|{h[4]}' for h in hot})).encode()).hexdigest()[:16]
            alert(f"UniFi IPS: {len(hot)} high-risk event(s)" + (f", {unblocked} not blocked" if unblocked else ""),
                  "\n".join(lines), level="critical" if unblocked else "warning", dedup_key=f"unifi-ips-{key}")
            cur.execute("UPDATE unifi_threat_events SET alerted = true WHERE flow_id = ANY(%s)", ([h[0] for h in hot],))
    cur.execute("DELETE FROM unifi_threat_events WHERE ts < now() - make_interval(days => %s)", (RETAIN_THREATS_DAYS,))
    log(f"threats: {len(rows)} IPS flows in window, {len(new)} new")
    return 0


# ---------------------------------------------------------------- Wi-Fi Stalker

def where(c: dict, dev_names: dict) -> str:
    """Human location of a client: the AP for Wi-Fi, switch + port for wired."""
    if c.get("is_wired"):
        sw = c.get("sw_mac") or ""
        return f"{dev_names.get(sw.lower(), sw or 'switch')} port {c.get('sw_port') or '?'}"
    ap = (c.get("ap_mac") or "").lower()
    return dev_names.get(ap, ap or "unknown AP")


def diff_clients(prev: dict, live: dict, dev_names: dict) -> list:
    """prev: {mac: (online, ap)}; live: {mac: client}. -> [(mac, kind, from_ap, to_ap)]."""
    ev = []
    for mac, c in live.items():
        here = where(c, dev_names)
        was = prev.get(mac)
        if not was or not was[0]:
            ev.append((mac, "connect", None, here))
        elif was[1] != here:
            ev.append((mac, "roam", was[1], here))
    for mac, (online, ap) in prev.items():
        if online and mac not in live:
            ev.append((mac, "disconnect", ap, None))
    return ev


def cmd_stalk(a):
    from psycopg2.extras import execute_values, Json
    conn = _ops(); cur = conn.cursor(); ensure_schema(cur)
    conn.autocommit = False              # events + state + presence land together or not at all
    _login()
    live = live_clients()
    dev_names = {(d.get("mac") or "").lower(): d.get("name") or d.get("model") or d.get("mac") for d in devices()}
    cur.execute("SELECT mac, online, ap FROM unifi_client_state")
    prev = {m: (o, ap) for m, o, ap in cur.fetchall()}
    events = diff_clients(prev, live, dev_names) if prev else []   # first run = silent baseline
    now = datetime.now(timezone.utc)
    if events:
        execute_values(cur, "INSERT INTO unifi_client_events (mac, kind, from_ap, to_ap, detail) VALUES %s",
                       [(m, k, f, t, Json({"name": (live.get(m) or {}).get("name") or (live.get(m) or {}).get("hostname"),
                                           "ip": (live.get(m) or {}).get("ip"),
                                           "essid": (live.get(m) or {}).get("essid")})) for m, k, f, t in events])
    if live:
        execute_values(cur, """INSERT INTO unifi_client_state (mac, name, hostname, ip, oui, is_wired, ap, essid, radio,
                                 channel, signal, network, network_id, online, connected_since, last_seen)
                               VALUES %s ON CONFLICT (mac) DO UPDATE SET name=EXCLUDED.name, hostname=EXCLUDED.hostname,
                                 ip=EXCLUDED.ip, oui=EXCLUDED.oui, is_wired=EXCLUDED.is_wired, ap=EXCLUDED.ap,
                                 essid=EXCLUDED.essid, radio=EXCLUDED.radio, channel=EXCLUDED.channel, signal=EXCLUDED.signal,
                                 network=EXCLUDED.network, network_id=EXCLUDED.network_id, online=true,
                                 connected_since=CASE WHEN unifi_client_state.online THEN unifi_client_state.connected_since
                                                      ELSE EXCLUDED.connected_since END,
                                 last_seen=EXCLUDED.last_seen, updated_at=now()""",
                       [(m, c.get("name"), c.get("hostname"), c.get("ip"), c.get("oui"), bool(c.get("is_wired")),
                         where(c, dev_names), c.get("essid"), c.get("radio"), c.get("channel"), c.get("signal"),
                         c.get("network"), c.get("network_id"), True, now, now) for m, c in live.items()])
        execute_values(cur, """INSERT INTO unifi_client_presence (mac, hour, ap, samples) VALUES %s
                               ON CONFLICT (mac, hour, ap) DO UPDATE SET samples = unifi_client_presence.samples + 1""",
                       [(m, now.replace(minute=0, second=0, microsecond=0), where(c, dev_names), 1) for m, c in live.items()])
    gone = [m for m, (o, _) in prev.items() if o and m not in live]
    if gone:
        cur.execute("UPDATE unifi_client_state SET online = false, updated_at = now() WHERE mac = ANY(%s)", (gone,))
    cur.execute("SELECT mac, coalesce(label, '') FROM unifi_tracked_devices WHERE alert")
    tracked = dict(cur.fetchall())
    for m, k, f, t in events:
        if m in tracked:
            c = live.get(m) or {}
            who = safe(tracked[m] or c.get("name") or c.get("hostname") or m, 60)
            what = {"connect": f"connected at {safe(t)}", "disconnect": f"left (last at {safe(f)})",
                    "roam": f"roamed {safe(f)} -> {safe(t)}"}[k]
            alert(f"Stalker: {who} {what}", f"{m} ip={safe(c.get('ip'), 40)} ssid={safe(c.get('essid'), 40)}",
                  level="info", dedup_key=f"stalk-{m}-{k}-{t or f}")
    if now.minute < STALK_INTERVAL_MIN:   # once an hour: retention
        cur.execute("DELETE FROM unifi_client_events WHERE ts < now() - make_interval(days => %s)", (RETAIN_EVENTS_DAYS,))
        cur.execute("DELETE FROM unifi_client_presence WHERE hour < now() - make_interval(days => %s)", (RETAIN_PRESENCE_DAYS,))
    conn.commit()
    log(f"stalk: {len(live)} online, {len(events)} events")
    return 0


MAC_RX = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def norm_mac(mac: str) -> str:
    m = (mac or "").strip().lower().replace("-", ":")
    if not MAC_RX.match(m):
        raise SystemExit(f"not a MAC address: {mac!r}")
    return m


def cmd_track(a):
    conn = _ops(); cur = conn.cursor(); ensure_schema(cur)
    if a.op == "list":
        cur.execute("""SELECT t.mac, t.label, t.alert, s.online, s.ap, s.last_seen FROM unifi_tracked_devices t
                       LEFT JOIN unifi_client_state s USING (mac) ORDER BY t.label NULLS LAST, t.mac""")
        for r in cur.fetchall():
            print(" | ".join("" if v is None else str(v) for v in r))
        return 0
    mac = norm_mac(a.mac)
    if a.op == "add":
        cur.execute("""INSERT INTO unifi_tracked_devices (mac, label, alert) VALUES (%s, %s, %s)
                       ON CONFLICT (mac) DO UPDATE SET label = EXCLUDED.label, alert = EXCLUDED.alert""",
                    (mac, a.label, not a.quiet))
    else:
        cur.execute("DELETE FROM unifi_tracked_devices WHERE mac = %s", (mac,))
    print(f"{a.op} {mac}")
    return 0


def cmd_report(a):
    mac = norm_mac(a.mac)
    conn = _ops(); cur = conn.cursor(); ensure_schema(cur)
    cur.execute("SELECT name, hostname, ip, online, ap, essid, signal, last_seen FROM unifi_client_state WHERE mac=%s", (mac,))
    print("now:", cur.fetchone())
    cur.execute(f"""SELECT ap, sum(samples) * {STALK_INTERVAL_MIN} AS minutes FROM unifi_client_presence
                    WHERE mac=%s AND hour > now() - make_interval(days => %s) GROUP BY ap ORDER BY 2 DESC""", (mac, a.days))
    dwell = cur.fetchall()
    print(f"dwell by AP, last {a.days} d (favourite first):")
    for ap, mins in dwell:
        print(f"  {ap}: {mins // 60} h {mins % 60} m")
    cur.execute("""SELECT extract(isodow FROM hour AT TIME ZONE 'America/Los_Angeles')::int,
                          extract(hour FROM hour AT TIME ZONE 'America/Los_Angeles')::int, sum(samples)
                   FROM unifi_client_presence WHERE mac=%s AND hour > now() - make_interval(days => %s) GROUP BY 1, 2""",
                (mac, a.days))
    grid = {(d, h): n for d, h, n in cur.fetchall()}
    top = max(grid.values(), default=0) or 1
    print("presence heatmap (Mon..Sun x 00..23, local):")
    for d, name in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], 1):
        print(f"  {name} " + "".join(" .:-=+*#%@"[min(9, round(9 * grid.get((d, h), 0) / top))] for h in range(24)))
    cur.execute("SELECT ts, kind, from_ap, to_ap FROM unifi_client_events WHERE mac=%s ORDER BY ts DESC LIMIT %s", (mac, a.limit))
    print("recent events:")
    for r in cur.fetchall():
        print("  ", " | ".join("" if v is None else str(v) for v in r))
    return 0


# ---------------------------------------------------------------- Network audit (House Arrest read side)

def posture_findings(nets, zns, all_pol) -> list:
    """The upstream /inspect findings: VLANs sharing a zone, isolation off, mDNS on."""
    names = {n.get("_id"): n.get("name") or "(unnamed)" for n in nets}
    lan = {n.get("_id") for n in nets if n.get("purpose") != "wan" and n.get("vlan") is not None}
    out = []
    for z in zns:
        members = [names[i] for i in z.get("network_ids") or [] if i in lan]
        if len(members) > 1:
            out.append({"severity": "warn", "where": z.get("name"),
                        "finding": f"{len(members)} VLANs share the {z.get('name')} zone",
                        "detail": f"{', '.join(members)} can reach each other unless a policy blocks it"})
    for n in nets:
        if n.get("purpose") == "wan" or n.get("vlan") is None:
            continue
        if not n.get("network_isolation_enabled"):
            out.append({"severity": "warn", "where": n.get("name"), "finding": "Network isolation is off",
                        "detail": f"VLAN {n.get('vlan')} can reach other networks unless a firewall policy stops it"})
        if n.get("mdns_enabled"):
            out.append({"severity": "info", "where": n.get("name"), "finding": "mDNS forwarding is on",
                        "detail": "service discovery crosses this boundary"})
    return out


def matrix_digest(matrix: dict, wl: list) -> dict:
    """{network: {column: state}} + {'wlan:<ssid>': {'client_isolation': bool}} — what drift is measured on."""
    d = {}
    for row in matrix.get("rows") or []:
        cells = row.get("cells") or {}
        d[str(row.get("name") or row.get("id"))] = {k: (v or {}).get("state") for k, v in sorted(cells.items())}
    for w in wl:
        d[f"wlan:{w.get('name')}"] = {"client_isolation": bool(w.get("l2_isolation")), "enabled": bool(w.get("enabled"))}
    return d


def digest_changes(old: dict, new: dict) -> list:
    out = []
    for k in sorted(set(old) | set(new)):
        o, n = old.get(k) or {}, new.get(k) or {}
        for col in sorted(set(o) | set(n)):
            if o.get(col) != n.get(col):
                out.append(f"{k} {col}: {o.get(col)} -> {n.get(col)}")
    return out


def cmd_audit(a):
    from psycopg2.extras import Json
    _login()
    nets, zns, all_pol, wl = networks(), zones(), policies(), wlans()
    try:
        devs, live = devices(), live_clients()
    except UniFiError:
        devs, live = [], {}
    gs = site_setting("global_switch")
    di_ids = [str(i) for i in (gs or {}).get("acl_device_isolation") or []] if gs is not None else None
    coverage = {n["_id"]: P.network_isolation_coverage(n["_id"], live, devs) for n in nets if n.get("_id")} if devs else None
    holes = {n["_id"]: P.isolation_exceptions(n, zns, all_pol, live) for n in nets
             if n.get("_id") and n.get("network_isolation_enabled")}
    matrix = P.build_isolation_matrix(nets, zns, device_isolation_ids=di_ids, coverage=coverage,
                                      switch_acl_supported=P.site_acl_capable(devs) if devs else None,
                                      isolation_exceptions=holes)
    ours = P.find_ours(all_pol)
    dev_pol = [p for p in ours if not P.is_network_policy(p) and not P.is_dns_policy(p)]
    known = {(c.get("mac") or "").lower(): c.get("name") or c.get("hostname") or "" for c in known_clients()}
    health = P.check_breakage(dev_pol, known)
    shadow = P.check_precedence(ours, all_pol)
    findings = posture_findings(nets, zns, all_pol)
    digest = matrix_digest(matrix, wl)
    lockdowns = {"health": health, "precedence": shadow, "count": len(ours)}

    conn = _ops(); cur = conn.cursor(); ensure_schema(cur)
    cur.execute("SELECT digest FROM unifi_network_audit ORDER BY ts DESC LIMIT 1")
    prev = cur.fetchone()
    cur.execute("INSERT INTO unifi_network_audit (digest, findings, lockdowns, matrix) VALUES (%s,%s,%s,%s)",
                (Json(digest), Json(findings), Json(lockdowns, dumps=lambda o: json.dumps(o, default=str)),
                 Json(matrix, dumps=lambda o: json.dumps(o, default=str))))
    cur.execute("DELETE FROM unifi_network_audit WHERE ts < now() - interval '180 days'")
    changes = digest_changes(prev[0], digest) if prev else []
    if changes:
        alert(f"UniFi network posture changed ({len(changes)})", "\n".join(safe(c, 160) for c in changes[:15]),
              level="warning", dedup_key="unifi-posture-" + hashlib.sha1("\n".join(changes).encode()).hexdigest()[:16])
    bad = [h for h in health if h.get("status") != "OK"]
    if bad:
        alert(f"House Arrest: {len(bad)} lockdown(s) no longer enforcing",
              "\n".join(f"{safe(h.get('name'), 80)}: {h.get('status')} — {safe(h.get('suggestion'), 120)}" for h in bad[:10]),
              level="warning", dedup_key="ha-broken-" + ",".join(sorted(str(h.get("policy_id")) for h in bad))[:80])
    log(f"audit: {len(digest)} rows, {len(findings)} findings, {len(ours)} House Arrest rules, "
        f"{len(bad)} broken, {len(changes)} posture changes")
    if a.verbose:
        print(json.dumps({"findings": findings, "lockdowns": lockdowns, "digest": digest}, indent=1, default=str))
    return 0


# ---------------------------------------------------------------- House Arrest writes

def record(action, args: dict, dry_run, ok, result):
    try:
        from psycopg2.extras import Json
        conn = _ops(); cur = conn.cursor(); ensure_schema(cur)
        cur.execute("INSERT INTO unifi_toolkit_actions (action, args, dry_run, ok, result, host, actor) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (action, Json(args), dry_run, ok, Json(result, dumps=lambda o: json.dumps(o, default=str)),
                     socket.gethostname(), os.environ.get("NOVA_ACTOR") or os.environ.get("USER")))
    except Exception as e:  # noqa: BLE001
        log(f"WARN: action not recorded: {e}")


def guard_devices(macs, preset, override_comms: bool):
    """Refuse fleet/network gear always; refuse internet-cutting presets on household devices unless
    overridden (nova_safety_guards P2: never silently cut a person's line to the outside)."""
    import nova_unifi_ctl as ctl
    live = live_clients()
    never = set()
    try:
        cur = _ops().cursor()
        cur.execute("SELECT value FROM service_config WHERE service='unifi' AND key='never_lock_macs'")
        row = cur.fetchone()
        never = {str(x).lower() for x in (row[0] if row else [])}
    except Exception as e:  # noqa: BLE001 — can't read the list = can't prove the device is lockable
        return f"REFUSED: never-lock list unreadable ({e})"
    for m in macs:
        if m in never:
            return f"REFUSED: {m} is on the never-lock list (service_config unifi/never_lock_macs)"
        why = ctl.is_infrastructure(m, live.get(m))
        if why:
            return f"REFUSED: {m} is {why}. House Arrest never touches the fleet or the network gear."
    if preset in (P.CUT_OFF, P.FULL_LOCKDOWN, P.LAN_ONLY) and not override_comms:
        try:
            import nova_safety_guards as g
            names = [n for m in macs for n in ((live.get(m) or {}).get("name"), (live.get(m) or {}).get("hostname")) if n]
            ok, why = g.comms_guard("", macs=list(macs), names=names)
        except Exception as e:  # noqa: BLE001 — unreadable ownership fails closed
            ok, why = False, f"comms guard unavailable ({e})"
        if not ok:
            return f"REFUSED: {why} (re-run with --override-comms-guard if Little Mister means it)"
    return None


def _zone_ids(zns):
    return P.find_zone_id(zns, "internal"), P.find_zone_id(zns, "external")


def _device_zone(macs, known, nets, zns, internal_id, caveats):
    """Upstream attribution: known-client -> last network -> that network's zone; mixed zones refused."""
    nets_by_id = {n.get("_id"): n for n in nets if n.get("_id")}
    by_mac = {(c.get("mac") or "").lower(): c for c in known if c.get("mac")}
    found, unknown = {}, []
    for m in macs:
        net = nets_by_id.get((by_mac.get(m) or {}).get("last_connection_network_id"))
        zid = P.zone_of_network(net, zns) if net else None
        (found.setdefault(zid, []) if zid else unknown).append(m)
    if len(found) > 1:
        return None, "devices sit in different firewall zones; lock them down per zone"
    zid = next(iter(found)) if found else internal_id
    if zid != internal_id:
        caveats.append(f"rules scoped to the \"{P.zone_name(zns, zid)}\" zone (where the controller last saw the device)")
        if unknown:
            caveats.append(f"no network attributable to {', '.join(unknown)}; included in that zone's scope")
    return zid, None


def _device_net_ids(macs, known_by_mac, live):
    out = []
    for m in macs:
        nid = (live.get(m) or {}).get("network_id") or (known_by_mac.get(m) or {}).get("last_connection_network_id")
        if nid and nid not in out:
            out.append(nid)
    return out


def _rollback(created_pols, created_acls):
    for pid in [c.get("_id") for c in created_pols if c.get("_id")]:
        delete_policy(pid)
    for r in created_acls:
        if r.get("_id"):
            delete_acl(r)


def cmd_lockdown(a):
    macs = [norm_mac(m) for m in a.mac]
    if a.preset not in P.PRESETS:
        raise SystemExit(f"preset must be one of {P.PRESETS}")
    label = (a.label or "").strip()
    if not label:
        raise SystemExit("--label is required (release is by label)")
    _login()
    refused = guard_devices(macs, a.preset, a.override_comms_guard)
    if refused:
        record("lockdown", vars_of(a), not a.apply, False, {"error": refused})
        print(refused); return 2
    zns, existing, nets, known = zones(), policies(), networks(), known_clients()
    internal_id, external_id = _zone_ids(zns)
    if not internal_id or not external_id:
        print("no Internal/External zone on this console"); return 1
    caveats = []
    zid, err = _device_zone(macs, known, nets, zns, internal_id, caveats)
    if err:
        print(err); return 1
    taken = {P._label_from_policy(p).strip().lower() for p in P.find_ours(existing)}
    if label.lower() in taken:
        print(f"label {label!r} is already in use — pick a unique one (release is by label)"); return 1
    arrested = P.arrested_macs(P.find_ours(existing))
    dupes = [m for m in macs if m in arrested]
    if dupes:
        print(f"already locked down: {', '.join(f'{m} ({arrested[m]})' for m in dupes)} — release first"); return 1
    live = live_clients()
    known_by_mac = {(c.get("mac") or "").lower(): c for c in known}
    nets_by_id = {n.get("_id"): n for n in nets if n.get("_id")}
    dev_nets = _device_net_ids(macs, known_by_mac, live)
    dns_ips = []
    if a.preset == P.INTERNET_ONLY:
        dns_ips, skipped = P.device_dns_resolvers([nets_by_id[n] for n in dev_nets if n in nets_by_id], nets, zns, zid)
        locked = P.dns_locked_network_ids(existing)
        for nid in skipped:
            if nid not in locked:
                caveats.append(f"{(nets_by_id.get(nid) or {}).get('name')} is isolated and its DNS is on another network: "
                               "this device has no DNS unless that network gets a DNS lockdown")
        if dns_ips:
            caveats.append(f"DNS keeps working to {', '.join(dns_ips)} on port 53")
    idx = P.next_free_index(existing, P.policy_count(a.preset) + (1 if dns_ips else 0))
    payloads = []
    if dns_ips:
        payloads.append(P.build_device_dns_allow(macs, label, zid, dns_ips, idx[0]))
        idx = idx[1:]
    payloads += P.build_lockdown(preset=a.preset, macs=macs, device_label=label, client_zone_id=zid,
                                 external_zone_id=external_id, indexes=idx, allow_inbound=P.inbound_for(a.preset))
    others = P.other_lan_zones(zns, zid)
    if others and a.preset in (P.CUT_OFF, P.FULL_LOCKDOWN, P.INTERNET_ONLY):
        caveats.append("other LAN zones (" + ", ".join(z.get("name") or "?" for z in others) + ") stay reachable")

    acl_payloads = []
    always = a.preset in P.NEIGHBOUR_ALWAYS_PRESETS
    if a.block_neighbours and a.preset not in P.NEIGHBOUR_BLOCK_PRESETS:
        raise SystemExit("--block-neighbours only works with cut_off and internet_only")
    if always or a.block_neighbours:
        rules, devs = acl_rules(), devices()
        by_net, unplaced, problem = {}, [], None
        for m in macs:
            nid = (live.get(m) or {}).get("network_id") or (known_by_mac.get(m) or {}).get("last_connection_network_id")
            (by_net.setdefault(nid, []) if nid else unplaced).append(m)
        verdicts = {}
        if rules is None or not devs:
            problem = "couldn't read the switches"
        elif P.site_acl_capable(devs) is False:
            problem = "no switch here supports the neighbour block"
        else:
            verdicts = {m: P.client_coverage(live.get(m), devs, live) for m in macs}
            if all(v["status"] == P.NOT_COVERED for v in verdicts.values()):
                problem = "no selected device sits behind an ACL-capable switch"
            elif any(r.get("name") == P.acl_rule_name(label) for r in rules):
                print(f"switch-rule name {P.acl_rule_name(label)!r} collides with an existing rule (ACL names are cut "
                      "to 32 chars) — use a shorter or more distinct label"); return 1
            elif P.neighbour_acls_for(rules, macs):
                print("a neighbour block already exists for this device — release first"); return 1
            elif unplaced:
                problem = f"network unknown for {', '.join(unplaced)} (bring it online)"
        resolver_macs = {}
        if not problem and a.preset == P.INTERNET_ONLY:
            for nid in by_net:
                found, missing = P.same_network_resolvers(nets_by_id.get(nid), live)
                if missing:
                    print(f"same-network DNS {', '.join(missing)} is offline/unknown: the neighbour block would cut DNS"); return 1
                resolver_macs[nid] = [r["mac"] for r in found]
                note = P.resolver_note(found)
                if note:
                    caveats.append(note)
        if problem and not always:
            print(problem + " — drop --block-neighbours"); return 1
        if problem:
            caveats.append("neighbour block NOT included: " + problem)
        else:
            gw = P.gateway_lan_macs(devs)
            aidx = P.next_acl_indexes(rules, 2 * len(by_net))
            for i, (nid, ms) in enumerate(sorted(by_net.items())):
                acl_payloads += P.build_neighbour_acls(ms, nid, gw, label, aidx[2 * i:2 * i + 2],
                                                       resolver_macs=resolver_macs.get(nid))
            caveats += list(P.NEIGHBOUR_CAVEATS)
            caveats += [f"{m}: {P.coverage_sentence(verdicts[m])}" for m in macs]
    caveats += P.caveats_for(a.preset)
    plan = {"policies": [p.get("name") for p in payloads], "acl_rules": [r.get("name") for r in acl_payloads],
            "caveats": caveats}
    if not a.apply:
        record("lockdown", vars_of(a), True, True, plan)
        print(json.dumps(plan, indent=1)); print("DRY RUN — add --apply to write these rules"); return 0

    created, acl_created = [], []
    for p in payloads:
        r = create_policy(p, label)
        if r is None:
            _rollback(created, []); record("lockdown", vars_of(a), False, False, {"error": f"create {p.get('name')} failed; rolled back"})
            print("create failed; rolled back"); return 1
        created.append(r)
    if dns_ips:
        stored = {p.get("_id"): p for p in policies()}
        mine = [stored.get(c.get("_id"), c) for c in created]
        blocks = [p for p in mine if P._blocks_lan_for(p)]
        allows = [p for p in mine if P.is_device_dns_allow(p)]
        if not allows or any(P.shadowed_dns_allows(b, allows, dev_nets) for b in blocks):
            _rollback(created, []); record("lockdown", vars_of(a), False, False, {"error": "DNS allow landed behind the block; rolled back"})
            print("gateway ordered the DNS allow after the block; rolled back — try again"); return 1
    for r in acl_payloads:
        res = create_acl(r, label)
        if res is None:
            _rollback(created, acl_created); record("lockdown", vars_of(a), False, False, {"error": "neighbour ACL failed; rolled back"})
            print("neighbour block failed; whole lockdown rolled back"); return 1
        acl_created.append(res)
    res = {**plan, "created": [c.get("_id") for c in created + acl_created]}
    record("lockdown", vars_of(a), False, True, res)
    alert(f"House Arrest: {safe(label, 60)} locked down ({a.preset})", "\n".join(safe(c, 200) for c in caveats[:6]), level="info")
    print(json.dumps(res, indent=1))
    return 0


def cmd_release(a):
    label = (a.label or "").strip()
    err = P.release_scope_error(a.kind, label, a.policy_id or None)
    if err:
        print(err); return 1
    _login()
    allp = policies()
    by_id = {p.get("_id"): p for p in allp}
    targets, refused = [], []
    if a.policy_id:
        for pid in a.policy_id:
            if not ID_RX.match(pid):
                refused.append({"policy_id": pid, "reason": "not a 24-hex id"}); continue
            pol = by_id.get(pid)
            if pol is None or not P.is_house_arrest(pol):
                refused.append({"policy_id": pid, "reason": "not found" if pol is None else "not a House Arrest policy"})
            else:
                targets.append(pol)
    else:
        targets = P.release_targets(allp, a.kind, label)
    acl_targets = []
    if a.kind != "network":
        dmacs = {m for p in targets if not P.is_network_policy(p) and not P.is_dns_policy(p) for m in P.policy_macs(p)}
        rules = acl_rules()
        if rules is None and dmacs:
            refused.append({"reason": "could not read switch ACLs; any neighbour block left in place"})
        for r in rules or []:
            if P.is_house_arrest_acl(r) and (set(dmacs) & set(P.acl_source_macs(r)) or (label and r.get("name") == P.acl_rule_name(label))):
                acl_targets.append(r)
        acl_targets.sort(key=lambda r: 0 if r.get("action") == "BLOCK" else 1)   # drop the block before its allows
    plan = {"would_delete": [p.get("name") for p in targets] + [f"{r.get('name')} (switch rule)" for r in acl_targets],
            "refused": refused}
    if not a.apply:
        record("release", vars_of(a), True, True, plan)
        print(json.dumps(plan, indent=1)); print("DRY RUN — add --apply to delete"); return 0
    if not targets and not acl_targets:
        print(f"nothing to release for {label!r}"); return 1
    deleted = []
    for r in acl_targets:
        if delete_acl(r):
            deleted.append(r.get("_id"))
        else:
            refused.append({"policy_id": r.get("_id"), "reason": "switch rule delete failed"})
            if r.get("action") == "BLOCK":
                break
    for pol in targets:
        if P.preset_from_policy(pol) and P.requires_network(P.preset_from_policy(pol)):
            refused.append({"policy_id": pol.get("_id"), "reason": "legacy VLAN-move quarantine: clear its network override in UniFi first"})
            continue
        if delete_policy(pol.get("_id"), allp):
            deleted.append(pol.get("_id"))
        else:
            refused.append({"policy_id": pol.get("_id"), "reason": "delete failed"})
    res = {"deleted": deleted, "refused": refused}
    record("release", vars_of(a), False, not refused, res)
    alert(f"House Arrest: released {safe(label or a.kind, 60)}", f"{len(deleted)} rule(s) removed, {len(refused)} refused", level="info")
    print(json.dumps(res, indent=1))
    return 0 if not refused else 1


def _find_network(nets, key):
    hit = [n for n in nets if n.get("_id") == key or (n.get("name") or "").lower() == (key or "").lower()]
    if len(hit) != 1:
        raise SystemExit(f"network {key!r}: {'not found' if not hit else 'ambiguous'}")
    return hit[0]


def cmd_isolate(a, release=False):
    _login()
    net = _find_network(networks(), a.network)
    if net.get("purpose") == "wan":
        raise SystemExit("refusing to isolate a WAN")
    if release:
        changes = {k: v for k, v in P.network_flags_to_release().items() if bool(net.get(k)) != bool(v)}
    else:
        if a.preset not in P.NETWORK_PRESETS:
            raise SystemExit(f"preset must be one of {P.NETWORK_PRESETS}")
        changes = P.network_changes_needed(net, a.preset)
    act = "release-network" if release else "isolate"
    if not changes:
        print(f"{net.get('name')}: nothing to change"); return 0
    if not a.apply:
        record(act, vars_of(a), True, True, changes); print(changes, "\nDRY RUN — add --apply"); return 0
    ok = set_network(net["_id"], **changes)
    record(act, vars_of(a), False, ok, changes)
    alert(f"UniFi: {act} {safe(net.get('name'), 40)} {'done' if ok else 'FAILED'}", json.dumps(changes), level="info" if ok else "warning")
    print("ok" if ok else "FAILED — settings did not read back"); return 0 if ok else 1


def _requeue_shadowing_blocks():
    """Upstream fix (measured 2026-10-01): a DNS lockdown created after a device lockdown can land behind
    the device's LAN block and cut its DNS. Recreate such blocks (new copy verified first, then the old
    one deleted) so the allow evaluates first."""
    allp = policies()
    known_by_mac = {(c.get("mac") or "").lower(): c for c in known_clients()}
    live = live_clients()
    moved, failed = [], []
    for block in [p for p in allp if P._blocks_lan_for(p)]:
        nets_ = _device_net_ids(P.policy_macs(block), known_by_mac, live)
        if not P.shadowed_dns_allows(block, allp, nets_) or not in_ledger(block.get("_id")):
            continue      # (a block Nova didn't create can't be swapped: its original couldn't be deleted)
        lbl = P._label_from_policy(block) or "a device"
        copy = create_policy(P.recreate_payload(block, P.next_free_index(allp, 1)[0]), lbl)
        if copy is None:
            failed.append(lbl); continue
        allp = policies()
        stored = next((p for p in allp if p.get("_id") == copy.get("_id")), copy)
        if P.shadowed_dns_allows(stored, allp, nets_):
            delete_policy(copy.get("_id"), allp); failed.append(lbl); continue
        delete_policy(block.get("_id"), allp)
        allp = [p for p in allp if p.get("_id") != block.get("_id")]
        moved.append(lbl)
    return moved, failed


def cmd_dns_lockdown(a):
    for ip in a.resolver:
        ipaddress.ip_address(ip)          # raises on junk
    _login()
    zns, existing, nets = zones(), policies(), networks()
    internal_id, external_id = _zone_ids(zns)
    chosen = [_find_network(nets, k) for k in a.network]
    ids = [n["_id"] for n in chosen]
    label = ", ".join(n.get("name") or "network" for n in chosen)
    by_zone = {}
    for n in chosen:
        by_zone.setdefault(P.zone_of_network(n, zns) or internal_id, []).append(n.get("name"))
    if len(by_zone) > 1:
        print("networks sit in different firewall zones; one DNS lockdown per zone"); return 1
    zid = next(iter(by_zone))
    already = P.dns_locked_network_ids(P.find_ours(existing))
    clash = [n.get("name") for n in chosen if n["_id"] in already]
    if clash:
        print(f"already DNS-locked: {', '.join(clash)} — dns-release first"); return 1
    lan_r, wan_r, foreign = P.classify_resolvers(a.resolver, nets, zns, zid)
    payloads = P.build_dns_lockdown(network_ids=ids, network_label=label, lan_resolvers=lan_r, wan_resolvers=wan_r,
                                    client_zone_id=zid, external_zone_id=external_id,
                                    indexes=P.next_free_index(existing, P.dns_policy_count(a.block_dot, bool(lan_r), bool(wan_r))),
                                    block_dot=a.block_dot, foreign_resolvers=foreign)
    caveats = P.dhcp_dns_conflicts(chosen, a.resolver) + list(P.DNS_CAVEATS)
    caveats += [f"{ip} is on {n.get('name')} itself: traffic to it never crosses the gateway"
                for n in chosen for ip in a.resolver if P._ip_in_subnet(ip, n.get("ip_subnet"))]
    try:
        caveats += P.dns_interception_caveats(site_setting("doh"), site_setting("ips"), content_filters(), ids,
                                              {n.get("_id"): n.get("name") or "network" for n in nets})
    except UniFiError as e:
        caveats.append(f"(could not read DNS interception settings: {e})")
    plan = {"label": label, "policies": [p.get("name") for p in payloads], "caveats": caveats}
    if not a.apply:
        record("dns-lockdown", vars_of(a), True, True, plan); print(json.dumps(plan, indent=1)); print("DRY RUN — add --apply"); return 0
    created = []
    for p in payloads:
        r = create_policy(p, label)
        if r is None:
            _rollback(created, []); record("dns-lockdown", vars_of(a), False, False, {"error": "create failed; rolled back"})
            print("create failed; rolled back"); return 1
        created.append(r)
    if not P.dns_order_is_safe(created, expect_allow=bool(lan_r or wan_r)):
        _rollback(created, []); record("dns-lockdown", vars_of(a), False, False, {"error": "allow after block; rolled back"})
        print("gateway put the allow after the blocks (would kill DNS); rolled back"); return 1
    moved, failed = _requeue_shadowing_blocks()
    res = {**plan, "created": [c.get("_id") for c in created], "requeued": moved, "requeue_failed": failed}
    record("dns-lockdown", vars_of(a), False, not failed, res)
    alert(f"House Arrest: DNS lockdown on {safe(label, 80)}", f"resolvers {', '.join(a.resolver)}"
          + (f"\nCOULD NOT re-order device lockdowns: {', '.join(failed)}" if failed else ""), level="warning" if failed else "info")
    print(json.dumps(res, indent=1)); return 0 if not failed else 1


def cmd_dns_release(a):
    label = (a.label or "").strip()
    if not label:
        print("--label is required; releasing every DNS lockdown at once is refused"); return 1
    _login()
    allp = policies()
    targets = [p for p in P.find_ours(allp) if P.is_dns_policy(p)
               and P.dns_label_from_policy(p).strip().lower() == label.lower()]
    plan = {"would_delete": [p.get("name") for p in targets]}
    if not a.apply:
        record("dns-release", vars_of(a), True, True, plan); print(json.dumps(plan, indent=1)); print("DRY RUN — add --apply"); return 0
    done = [p.get("_id") for p in targets if delete_policy(p.get("_id"), allp)]
    record("dns-release", vars_of(a), False, len(done) == len(targets), {"deleted": done})
    print(f"deleted {len(done)}/{len(targets)}"); return 0 if len(done) == len(targets) else 1


def cmd_wlan_isolation(a):
    _login()
    hit = [w for w in wlans() if w.get("_id") == a.ssid or (w.get("name") or "") == a.ssid]
    if len(hit) != 1:
        raise SystemExit(f"SSID {a.ssid!r}: {'not found' if not hit else 'ambiguous'}")
    w, on = hit[0], a.state == "on"
    if bool(w.get("l2_isolation")) == on:
        print("already set"); return 0
    if not a.apply:
        record("wlan-isolation", vars_of(a), True, True, {"l2_isolation": on}); print(f"would set l2_isolation={on}\nDRY RUN — add --apply"); return 0
    p = f"/api/s/{SITE}/rest/wlanconf/{w['_id']}"
    ok = _put_verified(p, p, {"l2_isolation": on}, strict=False)
    record("wlan-isolation", vars_of(a), False, ok, {"l2_isolation": on})
    print("ok" if ok else "FAILED"); return 0 if ok else 1


def cmd_mdns(a):
    _login()
    nets = networks()
    cfg = global_net_config()
    current = set(P.mdns_effective_ids(cfg, nets))
    nid = _find_network(nets, a.network)["_id"]
    wanted = sorted(current | {nid} if a.state == "on" else current - {nid})
    if sorted(current) == wanted:
        print("already set"); return 0
    if not a.apply:
        record("mdns", vars_of(a), True, True, {"mdns_networks": wanted}); print(f"would set mDNS networks {wanted}\nDRY RUN — add --apply"); return 0
    path = f"/v2/api/site/{SITE}/global/config/network"
    body = {"mdns_enabled_for": "some", "mdns_enabled_for_network_ids": wanted}   # partial PUT, as upstream measured
    ok = _call("PUT", path, body)[0] == 200
    if ok:
        for _ in range(10):
            if sorted(str(i) for i in (global_net_config() or {}).get("mdns_enabled_for_network_ids") or []) == wanted:
                break
            time.sleep(2)
        else:
            ok = False
    record("mdns", vars_of(a), False, ok, {"mdns_networks": wanted})
    print("ok" if ok else "FAILED"); return 0 if ok else 1


def cmd_dhcp_dns(a):
    for ip in a.resolver:
        ipaddress.ip_address(ip)
    _login()
    net = _find_network(networks(), a.network)
    payload = P.dhcp_dns_payload(a.resolver)
    if not a.apply:
        record("dhcp-dns", vars_of(a), True, True, payload); print(payload, "\nDRY RUN — add --apply"); return 0
    ok = set_network(net["_id"], strict=True, **payload)
    record("dhcp-dns", vars_of(a), False, ok, payload)
    print("ok" if ok else "FAILED"); return 0 if ok else 1


def cmd_block(a, unblock=False):
    import nova_unifi_ctl as ctl
    mac = norm_mac(a.mac)
    out = ctl.unblock(mac) if unblock else ctl.block(mac, a.reason or "unifi_toolkit")
    ok = not out.startswith(("REFUSED", "[error"))
    record("unblock" if unblock else "block", {"mac": mac, "reason": a.reason}, False, ok, {"out": out})
    if ok:
        try:
            cur = _ops().cursor(); ensure_schema(cur)
            cur.execute("INSERT INTO unifi_client_events (mac, kind, detail) VALUES (%s, %s, '{}')", (mac, "unblock" if unblock else "block"))
        except Exception as e:  # noqa: BLE001
            log(f"WARN: block event not recorded: {e}")
    print(out); return 0 if ok else 1


def vars_of(a) -> dict:
    return {k: v for k, v in vars(a).items() if k != "func"}


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("threats"); s.add_argument("--hours", type=int, default=24); s.set_defaults(func=cmd_threats)
    s = sp.add_parser("stalk"); s.set_defaults(func=cmd_stalk)
    s = sp.add_parser("audit"); s.add_argument("--verbose", action="store_true"); s.set_defaults(func=cmd_audit)
    s = sp.add_parser("track"); s.add_argument("op", choices=["add", "rm", "list"]); s.add_argument("mac", nargs="?")
    s.add_argument("--label"); s.add_argument("--quiet", action="store_true", help="record but don't alert"); s.set_defaults(func=cmd_track)
    s = sp.add_parser("report"); s.add_argument("mac"); s.add_argument("--days", type=int, default=14)
    s.add_argument("--limit", type=int, default=20); s.set_defaults(func=cmd_report)
    for name, unb in (("block", False), ("unblock", True)):
        s = sp.add_parser(name); s.add_argument("mac"); s.add_argument("--reason", default="")
        s.set_defaults(func=lambda a, unb=unb: cmd_block(a, unb))
    s = sp.add_parser("lockdown"); s.add_argument("--preset", required=True, choices=list(P.PRESETS))
    s.add_argument("--mac", action="append", required=True); s.add_argument("--label", required=True)
    s.add_argument("--block-neighbours", action="store_true"); s.add_argument("--override-comms-guard", action="store_true")
    s.add_argument("--apply", action="store_true"); s.set_defaults(func=cmd_lockdown)
    s = sp.add_parser("release"); s.add_argument("--label"); s.add_argument("--kind", choices=["device", "network"], default="device")
    s.add_argument("--policy-id", action="append"); s.add_argument("--apply", action="store_true"); s.set_defaults(func=cmd_release)
    s = sp.add_parser("isolate"); s.add_argument("--network", required=True); s.add_argument("--preset", required=True, choices=list(P.NETWORK_PRESETS))
    s.add_argument("--apply", action="store_true"); s.set_defaults(func=cmd_isolate)
    s = sp.add_parser("release-network"); s.add_argument("--network", required=True); s.add_argument("--apply", action="store_true")
    s.set_defaults(func=lambda a: cmd_isolate(a, release=True))
    s = sp.add_parser("dns-lockdown"); s.add_argument("--network", action="append", required=True)
    s.add_argument("--resolver", action="append", required=True); s.add_argument("--block-dot", action="store_true")
    s.add_argument("--apply", action="store_true"); s.set_defaults(func=cmd_dns_lockdown)
    s = sp.add_parser("dns-release"); s.add_argument("--label", required=True); s.add_argument("--apply", action="store_true")
    s.set_defaults(func=cmd_dns_release)
    s = sp.add_parser("wlan-isolation"); s.add_argument("--ssid", required=True); s.add_argument("state", choices=["on", "off"])
    s.add_argument("--apply", action="store_true"); s.set_defaults(func=cmd_wlan_isolation)
    s = sp.add_parser("mdns"); s.add_argument("--network", required=True); s.add_argument("state", choices=["on", "off"])
    s.add_argument("--apply", action="store_true"); s.set_defaults(func=cmd_mdns)
    s = sp.add_parser("dhcp-dns"); s.add_argument("--network", required=True); s.add_argument("--resolver", action="append", required=True)
    s.add_argument("--apply", action="store_true"); s.set_defaults(func=cmd_dhcp_dns)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        return a.func(a)
    except UniFiError as e:
        log(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
