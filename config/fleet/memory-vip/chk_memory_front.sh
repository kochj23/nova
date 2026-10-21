#!/bin/sh
# keepalived track script for the memory-server VIP (queue #3575, 2026-10-21).
# Non-zero -> this node drops to FAULT and releases 192.168.1.240.
# Drill / maintenance: `sudo touch /etc/keepalived/maintenance` hands the VIP to the peer; rm to take it back.
[ -e /etc/keepalived/maintenance ] && exit 1
exec /usr/bin/curl -s -o /dev/null -m 2 http://127.0.0.1:18790/health
