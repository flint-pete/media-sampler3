# Design path: how the media stack got to its current shape

The install guide and READMEs describe the stack **as it is now**. This page
covers **why** it looks this way: for each component, where it started, the key
turns, and where the detailed history lives. Each repo keeps its dated plans,
status logs, and superseded docs under `docs/history/`. Those files are kept as a
record, not as instructions; where they conflict with the current docs, the
current docs win.

The umbrella design and tracking repo is
[sage-design-planning](https://github.com/flint-pete/sage-design-planning):

- `pywaggle2-design.md`, the RFC behind node identity and the cache
- `Infra-problems-to-fix.md`, Sage platform issues such as the ECR build failures
- older runbooks

---

## The big picture: from "every plugin opens the camera" to producer/consumer

1. **Start (early 2026).** Each AI plugin (sage-yolo v1 / yolo-object-counter,
   sage-bioclip v1, birdnet) opened the camera itself. The stock
   `plugin-imagesampler` uploaded snapshots. Nothing was shared on the node, so
   results couldn't be tied to the same frame.
2. **Idea.** Split capture from analysis. One producer writes self-describing
   frames into a shared node-local cache, and consumers read it. This needed two
   things the platform lacked: a **bounded shared directory** (`/local-cache`)
   and a way for plugins to learn **who and where they are** (VSN and GPS).
   Inside a pod the VSN showed up as the placeholder `NODE`, because
   `/etc/waggle` exists only on the host.
3. **Platform pieces.** wes-local-cache-manager (the bound) and
   wes-nodeinfo-injection + pywaggle2-nodeinfo (identity) were built as WES
   add-ons, designed to be handed to the Sage CI team.
4. **Producer.** An enhanced imagesampler ("image-sampler2") established the v2
   cache-frame contract. It was then generalized to images **and** audio as
   **media-sampler3**.
5. **Consumers.** yolo2 and bioclip2 were rewritten as cache consumers, with yolo2
   also writing crops so bioclip2 classifies birds rather than whole frames.
6. **Production and replication.** On 2026-07-23, H00F switched to
   media-sampler3 as its live producer. A 12 h soak produced real species
   classifications. In Sep 2026 the whole stack was rebuilt from scratch on
   H038 and H041, which produced the current install guide. H038 revealed the
   WES prerequisite; H041 confirmed the full cascade with two cameras and showed
   that a power-cut reboot is survivable.

The constant cost of this design so far is **side-loading**. While these
components were being built, the Sage ECR portal couldn't build Thor images
(builder bugs since fixed by the cyberinfrastructure team), so everything was
built on the node and imported by hand. Nothing has been published to the
registry yet. That's why a reboot needs [REBOOT-RECOVERY.md](REBOOT-RECOVERY.md);
publishing the images through ECR is now possible and is the step that removes
that cost.

---

## media-sampler3 (producer)

- **Origin.** A fork of
  [waggle-sensor/plugin-imagesampler](https://github.com/waggle-sensor/plugin-imagesampler),
  enhanced in stages as "image-sampler2" (v0.2.0–v0.5.1, July 2026). The stages
  added:
  - capture-time naming (`<ts>-v2-<vsn>-<source>`)
  - EXIF provenance with a SHA-256 `unique_id`
  - a strict split between `--one-shot` (upload) and `--continuous` (cache only)
  - the bounded ring
  - heartbeats
  - the `--from-cache` uploader
  - `--max-count`/`--max-runtime` self-exit, so a producer can share a GPU time
    slot
- **Key turns.**
  - **Removed the silent `/tmp` fallback** (v0.5.1). Without `/local-cache`, the
    old code "worked" but wrote frames no consumer could ever read. Now it
    refuses to start.
  - **Base image switched to `python:3.12-slim`.** `waggle/plugin-base` tripped
    an ECR builder bug and shipped Python 3.8.
  - **Credentials are env-only.** They never appear as flags, so a password never
    shows up in `ps`, shell history, or the scheduler record.
  - **Audio.** Forked to media-sampler3 (2026-07-15). Added audio as a second
    media type: ffmpeg clips, a JSON sidecar instead of EXIF, and the sidecar
    written first. The camera microphone is read over the Reolink HTTP-FLV
    sub-stream, the same source birdnet uses.
  - **The great cut-over (2026-07-23).** media-sampler3 replaced image-sampler2
    on H00F with identical output, proven by yolo2 consuming its frames.
- **Details:** [docs/history/](docs/history/):
  - `CHANGELOG-development-log.md`, the full dated log including the inherited
    imagesampler releases
  - `IMPLEMENTATION-PLAN.md` and the `STAGE*` notes
  - `mediasampler.analysis.txt`, the upstream code study
  - `readiness-gap.txt` and `RESUME-HERE.md`, July status
  - `spikes/`

  The audio design is still current reference:
  [docs/AUDIO-EXTENSION-DESIGN.md](docs/AUDIO-EXTENSION-DESIGN.md).

## wes-local-cache-manager

- **Origin.** Modeled on WES's existing `/uploads` design: a shared host path
  drained by the upload-agent DaemonSet. A source check showed that **no storage
  quota exists** for plugins. SES sets only cpu, memory, and gpu limits, and
  kubelet doesn't count writes to host paths. So a purpose-built manager is the
  only thing that can bound the cache.
- **Key turns.**
  - **A two-layer model.** Producers bound their own rings (Layer 1); the manager
    is the node-wide safety net (Layer 2).
  - **Units (v0.1.1).** The definition of a "unit" was corrected from
    `<namespace>/<plugin>` to "any directory at depth 2", because that's what
    real producers write (`<cache-name>/<source>`).
  - **`.state/` (v0.2.0).** The directory was carved out so consumers' seen-stores
    are never evicted.
- **Details:** that repo's
  [DESIGN-AND-PURPOSE.md](https://github.com/flint-pete/wes-local-cache-manager/blob/master/DESIGN-AND-PURPOSE.md),
  `CHANGELOG.md`, and `docs/history/`.

## wes-nodeinfo-injection

- **Origin.** Plugins couldn't learn their VSN or GPS at runtime. The options
  considered:
  - mount `/etc/waggle` into pods
  - add a new identity API
  - put values in the environment

  Environment variables won because WES **already** has a `wes-identity`
  ConfigMap (with 2 vars) and every language can read env.
- **Key turns.**
  - **Extend the existing generator.** Instead of inventing a new mechanism, it
    adds GPS lat/lon and mobility (patch 0001 to waggle-edge-stack).
  - **Patch the scheduler** so that every plugin pod gets
    `envFrom: wes-identity` (patch 0002, a one-function change to edge-scheduler).
  - **Split rollout into two tiers.** Tier 1 (ConfigMap only) is harmless and
    reverts in seconds; Tier 2 (scheduler) touches the control plane and
    auto-reverts on failure.
  - **A live finding:** `pluginctl run` builds pods on the client side, so it
    bypasses Tier 2. That's why the producer passes `--vsn`.
  - **Tier 1b (H039, Oct 2026).** The same pod-builder code is compiled into
    `pluginctl`, so the Tier 2 build already contains a patched `pluginctl`.
    Installing it as `/usr/local/bin/pluginctl-nodeinfo` gives hand-launched pods the same
    `envFrom` that SES jobs get. That's one file, with no WES object
    changed and no scheduler swap. Verified end to end: the node's GPS reached the
    yolo2 crops and the Beehive records (`location_source: node`). It became the
    single launch path in the install guide; `--vsn` and the scheduler swap left
    the main path.
- **Details:** that repo's `README.md`, `HANDOFF.md`, `CHANGELOG.md`, and
  `docs/history/`, plus the RFC
  [pywaggle2-design.md](https://github.com/flint-pete/sage-design-planning/blob/master/pywaggle2-design.md).

## pywaggle2-nodeinfo

- **Origin.** The reader started as a single file inside wes-nodeinfo-injection,
  used by its end-to-end test.
- **Key turns.**
  - **Promoted to its own repo** in the upstream pywaggle layout
    (`waggle/data/node_info_env.py`), so it can land in pywaggle unchanged.
  - **Sentinel rules locked:** coordinates are checked by range, which catches
    the manifest's `999`, and nothing is ever fabricated.
  - **Vendored, not installed.** pywaggle2 isn't published yet, so plugins carry
    a copy (a pinned version) instead of a pip dependency.
- **Details:** that repo's
  [DESIGN.md](https://github.com/flint-pete/pywaggle2-nodeinfo/blob/master/DESIGN.md)
  and `docs/history/`.

## sage-yolo2 (test consumer)

- **Origin.** sage-yolo v1 (`yolo-object-counter`) grabbed its own RTSP or
  snapshot frames and published counts.
- **Key turns.**
  - **v2 (2.0.0) rewrite as a cache consumer.** It added:
    - wake/select/seen semantics (`--every`, `--all-unseen`, seen-store in
      `.state/`)
    - identity taken from the frame's EXIF rather than the pod
    - **frame-anchored** publishing, where the record timestamp is the capture
      time
  - **2.1.0 added the crop producer.** Matching detections are cropped and
    written back into the cache as v2 frames, so a classifier can consume them on
    its own schedule.
- **Details:** that repo's `V2-Design.md`, `CROP-PRODUCER-Design.md`, and
  `docs/history/` (v1 overview, status logs, the overnight deploy plan).

## sage-bioclip2 (test consumer)

- **Origin.** sage-bioclip v1 classified whole frames it fetched itself.
- **Key turns.**
  - **v2 is a cache consumer** built by copying yolo2's consumer modules
    (selection, seen-store, node_info), so both consumers behave the same way.
  - **It reads yolo2's crops** instead of whole frames.
  - **BioCLIP-2.5 (ViT-H/14) baked into the image**, enabled by a build-time
    patch, so it runs with no network at runtime.
  - Because of the copied code, bioclip2's seen-store lives under
    `.state/sage-yolo2/`. That's a known quirk, documented in its README.
- **Details:** that repo's `CHANGELOG.md`, `VENDORED.md`, and `docs/history/`.

## sage-birdnet2 (test consumer, audio)

- **Origin:** [birdnet](https://github.com/flint-pete/birdnet), a standalone BirdNET
  V2.4 plugin that opened the microphone (or the camera's audio) itself, with an
  eBird geo/season filter and the biophony/anthrophony/geophony topic routing.
- **Key turns.**
  - **Re-architected as a cache consumer** on the sage-yolo2 pattern: the copied
    `consumer.py`/`selection.py`/`seenstore.py`, plus one addition, a reader for
    media-sampler3's `<clip>.flac.json` sidecars (audio has no EXIF).
  - **Location from the node.** The geo filter originally needed `--lat/--lon`.
    With `pluginctl-nodeinfo` (H039, Oct 2026), it takes the node's GPS from the
    pod env with no flags.
  - **Pinned `birdnet==0.2.16`.** The first on-node build (H039) failed because
    the unpinned dependency resolved to birdnet 1.x, which no longer pulls in
    TensorFlow.
  - **Verified on H039** with a seeded Eastern Bluebird clip: *Sialia sialis*
    reached Beehive with the node's lat/lon. The live camera microphone is next.
- **Details:** that repo's `README.md`, `DOCKER-BUILD.md`, `VENDORED.md`, and
  `docs/history/HANDOFF.md`.
