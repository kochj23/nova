#!/bin/zsh
# wait-for-volume.sh — Block until an external volume is mounted AND readable.
# After a reboot, /Volumes/Data and /Volumes/MoreData may mount late (or not at
# all). Services whose data/models/packages live on them must wait, or they
# crash on boot (Postgres data dir, MLX draft model, face-recognition packages).
# Usage: source wait-for-volume.sh; wait_for_volume "/Volumes/Data" 120
#        source wait-for-volume.sh; wait_for_volumes 120   # both canonical vols
# Written by Jordan Koch.

wait_for_volume() {
    local path="$1"
    # 600s (was 120s): the external /Volumes/Data can auto-mount several minutes late on a cold
    # boot. Services here all have launchd KeepAlive, so a patient wait lets them ride out a late
    # mount instead of FATAL-looping. Root cause of the mlx/tinychat "volume unavailable" FATALs.
    local timeout="${2:-600}"
    local elapsed=0

    # Ready = mount point is an actual mount AND its contents are listable
    # (a stale/empty /Volumes/X dir is NOT ready).
    # Absolute paths throughout — launchd starts jobs with a minimal/odd PATH,
    # so a bare `sleep` can fail to resolve and spin the loop to a false timeout.
    while ! { /sbin/mount | /usr/bin/grep -q " on ${path} (" && /bin/ls "${path}" >/dev/null 2>&1; }; do
        if [ "$elapsed" -ge "$timeout" ]; then
            echo "[wait-for-volume] TIMEOUT: ${path} not mounted/readable after ${timeout}s" >&2
            return 1
        fi
        /bin/sleep 3
        elapsed=$((elapsed + 3))
    done
    echo "[wait-for-volume] ${path} mounted and readable after ${elapsed}s"
    return 0
}

wait_for_volumes() {
    # Convenience: wait for both canonical Nova data volumes.
    local timeout="${1:-600}"
    wait_for_volume "/Volumes/Data" "$timeout" || return 1
    wait_for_volume "/Volumes/MoreData" "$timeout" || return 1
    return 0
}
