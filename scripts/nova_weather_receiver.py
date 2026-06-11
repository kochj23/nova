#!/opt/homebrew/bin/python3
"""
nova_weather_receiver.py — Ecowitt protocol HTTP receiver for Ambient Weather station.

Listens on port 8087 for POST /data/report/ from the weather station at 192.168.1.33.
Parses form-urlencoded data and inserts into telemetry.weather (nova_ops).

On startup, configures the weather station to push data to this receiver.

Written by Jordan Koch.
"""

import sys
sys.path.insert(0, str(Path.home()) + "/.openclaw/scripts")

import logging
import math
import os
import signal
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from threading import Lock

import psycopg2

import nova_config

# ── Configuration ─────────────────────────────────────────────────────────────

LISTEN_PORT = 8087
STATION_IP = "192.168.1.33"
RECEIVER_IP = "192.168.1.6"
LOG_PATH = str(Path.home()) + "/.openclaw/logs/weather_receiver.log"
DB_DSN = "host=localhost dbname=nova_ops user=kochj"

# ── Logging ───────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("weather_receiver")

# ── State ─────────────────────────────────────────────────────────────────────

_state_lock = Lock()
_state = {
    "last_reading_ts": None,
    "reading_count": 0,
    "started_at": datetime.now(timezone.utc).isoformat(),
}


# ── Weather Calculations ──────────────────────────────────────────────────────

def calc_dew_point(temp_f: float, humidity: float) -> float:
    """Calculate dew point in Fahrenheit using Magnus-Tetens approximation."""
    temp_c = (temp_f - 32.0) * 5.0 / 9.0
    a, b = 17.27, 237.7
    alpha = (a * temp_c) / (b + temp_c) + math.log(humidity / 100.0)
    dew_c = (b * alpha) / (a - alpha)
    return dew_c * 9.0 / 5.0 + 32.0


def calc_heat_index(temp_f: float, humidity: float) -> float:
    """Calculate heat index in Fahrenheit (Rothfusz regression)."""
    if temp_f < 80.0:
        # Simple formula for low temps
        return 0.5 * (temp_f + 61.0 + ((temp_f - 68.0) * 1.2) + (humidity * 0.094))

    hi = (
        -42.379
        + 2.04901523 * temp_f
        + 10.14333127 * humidity
        - 0.22475541 * temp_f * humidity
        - 0.00683783 * temp_f ** 2
        - 0.05481717 * humidity ** 2
        + 0.00122874 * temp_f ** 2 * humidity
        + 0.00085282 * temp_f * humidity ** 2
        - 0.00000199 * temp_f ** 2 * humidity ** 2
    )

    # Low humidity adjustment
    if humidity < 13.0 and 80.0 <= temp_f <= 112.0:
        hi -= ((13.0 - humidity) / 4.0) * math.sqrt((17.0 - abs(temp_f - 95.0)) / 17.0)
    # High humidity adjustment
    elif humidity > 85.0 and 80.0 <= temp_f <= 87.0:
        hi += ((humidity - 85.0) / 10.0) * ((87.0 - temp_f) / 5.0)

    return hi


# ── Database ──────────────────────────────────────────────────────────────────

def get_db_conn():
    """Get a psycopg2 connection. Caller should handle exceptions."""
    return psycopg2.connect(DB_DSN)


def insert_reading(data: dict) -> bool:
    """Insert a weather reading into telemetry.weather. Returns True on success."""
    try:
        temp_f = _float(data.get("tempf"))
        humidity = _int(data.get("humidity"))
        pressure_in = _float(data.get("baromrelin"))
        wind_speed_mph = _float(data.get("windspeedmph"))
        wind_dir = _int(data.get("winddir"))
        wind_gust_mph = _float(data.get("windgustmph"))
        rain_rate_in = _float(data.get("rainratein"))
        rain_daily_in = _float(data.get("dailyrainin"))
        rain_weekly_in = _float(data.get("weeklyrainin"))
        rain_monthly_in = _float(data.get("monthlyrainin"))
        rain_yearly_in = _float(data.get("yearlyrainin"))
        solar_radiation = _float(data.get("solarradiation"))
        uv_index = _float(data.get("uv"))
        temp_indoor_f = _float(data.get("tempinf"))
        humidity_indoor = _int(data.get("humidityin"))
        pm25 = _float(data.get("pm25_ch1"))

        # Dew point: use provided or calculate
        dew_point_f = _float(data.get("dewpointf"))
        if dew_point_f is None and temp_f is not None and humidity is not None:
            dew_point_f = round(calc_dew_point(temp_f, humidity), 1)

        # Heat index: use provided or calculate
        heat_index_f = _float(data.get("heatindexf"))
        if heat_index_f is None and temp_f is not None and humidity is not None:
            heat_index_f = round(calc_heat_index(temp_f, humidity), 1)

        # Feels like: use provided value
        feels_like_f = _float(data.get("feelslikef"))

        # Timestamp from dateutc or now
        ts = datetime.now(timezone.utc)
        dateutc = data.get("dateutc", "")
        if dateutc and dateutc != "now":
            try:
                ts = datetime.strptime(dateutc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        conn = get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO telemetry.weather (
                        ts, temp_f, humidity, pressure_in, wind_speed_mph, wind_dir,
                        wind_gust_mph, rain_rate_in, rain_daily_in, rain_weekly_in,
                        rain_monthly_in, rain_yearly_in, solar_radiation, uv_index,
                        temp_indoor_f, humidity_indoor, pm25, dew_point_f,
                        heat_index_f, feels_like_f
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s
                    )
                """, (
                    ts, temp_f, humidity, pressure_in, wind_speed_mph, wind_dir,
                    wind_gust_mph, rain_rate_in, rain_daily_in, rain_weekly_in,
                    rain_monthly_in, rain_yearly_in, solar_radiation, uv_index,
                    temp_indoor_f, humidity_indoor, pm25, dew_point_f,
                    heat_index_f, feels_like_f,
                ))
            conn.commit()
        finally:
            conn.close()

        with _state_lock:
            _state["last_reading_ts"] = ts.isoformat()
            _state["reading_count"] += 1

        return True

    except Exception as e:
        log.error(f"DB insert failed: {e}")
        try:
            nova_config.post_both(
                f":warning: Weather receiver DB insert failed: {e}",
                slack_channel=nova_config.SLACK_BB,
            )
        except Exception:
            pass
        return False


def _float(val) -> float | None:
    """Convert to float or return None."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _int(val) -> int | None:
    """Convert to int or return None."""
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class WeatherHandler(BaseHTTPRequestHandler):
    """Handle Ecowitt protocol POSTs and health check GETs."""

    def log_message(self, format, *args):
        """Override to use our logger instead of stderr."""
        log.debug(f"HTTP: {format % args}")

    def do_POST(self):
        if "/data/report" not in self.path and "/weatherstation" not in self.path:
            log.warning(f"POST to unknown path: {self.path}")
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No data")
            return

        body = self.rfile.read(content_length)
        try:
            data = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
            # parse_qs returns lists; flatten to single values
            flat = {k: v[0] if v else "" for k, v in data.items()}
        except Exception as e:
            log.error(f"Failed to parse POST body: {e}")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Parse error")
            return

        if not flat.get('tempf') and flat:
            log.warning(f"Unknown field names received. Keys: {sorted(flat.keys())}")

        log.info(
            f"Reading: temp={flat.get('tempf', '?')}F "
            f"humidity={flat.get('humidity', '?')}% "
            f"wind={flat.get('windspeedmph', '?')}mph "
            f"rain_rate={flat.get('rainratein', '?')}in/hr "
            f"solar={flat.get('solarradiation', '?')}W/m2"
        )

        success = insert_reading(flat)

        self.send_response(200 if success else 500)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK" if success else b"DB Error")

    def do_GET(self):
        if self.path.startswith("/health"):
            import json
            with _state_lock:
                payload = json.dumps({
                    "status": "ok",
                    "started_at": _state["started_at"],
                    "last_reading_ts": _state["last_reading_ts"],
                    "reading_count": _state["reading_count"],
                    "uptime_seconds": int(
                        (datetime.now(timezone.utc) - datetime.fromisoformat(_state["started_at"])).total_seconds()
                    ),
                })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode())
        elif "?" in self.path or "&" in self.path:
            # Ambient Weather sends: /data/report/&KEY=val&KEY=val (& instead of ?)
            # Also handles standard ?KEY=val format
            raw = self.path
            if "?" in raw:
                query_string = raw.split("?", 1)[1]
            elif "&" in raw:
                query_string = raw.split("&", 1)[1]
            else:
                query_string = ""
            data = urllib.parse.parse_qs(query_string, keep_blank_values=True)
            flat = {k: v[0] if v else "" for k, v in data.items()}
            if flat:
                log.info(
                    f"Weather: temp={flat.get('tempf', '?')}F "
                    f"humidity={flat.get('humidity', '?')}% "
                    f"wind={flat.get('windspeedmph', '?')}mph "
                    f"solar={flat.get('solarradiation', '?')}W/m2 "
                    f"({len(flat)} fields)"
                )
                if not flat.get('tempf') and flat:
                    log.warning(f"No tempf! Field names: {sorted(flat.keys())[:20]}")
                insert_reading(flat)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")


# ── Station Configuration ─────────────────────────────────────────────────────

def configure_station():
    """
    POST to the weather station to configure custom server settings.
    Uses Ambient Weather protocol (WU-compatible GET with query params).
    """
    url = f"http://{STATION_IP}/set_ws_settings"
    params = {
        "Customized": "enable",
        "Protocol": "amb_protocol",
        "ecowitt_ip": RECEIVER_IP,
        "ecowitt_port": str(LISTEN_PORT),
        "ecowitt_path": "/data/report/",
        "ecowitt_upload": "16",
        "usr_wu_path": "/weatherstation/updateweatherstation.php?",
        "usr_wu_port": str(LISTEN_PORT),
        "usr_wu_upload": "16",
    }

    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            reply = resp.read().decode("utf-8", errors="replace")
            log.info(f"Station config response ({status}): {reply[:200]}")
            if status == 200:
                log.info("Weather station configured successfully")
            else:
                log.warning(f"Station config returned HTTP {status}")
    except Exception as e:
        log.warning(f"Could not configure station at {STATION_IP}: {e}")
        log.warning("Station may need manual configuration via its web UI")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info(f"Nova Weather Receiver starting on port {LISTEN_PORT}")
    log.info(f"Expecting data from station at {STATION_IP}")
    log.info(f"Database: {DB_DSN}")

    # Verify DB connectivity
    try:
        conn = get_db_conn()
        conn.close()
        log.info("Database connection verified")
    except Exception as e:
        log.error(f"Cannot connect to database: {e}")
        nova_config.post_both(
            f":x: Weather receiver cannot connect to DB: {e}",
            slack_channel=nova_config.SLACK_BB,
        )
        sys.exit(1)

    # Configure station to push to us
    configure_station()

    # Start threaded HTTP server (prevents single stuck connection from blocking)
    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedHTTPServer(("0.0.0.0", LISTEN_PORT), WeatherHandler)

    def _shutdown(signum, frame):
        log.info(f"Received signal {signum}, shutting down")
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info(f"Listening on 0.0.0.0:{LISTEN_PORT} — POST /data/report/ | GET /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        log.info("Weather receiver stopped")


if __name__ == "__main__":
    main()
