#!/bin/zsh
# postgres-wrapper.sh — launchd entrypoint for postgresql@17 (Nova control-plane DB).
#
# Relocated here 2026-06-26: the original lived in /opt/homebrew/opt/postgresql@17/bin/
# and a Homebrew upgrade (17.9 -> 17.10) wiped that dir, deleting the wrapper and taking
# PostgreSQL — and therefore all of Nova — down with no way for launchd to restart it.
# This location survives brew upgrades. Point the plist's ProgramArguments here.
#
# LC_ALL/LANG pin the locale to dodge the Tahoe PG17 multithreaded-startup crash.

export LC_ALL=en_US.UTF-8
export LANG=en_US.UTF-8

DATADIR=/Volumes/MoreData/postgresql@17
PGBIN=/opt/homebrew/opt/postgresql@17/bin/postgres

# Boot-race guard: the data dir is on an external volume that can mount late.
# Mount-table check only (no `ls`) to avoid the launchd TCC trap on external volumes.
i=0
until /sbin/mount | /usr/bin/grep -q " on /Volumes/MoreData ("; do
    if [ "$i" -ge 60 ]; then
        echo "[pg-wrapper] /Volumes/MoreData not mounted after 180s — refusing to start" >&2
        exit 1
    fi
    /bin/sleep 3
    i=$((i + 1))
done

exec "$PGBIN" -D "$DATADIR"
