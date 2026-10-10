#!/usr/bin/env python3
"""
nova_awn_poller.py — pull the Ambient AQIN indoor air-quality monitor (PM2.5/PM10/
CO2) from the Ambient Weather Network cloud API into telemetry.air_quality.

The AQIN is Wi-Fi-direct to AWN's cloud — it never passes through the local console
push that feeds nova_weather_receiver — so we read it from the AWN REST API instead.
Keys live in macOS Keychain (nova-ambient-api-key / nova-ambient-app-key); they are
never hardcoded. AWN rate limit: 1 req/sec per apiKey — a 5-min schedule is plenty.
"""
import json
import subprocess
import urllib.parse
import urllib.request

import psycopg2

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
API_URL = "https://api.ambientweather.net/v1/devices"


def _kc(service):
    import nova_config  # Keychain -> fleet store -> env (portable across the cluster)
    return nova_config._keychain(service, required=False)


def _f(d, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def main():
    app, api = _kc("nova-ambient-app-key"), _kc("nova-ambient-api-key")
    if not app or not api:
        print("[awn] missing AWN keys in Keychain")
        return 1
    url = f"{API_URL}?" + urllib.parse.urlencode({"applicationKey": app, "apiKey": api})
    req = urllib.request.Request(url, headers={"User-Agent": "Nova/1.0"})
    try:
        devices = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception as e:
        print(f"[awn] fetch failed: {type(e).__name__}: {str(e)[:120]}")
        return 1

    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    stored = 0
    for dev in devices:
        last = dev.get("lastData", {})
        pm25 = _f(last, "pm25_in_aqin")
        pm10 = _f(last, "pm10_in_aqin")
        co2 = _f(last, "co2_in_aqin")
        if pm25 is None and pm10 is None and co2 is None:
            continue  # this device has no AQIN attached
        aqi25 = _f(last, "aqi_pm25_aqin")
        aqi10 = _f(last, "aqi_pm10_aqin")
        temp_f = _f(last, "pm_in_temp_aqin")
        humidity = _f(last, "pm_in_humidity_aqin")
        batt = last.get("batt_co2")
        battery_ok = (str(batt) == "1") if batt is not None else None
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO telemetry.air_quality "
                "(ts, source, room, pm25, pm10, co2, aqi_pm25, aqi_pm10, temp_f, humidity, battery_ok) "
                "VALUES (now(), 'aqin', 'indoor', %s, %s, %s, %s, %s, %s, %s, %s)",
                (pm25, pm10, co2, aqi25, aqi10, temp_f, humidity, battery_ok))
        stored += 1
        print(f"[awn] AQIN: pm2.5={pm25} pm10={pm10} co2={co2}ppm aqi_pm25={aqi25} "
              f"temp={temp_f}F hum={humidity}% batt_ok={battery_ok}")
    conn.close()
    print(f"[awn] stored {stored} air-quality reading(s)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
