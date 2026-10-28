#!/bin/bash
# Keep the UNAS nas/Data share mounted over NFS at /Volumes/Data (same path the USB disk used; USB now at /Volumes/Data-usb via fstab) on the Studio (.6).
# Why NFS, not the SMB /Volumes/nas: the UNAS's Samba denies exec of anything on the share, so
# binaries/venvs/SwarmUI can't run from it. Export is managed in UniFi Drive (see memory
# unas-smb-no-exec-2026-10-25); it requires a reserved port, hence root + resvport.
# rsize/wsize 1M + readahead 128: macOS default 32K gave 31 MB/s uncached, this gives ~190 MB/s (2026-10-25).
# ponytail: 60s poll from launchd, no failover — Data has no replica to fail over to.
M=${1:-/Volumes/Data}   # arg overrides: .77 uses /Volumes/nas-data (its own disk owns /Volumes/Data)
SRC=192.168.1.19:/var/nfs/shared/nas/Data
# HEALTH = does statfs answer within 10 s (a dead hard NFS mount stays in the mount table and hangs).
# Not `ls`: under launchd, TCC network-volume protection denies listing the share even to root
# (EPERM), which made every 60 s run "repair" a healthy mount on all Macs (found 2026-10-27).
alive() { /bin/df "$M" >/dev/null 2>&1 & local p=$!
  for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 $p 2>/dev/null || { wait $p; return; }; sleep 1; done
  kill $p 2>/dev/null; return 1; }
if mount | grep -q " on $M (nfs" && alive; then exit 0; fi
mount | grep -q " on $M (nfs" && { echo "$(date '+%F %T') $M not answering, remounting"; umount -f "$M"; }
mkdir -p "$M"
mount -t nfs -o resvport,vers=3,rw,nosuid,hard,rsize=1048576,wsize=1048576,readahead=128 "$SRC" "$M" && echo "$(date '+%F %T') mounted $SRC"
