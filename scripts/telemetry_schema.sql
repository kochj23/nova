-- ============================================================
-- Telemetry Schema for nova_ops
-- Home telemetry time-series tables with range partitioning
-- Created: 2026-06-09
-- ============================================================

BEGIN;

-- Create the schema
CREATE SCHEMA IF NOT EXISTS telemetry;

COMMENT ON SCHEMA telemetry IS 'Home telemetry time-series data. Retention policy: raw data 90 days, materialized view aggregates kept forever.';

-- ============================================================
-- 1. telemetry.weather — Ecowitt push data (16-60s interval)
-- ============================================================
CREATE TABLE telemetry.weather (
    ts              timestamptz NOT NULL,
    temp_f          real,
    humidity        int,
    pressure_in     real,
    wind_speed_mph  real,
    wind_dir        int,
    wind_gust_mph   real,
    rain_rate_in    real,
    rain_daily_in   real,
    rain_weekly_in  real,
    rain_monthly_in real,
    rain_yearly_in  real,
    solar_radiation real,
    uv_index        real,
    temp_indoor_f   real,
    humidity_indoor  int,
    pm25            real,
    dew_point_f     real,
    heat_index_f    real,
    feels_like_f    real
) PARTITION BY RANGE (ts);

CREATE INDEX idx_weather_ts ON telemetry.weather (ts);

-- Partitions
CREATE TABLE telemetry.weather_202606 PARTITION OF telemetry.weather
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE telemetry.weather_202607 PARTITION OF telemetry.weather
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

-- ============================================================
-- 2. telemetry.av_state — Onkyo + Bose state snapshots
-- ============================================================
CREATE TABLE telemetry.av_state (
    ts              timestamptz NOT NULL,
    device_id       text NOT NULL,
    device_type     text NOT NULL,
    power           bool,
    volume          int,
    mute            bool,
    source_input    text,
    listening_mode  text,
    media_title     text,
    zone            text DEFAULT 'main'
) PARTITION BY RANGE (ts);

CREATE INDEX idx_av_state_ts ON telemetry.av_state (ts);
CREATE INDEX idx_av_state_device_ts ON telemetry.av_state (device_id, ts);

-- Partitions
CREATE TABLE telemetry.av_state_202606 PARTITION OF telemetry.av_state
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE telemetry.av_state_202607 PARTITION OF telemetry.av_state
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

-- ============================================================
-- 3. telemetry.energy — Eve Energy strip readings (60s)
-- ============================================================
CREATE TABLE telemetry.energy (
    ts              timestamptz NOT NULL,
    device_id       text NOT NULL,
    device_name     text,
    watts           real,
    volts           real,
    amps            real,
    kwh_total       real,
    on_state        bool
) PARTITION BY RANGE (ts);

CREATE INDEX idx_energy_ts ON telemetry.energy (ts);
CREATE INDEX idx_energy_device_ts ON telemetry.energy (device_id, ts);

-- Partitions
CREATE TABLE telemetry.energy_202606 PARTITION OF telemetry.energy
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE telemetry.energy_202607 PARTITION OF telemetry.energy
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

-- ============================================================
-- 4. telemetry.network — UniFi per-client stats (5min)
-- ============================================================
CREATE TABLE telemetry.network (
    ts              timestamptz NOT NULL,
    client_mac      text NOT NULL,
    client_name     text,
    ip              text,
    rx_bytes        bigint,
    tx_bytes        bigint,
    signal_dbm      int,
    channel         int,
    radio           text,
    uptime_s        bigint,
    is_wired        bool
) PARTITION BY RANGE (ts);

CREATE INDEX idx_network_ts ON telemetry.network (ts);
CREATE INDEX idx_network_client_ts ON telemetry.network (client_mac, ts);

-- Partitions
CREATE TABLE telemetry.network_202606 PARTITION OF telemetry.network
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE telemetry.network_202607 PARTITION OF telemetry.network
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

-- ============================================================
-- 5. telemetry.climate — Per-room climate readings (2min)
-- ============================================================
CREATE TABLE telemetry.climate (
    ts              timestamptz NOT NULL,
    room            text NOT NULL,
    source          text NOT NULL,
    temp_f          real,
    humidity        int,
    light_lux       real,
    motion          bool
) PARTITION BY RANGE (ts);

CREATE INDEX idx_climate_ts ON telemetry.climate (ts);
CREATE INDEX idx_climate_room_ts ON telemetry.climate (room, ts);
CREATE INDEX idx_climate_source_ts ON telemetry.climate (source, ts);

-- Partitions
CREATE TABLE telemetry.climate_202606 PARTITION OF telemetry.climate
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE telemetry.climate_202607 PARTITION OF telemetry.climate
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

-- ============================================================
-- 6. telemetry.nova_meta — Nova system metrics (5min)
-- ============================================================
CREATE TABLE telemetry.nova_meta (
    ts              timestamptz NOT NULL,
    metric          text NOT NULL,
    value           real,
    metadata        jsonb
) PARTITION BY RANGE (ts);

CREATE INDEX idx_nova_meta_ts ON telemetry.nova_meta (ts);
CREATE INDEX idx_nova_meta_metric_ts ON telemetry.nova_meta (metric, ts);

-- Partitions
CREATE TABLE telemetry.nova_meta_202606 PARTITION OF telemetry.nova_meta
    FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE telemetry.nova_meta_202607 PARTITION OF telemetry.nova_meta
    FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

-- ============================================================
-- 7. telemetry.device_power_events — AV on/off transitions
--    (Not partitioned — low volume event table)
-- ============================================================
CREATE TABLE telemetry.device_power_events (
    ts              timestamptz NOT NULL,
    device_id       text NOT NULL,
    event           text NOT NULL,
    source_input    text
);

CREATE INDEX idx_device_power_events_ts ON telemetry.device_power_events (ts);
CREATE INDEX idx_device_power_events_device_ts ON telemetry.device_power_events (device_id, ts);

-- ============================================================
-- Materialized Views
-- ============================================================

-- Weather daily aggregates
CREATE MATERIALIZED VIEW telemetry.weather_daily AS
SELECT
    date_trunc('day', ts)::date AS day,
    min(temp_f)             AS temp_min_f,
    max(temp_f)             AS temp_max_f,
    avg(temp_f)::real       AS temp_avg_f,
    sum(rain_daily_in)      AS rain_total_in,
    max(wind_speed_mph)     AS wind_max_mph,
    max(wind_gust_mph)      AS gust_max_mph,
    avg(pressure_in)::real  AS pressure_avg_in,
    avg(humidity)::real     AS humidity_avg,
    max(solar_radiation)    AS solar_max,
    max(uv_index)           AS uv_max
FROM telemetry.weather
GROUP BY 1
ORDER BY 1;

CREATE UNIQUE INDEX idx_weather_daily_day ON telemetry.weather_daily (day);

-- Energy hourly aggregates (per device)
CREATE MATERIALIZED VIEW telemetry.energy_hourly AS
SELECT
    date_trunc('hour', ts)  AS hour,
    device_id,
    device_name,
    avg(watts)::real        AS avg_watts,
    max(watts)              AS max_watts,
    (max(kwh_total) - min(kwh_total))::real AS kwh_delta
FROM telemetry.energy
GROUP BY 1, 2, 3
ORDER BY 1, 2;

CREATE UNIQUE INDEX idx_energy_hourly_device_hour ON telemetry.energy_hourly (hour, device_id);

-- Network hourly aggregates (per client)
CREATE MATERIALIZED VIEW telemetry.network_hourly AS
SELECT
    date_trunc('hour', ts)  AS hour,
    client_mac,
    client_name,
    max(rx_bytes) - min(rx_bytes) AS rx_bytes_delta,
    max(tx_bytes) - min(tx_bytes) AS tx_bytes_delta,
    avg(signal_dbm)::real   AS avg_signal_dbm
FROM telemetry.network
GROUP BY 1, 2, 3
ORDER BY 1, 2;

CREATE UNIQUE INDEX idx_network_hourly_client_hour ON telemetry.network_hourly (hour, client_mac);

-- ============================================================
-- Retention policy comments
-- ============================================================
COMMENT ON TABLE telemetry.weather IS 'Ecowitt weather station push data (16-60s). Retention: 90 days raw, then purge. Partitioned by month.';
COMMENT ON TABLE telemetry.av_state IS 'AV receiver/speaker state snapshots. Retention: 90 days raw. Partitioned by month.';
COMMENT ON TABLE telemetry.energy IS 'Eve Energy strip power readings (60s). Retention: 90 days raw. Partitioned by month.';
COMMENT ON TABLE telemetry.network IS 'UniFi per-client network stats (5min). Retention: 90 days raw. Partitioned by month.';
COMMENT ON TABLE telemetry.climate IS 'Per-room climate from Hue/HomePod/weather (2min). Retention: 90 days raw. Partitioned by month.';
COMMENT ON TABLE telemetry.nova_meta IS 'Nova system metrics (5min). Retention: 90 days raw. Partitioned by month.';
COMMENT ON TABLE telemetry.device_power_events IS 'AV power transitions for uptime tracking. Retention: 90 days raw.';
COMMENT ON MATERIALIZED VIEW telemetry.weather_daily IS 'Daily weather aggregates. Retention: forever.';
COMMENT ON MATERIALIZED VIEW telemetry.energy_hourly IS 'Hourly per-device energy aggregates. Retention: forever.';
COMMENT ON MATERIALIZED VIEW telemetry.network_hourly IS 'Hourly per-client network aggregates. Retention: forever.';

COMMIT;
