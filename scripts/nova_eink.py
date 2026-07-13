#!/usr/bin/env python3
"""
nova_eink.py — Nova render module for the Seeed reTerminal E1002
(7.3" E Ink Spectra 6 / E6, 800x480, ESP32-S3).

LANE: server-side, FIRMWARE-AGNOSTIC. This module ONLY produces an
800x480 status image (color 6-pigment OR 1-bit mono) and serves it over
HTTP on Nova's LAN. Any of the supported firmware paths (stock SenseCraft,
Home Assistant native, ESPHome `online_image`, TRMNL/BYOS) consume the
same renderer.

Run:
    uv run --with pillow --with fastapi --with uvicorn python nova_eink.py
    # -> http://0.0.0.0:8073   (bind Nova .6 or .2 in production)

Endpoints (see bottom of file):
    GET /eink/nova.png        color 6-color PNG  (ESPHome online_image type: RGB; SenseCraft; HA)
    GET /eink/nova.bmp        1-bit mono BMP     (TRMNL device direct; generic)
    GET /eink/nova-mono.png   1-bit mono PNG
    GET /api/setup            TRMNL BYOS provisioning
    GET /api/display          TRMNL BYOS image pointer (JSON)
    GET /api/log              TRMNL BYOS log sink (204)
    GET /eink/health          JSON status

Pillow >= 10 (tested 12.1.1).
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

# Module-level so FastAPI can resolve the PEP-563 string annotation `request: Request`
# on the route handlers (with `from __future__ import annotations`, annotations are
# evaluated against MODULE globals, not build_app's locals). Guarded so --render works
# without fastapi installed.
try:
    from fastapi import FastAPI, Request, Response
except ImportError:
    FastAPI = Request = Response = None

# ----------------------------------------------------------------------------
# 1. PANEL GEOMETRY  (E Ink Spectra 6, reTerminal E1002 / Waveshare 7.3" E6)
# ----------------------------------------------------------------------------
WIDTH, HEIGHT = 800, 480          # native panel resolution, landscape

# ----------------------------------------------------------------------------
# 2. SPECTRA-6 PALETTE
#    The E6 panel renders 6 fixed pigments. We dither against a *measured*
#    (muted, on-glass) palette for perceptually-correct Floyd-Steinberg error
#    diffusion, then remap each index 1:1 to the *canonical* primary so the
#    firmware's own nearest-color quantizer lands on an exact match (no speckle
#    on flat UI fills, clean photos). Index order is identity between the two.
#
#    Measured anchors: red #a02020, blue #5080b8 come from the community
#    Spectra-6 converter (Toon-nooT). Blue/green are notably shifted on-panel.
# ----------------------------------------------------------------------------
#                 name      canonical(ideal)   measured(on-glass)
SPECTRA6 = [
    ("black",   (0,   0,   0),   (0,   0,   0)),
    ("white",   (255, 255, 255), (230, 230, 225)),
    ("yellow",  (255, 255, 0),   (228, 200, 40)),
    ("red",     (255, 0,   0),   (160, 32,  32)),   # #a02020
    ("blue",    (0,   0,   255), (80,  128, 184)),  # #5080b8
    ("green",   (0,   255, 0),   (66,  114, 72)),
]
IDEAL = [c for _, c, _ in SPECTRA6]
MEASURED = [m for _, _, m in SPECTRA6]


def _palette_image(colors: list[tuple[int, int, int]]) -> Image.Image:
    """A 1x1 'P' image carrying a palette padded to 256 entries.
    Pad with the last real color so stray indices never introduce a 7th hue."""
    flat: list[int] = []
    for r, g, b in colors:
        flat += [r, g, b]
    last = colors[-1]
    while len(flat) < 256 * 3:
        flat += list(last)
    p = Image.new("P", (1, 1))
    p.putpalette(flat)
    return p


_MEASURED_PAL = _palette_image(MEASURED)


def quantize_spectra6(rgb: Image.Image, dither: bool = True) -> Image.Image:
    """RGB -> 6-color RGB. Dither vs measured palette, then swap to canonical.
    Flat regions painted in exact theme colors diffuse zero error => crisp UI;
    photos/gradients get Floyd-Steinberg."""
    d = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    q = rgb.convert("RGB").quantize(palette=_MEASURED_PAL, dither=d)  # 'P', measured
    q.putpalette([c for rgb3 in IDEAL for c in rgb3]                  # re-map to canonical
                 + list(IDEAL[-1]) * (256 - len(IDEAL)))
    return q.convert("RGB")


def to_mono_1bit(rgb: Image.Image, dither_photos: bool = True) -> Image.Image:
    """RGB -> 1-bit. UI is already pure B/W in mono theme; photos optionally FS."""
    d = Image.Dither.FLOYDSTEINBERG if dither_photos else Image.Dither.NONE
    return rgb.convert("L").convert("1", dither=d)


# ----------------------------------------------------------------------------
# 3. THEME  — semantic colors resolve differently for color vs mono panels
# ----------------------------------------------------------------------------
@dataclass
class Theme:
    bg: tuple
    fg: tuple
    accent: tuple      # primary highlight
    warn: tuple        # alerts / battery low
    ok: tuple          # healthy
    info: tuple        # presence / neutral data
    hi: tuple          # secondary highlight

    @staticmethod
    def color() -> "Theme":
        return Theme(bg=(255, 255, 255), fg=(0, 0, 0), accent=(255, 0, 0),
                     warn=(255, 0, 0), ok=(0, 255, 0), info=(0, 0, 255),
                     hi=(255, 255, 0))

    @staticmethod
    def mono() -> "Theme":
        b, w = (0, 0, 0), (255, 255, 255)
        return Theme(bg=w, fg=b, accent=b, warn=b, ok=b, info=b, hi=b)


# ----------------------------------------------------------------------------
# 4. DATA SNAPSHOT  — firmware-agnostic input. Fill via collect() from your
#    homelab (Home Assistant REST, MQTT, Plex API, nova_ops Postgres, etc.).
# ----------------------------------------------------------------------------
@dataclass
class Snapshot:
    ts: datetime = field(default_factory=datetime.now)
    presence: list = field(default_factory=list)       # ["Jordan", ...]
    indoor_f: float | None = None
    indoor_humidity: int | None = None
    indoor_room: str = "Indoor"
    garage_f: float | None = None
    hot_room: tuple | None = None                       # (name, °F) hottest room
    outdoor_f: float | None = None
    aqi: int | None = None                              # PM2.5 µg/m³
    house_w: int | None = None                          # whole-home watts (plugs+eve+poe)
    temp_hist: list = field(default_factory=list)        # 24h outdoor °F for sparkline
    backups_ok: bool | None = None
    backups_note: str = ""
    fleet_ok: int = 0
    fleet_total: int = 0
    memories: int | None = None
    alerts: list = field(default_factory=list)           # [(msg, "warn"|"ok")]
    nova_line: str = ""                                  # Nova's voice / resurfaced memory
    delight: str = ""                                    # rotating: flight / chp / on-this-day
    stale: bool = False
    rooms: list = field(default_factory=list)            # [(name, °F)] all sensors, hottest first
    top_loads: list = field(default_factory=list)        # [(device, watts)] biggest consumers
    fleet_down: list = field(default_factory=list)       # ["hue", "syslog"]
    wx: dict = field(default_factory=dict)               # feels/hum/wind/gust/uv/pm25/dew/co2
    flight: str = ""                                     # ✈ separate from chp delight
    chp: str = ""


def sample() -> Snapshot:
    return Snapshot(
        presence=["Jordan", "Tricia"],
        indoor_f=73.8, indoor_humidity=54, indoor_room="Living Room",
        garage_f=78.8, hot_room=("Server Rack", 94.4), outdoor_f=75, aqi=7,
        house_w=4602, temp_hist=[70, 68, 67, 69, 73, 78, 82, 85, 84, 80, 76, 74],
        backups_ok=True, backups_note="11:08",
        fleet_ok=29, fleet_total=31, memories=1_668_182,
        alerts=[("Server Rack 94°F — normal for the rack", "info")],
        nova_line="Twenty-three years in and the rack still runs hotter than my opinions.",
        delight="✈ SKW2453 SkyWest overhead · 31,000 ft NW",
    )


def _q1(cur, sql, params=None):
    # Pass params ONLY when present — an empty tuple still triggers psycopg2's
    # %-substitution and chokes on literal % (e.g. LIKE '0x%').
    cur.execute(sql, params) if params else cur.execute(sql)
    r = cur.fetchone()
    return r if r else None


def collect() -> Snapshot:
    """Live snapshot from nova_ops + nova_memories. Per-section try/except so one
    dead source never blanks the panel; whole-failure falls back to sample(stale)."""
    import psycopg2
    s = Snapshot()
    try:
        c = psycopg2.connect("host=127.0.0.1 dbname=nova_ops user=kochj connect_timeout=4")
        c.autocommit = True
        cur = c.cursor()
    except Exception:
        s = sample(); s.stale = True; return s

    def sect(fn):
        try: fn()
        except Exception: pass

    @sect
    def _presence():
        # the person column also holds detector artifacts (motion, camera_detected,
        # occupant…) — keep only real, single-token human names.
        deny = {"motion", "occupant", "unknown", "person", "presence", "away", "home",
                "detected", "nobody", "camera_detected", "camera", "guest"}
        cur.execute("SELECT DISTINCT person FROM telemetry.presence WHERE ts>now()-interval '25 min' AND person IS NOT NULL")
        ppl = [r[0] for r in cur.fetchall()]
        s.presence = sorted({p.title() for p in ppl if p.lower() not in deny and "_" not in p})[:5]

    @sect
    def _climate():
        # "server_rack" sensor = the RACK (runs hot, ~94F); "office_presence" = the actual
        # room. Keep them distinct so the rack's heat isn't blamed on the room.
        namemap = {"server_rack": "Server Rack", "outdoor_front": "Outdoor"}
        def disp(nm):
            return namemap.get(nm) or nm.replace("_presence", "").replace("_", " ").title()
        cur.execute("SELECT DISTINCT ON (room) room,temp_f,humidity FROM telemetry.climate WHERE ts>now()-interval '40 min' AND temp_f IS NOT NULL ORDER BY room,ts DESC")
        rows = cur.fetchall()
        rmap = {r[0]: (r[1], r[2]) for r in rows}
        for key in ("living_room_presence", "living_room", "office_presence", "master_bedroom_presence"):
            if key in rmap:
                s.indoor_f, s.indoor_humidity = round(rmap[key][0], 1), rmap[key][1]
                s.indoor_room = disp(key); break
        for key in ("garage_presence", "garage"):
            if key in rmap: s.garage_f = round(rmap[key][0], 1); break
        indoor = [(r[0], r[1]) for r in rows if not r[0].startswith("outdoor") and "patio" not in r[0]]
        if indoor:
            nm, t = max(indoor, key=lambda x: x[1])
            s.hot_room = (disp(nm), round(t, 1))
        # dense room table: all sensors, dedup by DISPLAY name (Office vs Office Rack
        # stay separate), hottest first
        seen = {}
        for nm, t, _h in sorted(rows, key=lambda r: -r[1]):
            dn = disp(nm)
            if dn not in seen:
                seen[dn] = t
        s.rooms = [(dn, round(t)) for dn, t in sorted(seen.items(), key=lambda x: -x[1])]

    @sect
    def _weather():
        r = _q1(cur, "SELECT round(temp_f::numeric,0), pm25, round(feels_like_f::numeric,0), humidity, round(wind_speed_mph::numeric,0), uv_index, round(dew_point_f::numeric,0), co2 FROM telemetry.weather WHERE temp_f IS NOT NULL ORDER BY ts DESC LIMIT 1")
        if r:
            s.outdoor_f = int(r[0]) if r[0] is not None else None
            s.aqi = int(r[1]) if r[1] is not None else None
            s.wx = {"feels": r[2], "hum": r[3], "wind": r[4], "uv": r[5], "dew": r[6], "co2": r[7]}
        cur.execute("SELECT temp_f FROM telemetry.weather WHERE ts>now()-interval '24 hours' AND temp_f IS NOT NULL ORDER BY ts")
        vals = [float(x[0]) for x in cur.fetchall()]
        if len(vals) > 60:
            step = len(vals) / 60.0
            vals = [vals[int(i * step)] for i in range(60)]
        s.temp_hist = vals

    @sect
    def _energy():
        plugs = _q1(cur, "SELECT sum(w) FROM (SELECT DISTINCT ON (device_name) watts w FROM telemetry.energy WHERE ts>now()-interval '15 min' AND device_name NOT LIKE '0x%' ORDER BY device_name,ts DESC) s")
        poe = _q1(cur, "SELECT sum(value) FROM (SELECT DISTINCT ON (metadata->>'device',metadata->>'port') value FROM telemetry.unifi_metrics WHERE metric='unifi_port_poe_w' AND ts>now()-interval '6 min' ORDER BY metadata->>'device',metadata->>'port',ts DESC) s")
        tot = (float(plugs[0]) if plugs and plugs[0] else 0) + (float(poe[0]) if poe and poe[0] else 0)
        if tot > 0: s.house_w = int(round(tot))
        cur.execute("SELECT device_name, round(watts::numeric,0) FROM (SELECT DISTINCT ON (device_name) device_name,watts FROM telemetry.energy WHERE ts>now()-interval '15 min' AND device_name NOT LIKE '0x%' AND watts>2 ORDER BY device_name,ts DESC) s ORDER BY watts DESC LIMIT 5")
        s.top_loads = [(n.replace("Eve Energy Strip", "Eve").replace("_", " ").title(), int(w)) for n, w in cur.fetchall()]

    @sect
    def _backups():
        r = _q1(cur, "SELECT ok, to_char(ts,'HH24:MI') FROM telemetry.backup_runs ORDER BY ts DESC LIMIT 1")
        if r: s.backups_ok, s.backups_note = r[0], (r[1] or "")

    @sect
    def _fleet():
        r = _q1(cur, "SELECT count(*) FILTER (WHERE status='up'), count(*) FROM service_registry")
        if r: s.fleet_ok, s.fleet_total = r
        cur.execute("SELECT DISTINCT service_name FROM service_registry WHERE status<>'up' ORDER BY 1")
        s.fleet_down = [x[0] for x in cur.fetchall()]

    @sect
    def _delight():
        r = _q1(cur, "SELECT callsign, operator, alt_ft, compass FROM telemetry.overhead_flights WHERE ts>now()-interval '20 min' AND callsign IS NOT NULL ORDER BY dist_nm NULLS LAST LIMIT 1")
        if r: s.flight = f"{r[0]} {r[1] or ''} · {r[2] or '?'}ft {r[3] or ''}".strip()
        r = _q1(cur, "SELECT type, COALESCE(location_desc,location) FROM telemetry.chp_incidents WHERE ts>now()-interval '3 hours' ORDER BY ts DESC LIMIT 1")
        if r: s.chp = f"{r[0]} — {r[1]}"[:60]

    @sect
    def _alerts():
        if s.hot_room and s.hot_room[1] >= 90:
            s.alerts.append((f"{s.hot_room[0]} {s.hot_room[1]:.0f}°F — running hot", "warn"))
        if s.backups_ok is False:
            s.alerts.append((f"Backup needs a look (last run {s.backups_note})", "warn"))
        if s.fleet_total and s.fleet_ok < s.fleet_total:
            s.alerts.append((f"{s.fleet_total - s.fleet_ok} service(s) down", "warn"))
        if s.aqi and s.aqi > 75:
            s.alerts.append((f"Air quality PM2.5 {s.aqi}", "warn"))

    c.close()

    # memories + Nova's voice (separate DB)
    try:
        m = psycopg2.connect("host=127.0.0.1 dbname=nova_memories user=kochj connect_timeout=4")
        m.autocommit = True
        mc = m.cursor()
        r = _q1(mc, "SELECT count(*) FROM memories")
        if r: s.memories = r[0]
        try:
            mc.execute("SELECT text FROM memories TABLESAMPLE SYSTEM (0.03) "
                       "WHERE text ~ '^[A-Z][a-z].{40,140}[.!?]$' AND text !~ 'HealthKit|data for|=|http|@' LIMIT 1")
            row = mc.fetchone()
            if row: s.nova_line = row[0].strip()
        except Exception:
            pass
        m.close()
    except Exception:
        pass

    if not s.nova_line:
        s.nova_line = "1.6 million memories and counting. Ask me anything; I probably overthought it."
    return s


# ----------------------------------------------------------------------------
# 5. FONTS  (DejaVu ships with Pillow; falls back to default bitmap)
# ----------------------------------------------------------------------------
def _font(size: int, bold: bool = False):
    names = (["DejaVuSans-Bold.ttf"] if bold else ["DejaVuSans.ttf"])
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default()


F_HUGE = _font(64, True)
F_BIG = _font(40, True)
F_NUM = _font(30, True)
F_MED = _font(26, True)
F_LBL = _font(18, True)
F_SM = _font(16)
F_XS = _font(13)


# ----------------------------------------------------------------------------
# 6. RENDERER  -> RGB canvas at 800x480
# ----------------------------------------------------------------------------
def _card(d, box, title, th):
    x0, y0, x1, y1 = box
    d.rectangle(box, outline=th.fg, width=2, fill=th.bg)
    d.rectangle((x0, y0, x1, y0 + 26), fill=th.fg)
    d.text((x0 + 8, y0 + 4), title.upper(), font=F_LBL, fill=th.bg)
    return x0 + 10, y0 + 34


def _sparkline(d, box, vals, color, th):
    x0, y0, x1, y1 = box
    if not vals or len(vals) < 2:
        return
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts = [(x0 + (x1 - x0) * i / (n - 1), y1 - (y1 - y0) * (v - lo) / rng) for i, v in enumerate(vals)]
    d.line(pts, fill=color, width=2, joint="curve")
    # endpoint dot + hi/lo labels
    d.ellipse((pts[-1][0] - 3, pts[-1][1] - 3, pts[-1][0] + 3, pts[-1][1] + 3), fill=color)
    d.text((x0, y0 - 2), f"{hi:.0f}°", font=F_SM, fill=th.fg)
    d.text((x0, y1 - 14), f"{lo:.0f}°", font=F_SM, fill=th.fg)


def render(snap: Snapshot, mode: str = "mono") -> Image.Image:
    th = Theme.mono()                       # display is now black & white
    B, Wt = th.fg, th.bg
    img = Image.new("RGB", (WIDTH, HEIGHT), Wt)
    d = ImageDraw.Draw(img)

    def sec(x, y, w, title):
        d.rectangle((x, y, x + w, y + 17), fill=B)
        d.text((x + 4, y + 1), title.upper(), font=F_XS, fill=Wt)
        return y + 21

    def kv(x, y, w, label, val, font=F_XS):
        d.text((x, y), str(label), font=font, fill=B)
        vw = d.textlength(str(val), font=font)
        d.text((x + w - vw, y), str(val), font=font, fill=B)
        return y + font.size + 4

    # header
    d.rectangle((0, 0, WIDTH, 30), fill=B)
    d.text((10, 2), "NOVA", font=F_MED, fill=Wt)
    clk = snap.ts.strftime("%a %d %b  %H:%M")
    d.text((WIDTH - d.textlength(clk, font=F_SM) - 10, 6), clk, font=F_SM, fill=Wt)

    # alert / nominal line
    ay = 34
    if snap.alerts:
        msg = " · ".join(a[0] for a in snap.alerts)
        d.rectangle((0, ay, WIDTH, ay + 21), fill=B)
        d.text((8, ay + 2), ("⚠ " + msg)[:104], font=F_SM, fill=Wt)
    else:
        d.text((8, ay + 2), "✓ All systems nominal", font=F_SM, fill=B)

    top = 62
    colw = 250
    cx = [8, 8 + colw + 12, 8 + 2 * (colw + 12)]   # 8, 270, 532
    for vx in (cx[1] - 6, cx[2] - 6):
        d.line((vx, top, vx, 412), fill=B, width=1)

    # ---- Column A: ROOMS + WEATHER ----
    x = cx[0]; y = sec(x, top, colw, "Rooms °F")
    for nm, t in snap.rooms[:9]:
        y = kv(x, y, colw, nm[:18], f"{t}°")
    y += 6
    y = sec(x, y, colw, "Weather")
    wx = snap.wx or {}
    if snap.outdoor_f is not None: y = kv(x, y, colw, "Outdoor", f"{snap.outdoor_f}°F")
    if wx.get("feels") is not None: y = kv(x, y, colw, "Feels like", f"{int(wx['feels'])}°")
    if wx.get("hum") is not None: y = kv(x, y, colw, "Humidity", f"{wx['hum']}%")
    if wx.get("wind") is not None: y = kv(x, y, colw, "Wind", f"{int(wx['wind'])} mph")
    if wx.get("uv") is not None: y = kv(x, y, colw, "UV index", f"{wx['uv']}")
    if wx.get("dew") is not None: y = kv(x, y, colw, "Dew point", f"{int(wx['dew'])}°")
    if snap.aqi is not None: y = kv(x, y, colw, "Air PM2.5", f"{snap.aqi}")
    if wx.get("co2") is not None: y = kv(x, y, colw, "CO2", f"{wx['co2']} ppm")

    # ---- Column B: POWER + 24h temp ----
    x = cx[1]; y = sec(x, top, colw, "House Power")
    if snap.house_w is not None:
        nw = f"{snap.house_w:,}"
        d.text((x, y - 2), nw, font=F_NUM, fill=B)
        d.text((x + d.textlength(nw, font=F_NUM) + 6, y + 8), "W", font=F_MED, fill=B)
        y += 36
    for nm, w in snap.top_loads[:5]:
        y = kv(x, y, colw, nm[:20], f"{w} W")
    y += 6
    y = sec(x, y, colw, "Outdoor 24h")
    if snap.temp_hist:
        _sparkline(d, (x + 2, y + 4, x + colw - 6, y + 52), snap.temp_hist, B, th)
        y += 58

    # ---- Column C: HEALTH + PRESENCE + MEMORY ----
    x = cx[2]; y = sec(x, top, colw, "Health")
    y = kv(x, y, colw, "Backup " + (snap.backups_note or ""), "OK" if snap.backups_ok else "CHECK", F_SM)
    y = kv(x, y, colw, "Fleet", f"{snap.fleet_ok}/{snap.fleet_total}", F_SM)
    if snap.fleet_down:
        y = kv(x, y, colw, "Down", (", ".join(snap.fleet_down))[:20], F_XS)
    y += 6
    y = sec(x, y, colw, "Who's Home")
    if snap.presence:
        for p in snap.presence[:4]:
            d.text((x, y), "• " + p, font=F_SM, fill=B); y += 19
    else:
        d.text((x, y), "nobody home", font=F_SM, fill=B); y += 19
    y += 6
    y = sec(x, y, colw, "Nova Memory")
    if snap.memories is not None:
        d.text((x, y - 2), f"{snap.memories:,}", font=F_NUM, fill=B)
        d.text((x, y + 32), "memories", font=F_XS, fill=B)

    # ---- Nova voice band ----
    ny = HEIGHT - 44
    d.line((8, ny, WIDTH - 8, ny), fill=B, width=1)
    d.text((8, ny + 4), "NOVA ▸", font=F_LBL, fill=B)
    _wrap(d, snap.nova_line, F_XS, B, 86, ny + 5, WIDTH - 98, lh=15, lines=1)

    # ---- footer: flight + chp + stamp ----
    fy = HEIGHT - 18
    foot = []
    if snap.flight: foot.append("✈ " + snap.flight)
    if snap.chp: foot.append("CHP " + snap.chp)
    if foot:
        d.text((8, fy), ("   ".join(foot))[:92], font=F_XS, fill=B)
    stamp = ("stale · " if snap.stale else "") + "upd " + snap.ts.strftime("%H:%M")
    d.text((WIDTH - d.textlength(stamp, font=F_XS) - 8, fy), stamp, font=F_XS, fill=B)
    return img


def _wrap(d, text, font, fill, x, y, maxw, lh=30, lines=3):
    words, line, out = text.split(), "", []
    for w in words:
        t = (line + " " + w).strip()
        if d.textlength(t, font=font) <= maxw:
            line = t
        else:
            out.append(line)
            line = w
    out.append(line)
    for i, ln in enumerate(out[:lines]):
        d.text((x, y + i * lh), ln, font=font, fill=fill)


# ----------------------------------------------------------------------------
# 7. ENCODERS  — exact bytes each consumer wants
# ----------------------------------------------------------------------------
def png_color(snap: Snapshot) -> bytes:
    """nova.png — served to the device. Pure 2-tone black/white (UI thresholded, no
    dither) re-expanded to RGB so ESPHome online_image (type: RGB) decodes it and the
    panel shows crisp B/W with zero color speckle on text edges."""
    out = to_mono_1bit(render(snap, "mono"), dither_photos=False).convert("RGB")
    buf = io.BytesIO(); out.save(buf, "PNG", optimize=True); return buf.getvalue()


def bmp_mono(snap: Snapshot) -> bytes:
    """1-bit BMP 800x480. Most reliable TRMNL device format."""
    out = to_mono_1bit(render(snap, "mono"))
    buf = io.BytesIO(); out.save(buf, "BMP"); return buf.getvalue()


def png_mono(snap: Snapshot) -> bytes:
    out = to_mono_1bit(render(snap, "mono"))
    buf = io.BytesIO(); out.save(buf, "PNG", optimize=True); return buf.getvalue()


# ----------------------------------------------------------------------------
# 8. HTTP SERVICE  (FastAPI)  — render-on-demand with a short TTL cache so the
#    panel pulls a fresh frame each wake, then deep-sleeps.
# ----------------------------------------------------------------------------
CACHE_TTL = 60            # seconds; renders are cheap, panel wakes are minutes apart
REFRESH_RATE = 900        # seconds the TRMNL device should sleep between pulls
_cache: dict[str, tuple[float, bytes]] = {}


def _cached(key: str, producer):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    data = producer(collect())
    _cache[key] = (now, data)
    return data


def build_app():
    from fastapi import FastAPI, Request, Response

    app = FastAPI(title="nova-eink")

    @app.get("/eink/nova.png")
    def nova_png():
        return Response(_cached("png", png_color), media_type="image/png")

    @app.get("/eink/nova.bmp")
    def nova_bmp():
        return Response(_cached("bmp", bmp_mono), media_type="image/bmp")

    @app.get("/eink/nova-mono.png")
    def nova_mono_png():
        return Response(_cached("mpng", png_mono), media_type="image/png")

    @app.get("/eink/health")
    def health():
        return {"ok": True, "w": WIDTH, "h": HEIGHT, "palette": [n for n, _, _ in SPECTRA6]}

    # ---- TRMNL BYOS shim -------------------------------------------------
    # Device sends header `ID` (MAC) and `Access-Token`. We point it at a
    # cache-busting filename so the ESP32 always re-downloads.
    @app.get("/api/setup")
    def trmnl_setup(request: Request):
        mac = request.headers.get("ID", "unknown")
        base = str(request.base_url).rstrip("/")
        return {
            "status": 200,
            "api_key": "nova-" + mac.replace(":", "").lower(),
            "friendly_id": "NOVA1",
            "image_url": f"{base}/eink/nova.bmp",
            "message": "Welcome to Nova",
        }

    @app.get("/api/display")
    def trmnl_display(request: Request):
        base = str(request.base_url).rstrip("/")
        stamp = int(time.time() // CACHE_TTL)          # changes each TTL window
        return {
            "status": 0,
            "image_url": f"{base}/eink/nova.bmp?v={stamp}",
            "filename": f"nova-{stamp}.bmp",            # changing name => device refreshes
            "refresh_rate": REFRESH_RATE,              # seconds to deep-sleep
            "update_firmware": False,
            "firmware_url": None,
            "reset_firmware": False,
            "special_function": "none",
        }

    @app.get("/api/log")
    @app.post("/api/log")
    def trmnl_log():
        return Response(status_code=204)

    return app


if __name__ == "__main__":
    import sys
    if "--render" in sys.argv:                          # offline smoke test
        snap = sample()
        open("/tmp/nova_color.png", "wb").write(png_color(snap))
        open("/tmp/nova_mono.bmp", "wb").write(bmp_mono(snap))
        print("wrote /tmp/nova_color.png and /tmp/nova_mono.bmp")
    else:
        import uvicorn
        uvicorn.run(build_app(), host="0.0.0.0", port=8073)
