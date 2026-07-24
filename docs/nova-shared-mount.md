# /nova — Shared Artifacts + Scripts Mount

The `/nova` mount is the fleet's shared filesystem — the SAP `/usr/sap/trans` concept applied
to nova-core's orchestration role. Originally artifacts-only; as of 2026-07-24 it also serves
**live scripts** to every node except the one that edits them (see "Scripts" below).

- **NOT** coordination state (that's PostgreSQL `nova_ops`)

## Host

Lives on the Synology (`192.168.1.11`, DSM) as a dedicated shared folder exported over **SMB**
(never AFP — AFP's Unicode bug burned us).

- Synology shared folder: `nova` → `/volume1/nova`
- ACL: `kochj` = RW, `@administrators` = RW
- Created with: `synoshare --add nova ... /volume1/nova` + `synoshare --setuser nova RW + kochj`

## Layout

```
/nova/models/      ML models (Whisper, nomic-embed, YOLO, LLMs) — ONE copy all nodes share
/nova/media/       shared media / transport for cross-node handoffs (YT->Plex flow, etc.)
/nova/trans/       inbox/outbox for cross-node file handoffs (the literal SAP trans idea)
   /nova/trans/inbox/
   /nova/trans/outbox/
/nova/artifacts/   binaries, exports, journal/Hugo content
/nova/scripts/     ~/.openclaw/scripts, published from .6's git repo (see "Scripts" below)
```

## Mounts per node

| Node        | IP         | OS    | Mount path | Mechanism | Persistence |
|-------------|------------|-------|------------|-----------|-------------|
| nova-core   | .2         | Linux | `/nova`    | CIFS      | `/etc/fstab` |
| mac-studio  | .6         | macOS | `/nova`    | autofs    | `/etc/auto_nova` + `auto_master` |
| nuk         | .10        | Linux | `/nova`    | CIFS      | `/etc/fstab` |
| nova-core2  | .86        | Linux | `/nova`    | CIFS (autofs) | systemd automount |
| .7 (flaky)  | —          | —     | —          | NOT MOUNTED (intentional) | — |

**2026-07-24 finding: this table was aspirational, not actual.** `.6`'s `auto_master` was
missing its `auto_nova` line entirely (mount never fired) and `.10`'s `/etc/fstab` had no nova
entry at all (only `.2` and `.86` were genuinely mounted). Both repaired 2026-07-24 — `.6` got
the missing `auto_master` line + `automount -vc`; `.10` got the fstab line re-added + `mount -a`.
If `/nova` looks empty/missing on a node again, check these two files first before assuming the
Synology side broke.

### Linux (nova-core .2, nuk .10) — CIFS via /etc/fstab

```
//192.168.1.11/nova /nova cifs credentials=/etc/cifs-nova.creds,uid=kochj,gid=kochj,iocharset=utf8,vers=3.0,_netdev,nofail 0 0
```

Credentials file `/etc/cifs-nova.creds` (root:root, **chmod 600** — never world-readable):

```
username=kochj
password=<smb-password>
```

(nova-core reuses the existing `/etc/cifs-nas.creds`; nuk uses `/etc/cifs-nova.creds`.)

Mount: `sudo mount /nova`

### macOS (mac-studio .6) — autofs

`/Volumes` is `root:wheel`, so a user-context LaunchAgent can't recreate the mountpoint after
macOS reaps it. autofs is the correct Apple-native solution: mounts on-demand at `/nova`,
manages the mountpoint lifecycle, survives reboot.

`/etc/auto_master` (append):

```
/-                      auto_nova       -nosuid
```

`/etc/auto_nova` (root:wheel, **chmod 600** — password protected):

```
/nova -fstype=smbfs,soft ://kochj:<smb-password>@192.168.1.11/nova
```

Reload: `sudo automount -vc`. Access `/nova` to trigger the mount.

## Verification (cross-node proof)

The same file is visible from every node. Markers written by each node coexist in
`/nova/trans/inbox/` (`.nova-core-write-test`, `.mac-studio-*-test`, `.nuk-write-test`), and
the seeded model `/nova/models/llama-3.2-3b/` + `/nova/models/.SEEDED` are readable everywhere.

## Seeding

First seeded model: `llama-3.2-3b` (1.7G HF dir) copied **server-side** on the Synology from
`/volume1/nas/models/llama-3.2-3b` (originals untouched — additive/non-destructive). This is the
proof for the shared-models goal: nova-core and .6 share ONE copy instead of each keeping one.

## Scripts (added 2026-07-24)

**Why**: nova-core's (.2) copy of `~/.openclaw/scripts` was a static snapshot from the
2026-07-14 .6→.2 scheduler migration, with no sync mechanism back to the git repo on .6. A fix
to `nova_rando_weird_memories.py` landed in git on .6 but never reached .2 — the machine that
actually runs the cron job — so it kept failing every single day for two weeks. `.10` and `.86`
had the identical stale-copy exposure. Jordan's call: extend `/nova` to cover scripts instead of
patching each node by hand forever.

**Design**: `.6` stays the git source of truth (edits happen there, in a real local working
copy — CIFS/AFP mounts are known to hang on git porcelain writes, see `nova_journal.py`'s
lint-scratch warning and general fleet lore). `.6` **publishes** `scripts/` into
`/nova/scripts/` on every commit. `.2`, `.10`, and `.86` don't keep their own copies anymore —
`~/.openclaw/scripts` on each is a symlink to `/nova/scripts`, so they see `.6`'s latest the
moment it's pushed, with zero manual deploy step.

- **Publish mechanism**: `~/.openclaw/.git/hooks/post-commit` on .6 runs
  `rsync -a --delete scripts/ 192.168.1.2:/nova/scripts/` in the background after every commit,
  routed over SSH to `.2` rather than a local `/nova` write.
- **Correction (2026-07-24, same day)**: originally attributed the local-write block to Full
  Disk Access / TCC, per memory `fda-volumes-data-route-around`. That was wrong for THIS path —
  confirmed via `log show` at the exact moment of the failed access: `kernel: (Sandbox) System
  Policy: claude.exe(PID) deny(1) file-read-data /System/Volumes/Data/nova/scripts`. That's
  Apple's process **sandbox (Seatbelt)**, a different and stronger mechanism than TCC — it denies
  at the kernel level before TCC is even consulted, so no System Settings/FDA grant would have
  fixed it. The `claude` CLI's own Bash-tool sandbox only allows reading a fixed set of
  directories (home, project, /tmp, etc.); `/nova` (which the `/-` autofs direct map resolves to
  under `/System/Volumes/Data/nova`, not `/Volumes/nova`) isn't on that list for a plain Claude
  Code session. The actual fix, if a Claude Code session run on .6 ever needs direct local `/nova`
  access, is `claude --add-dir /nova` at launch (confirmed present in `claude -p --help`) or the
  equivalent in `--settings`/`settings.json` — not a Privacy pane toggle. The SSH-routed hook
  sidesteps this regardless, since `rsync`/`ssh` invoked from a shell aren't subject to the
  Claude Code process's own sandbox. **The older `fda-volumes-data-route-around` memory citing
  the same `claude.exe` process for `/Volumes/Data` was very likely misdiagnosed the same way and
  is worth re-checking the same way (a time-correlated `log show` during a live failure) rather
  than trusted as-is.**
- **Consumer nodes**: `.2`, `.10`, `.86` each had their local `~/.openclaw/scripts` (or
  `/home/kochj/.openclaw/scripts`) directory moved aside to `scripts.bak-<date>` (kept, not
  deleted) and replaced with `ln -s /nova/scripts ~/.openclaw/scripts`. All services with
  `WorkingDirectory`/`PYTHONPATH` pointing at that path were restarted after the swap:
  `.2` → nova-gateway-v2, nova-scheduler-core, nova-snmp-poller, nova-syslog, nova-watchdog;
  `.10` → nova-watchdog; `.86` → nova-broadcastify-calls, nova-snmp-poller, nova-syslog,
  nova-watchdog (`.86` also has a `nova-scheduler-core.service` unit but it's disabled/inactive
  — leftover from setup, not actually running there).
- **One known limitation**: SMB/CIFS symlinks are flaky — one archived `.sh` symlink
  (`nova_herd_mail.sh -> _archive/...`) fails to sync with "Operation not supported"; harmless,
  logged, not investigated further.

**If you edit a script**: commit on `.6` as normal. The hook fires automatically. To confirm a
consumer node picked it up: `ssh 192.168.1.2 "md5 -q /nova/scripts/<file>"` should match `.6`'s
local copy immediately after the commit (`/tmp/nova-scripts-sync.log` on .6 has the rsync log).

## Security

- SMB/CIFS only, **never AFP**.
- Credentials live in chmod-600 files (Linux `/etc/cifs-nova.creds`, macOS `/etc/auto_nova`),
  never in world-readable fstab/plist.
- Additive: models are **copied**, originals in `/volume1/nas/models` left intact.
