# Nova Cluster (Mac + iPad)

**Version 1.1.0 (build 3)** · macOS 14+ · iPadOS 17+ · SwiftUI · xcodegen

Nova Cluster is a live view of what every machine in Nova's cluster is doing with its language
models, and what that work turns into. Each machine's Ollama and llama.cpp traffic arrives from
Nova's hub every 2 s: the models in memory, the requests being served right now, and the finished
requests. So does the minions' reading work: current jobs, tonight's works and lessons, episodes
transcribed, and the latest lesson. The app only displays this data. It never computes or writes
anything back. Since 1.1.0 it also contains [Nova Minion](../NovaMinionPad/): the lesson
constellation is its third view, so one app shows the traffic and what the traffic turns into.

There is one multiplatform xcodegen project with two app targets that share every source in
`NovaCluster/`. Platform-specific code sits behind `#if os(iOS)` / `#if os(macOS)`.

| Target | Platform | Bundle id | Product |
|---|---|---|---|
| `NovaClusterMac` | macOS 14+ | `net.digitalnoise.NovaCluster` | `Nova Cluster.app` |
| `NovaClusterPad` | iPadOS 17+ | `net.digitalnoise.NovaClusterPad` | `NovaCluster.app` / `.ipa` |
| `NovaClusterTests` | macOS 14+ | `net.digitalnoise.NovaClusterTests` | XCTest bundle (not hosted in the app) |

## Features

### Every machine shows its LLM job and its core purpose (both views)

Each station label on the map and each machine card on the dashboard always carries two lines.
The lines have a fixed height, so the layout never moves when the text changes:

1. **▸ LLM job.** This is the hub's `llm_job.text`: the job that sent the request now running on this
   machine's LLM. Examples are `nova_notifier @ mac-studio → qwen3:8b · generating 52 tok`,
   `reading: <work>` and `transcribing (whisper): <episode>`. When nothing is running it shows
   `Idle · last: … · 4m ago`, `Idle · 2 models loaded`, `No LLM on this node` or
   `Silent · probe last heard …`. The hub names jobs from `minion_activity` and from the probes'
   connection tables (script and app names only). The app never guesses a job. A busy slot whose
   caller can't be matched says `caller not identified`. An older hub with no `llm_job` field falls
   back to what the snapshot itself shows.
2. **Core purpose.** This comes from `minion_nodes.purpose` in PG, for example
   `PG standby · Mosquitto · embeddings`. The machine's name includes its alias too, as in
   `mac-studio (nova-core8)` (`minion_nodes.alias`). To change either, run an `UPDATE minion_nodes …`.
   The app picks up the change on the next poll.

All hub text is treated as data, not markup. It is reduced to one clean line: control characters
and bidi overrides are removed and the length is capped at 240 characters. Long lines are truncated.
To see the full text, hover over it (Mac), tap a station on the map (both platforms), or long-press a
dashboard card (iPad).

### Three views

Tap the wordmark to step through **Cluster Map → Dashboard → Constellation**. On the Mac you can also use
**View › Cluster Map (⌘1) / Dashboard (⌘2) / Constellation (⌘3)**, or
`novacluster://view?mode=map|dashboard|constellation`. The app remembers your choice (`view.mode`).

#### Cluster map (`NOVA · CLUSTER`)

| Area | Shows |
|---|---|
| Hero (top left) | **Tokens today** across the cluster, shown as a rolling odometer. Between polls it ticks up at the live `tok_s_total` (decode + prefill), and at each poll it eases to the hub's figure. It never counts backwards, except at the daily reset, and it extrapolates at most 3 s past the last poll. The cluster's tok/s sits beside it and the all-time total sits under it. |
| Header tiles | LLM traffic: machines live, models loaded, model memory (GB), active requests and requests/min. Minions tonight: works, lessons, episodes transcribed (+ audio hours), and works still to read (+ ETA). |
| Map | mac-studio (Nova's hub) sits at the center and the other machines sit on a ring, Macs first. **Moons** are loaded models: size shows memory, color shows model family (legend at the bottom), and a hollow moon runs on the CPU. The hub splits its models across two rings. When a slot is busy its moon pulses. Prefill draws a filling amber arc (`prefill 46%`) and generation draws a spinning ring with the decoded count (`▸ 1,090 tok`). Flowing dashes run along a busy station's spoke. When a request finishes, a spark bursts at the station and a comet flies back to the hub, sized by prompt + decoded tokens. The hub's own requests send out a shock ring. |
| Stations | ● marks a minion. Its ring shows its state (solid = dedicated, dashed = idle, dotted = away), with a state badge, its current job (`› reading: …`, `› mapping: …`, `› transcribing: …`) and its tonight figures (`tonight: 96 works · 156 lessons` / `4 episodes · 1.6 h`). ◆ marks a service node (a plain LLM box) with a `FAST POOL` / `EMBEDDINGS` badge and its models. Every station shows chip · OS, RAM free/total · load, and tok/s · tokens today · req/min. A silent station (`live=false`) dims and shows `silent 42s`. |
| Hover / tap | On the Mac, hover over a moon. On the iPad, tap it. Shows the model, machine, GB (GPU or CPU), ctx, port and live slot progress. |
| Side panel | **Now generating** lists every busy slot, such as `qwen3:30b-a3b · nova-core10 · prefill 5,573/6,915` with a progress bar, or `generating 240 tok`. **Recent requests** shows the hub's finished-request log (model · node · prompt→decoded · age), with new rows sliding in. |
| Footer | The latest lesson the minions learned, which is what all the traffic is for. It steps through recent lessons every ~20 s. Also shows the model legend and either `live · 13:22` or `hub unreachable · last heard … · showing the last map`. |

#### Dashboard (`NOVA · DASHBOARD`, in Nova Minion's style)

- A **card per machine** across the top. Each card has the two fixed lines, live requests and
  minion jobs typed out terminal-style, loaded models, role · tokens today, and tonight's totals.
  On the Mac, hover over a card to see the full detail. On the iPad, long-press it (context menu).
- A **traffic starfield**: every finished request becomes a star in a drifting sky. Each machine's
  traffic forms its own constellation in that machine's hue. The hub comes first, and the same hues
  are used on the cards and in the legend.
- The **latest lesson** in large serif type, the tokens-today hero, and the cluster and fleet totals
  along the bottom.

#### Constellation (`NOVA · MINION`, Nova Minion folded in, 1.1.0)

| Area | Shows |
|---|---|
| Sky | Every lesson the minions learned is a star in the color of the machine that learned it (the same hues as the dashboard cards). Stars from the same work cluster and are joined by faint lines, over three slowly drifting parallax depths. New stars fade in; the star whose lesson is on stage gets a pulsing ring. Placement comes from the shared core in [`../Shared/`](../Shared/), the same code the Nova Minion screensaver uses, so a lesson sits in the same place on both. |
| Minion cards | One card per machine doing minion work: state dot (dedicated / idle / away / offline), tok/s, OS, `tonight:` totals, and up to three `reading:` / `transcribing:` lines typed out terminal-style. Mac: as many ~250 pt cards across as fit; iPad: 3 landscape / 2 portrait. |
| Lesson | The current lesson in large serif with `— from <title>`, kind · node · time. A brand-new lesson takes the stage at once; otherwise it steps through recent lessons every ~22 s. |
| Bottom | Machine legend and the fleet line: minions, works and lessons tonight, episodes transcribed, and the reading queue with its ETA. |

It reads the same `/cluster` poll as the other two views (no second hub client, no second token).

### Resilience

- If the hub misses two polls in a row, the app shows the offline banner. It keeps drawing from the
  last snapshot.
- Every 30 s, the last good payload is cached in UserDefaults (`cluster.cache.v1`), so a cold launch
  shows the last map at once.
- Finished requests are de-duplicated by key. The first poll after launch only primes the list, so
  old traffic doesn't fire comets. New requests are spread across the 2 s interval (at most 24 per
  poll), so traffic reads as a steady stream rather than a pulse.

## Architecture

```mermaid
flowchart LR
    subgraph Machines["LLM machines (mac-studio, nova-core2…10)"]
        P["nova_llm_probe<br/>(Ollama / llama.cpp:<br/>loaded models, runners, slots,<br/>finished requests, connections)"]
        M["minions<br/>(heartbeat, now_reading,<br/>lessons, Whisper episodes)"]
    end
    subgraph Hub["Nova hub"]
        LLM["POST /llm<br/>probe samples, 1 s"]
        AGG["aggregate<br/>totals · today / all-time tokens<br/>events · llm_job · fleet"]
        CL["GET /cluster?node=…"]
    end
    PG[("nova_ops<br/>minion_nodes (purpose, alias)<br/>minion_activity · token usage")]
    P --> LLM --> AGG
    M --> AGG
    AGG <--> PG
    AGG --> CL
    subgraph App["Nova Cluster (shared SwiftUI sources)"]
        CM["ClusterModel<br/>2 s poller · cache · CometQueue · TokenTicker"]
        CFG["ClusterConfig<br/>UserDefaults + Keychain"]
        MAP["ClusterMapView<br/>Canvas 30 fps"]
        DASH["DashboardView<br/>cards + TrafficStarfield"]
        CON["ConstellationView<br/>lesson stars + minion cards"]
        CORE["Shared/NovaConstellationCore<br/>StableHash · SeededRandom · LessonText · layout"]
        NL["NodeLines<br/>LLM job + purpose"]
    end
    CL -- "JSON, X-Minion-Token" --> CM
    CFG --> CM
    CM --> MAP & DASH & CON
    CORE --> CON
    CORE --> SAVER["Nova Minion screensaver<br/>(Core Animation, own target)"]
    NL --> MAP & DASH
```

### Data flow per poll

```mermaid
sequenceDiagram
    participant T as Poll loop (2 s)
    participant M as ClusterModel
    participant H as Hub /cluster
    participant C as UserDefaults cache
    participant V as Views (map / dashboard)
    T->>M: pollOnce()
    M->>M: ClusterConfig.current (hub, node, Keychain token)
    M->>H: GET /cluster?node=<viewer> (3 s timeout, X-Minion-Token header)
    alt 200 + valid JSON
        H-->>M: ClusterSnapshot
        M->>M: ingestEvents → CometQueue (new finished requests only)
        M->>M: TokenTicker.update(today, all_time, tok_s_total)
        M->>V: publish snapshot (only if changed)
        M->>C: save raw payload (every ~30 s)
    else error / non-200 / bad JSON
        M->>M: failures += 1, keep the last snapshot
        M->>V: offline banner after 2 misses
    end
    V->>V: each frame: drain comets, read ticker, draw (no re-layout)
```

## Mac app

The app installs to `/Volumes/Data/Applications/Nova Cluster.app`, with a symlink at
`/Applications/Nova Cluster.app`. It opens a resizable window (1440×900 at first, minimum 1000×680,
hidden title bar, always dark) that supports native full screen. **⌘,** opens Settings (hub URL,
viewer node, token and live status). **⌘R** polls now. Closing the window quits the app.

The hub and node live in UserDefaults (`hub.url`, `hub.node`; the default node is `mac-studio-view`).
The token lives in the login Keychain (service `net.digitalnoise.NovaCluster`, account
`cluster-token`). To preconfigure without opening Settings:

```sh
defaults write net.digitalnoise.NovaCluster hub.url "http://<hub-host>:<port>"
defaults write net.digitalnoise.NovaCluster hub.node mac-studio-view
security add-generic-password -U -s net.digitalnoise.NovaCluster -a cluster-token \
  -T "/Volumes/Data/Applications/Nova Cluster.app" -w "$(cat <token-file>)"
```

The `-T` flag lets the app read the item without a Keychain prompt. The app is signed with the team's
Apple Development identity, so the grant survives rebuilds.

## iPad app

The iPad app runs full screen, in landscape or portrait. The screen stays awake while the app is in
front, and the status bar is hidden. **Long-press the wordmark** to open Settings (the token is kept in
this iPad's Keychain, this-device-only). The default node is `ipad-air`. To configure it from the Mac:

```sh
xcrun devicectl device process launch --device <udid> --terminate-existing \
  --payload-url "novacluster://setup?hub=http://<hub-host>:<port>&node=ipad-air&token=$(cat <token-file>)" \
  net.digitalnoise.NovaClusterPad
```

On first launch iOS may ask for Local Network access. Tap **Allow**.

## Configuration

Nothing network-specific is compiled in. The hub can come from any of these sources:

| Source | How |
|---|---|
| Settings | Mac: ⌘, · iPad: long-press the wordmark |
| Deep link | `novacluster://setup?hub=…&node=…&token=…` (missing fields keep their current value) |
| Environment on launch | `NOVA_CLUSTER_HUB`, `NOVA_CLUSTER_NODE`, `NOVA_CLUSTER_TOKEN` (`DEVICECTL_CHILD_…` / `SIMCTL_CHILD_…`) |
| View switch | `novacluster://view?mode=map\|dashboard` |
| Debug | `-demoBusy YES` marks a few slots busy (prefill + generating) to check the visuals |

## Security

- The token is stored only in the Keychain: the login keychain on macOS, and
  `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` on iOS. No secrets or hosts are in the source.
- The token is sent in the `X-Minion-Token` header, never in the URL (covered by a test).
- Hub strings are cleaned before display. Control characters and bidi overrides are removed and the
  length is capped, so a node's name can't spoof the layout.
- ATS allows only local networking (`NSAllowsLocalNetworking`). The Mac app uses the hardened runtime.

## Build

```sh
cd apps/NovaCluster
xcodegen generate                      # project.yml is the source of truth; never hand-edit the pbxproj

# Mac
xcodebuild -project NovaCluster.xcodeproj -scheme NovaClusterMac -configuration Release \
  -destination 'generic/platform=macOS' -derivedDataPath <dd> -allowProvisioningUpdates build

# iPad
xcodebuild -project NovaCluster.xcodeproj -scheme NovaClusterPad -configuration Release \
  -destination 'generic/platform=iOS' -derivedDataPath <dd> -allowProvisioningUpdates build
xcrun devicectl device install app --device <udid> <dd>/Build/Products/Release-iphoneos/NovaCluster.app
```

Build both targets before you commit, because the sources are shared. Because the NAS mount is
unreliable, build from a local copy of the source with your own `-derivedDataPath`.

## Test

```sh
xcodebuild -project NovaCluster.xcodeproj -scheme NovaClusterMac -configuration Release \
  -destination 'platform=macOS' -derivedDataPath <dd> test
```

There are 28 XCTest cases (`Tests/NodeLinesTests.swift`, `Tests/ConstellationTests.swift`), all of which pass in 1.1.0. The constellation tests pin the shared hash and random stream so the screensaver's sky can't shift silently. The test bundle
is not hosted in the app: it has no window, no live hub, and no real UserDefaults or Keychain. The
sources under test are compiled straight into the bundle. The network is stubbed with a `URLProtocol`.

| Category | Covers |
|---|---|
| Security | hub text becomes one clean bounded line · display names can't spoof lines · the token travels in a header, not the URL |
| Performance | decoding plus building lines stays fast (timed bound) · cleaning huge input stays bounded |
| Retry | failures count up and then recover with both lines · garbage keeps the last good snapshot |
| Unit | display name and alias · purpose fallback · state mapping |
| Integration | all ten nodes carry both lines · the cache goes only to the injected suite |
| Functional | hub job text is shown verbatim · an older hub never gets an invented job · detail has everything |
| Frame | height is fixed whatever the text · a narrow width still shows two lines |

## Release

1. Bump `MARKETING_VERSION` / `CURRENT_PROJECT_VERSION` in `project.yml`, then run `xcodegen generate`.
2. `xcodebuild archive` for both schemes, then `-exportArchive` with `method = debugging` (team `QRRCB8HB3W`).
3. Export to `/Volumes/Data/xcode/Binaries/YYYYMMDD-NovaCluster-vX.Y.Z/`, which holds `Nova Cluster.app`,
   `iPad/NovaCluster.ipa` + `iPad/NovaCluster.app`, and `NovaCluster-vX.Y.Z-buildN.dmg` (drag to Applications).
4. Copy the folder to `/Volumes/NAS/binaries/`. Wrap every NAS command in `timeout`.

## Performance

The animated layers draw in SwiftUI `Canvas` + `TimelineView`. The map runs at 30 fps and is rendered
through Metal with `drawingGroup`. The dashboard starfield also runs at 30 fps, the hero at 15 fps, and
the wordmark's breath at 10 fps. The map, starfield and hero pause when the scene is inactive. They have fixed sizes, so
a tick never re-lays out the window. Text and panels change only when a poll lands. Measured on the M3
Ultra at 1440×900 with live traffic, the app uses about 24–28 % of one core.

## Files

| File | Contents |
|---|---|
| `project.yml` | xcodegen spec: the Pad, Mac and Tests targets, Info.plist keys, signing and version |
| `NovaCluster/NovaClusterApp.swift` | app entry: iOS `WindowGroup` (idle timer off), macOS `Window` + `Settings` + View menu (⌘1/⌘2/⌘R) |
| `NovaCluster/Cluster.swift` | `/cluster` model, 2 s poller, offline cache, finished request → `CometQueue`, `TokenTicker`, `-demoBusy` |
| `NovaCluster/ClusterConfig.swift` | hub/node/token storage, Keychain, `novacluster://` deep links, environment setup |
| `NovaCluster/ClusterMap.swift` | station and moon view models, ring geometry, map renderer, station labels |
| `NovaCluster/NodeLines.swift` | per-machine LLM-job + purpose lines (text, cleaning, fixed-height view) |
| `NovaCluster/Views.swift` | root view (view switch), wordmark, header + odometer hero, side panel, footer, iPad settings sheet |
| `NovaCluster/DashboardView.swift` | Minion-style dashboard: machine hues, traffic starfield, machine cards, lesson, totals |
| `NovaCluster/Palette.swift` | palette, model-family hues, number and age formatting, dust layer |
| `NovaClusterMac/MacSettings.swift` | macOS Settings window |
| `NovaCluster/Assets.xcassets`, `NovaClusterMac/Assets.xcassets` | iPad and macOS app icons |
| `Tests/NodeLinesTests.swift` | XCTest suite covering all 7 categories |

## Version history

| Version | Date | Notes |
|---|---|---|
| 1.0.1 (2) | 2026-10-17 | Every machine shows its LLM job and core purpose in both views; release build is clean; 17/17 tests pass; DMG |
| 1.0.0 (1) | 2026-10-16 | First release: live Mac + iPad view of the whole cluster's LLM traffic |
