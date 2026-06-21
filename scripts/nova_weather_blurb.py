#!/usr/bin/env python3
"""
nova_weather_blurb.py — Live backyard weather-station snapshot for LOCAL articles.

Pulls the latest reading from telemetry.weather (Ambient Weather station @ 192.168.1.33,
fed by nova_weather_receiver.py) and formats it as a fact block to inject into journal
prompts so every `local`-section article opens with a real date/time/conditions dateline.

Rain is now LIVE (gauge confirmed working 2026-06-20) and included below.

Written by Jordan Koch.
"""
from datetime import datetime

PG_DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"

_FIELDS = ["ts", "temp_f", "feels_like_f", "humidity", "wind_speed_mph",
           "wind_gust_mph", "wind_dir", "pressure_in", "uv_index",
           "solar_radiation", "dew_point_f", "pm25",
           "rain_rate_in", "rain_daily_in"]


def weather_snapshot():
    """Latest non-rain weather reading as a dict, or None if unavailable/stale."""
    try:
        import psycopg2
        with psycopg2.connect(PG_DSN, connect_timeout=5) as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT " + ", ".join(_FIELDS) + " FROM telemetry.weather "
                    "ORDER BY ts DESC LIMIT 1"
                )
                row = cur.fetchone()
        if not row:
            return None
        snap = dict(zip(_FIELDS, row))
        # Guard against stale data (receiver down) — older than 1h is suspect.
        if snap["ts"] and (datetime.now(snap["ts"].tzinfo) - snap["ts"]).total_seconds() > 3600:
            snap["_stale"] = True
        return snap
    except Exception:
        return None


def _dir_name(deg):
    if deg is None:
        return ""
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((float(deg) % 360) / 22.5 + 0.5) % 16]


def _fmt(v, fmt):
    try:
        return format(float(v), fmt)
    except (TypeError, ValueError):
        return None


def weather_facts() -> str:
    """One-line human conditions string (incl. rain), or '' if unavailable."""
    w = weather_snapshot()
    if not w:
        return ""
    parts = []
    t = _fmt(w.get("temp_f"), ".0f")
    if t:
        parts.append(f"{t}°F")
    fl = _fmt(w.get("feels_like_f"), ".0f")
    if fl and fl != t:
        parts.append(f"feels like {fl}°F")
    h = _fmt(w.get("humidity"), ".0f")
    if h:
        parts.append(f"{h}% humidity")
    ws = _fmt(w.get("wind_speed_mph"), ".0f")
    if ws is not None:
        wind = f"wind {ws} mph {_dir_name(w.get('wind_dir'))}".strip()
        g = _fmt(w.get("wind_gust_mph"), ".0f")
        if g and float(g) > float(ws or 0):
            wind += f" (gusts {g})"
        parts.append(wind)
    p = _fmt(w.get("pressure_in"), ".2f")
    if p:
        parts.append(f"{p} inHg")
    uv = _fmt(w.get("uv_index"), ".0f")
    if uv:
        parts.append(f"UV {uv}")
    pm = _fmt(w.get("pm25"), ".0f")
    if pm:
        parts.append(f"PM2.5 {pm}")
    # Rain — only surface it when it's actually relevant (raining now or rained today)
    rate = _fmt(w.get("rain_rate_in"), ".2f")
    daily = _fmt(w.get("rain_daily_in"), ".2f")
    if rate and float(rate) > 0:
        parts.append(f"raining {rate} in/hr")
    elif daily and float(daily) > 0:
        parts.append(f'{daily}" rain today')
    return ", ".join(parts)


def weather_intro_context() -> str:
    """Prompt-injection block: instructs the model to open the article with an
    in-voice dateline using the live date/time + backyard station conditions."""
    now = datetime.now()
    dateline = now.strftime("%A, %B %d, %Y at %I:%M %p").replace(" 0", " ")
    facts = weather_facts()
    if not facts:
        return (f"OPEN THE ARTICLE with a brief, in-voice dateline noting it is {dateline}. "
                f"(Backyard weather-station data is unavailable right now — just give the date/time.)")
    return (
        "OPEN THE ARTICLE with a short, in-voice dateline blurb (1-2 sentences) that grounds "
        "the reader in the moment: the date, the local time, and the live reading from Jordan's "
        "backyard weather station in Burbank. Use these exact conditions (rain included when present):\n"
        f"  {dateline} — Burbank backyard station: {facts}."
    )


# ── Forecast (NWS — for daily local reports) ────────────────────────────────
# Backyard station reads CURRENT conditions only; the forecast comes from NWS.
BURBANK_LAT, BURBANK_LON = 34.1808, -118.3090


def weather_forecast() -> str:
    """Short NWS forecast for Burbank — next ~3 periods (today/tonight/tomorrow).
    Returns '' on any failure. NWS includes precip outlook (this is a forecast,
    distinct from the backyard station's rain sensor which is not yet configured)."""
    try:
        import json
        import urllib.request
        hdr = {"User-Agent": "Nova-Journal (nova.digitalnoise.net)",
               "Accept": "application/geo+json"}
        pts = urllib.request.Request(
            f"https://api.weather.gov/points/{BURBANK_LAT},{BURBANK_LON}", headers=hdr)
        meta = json.loads(urllib.request.urlopen(pts, timeout=10).read())
        furl = meta["properties"]["forecast"]
        fc = json.loads(urllib.request.urlopen(
            urllib.request.Request(furl, headers=hdr), timeout=10).read())
        periods = fc["properties"]["periods"][:3]
        return " ".join(
            f"{p['name']}: {p['shortForecast']}, {p['temperature']}°{p['temperatureUnit']}."
            for p in periods
        )
    except Exception:
        return ""


def weather_forecast_context() -> str:
    """Prompt block for DAILY local reports: instruct Nova to work in the forecast."""
    fc = weather_forecast()
    if not fc:
        return ""
    return ("ALSO WORK THE LOCAL WEATHER FORECAST into the piece naturally, in your voice "
            "(this is a daily local report — give readers the outlook):\n"
            f"  NWS Burbank forecast — {fc}")


def weather_dateline_line() -> str:
    """A clean, ready-to-render markdown dateline line for the TOP OF THE BODY
    (prepended by the publisher, NOT written by the model — so it never becomes
    the title). Italic: place, date, time, live backyard conditions incl. rain."""
    from datetime import datetime
    now = datetime.now()
    dl = now.strftime("%A, %B %d, %Y · %I:%M %p").replace(" 0", " ")
    f = weather_facts()
    return f"*Burbank · {dl}" + (f" · {f}" if f else "") + "*\n\n"


if __name__ == "__main__":
    print("snapshot:", weather_snapshot())
    print("dateline:", weather_dateline_line().strip())
    print("facts:", weather_facts())
    print("---\n" + weather_intro_context())
