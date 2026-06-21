# /nova — Shared Artifacts Mount

The `/nova` mount is the fleet's shared **artifacts** filesystem — the SAP `/usr/sap/trans`
concept applied to nova-core's orchestration role. It is for **ARTIFACTS ONLY**:

- **NOT** live code (code is git → local on each node)
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
```

## Mounts per node

| Node       | IP         | OS    | Mount path | Mechanism | Persistence |
|------------|------------|-------|------------|-----------|-------------|
| nova-core  | .2         | Linux | `/nova`    | CIFS      | `/etc/fstab` |
| mac-studio | .6         | macOS | `/nova`    | autofs    | `/etc/auto_nova` + `auto_master` |
| nuk        | .10        | Linux | `/nova`    | CIFS      | `/etc/fstab` |
| .7 (flaky) | —          | —     | —          | NOT MOUNTED (intentional) | — |

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

## Security

- SMB/CIFS only, **never AFP**.
- Credentials live in chmod-600 files (Linux `/etc/cifs-nova.creds`, macOS `/etc/auto_nova`),
  never in world-readable fstab/plist.
- Additive: models are **copied**, originals in `/volume1/nas/models` left intact.
