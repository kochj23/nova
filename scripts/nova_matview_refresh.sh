#!/bin/bash
# Refresh Grafana-backing materialized views (energy_hourly, weather_daily).
# They have no other refresher and pg_cron is not installed; this keeps the
# nova-energy "Daily kWh" and nova-weather "Daily Summary" panels current.
# Created 2026-09-08 after both went stale (43d / 3mo).
export PATH="/opt/homebrew/bin:/usr/bin:/bin"
LOG=~/.openclaw/logs/nova-matview-refresh.log
for mv in telemetry.energy_hourly telemetry.weather_daily; do
  if psql -h localhost -U kochj -d nova_ops -c "REFRESH MATERIALIZED VIEW $mv;" >/dev/null 2>>"$LOG"; then
    echo "[$(date '+%F %T')] refreshed $mv" >> "$LOG"
  else
    echo "[$(date '+%F %T')] FAILED $mv" >> "$LOG"
  fi
done
