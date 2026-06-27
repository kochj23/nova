#!/usr/bin/env python3
"""
nova_bambu_watch.py — Nova adopts the Bambu Lab X1C printers (local MQTT/TLS).

Persistent daemon that connects to each printer's local MQTT broker, tracks live
state, writes a status digest to Nova's memory (so "Nova, how are the printers?"
works like "how's the 134?"), and alerts on the events that matter — print
started / finished / FAILED, pauses, filament runout and HMS errors — to Slack,
with a phone push (ntfy) for failures and runouts.

Also a control CLI: status / pause / resume / stop / light / speed, and
print (FTPS upload of a .3mf + remote start).

Access codes live in Keychain (nova-bambu-<serial>); nothing secret is stored here.

Usage:
  nova_bambu_watch.py watch                 # daemon (launchd runs this)
  nova_bambu_watch.py status [P1|P2]        # one-shot status (also refreshes memory)
  nova_bambu_watch.py pause|resume|stop P1
  nova_bambu_watch.py light P1 on|off
  nova_bambu_watch.py speed P1 silent|standard|sport|ludicrous
  nova_bambu_watch.py print P1 /path/to/file.3mf [--no-ams]
  nova_bambu_watch.py selftest

Written by Jordan Koch.
"""

import argparse
import os
import ftplib
import json
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).parent))
from bambu_printers import PRINTERS
from nova_notify import notify  # Slack/Discord via PG telemetry.events

SOURCE = "bambu"
MEMORY_URL = "http://192.168.1.6:18790"
DIGEST_EVERY_S = 300  # write a status digest to memory every 5 min
SAMPLE_EVERY_S = 300  # write a telemetry row per printer to PG this often (Grafana)
PG_DSN = "host=localhost dbname=nova_ops user=kochj"
BUSY_STATES = {"RUNNING", "PAUSE", "PREPARE", "SLICING", "RESUMING"}


def is_busy(state):
    return state in BUSY_STATES

# gcode_state -> friendly
SPEED_LEVELS = {"silent": "1", "standard": "2", "sport": "3", "ludicrous": "4"}
STAGE = {  # stg_cur: a few common ones for human digests
    -1: "idle", 0: "printing", 1: "auto bed level", 2: "heatbed preheat",
    8: "calibrating extrusion", 9: "scanning bed surface", 14: "cleaning nozzle",
}


def log(msg):
    print(f"[nova_bambu {datetime.now():%H:%M:%S}] {msg}", flush=True)


def code(serial):
    """Access code from Keychain. Raises if missing — we never hardcode it."""
    return subprocess.check_output(
        ["security", "find-generic-password", "-a", "kochj", "-s", f"nova-bambu-{serial}", "-w"]
    ).decode().strip()


def _ntfy_topic():
    try:
        return subprocess.check_output(
            ["security", "find-generic-password", "-a", "nova", "-s", "nova-canary-topic", "-w"]
        ).decode().strip()
    except Exception:
        return ""


def push(title, message, priority="high", tags="printer"):
    """Phone push via ntfy (same topic the canary uses). Never raises."""
    topic = _ntfy_topic()
    if not topic:
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=message.encode(),
            headers={"Title": title.encode(), "Priority": priority, "Tags": tags,
                     "Content-Type": "text/plain; charset=utf-8"}, method="POST"), timeout=10)
    except Exception as e:
        log(f"ntfy push failed: {e}")


def remember(text):
    """Store a digest into Nova's vector memory (source=bambu). Never raises."""
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{MEMORY_URL}/remember", data=json.dumps({"text": text, "source": SOURCE}).encode(),
            headers={"Content-Type": "application/json"}), timeout=8).read()
    except Exception as e:
        log(f"memory store failed: {e}")


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """Bambu uses *implicit* FTPS on :990 — the socket is TLS from the first byte.
    ftplib only does explicit TLS, so wrap the data/command socket immediately."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value


def _alert(level, title, body, dedup_key, phone=False):
    """Slack (+ phone push if phone=True). level: info|warning|critical. Never raises."""
    try:
        notify(title, body=body, level=level, category="bambu", source="bambu", dedup_key=dedup_key)
    except Exception as e:
        log(f"slack notify failed: {e}")
    if phone:
        push(title, body or title, priority="high")


class Printer:
    def __init__(self, key, cfg):
        self.key = key
        self.name = cfg["name"]
        self.ip = cfg["ip"]
        self.serial = cfg["serial"]
        self.state = {}          # merged 'print' report (deltas accumulate here)
        self.gcode_state = None  # last seen, for transition detection
        self.hms_codes = set()
        self.connected = False
        self._seq = 0
        # client_id MUST be unique per process: the persistent daemon and any CLI
        # invocation otherwise share "nova-<key>", and an MQTT broker boots the older
        # connection on a duplicate id — a connect/disconnect war that flaps (kills)
        # the daemon. The pid suffix keeps every process distinct.
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"nova-{key}-{os.getpid()}")
        self.client.username_pw_set("bblp", code(self.serial))
        self.client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLSv1_2)
        self.client.tls_insecure_set(True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    # ── MQTT plumbing ────────────────────────────────────────────────────────
    def connect(self):
        self.client.connect(self.ip, 8883, keepalive=60)
        self.client.loop_start()

    def _on_connect(self, c, u, flags, rc, props=None):
        self.connected = (rc == 0)
        log(f"{self.name}: connect rc={rc}")
        if self.connected:
            c.subscribe(f"device/{self.serial}/report")
            self.request_full()

    def _on_disconnect(self, c, u, *a):
        self.connected = False
        log(f"{self.name}: disconnected")

    def request_full(self):
        self.client.publish(f"device/{self.serial}/request",
                            json.dumps({"pushing": {"sequence_id": str(self._next()), "command": "pushall"}}))

    def _next(self):
        self._seq += 1
        return self._seq

    def _on_message(self, c, u, msg):
        try:
            data = json.loads(msg.payload)
        except Exception:
            return
        p = data.get("print")
        if not isinstance(p, dict):
            return
        self.state.update(p)  # deltas accumulate
        self._detect_transitions(p)

    # ── transition detection / alerts ────────────────────────────────────────
    def _detect_transitions(self, delta):
        st = self.state.get("gcode_state")
        job = self.state.get("subtask_name") or "print"
        pct = self.state.get("mc_percent")

        if st and st != self.gcode_state:
            prev, self.gcode_state = self.gcode_state, st
            if prev is not None:  # skip the very first report (initial sync, not a real transition)
                self._on_state_change(prev, st, job, pct)

        # print error code (non-zero) — independent of gcode_state
        err = self.state.get("print_error", 0)
        if err and err != getattr(self, "_last_err", 0):
            self._last_err = err
            _alert("critical", f"❌ {self.name} print error",
                   f"{job}: error code {err} (hex {err:#010x}). Check the printer.",
                   dedup_key=f"bambu-{self.key}-err-{err}", phone=True)

        # HMS (health management) — new codes only
        hms = self.state.get("hms")
        if isinstance(hms, list):
            now = {(h.get("attr"), h.get("code")) for h in hms if isinstance(h, dict)}
            fresh = now - self.hms_codes
            self.hms_codes = now
            for attr, hcode in fresh:
                runout = _is_filament_runout(attr, hcode)
                _alert("critical" if runout else "warning",
                       f"{'🧵 Filament runout' if runout else '⚠️ HMS alert'} — {self.name}",
                       f"{job}: HMS {attr:#x}:{hcode:#x}. https://wiki.bambulab.com/en/x1/troubleshooting/hmscode",
                       dedup_key=f"bambu-{self.key}-hms-{attr}-{hcode}", phone=runout)

    def _on_state_change(self, prev, new, job, pct):
        log(f"{self.name}: {prev} -> {new} ({job})")
        if new == "RUNNING" and prev not in ("PAUSE", "RESUMING"):
            _alert("info", f"▶️ {self.name} started", f"{job}", f"bambu-{self.key}-start-{job}")
        elif new == "FINISH":
            _alert("info", f"✅ {self.name} finished", f"{job} — done.", f"bambu-{self.key}-finish-{job}")
        elif new == "FAILED":
            _alert("critical", f"❌ {self.name} FAILED",
                   f"{job} failed at {pct}%. fail_reason={self.state.get('fail_reason')}",
                   f"bambu-{self.key}-failed-{job}", phone=True)
        elif new == "PAUSE":
            _alert("warning", f"⏸ {self.name} paused",
                   f"{job} paused at {pct}% — may need attention (runout/error/manual).",
                   f"bambu-{self.key}-pause-{job}", phone=True)

    # ── status / digest ──────────────────────────────────────────────────────
    def status_line(self):
        if not self.state:
            return f"{self.name} ({self.ip}): {'connecting…' if not self.connected else 'no report yet'}"
        s = self.state
        st = s.get("gcode_state", "?")
        job = s.get("subtask_name") or "—"
        pct = s.get("mc_percent", 0)
        rem = s.get("mc_remaining_time", 0)
        ln, tot = s.get("layer_num", 0), s.get("total_layer_num", 0)
        noz, bed = s.get("nozzle_temper", 0), s.get("bed_temper", 0)
        base = f"{self.name}: {st}"
        if st in ("RUNNING", "PAUSE"):
            base += f" — {job} {pct}%, layer {ln}/{tot}, ~{rem}m left, nozzle {noz:.0f}°/bed {bed:.0f}°"
        else:
            base += f" (idle; last: {job}). nozzle {noz:.0f}°/bed {bed:.0f}°"
        return base

    # ── control ──────────────────────────────────────────────────────────────
    def _print_cmd(self, command, **extra):
        payload = {"print": {"sequence_id": str(self._next()), "command": command, **extra}}
        self.client.publish(f"device/{self.serial}/request", json.dumps(payload))

    def pause(self):  self._print_cmd("pause")
    def resume(self): self._print_cmd("resume")
    def stop(self):   self._print_cmd("stop")

    # Full calibration: option bitmask 1 LIDAR | 2 bed-level | 4 vibration | 8 motor-noise = 14.
    def calibrate(self): self._print_cmd("calibration", option=14)

    def speed(self, level):
        self._print_cmd("print_speed", param=SPEED_LEVELS.get(level, "2"))

    def light(self, on):
        payload = {"system": {"sequence_id": str(self._next()), "command": "ledctrl",
                              "led_node": "chamber_light", "led_mode": "on" if on else "off",
                              "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0}}
        self.client.publish(f"device/{self.serial}/request", json.dumps(payload))

    def upload_and_print(self, filepath, use_ams=True):
        """FTPS-upload a .3mf to the printer, then start it. Returns (ok, message)."""
        fp = Path(filepath)
        if not fp.exists():
            return False, f"file not found: {filepath}"
        name = fp.name
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ftp = ImplicitFTP_TLS(context=ctx)
            ftp.connect(self.ip, 990, timeout=30)
            ftp.login("bblp", code(self.serial))
            ftp.prot_p()
            with open(fp, "rb") as f:
                ftp.storbinary(f"STOR {name}", f)
            ftp.quit()
        except Exception as e:
            return False, f"FTPS upload failed: {e}"
        # Start it. param points at the sliced gcode inside the 3mf; plate_1 is the default plate.
        self._print_cmd(
            "project_file",
            param="Metadata/plate_1.gcode", subtask_name=fp.stem,
            url=f"file:///mnt/sdcard/{name}", bed_type="auto",
            timelapse=False, bed_leveling=True, flow_cali=True, vibration_cali=True,
            layer_inspect=True, use_ams=use_ams, profile_id="0", project_id="0",
            subtask_id="0", task_id="0",
        )
        return True, f"uploaded {name} and sent print start to {self.name}"


def _is_filament_runout(attr, hcode):
    # Bambu HMS filament-runout family. Codes vary by AMS slot; match the known runout signature.
    return hcode in (0x07008011, 0x07FF8011, 0x07018011, 0x07028011, 0x07038011)


# ── telemetry sampling → Postgres (for Grafana) ───────────────────────────────
_pg_conn = None


def _pg():
    """Lazy, self-healing psycopg2 connection. Returns None if PG is unreachable —
    telemetry is best-effort and must never stall or crash the watch loop."""
    global _pg_conn
    try:
        if _pg_conn is None or _pg_conn.closed:
            import psycopg2
            _pg_conn = psycopg2.connect(PG_DSN)
            _pg_conn.autocommit = True
        return _pg_conn
    except Exception as e:
        log(f"pg connect failed: {e}")
        _pg_conn = None
        return None


def pg_sample(printers):
    """Insert one telemetry row per printer (best-effort; drops the connection on error so the next tick reconnects)."""
    global _pg_conn
    conn = _pg()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            for p in printers:
                s = p.state
                if not s:
                    continue
                st = s.get("gcode_state")
                cur.execute(
                    "INSERT INTO bambu_telemetry "
                    "(printer,name,state,busy,stage,job,nozzle_temp,bed_temp,chamber_temp,pct,layer,total_layer) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (p.key, p.name, st, is_busy(st),
                     STAGE.get(s.get("stg_cur"), str(s.get("stg_cur"))),
                     s.get("subtask_name") or None,
                     s.get("nozzle_temper"), s.get("bed_temper"), s.get("chamber_temper"),
                     s.get("mc_percent"), s.get("layer_num"), s.get("total_layer_num")))
    except Exception as e:
        log(f"pg sample failed: {e}")
        try:
            conn.close()
        except Exception:
            pass
        _pg_conn = None


# ── digest across all printers ────────────────────────────────────────────────
def write_digest(printers, blocking=True):
    lines = [p.status_line() for p in printers]
    text = f"Printer status {datetime.now():%Y-%m-%d %H:%M}:\n" + "\n".join(lines)
    if blocking:
        remember(text)
    else:
        # daemon path: never let a hung memory service stall the watch/alert loop
        import threading
        threading.Thread(target=remember, args=(text,), daemon=True).start()
    return lines


def watch():
    printers = [Printer(k, v) for k, v in PRINTERS.items()]
    for p in printers:
        try:
            p.connect()
        except Exception as e:
            log(f"{p.name}: connect failed: {e}")
    log(f"watching {len(printers)} printers")
    last_digest = 0
    last_sample = 0
    try:
        while True:
            time.sleep(5)
            for p in printers:
                if not p.connected:
                    try:
                        p.client.reconnect()
                    except Exception:
                        pass
            if time.time() - last_sample >= SAMPLE_EVERY_S:
                pg_sample(printers)
                last_sample = time.time()
            if time.time() - last_digest >= DIGEST_EVERY_S:
                write_digest(printers, blocking=False)
                last_digest = time.time()
    except KeyboardInterrupt:
        for p in printers:
            p.client.loop_stop()


def _one(printer_key):
    if printer_key not in PRINTERS:
        sys.exit(f"unknown printer '{printer_key}' (have: {', '.join(PRINTERS)})")
    p = Printer(printer_key, PRINTERS[printer_key])
    p.connect()
    for _ in range(30):  # wait up to ~9s for first report
        if p.state:
            break
        time.sleep(0.3)
    return p


def cli_status(key=None):
    keys = [key] if key else list(PRINTERS)
    out = []
    ps = []
    for k in keys:
        p = _one(k)
        ps.append(p)
        out.append(p.status_line())
    print("\n".join(out))
    write_digest(ps)  # keep memory fresh on manual checks too
    for p in ps:
        p.client.loop_stop()


def selftest():
    # config + keychain wiring
    assert set(PRINTERS) == {"P1", "P2"}
    for k, v in PRINTERS.items():
        assert {"name", "ip", "serial"} <= v.keys()
        c = code(v["serial"])
        assert len(c) == 8, f"{k} access code wrong length"
    assert SPEED_LEVELS["ludicrous"] == "4"
    assert _is_filament_runout(0, 0x07008011) and not _is_filament_runout(0, 0x12345678)
    # telemetry: busy classification (drives the Grafana idle/in-use panel)
    assert is_busy("RUNNING") and is_busy("PAUSE") and not is_busy("FINISH") and not is_busy(None)
    # status_line tolerates empty + populated state
    p = Printer("P1", PRINTERS["P1"]); p.client.loop_stop()
    assert "Printer 1" in p.status_line()
    p.state = {"gcode_state": "RUNNING", "subtask_name": "x", "mc_percent": 42,
               "mc_remaining_time": 10, "layer_num": 5, "total_layer_num": 9,
               "nozzle_temper": 220, "bed_temper": 60, "total_layer_num": 9}
    assert "42%" in p.status_line()
    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("watch")
    sub.add_parser("selftest")
    sp = sub.add_parser("status"); sp.add_argument("printer", nargs="?")
    for c in ("pause", "resume", "stop"):
        x = sub.add_parser(c); x.add_argument("printer")
    xl = sub.add_parser("light"); xl.add_argument("printer"); xl.add_argument("mode", choices=["on", "off"])
    xs = sub.add_parser("speed"); xs.add_argument("printer"); xs.add_argument("level", choices=list(SPEED_LEVELS))
    xp = sub.add_parser("print"); xp.add_argument("printer"); xp.add_argument("file"); xp.add_argument("--no-ams", action="store_true")
    xc = sub.add_parser("calibrate"); xc.add_argument("printer", nargs="?")  # default: both
    args = ap.parse_args()

    if args.cmd == "watch":
        watch()
    elif args.cmd == "selftest":
        selftest()
    elif args.cmd == "status":
        cli_status(args.printer)
    elif args.cmd == "calibrate":
        # Full calibration moves the toolhead/bed and runs ~3-5 min. NEVER start one
        # on top of a live job, so verify each printer is idle first.
        keys = [args.printer] if args.printer else list(PRINTERS)
        for k in keys:
            p = _one(k)
            st = p.state.get("gcode_state")
            if st in ("RUNNING", "PREPARE", "PAUSE", "SLICING"):
                print(f"{p.name}: BUSY ({st}) — refusing to calibrate an active job")
            elif st is None:
                print(f"{p.name}: no status (offline?) — skipping for safety")
            else:
                p.calibrate()
                print(f"{p.name}: full calibration STARTED (was {st}; LIDAR+bed+vibration+motor, ~3-5 min)")
            time.sleep(1.5)  # let the publish flush
            p.client.loop_stop()
    elif args.cmd in ("pause", "resume", "stop", "light", "speed", "print"):
        p = _one(args.printer)
        if args.cmd == "light":
            p.light(args.mode == "on"); print(f"{p.name}: light {args.mode}")
        elif args.cmd == "speed":
            p.speed(args.level); print(f"{p.name}: speed {args.level}")
        elif args.cmd == "print":
            ok, m = p.upload_and_print(args.file, use_ams=not args.no_ams); print(m)
        else:
            getattr(p, args.cmd)(); print(f"{p.name}: {args.cmd} sent")
        time.sleep(1.5)  # let the publish flush
        p.client.loop_stop()
