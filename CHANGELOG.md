# Changelog

All notable changes to **media-sampler3** are recorded here. Format:
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
`sage.yaml`.

Convention: add an entry under `[Unreleased]` with each change. When a version
ships, move the entries under a `[x.y.z] - YYYY-MM-DD` heading and bump
`sage.yaml`.

The detailed, dated development log (stage-by-stage build-up, on-node validation
runs, the H00F cut-over, and the inherited image-sampler2 releases 0.2.0–0.5.1) is
preserved in
[docs/history/CHANGELOG-development-log.md](docs/history/CHANGELOG-development-log.md).

## [Unreleased]

### Verified
- **REBOOT-RECOVERY.md followed after a real reboot (H039, Oct 2026).**
  - Survived: all side-loaded images, the 5-var `wes-identity`,
    `pluginctl-nodeinfo`, the cache manager and `/local-cache` (including the
    seen-stores).
  - The `pluginctl` pods vanished rather than showing as `Unknown`, and the docs
    now say so. Step 0's grep is narrowed so it no longer matches the
    `wes-camera-provisioner` cron pods.
  - Step 8 was run exactly as written. A seeded image and clip then reached
    Beehive from all three consumers, with lat/lon.
  - Added a no-camera verification note to step 9 (bioclip2 needs new pixels to
    re-fire).
  - Not covered: steps 5–7 (camera credentials and producers), which need a
    camera.

### Added
- **sage-birdnet2 is the third test consumer (audio).** It's in the components
  table and the diagram, cloned in Step 0, built in Step 5, and run in 6e, which
  is new. 6b also seeds a bird-song clip (sage-birdnet2's
  `tests/test-audio/eastern-bluebird-XC179669.flac`, CC BY-SA), 6f checks
  `env.detection.*`, and 6g removes both seeds. In 6h the audio producer is now a
  normal step. Re-numbered: verify 6e→6f, remove seeds 6f→6g, live camera 6g→6h.
  REBOOT-RECOVERY relaunches the audio producer and birdnet2.
  HOW-IT-WORKS (components, cache layout, topics) and DESIGN-PATH (a new
  sage-birdnet2 section) are updated to match. Verified on H039 with the seeded
  clip.

### Changed (install guide simplified to one path)
- Launch everything with `sudo pluginctl-nodeinfo run`, the patched `pluginctl`
  installed to `/usr/local/bin` in Step 3. It's a normal install step, so the
  `$PCTL` choice is gone, and the producer commands no longer pass `--vsn`.
- Step 2 is the `wes-identity` ConfigMap and Step 3 the patched `pluginctl`. The
  patched-scheduler swap (Tier 2) moved out of the guide, to wes-nodeinfo-injection's
  docs; REBOOT-RECOVERY only mentions it in an "if you installed it" note.
- Step 6 is one sequence: producer check, seed the bird, yolo2, bioclip2, verify,
  remove the seed, then the live camera (6g). One camera in the main path, with a
  short "more cameras" note. The seeded test is now 6b–6f, and the live producer
  moved from 6b to 6g.
- Removed `export KUBECTL=...` and the old `__ABSENT__` warning (the scripts
  auto-detect `kubectl`, which exists on these nodes as a link to k3s), the
  separate "create /local-cache" step (the cache-manager script creates it), the
  tag-checkout option, and the per-build `--dry-run`.

Fixes from following the install guide from scratch on a fresh node (H039, Oct 2026).

### Fixed
- `make test` works on a fresh clone. It now creates `.venv-test` itself, from
  `requirements.txt` plus pytest. Before, it assumed a venv that only existed on the
  developer's machine.
- Install guide: `--stream` is required. It was described as optional when `--name`
  is given, but the producer exits 2 without it.

### Added (install guide)
- Tier 1b: launch plugins with the patched `pluginctl-nodeinfo`
  (wes-nodeinfo-injection `install-pluginctl-nodeinfo.sh`), so `pluginctl` pods get
  `wes-identity` like SES jobs do. New `$PCTL` convention; every launch in the guide
  and in REBOOT-RECOVERY is `sudo $PCTL run`. Also: a passwordless-sudo prerequisite,
  a note that `--env-from` credentials end up in the pod spec, and two 6f re-run
  surprises (identical crops are deduped; records carry the capture time).
- A caveat at the top of the guide: on Thor nodes other than H00F, no plugin gets
  the GPU. The default containerd runtime is plain runc, and nothing requests
  NVIDIA's runtime. Survey: H01A, H038, H039, H041, H043. H00F's hand-made
  `config.toml.tmpl` is the exception.
- 6a: a producer check that needs no camera. It uses a fake camera address and
  dummy credentials, and proves the image, the cache mount and the heartbeat path
  to Beehive all work.
- 6a: a table of the producer's start-up exit-2 messages and how to fix each.
- Step 3: show `wes-identity` before and after Tier 1. Tier 2: a rollout check
  anyone can do; the injection check uses `-n ses` and needs an SES job; the
  scheduler's start-up clean-up doesn't touch `pluginctl` pods.
- 6c/6d: how to tell whether a consumer is on the GPU. On H039 sage-yolo2 runs on
  the CPU (no GPU device plugin and no NVIDIA default runtime), and sage-bioclip2
  runs on the CPU everywhere (it never passes a device to pybioclip). Also noted
  that bioclip2 contacts huggingface.co at start-up.
- Real Step 5 build times, and H039 added to the list of verified nodes.

## [0.1.0] - 2026-10-01

First tagged release of media-sampler3: the image and audio producer for the
shared `/local-cache`, verified end to end on H041 (Sep 2026).

### Added
- Audio as a second media type (`--media audio`). Bounded FLAC/WAV clips via
  ffmpeg from a camera mic (Reolink HTTP-FLV, `--source-type camera_mic`) or a USB
  mic (ALSA), each with a `<clip>.json` provenance sidecar written before the clip.
- Student-facing docs:
  - `INSTALLING-MEDIA-SAMPLER3.md`: full-stack install
  - `REBOOT-RECOVERY.md`: restart runbook
  - `docs/HOW-IT-WORKS.md`: data flow, identity, code map
  - `DESIGN-PATH.md`: design history index
- `make sideload`: native podman build plus k3s import, with the version taken
  from `sage.yaml`.

### Changed
- Docs reorganized. Current docs are at the top level; dated plans, stage notes,
  status files, spikes, and the development log moved to `docs/history/`.
- Install guide corrections:
  - Audio uses the camera's HTTP port (HTTP-FLV), not RTSP 554.
  - The Tier 2 patch-apply command is fixed.
  - Host Go isn't required.
  - `pluginctl` pods don't receive `wes-identity`.
  - Crop directory naming is explained, along with the known limitations.
  - ECR wording updated: the Sage ECR portal now builds Thor images (the QEMU
    blocker is fixed); these images simply aren't published yet.
- `--name` / `--stream` help text now says what they actually control (the
  `--name` label names the cache subdirectory and the filename source). Log
  prefixes `STAGE n:` were renamed to the mode (`continuous:`, `heartbeat:`, …).
- `sage.yaml`:
  - branch and homepage now point at `master`
  - inputs list all current flags (adds `name`, `media`, the audio flags, and
    `node-manifest`; drops the non-existent `camera-name`)
  - the identity note is corrected
- `jobs/`: marked as untested SES templates; placeholders `<VSN>` and camera
  port 80.
- CI workflow triggers on `master`.

### Security
- Removed a camera-password reference from the history notes, and replaced
  private camera LAN IPs and account names with placeholders across the tree
  (the test fixture now uses the documentation address `192.0.2.10`). `*.env`
  is now gitignored.
