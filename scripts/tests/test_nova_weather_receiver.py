"""
test_nova_weather_receiver.py — Tests for the Ecowitt HTTP -> Postgres receiver.

FOCUS: untrusted Ecowitt HTTP POST -> PG.
  Unit:     field parse/validation (_float / _int / dew point / heat index).
  Security: missing / garbage / oversized / injection-y fields must be rejected
            (coerced to None) or coerced to a safe numeric type — never inserted
            raw — and the SQL must be a static parameterized statement.

All DB access is mocked; no live service is touched.

Written by Jordan Koch.
"""

import math
from unittest.mock import MagicMock, patch

import pytest

import nova_weather_receiver as w


# ── Helpers ───────────────────────────────────────────────────────────────────

class RecordingCursor:
    """Minimal cursor that records execute/executemany calls."""

    def __init__(self):
        self.calls = []          # list of (sql, params) from execute()
        self.many = []           # list of (sql, seq) from executemany()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def executemany(self, sql, seq):
        self.many.append((sql, list(seq)))


def _fake_conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


def _capture_insert(data):
    """Run insert_reading with a mocked DB; return (result, cursor)."""
    cur = RecordingCursor()
    with patch.object(w, "get_db_conn", return_value=_fake_conn(cur)):
        result = w.insert_reading(data)
    return result, cur


# Column order of the INSERT INTO telemetry.weather statement (params tuple).
WEATHER_COLS = [
    "ts", "temp_f", "humidity", "pressure_in", "wind_speed_mph", "wind_dir",
    "wind_gust_mph", "rain_rate_in", "rain_daily_in", "rain_weekly_in",
    "rain_monthly_in", "rain_yearly_in", "solar_radiation", "uv_index",
    "temp_indoor_f", "humidity_indoor", "pm25", "dew_point_f",
    "heat_index_f", "feels_like_f", "pm10", "co2",
]


def _weather_params(cur):
    """Return the params tuple from the weather INSERT as a name->value dict."""
    for sql, params in cur.calls:
        if "telemetry.weather" in sql:
            return dict(zip(WEATHER_COLS, params))
    raise AssertionError("no telemetry.weather insert recorded")


# ── Unit: _float ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("val,expected", [
    ("3.5", 3.5),
    ("0", 0.0),
    ("-12.25", -12.25),
    ("1e3", 1000.0),
    ("  7.5  ", 7.5),
    (42, 42.0),
    (3.14, 3.14),
])
def test_float_valid(val, expected):
    assert w._float(val) == expected


@pytest.mark.parametrize("val", [None, "", "abc", "12abc", "NaNaN", [], {}, "0x10"])
def test_float_garbage_returns_none(val):
    assert w._float(val) is None


def test_float_never_raises():
    # No untrusted input should ever blow up the coercer.
    for junk in ["'; DROP TABLE weather;--", "\x00\xff", "∞", "1,000", "true"]:
        assert w._float(junk) is None


# ── Unit: _int ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("val,expected", [
    ("5", 5),
    ("5.9", 5),      # truncates via float()
    ("-3.7", -3),
    (7.99, 7),
    ("  42 ", 42),
])
def test_int_valid(val, expected):
    assert w._int(val) == expected


@pytest.mark.parametrize("val", [None, "", "abc", "1/2", [], "0xFF"])
def test_int_garbage_returns_none(val):
    assert w._int(val) is None


# ── Unit: weather calculations ────────────────────────────────────────────────

def test_dew_point_reasonable():
    # Dew point must be <= temperature.
    dp = w.calc_dew_point(80.0, 50.0)
    assert dp < 80.0
    assert 55.0 < dp < 62.0


def test_dew_point_saturation():
    # 100% humidity => dew point ~= temperature.
    dp = w.calc_dew_point(70.0, 100.0)
    assert abs(dp - 70.0) < 0.5


def test_heat_index_low_branch():
    # Below 80F uses the simple formula (no Rothfusz regression).
    hi = w.calc_heat_index(70.0, 50.0)
    assert isinstance(hi, float)
    assert 60.0 < hi < 80.0


def test_heat_index_high_branch():
    # Hot + humid => heat index above the actual temperature.
    hi = w.calc_heat_index(95.0, 60.0)
    assert hi > 95.0


def test_heat_index_low_humidity_adjustment():
    # Exercises the low-humidity correction branch without raising.
    hi = w.calc_heat_index(100.0, 10.0)
    assert isinstance(hi, float) and not math.isnan(hi)


# ── Security invariant: garbage / injection never inserted raw ────────────────

def test_injection_string_coerced_to_none():
    payload = {
        "tempf": "'; DROP TABLE telemetry.weather;--",
        "humidity": "<script>alert(1)</script>",
        "windspeedmph": "$(rm -rf /)",
        "baromrelin": "NULL))--",
    }
    ok, cur = _capture_insert(payload)
    assert ok is True
    p = _weather_params(cur)
    # Every malicious string field became None, not the raw string.
    assert p["temp_f"] is None
    assert p["humidity"] is None
    assert p["wind_speed_mph"] is None
    assert p["pressure_in"] is None


def test_sql_is_static_parameterized():
    # The executed SQL text must contain only %s placeholders and no
    # interpolated attacker data.
    payload = {"tempf": "'; DROP TABLE x;--", "humidity": "50"}
    _, cur = _capture_insert(payload)
    sql, params = next(c for c in cur.calls if "telemetry.weather" in c[0])
    assert "DROP TABLE" not in sql
    assert "'; " not in sql
    assert "%s" in sql
    # Attacker data is confined to the params tuple, never the SQL string.
    assert isinstance(params, tuple)


def test_all_values_are_safe_types():
    # Whatever the input, every bound param is None or a numeric/datetime —
    # never a raw untrusted str.
    from datetime import datetime
    payload = {k: "garbage!!" for k in [
        "tempf", "humidity", "baromrelin", "windspeedmph", "winddir",
        "windgustmph", "rainratein", "dailyrainin", "solarradiation", "uv",
    ]}
    payload["tempf"] = "72.5"      # one legit value
    _, cur = _capture_insert(payload)
    p = _weather_params(cur)
    for col, val in p.items():
        if col == "ts":
            assert isinstance(val, datetime)
        else:
            assert val is None or isinstance(val, (int, float)), f"{col}={val!r}"


def test_oversized_numeric_stays_numeric():
    # A giant but numeric string is coerced to a float (safe type), not kept as str.
    huge = "9" * 400
    ok, cur = _capture_insert({"tempf": huge, "humidity": "50"})
    assert ok is True
    p = _weather_params(cur)
    assert isinstance(p["temp_f"], float)


def test_missing_fields_all_none_still_inserts():
    ok, cur = _capture_insert({})   # empty POST body
    assert ok is True
    p = _weather_params(cur)
    # No optional field is fabricated; dew/heat need temp+humidity so stay None.
    assert p["temp_f"] is None
    assert p["humidity"] is None
    assert p["dew_point_f"] is None
    assert p["heat_index_f"] is None


# ── Field-mapping / parse logic ───────────────────────────────────────────────

def test_dew_point_calculated_when_absent():
    ok, cur = _capture_insert({"tempf": "80", "humidity": "50"})
    assert ok is True
    p = _weather_params(cur)
    assert p["dew_point_f"] is not None
    assert p["dew_point_f"] == round(w.calc_dew_point(80.0, 50), 1)


def test_dew_point_uses_provided_value():
    ok, cur = _capture_insert({"tempf": "80", "humidity": "50", "dewpointf": "61.2"})
    assert ok is True
    assert _weather_params(cur)["dew_point_f"] == 61.2


def test_pm25_fallback_chain():
    # pm25 falls back through pm25_aqin then pm25_ch1.
    _, cur = _capture_insert({"pm25_ch1": "12.3"})
    assert _weather_params(cur)["pm25"] == 12.3
    _, cur2 = _capture_insert({"pm25_aqin": "7.0", "pm25_ch1": "99"})
    assert _weather_params(cur2)["pm25"] == 7.0
    _, cur3 = _capture_insert({"pm25": "3.0", "pm25_aqin": "99"})
    assert _weather_params(cur3)["pm25"] == 3.0


def test_bad_dateutc_falls_back_to_now_not_crash():
    from datetime import datetime, timezone
    before = datetime.now(timezone.utc)
    ok, cur = _capture_insert({"tempf": "70", "dateutc": "not-a-date"})
    assert ok is True
    ts = _weather_params(cur)["ts"]
    assert isinstance(ts, datetime)
    assert ts >= before


def test_valid_dateutc_parsed():
    from datetime import datetime, timezone
    ok, cur = _capture_insert({"tempf": "70", "dateutc": "2026-01-02 03:04:05"})
    assert ok is True
    ts = _weather_params(cur)["ts"]
    assert ts == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_dateutc_now_uses_current_time():
    from datetime import datetime
    _, cur = _capture_insert({"tempf": "70", "dateutc": "now"})
    assert isinstance(_weather_params(cur)["ts"], datetime)


# ── insert_soil ───────────────────────────────────────────────────────────────

def test_soil_only_present_sensors():
    from datetime import datetime, timezone
    cur = RecordingCursor()
    ts = datetime.now(timezone.utc)
    with patch.object(w, "get_db_conn", return_value=_fake_conn(cur)):
        w.insert_soil({"soilhum1": "40", "soilhum3": "55", "soilbatt1": "1.5"}, ts)
    assert len(cur.many) == 1
    sql, rows = cur.many[0]
    assert "telemetry.soil" in sql and "%s" in sql
    sensors = {r[1] for r in rows}
    assert sensors == {"soil1", "soil3"}
    # moisture coerced to float; battery of soil3 (absent) is None.
    by_sensor = {r[1]: r for r in rows}
    assert by_sensor["soil1"][2] == 40.0
    assert by_sensor["soil1"][3] == 1.5
    assert by_sensor["soil3"][3] is None


def test_soil_no_sensors_no_db_call():
    cur = RecordingCursor()
    conn = _fake_conn(cur)
    with patch.object(w, "get_db_conn", return_value=conn) as gdc:
        w.insert_soil({"tempf": "70"}, None)
    assert gdc.call_count == 0
    assert cur.many == []


def test_soil_garbage_moisture_skipped():
    from datetime import datetime, timezone
    cur = RecordingCursor()
    with patch.object(w, "get_db_conn", return_value=_fake_conn(cur)):
        w.insert_soil({"soilhum1": "garbage"}, datetime.now(timezone.utc))
    # garbage moisture => _float None => sensor skipped => no insert
    assert cur.many == []


# ── Failure handling (no retry loop exists; assert graceful failure) ──────────

def test_insert_reading_returns_false_on_db_error():
    # DB blows up -> function must swallow, alert, and return False (not raise).
    with patch.object(w, "get_db_conn", side_effect=RuntimeError("db down")), \
         patch.object(w.nova_config, "post_both") as post:
        ok = w.insert_reading({"tempf": "70", "humidity": "50"})
    assert ok is False
    # Best-effort Slack alert attempted.
    assert post.called
